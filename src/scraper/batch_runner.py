"""Bounded chapter workers, audited repairs, and durable per-chapter outcomes."""
import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

from src.agent.classifier import ChapterLink
from src.agent.ch import DOMStructurePlan, ChapterCodeGenerator, ExtractedChapter, SelfHealer
from src.agent.ch.observer import ExtractionObserver
from src.core.obscura_client import ObscuraClient
from src.scraper.storage import NovelStorage
from src.scraper.runtime import ChapterCheckpoint, ScraperLimits, access_error, fingerprint, canonical_url, safe_error
from src.config import DEFAULT_CONCURRENCY, MIN_DELAY_SECONDS, MAX_DELAY_SECONDS

logger = logging.getLogger("scraper.batch")


class BatchScraperRunner:
    def __init__(self, novel_title: str, initial_plan: DOMStructurePlan,
                 obscura_client=None, storage=None, concurrency=DEFAULT_CONCURRENCY,
                 min_delay=MIN_DELAY_SECONDS, max_delay=MAX_DELAY_SECONDS,
                 on_progress=None, on_log=None, previous_interaction_id=None, llm_client=None,
                 checkpoint: Optional[ChapterCheckpoint] = None, sample_html=None, on_stage=None):
        self.novel_title = novel_title
        self.plan = initial_plan
        self.obscura = obscura_client or ObscuraClient()
        self.storage = storage or NovelStorage()
        self.concurrency = max(1, min(concurrency, 10))
        self.min_delay, self.max_delay = min_delay, max_delay
        self.on_progress, self.on_log = on_progress, on_log
        self.previous_interaction_id = previous_interaction_id
        self.healer = SelfHealer(llm_client=llm_client)
        self.integrity_observer = ExtractionObserver(self.healer.llm)
        self.checkpoint = checkpoint
        self.sample_html = sample_html or {}
        self.limits = ScraperLimits.from_env()
        self._is_paused = self._is_cancelled = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._repair_locks, self._layout_plans, self._repair_counts = {}, {}, {}
        self._content_hashes = {}
        self.outcomes = []
        self.metrics = {"peak_active_workers": 0, "repair_calls": 0}
        self._active = 0
        self.require_layout_approval = False
        self.pre_results = {}
        self.chapters_per_minute = 0.0
        self.on_stage = on_stage

    def log(self, message, level="info"):
        if self.on_log:
            self.on_log(message, level)
        getattr(logger, level if level in {"info", "warning", "error"} else "info")(message)

    def pause(self):
        self._is_paused = True
        self._pause_event.clear()

    def resume(self):
        self._is_paused = False
        self._pause_event.set()

    def cancel(self):
        self._is_cancelled = True
        self._pause_event.set()

    def cancelled(self):
        return self._is_cancelled or bool(self.checkpoint and self.checkpoint.cancelled())

    def _result(self, link, status, **fields):
        return {"index": link.index, "title": link.title, "url": link.url,
                "status": status, "success": status in {"saved", "skipped"},
                "downloaded": status in {"saved", "skipped"}, **fields}

    def _verified_extract(self, plan, html):
        generator = ChapterCodeGenerator(plan)
        result = generator.extract(html)
        if result.success:
            review = self.integrity_observer._heuristic_review(html, plan, generator.test_and_verify(html, result))
            if not review.is_accurate:
                return result.model_copy(update={"success": False, "error": "; ".join(review.issues) or "Chapter integrity check failed"})
        return result

    async def _scrape_single_chapter(self, link, semaphore=None):
        await self._pause_event.wait()
        if self.cancelled():
            return self._result(link, "cancelled")
        if link.url in self.pre_results:
            status, error = self.pre_results[link.url]
            return self._result(link, status, error=error)
        if self.checkpoint:
            saved = self.checkpoint.verified(link.url, self.storage.get_novel_dir(self.novel_title))
            if saved:
                return self._result(link, "skipped", **{k: v for k, v in saved.items() if k not in {"status", "index", "title", "url", "success", "downloaded"}})
        # Also reuse validated source receipts across separate jobs.
        receipt = self.storage.sources.get(canonical_url(link.url))
        if receipt and not self.storage.overwrite:
            directory = self.storage.get_novel_dir(self.novel_title)
            path = directory / receipt["file"]
            if path.parent == directory and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]:
                return self._result(link, "skipped", **receipt)
        html = self.sample_html.pop(link.url, None) or await self.obscura.fetch_html(link.url)
        blocker = access_error(html)
        if blocker:
            return self._result(link, "blocked", error=blocker)
        layout = fingerprint(html)
        plan = self._layout_plans.get(layout, self.plan)
        result = self._verified_extract(plan, html)
        if self.require_layout_approval and layout not in self._layout_plans:
            result = result.model_copy(update={"success": False, "error": "Chapter layout requires approval"})
        if not result.success and not result.is_paywalled:
            html = await self.obscura.fetch_html(link.url, force_browser=True, content_selector=plan.content_selector)
            layout = fingerprint(html)
            plan = self._layout_plans.get(layout, self.plan)
            result = self._verified_extract(plan, html)
            if self.require_layout_approval and layout not in self._layout_plans:
                result = result.model_copy(update={"success": False, "error": "Chapter layout requires approval"})
        if result.is_paywalled:
            return self._result(link, "blocked", error="Paywall or premium teaser; authorized access required")
        if not result.success:
            lock = self._repair_locks.setdefault(layout, asyncio.Lock())
            async with lock:
                if self.cancelled():
                    return self._result(link, "cancelled")
                if layout in self._layout_plans:
                    plan = self._layout_plans[layout]
                    result = self._verified_extract(plan, html)
                while not result.success and self._repair_counts.get(layout, 0) < self.limits.repair_attempts:
                    if self.on_stage:
                        self.on_stage("repair", f"Validating a replacement layout for {link.title}")
                    self._repair_counts[layout] = self._repair_counts.get(layout, 0) + 1
                    self.metrics["repair_calls"] += 1
                    healed, candidate, candidate_result = await self.healer.attempt_heal(
                        url=link.url, html=html, current_plan=plan,
                        previous_interaction_id=self.previous_interaction_id)
                    if healed and candidate_result and not candidate_result.is_paywalled:
                        checked = self._verified_extract(candidate, html)
                        if not checked.success:
                            continue
                        plan, result = candidate, checked
                        self._layout_plans[layout] = plan
                        from src.agent.domain_memory import domain_memory
                        domain_memory.update_chapter_plan(link.url, plan, sample_url=link.url, sample_html=html)
                        break
        if not result.success:
            return self._result(link, "blocked" if "unsupported" in (result.error or "").lower() else "failed", error=result.error, attempts=self._repair_counts.get(layout, 0))
        result = await self._stitch_subpages(link.url, html, result, plan)
        if not result.success:
            return self._result(link, "failed", error=result.error)
        content_hash = hashlib.sha256(result.content_markdown.encode()).hexdigest()
        if content_hash in self._content_hashes and self._content_hashes[content_hash] != link.url:
            return self._result(link, "failed", error="Identical chapter content returned for distinct source URLs")
        self._content_hashes[content_hash] = link.url
        if self.cancelled():
            return self._result(link, "cancelled")
        path = await self.storage.save_chapter(novel_title=self.novel_title, chapter_index=link.index,
            chapter_title=link.title, chapter=result, source_url=link.url)
        return self._result(link, "saved", file=path.name,
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            content_sha256=content_hash, recipe_version=layout,
                            attempts=1 + self._repair_counts.get(layout, 0))

    async def _stitch_subpages(self, initial_url, initial_html, initial_result, plan=None):
        current_html, current_url = initial_html, initial_url
        generator = ChapterCodeGenerator(plan or self.plan)
        combined = [initial_result.content_markdown]
        visited = {canonical_url(initial_url)}
        for _ in range(self.limits.toc_pages):
            continuation = None
            soup = BeautifulSoup(current_html, "lxml")
            for anchor in soup.select("a[href]"):
                text, href = anchor.get_text(" ", strip=True).lower(), anchor["href"]
                if not any(word in text for word in ("next page", "下一页", "下页", "หน้าถัดไป")):
                    continue
                candidate = canonical_url(urljoin(current_url, href))
                base, target = urlsplit(initial_url), urlsplit(candidate)
                if base.netloc != target.netloc:
                    return initial_result.model_copy(update={"success": False, "error": "Chapter continuation changed origin"})
                # Same path or an evidenced numbered subpage suffix; never follow a next-chapter link.
                same_identity = target.path == base.path or target.path.startswith(base.path.rstrip("/") + "/page/")
                if not same_identity:
                    return initial_result.model_copy(update={"success": False, "error": "Chapter continuation identity is uncertain"})
                continuation = candidate
                break
            if continuation is None:
                content = "\n\n".join(combined)
                return initial_result.model_copy(update={"content_markdown": content, "char_count": len(content)})
            if continuation in visited:
                return initial_result.model_copy(update={"success": False, "error": "Chapter continuation cycle"})
            if self.cancelled():
                return initial_result.model_copy(update={"success": False, "error": "Cancelled during chapter continuation"})
            visited.add(continuation)
            current_html = await self.obscura.fetch_html(continuation)
            sub = self._verified_extract(plan or self.plan, current_html)
            if not sub.success or sub.is_paywalled or access_error(current_html):
                return initial_result.model_copy(update={"success": False, "error": "Unfinished or inaccessible chapter continuation"})
            combined.append(sub.content_markdown)
            current_url = continuation
        return initial_result.model_copy(update={"success": False, "error": "Chapter continuation budget exhausted"})

    async def run(self, chapters: List[ChapterLink], source_url: str, author=None, description=None):
        start = time.monotonic()
        queue = asyncio.Queue(maxsize=self.concurrency * 2)
        total = len(chapters)
        async def worker():
            while True:
                link = await queue.get()
                if link is None:
                    queue.task_done()
                    return
                self._active += 1
                self.metrics["peak_active_workers"] = max(self.metrics["peak_active_workers"], self._active)
                try:
                    outcome = await self._scrape_single_chapter(link)
                    # Compatibility for existing worker substitutions.
                    if isinstance(outcome, bool):
                        outcome = self._result(link, "saved" if outcome else "failed")
                except PermissionError as error:
                    outcome = self._result(link, "blocked", error=safe_error(error))
                except Exception as error:
                    outcome = self._result(link, "failed", error=safe_error(error))
                finally:
                    self._active -= 1
                self.outcomes.append(outcome)
                if self.checkpoint:
                    try:
                        self.checkpoint.save(link.url, outcome)
                    except OSError as error:
                        outcome.update(status="failed", success=False, error=f"Checkpoint write failed: {error}")
                if self.on_progress:
                    self.chapters_per_minute = sum(item["success"] for item in self.outcomes) / max(time.monotonic() - start, 0.001) * 60
                    try:
                        self.on_progress(len(self.outcomes), total, link.title, round(len(self.outcomes) / max(total, 1) * 100, 1))
                    except Exception as error:
                        self.log(f"Progress consumer failed: {error}", "warning")
                        if isinstance(error, BrokenPipeError):
                            self.cancel()
                queue.task_done()
        tasks = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
        try:
            for chapter in chapters:
                await queue.put(chapter)
            for _ in tasks:
                await queue.put(None)
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        counts = {state: sum(item["status"] == state for item in self.outcomes)
                  for state in ("saved", "skipped", "failed", "blocked", "cancelled")}
        from src.agent.llm import token_tracker
        elapsed = time.monotonic() - start
        summary = {"novel_title": self.novel_title, "total": total, **counts,
                   "completed": counts["saved"] + counts["skipped"],
                   "elapsed_seconds": round(elapsed, 3),
                   "chapters_per_minute": (counts["saved"] + counts["skipped"]) / max(elapsed, 0.001) * 60,
                   "outcomes": sorted(self.outcomes, key=lambda item: item["index"]),
                   "token_usage": token_tracker.get_summary(),
                   "metrics": {**getattr(self.obscura, "metrics", {}), **self.metrics}}
        await self.storage.save_metadata(self.novel_title, source_url, author=author, description=description,
                                         total_chapters=total, completed_chapters=summary["completed"],
                                         chapter_index_list=summary["outcomes"], token_usage=summary["token_usage"])
        return summary

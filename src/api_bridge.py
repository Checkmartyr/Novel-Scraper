"""JSON-lines worker contract for automatic discovery and resumable extraction."""
import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from src.core.obscura_client import ObscuraClient
from src.agent.classifier import PageClassifier, ChapterLink
from src.agent.llm import LLMClient
from src.agent.toc.agent import TocAgent
from src.agent.toc.discovery import observed_navigation
from src.agent.ch.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.ch.review_loop import ReviewLoopOrchestrator
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.storage import NovelStorage
from src.scraper.runtime import ScraperLimits, ChapterCheckpoint, DestinationLock, atomic_json, canonical_url, fingerprint, belongs_to_observed_novel, safe_error
from src.utils.novel_url import belongs_to_recipe_novel

logger = logging.getLogger("api_bridge")


def _scope_chapters(url, chapters):
    from src.agent.domain_memory import domain_memory
    recipe = domain_memory.get_recipe(url)
    if recipe:
        chapters = [chapter for chapter in chapters if belongs_to_recipe_novel(
            url, chapter.url, recipe.sample_toc_url, recipe.sample_chapter_url) is not False]
    unique = {}
    for chapter in chapters:
        if belongs_to_observed_novel(url, chapter.url):
            unique.setdefault(canonical_url(chapter.url), chapter)
    for index, chapter in enumerate(unique.values(), 1):
        chapter.index = index
    return list(unique.values())


def select_chapters(chapters, chapter_indices=None, start_chapter=None, end_chapter=None):
    if chapter_indices is not None:
        if not chapter_indices or any(index < 1 for index in chapter_indices):
            raise ValueError("Explicit chapter selection must contain positive indices")
        available = {chapter.index for chapter in chapters}
        if set(chapter_indices) - available:
            raise ValueError("Selected chapter indices do not exist in the inspected catalog")
        selected = [chapter for chapter in chapters if chapter.index in set(chapter_indices)]
    else:
        if (start_chapter is not None and start_chapter < 1) or (end_chapter is not None and end_chapter < 1):
            raise ValueError("Chapter range indices must be positive")
        if start_chapter is not None and end_chapter is not None and start_chapter > end_chapter:
            raise ValueError("Chapter range start exceeds end")
        selected = [chapter for chapter in chapters
                    if (start_chapter is None or chapter.index >= start_chapter)
                    and (end_chapter is None or chapter.index <= end_chapter)]
    if not selected:
        raise ValueError("Requested selection matches no chapters")
    return selected


def partial_discovery(url, obscura):
    state = getattr(obscura, "last_toc_state", {})
    chapters = state.get("extracted_chapters", [])
    return {"url": url, "success": bool(chapters), "novel_title": state.get("novel_title") or "Unknown Novel",
            "chapters": [chapter.model_dump() for chapter in chapters], "total_chapters": len(chapters),
            "is_complete": False, "issues": state.get("issues", []) + ["Discovery deadline exhausted"],
            "completeness_evidence": state.get("completeness_evidence", [])}


async def discover(url, obscura, llm):
    limits = ScraperLimits.from_env()
    toc = TocAgent(obscura_client=obscura, llm_client=llm)
    classifier = PageClassifier(llm_client=llm, obscura_client=obscura, toc_agent=toc)
    html = await obscura.fetch_html(url)
    classification = await classifier.classify(url, html)
    state = classification.toc_state or {}
    catalog_url = url if classification.page_type == "TOC" else None
    if classification.page_type == "TOC":
        if not state:
            state = await toc.extract_toc(url, html=html)
        if not state.get("extracted_chapters"):
            rendered = await obscura.fetch_html(url, force_browser=True)
            state = await toc.extract_toc(url, html=rendered)
    else:
        navigation = observed_navigation(html, url)
        witnessed = {canonical_url(urljoin(url, anchor["href"])) for anchor in BeautifulSoup(html, "lxml").select("a[href]")}
        toc_url = navigation.get("toc")
        if not toc_url and classification.toc_url:
            from src.agent.domain_memory import domain_memory
            from src.utils.novel_url import toc_url_from_recipe
            recipe = domain_memory.get_recipe(url)
            derived = toc_url_from_recipe(url, recipe.sample_toc_url, recipe.sample_chapter_url) if recipe else None
            if canonical_url(classification.toc_url) in witnessed or classification.toc_url == derived:
                toc_url = classification.toc_url
        if toc_url:
            catalog_url = toc_url
            state = await toc.extract_toc(toc_url)
        else:
            # Follow only observed chapter navigation; never invent chapter URLs.
            current = ChapterLink(index=1, title=classification.chapter_title or "Chapter 1", url=url)
            before, after, seen, issues = [], [], {canonical_url(url)}, []
            for direction, destination in (("previous", before), ("next", after)):
                page_html, page_url = html, url
                while len(seen) < limits.toc_pages:
                    target = observed_navigation(page_html, page_url).get(direction)
                    if not target:
                        break
                    if target in seen:
                        issues.append("Chapter navigation cycle")
                        break
                    seen.add(target)
                    try:
                        page_html = await obscura.fetch_html(target)
                    except Exception as error:
                        issues.append(str(error))
                        break
                    page_url = target
                    soup = BeautifulSoup(page_html, "lxml")
                    heading = soup.select_one("h1, h2")
                    destination.append(ChapterLink(index=1, title=heading.get_text(" ", strip=True) if heading else "Chapter", url=target))
                else:
                    issues.append("Chapter navigation budget exhausted")
            state = {"extracted_chapters": list(reversed(before)) + [current] + after,
                     "is_complete": not issues, "issues": issues,
                     "completeness_evidence": ["Observed previous/next chapter chain exhausted"] if not issues else [],
                     "extraction_strategy": "chapter_navigation", "novel_title": classification.novel_title}
    chapters = _scope_chapters(url, state.get("extracted_chapters", classification.chapter_links))
    return {
        "success": bool(chapters), "url": url, "page_type": classification.page_type,
        "novel_title": state.get("novel_title") or classification.novel_title or "Unknown Novel",
        "author": state.get("author") or classification.author,
        "description": state.get("description") or classification.description,
        "total_chapters": len(chapters),
        "chapters": [chapter.model_dump() for chapter in chapters],
        "is_complete": bool(chapters) and bool(state.get("is_complete")),
        "claimed_chapter_count": state.get("claimed_chapter_count"),
        "confidence_score": state.get("confidence_score", 0),
        "completeness_evidence": state.get("completeness_evidence", []),
        "issues": state.get("issues", []), "extraction_strategy": state.get("extraction_strategy"),
        "toc_url": catalog_url,
    }


async def inspect_novel_url(url: str):
    obscura = ObscuraClient()
    try:
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(discover(url, obscura, LLMClient()), ScraperLimits.from_env().discovery_seconds)
        except asyncio.TimeoutError:
            result = partial_discovery(url, obscura)
        result["metrics"] = {**obscura.metrics, "discovery_seconds": time.monotonic() - start}
        return result
    except Exception as error:
        return {"success": False, "url": url, "error": safe_error(error), "total_chapters": 0, "chapters": [], "is_complete": False}
    finally:
        await obscura.close()


async def extract_novel_chapters(url, output_dir, chapter_indices=None, start_chapter=None, end_chapter=None,
                                 concurrency=3, include_frontmatter=False, on_progress=None, overwrite=False,
                                 state_dir=None, job_id=None, snapshot_file=None):
    obscura = ObscuraClient()
    destination = Path(output_dir).resolve()
    state_dir = Path(state_dir) if state_dir else destination / ".scraper"
    job_id = job_id or uuid.uuid4().hex
    checkpoint = ChapterCheckpoint(state_dir, job_id)
    destination_lock = DestinationLock(state_dir, destination)
    snapshot_path = state_dir / "snapshots" / f"{job_id}.json"
    def emit(stage, message, current=0, total=0, percent=0):
        if on_progress:
            on_progress({"type": "progress", "stage": stage,
                         "current": current, "total": total, "percent": percent, "message": message[:1000]})
    try:
        destination_lock.acquire()
        from src.agent.llm import token_tracker
        token_tracker.reset()
        llm = LLMClient()
        emit("discovery", "Discovering accessible catalog")
        discovery_started = time.monotonic()
        supplied = Path(snapshot_file) if snapshot_file else snapshot_path
        if supplied.is_file():
            snapshot = json.loads(supplied.read_text(encoding="utf-8"))
            if canonical_url(snapshot["url"]) != canonical_url(url):
                raise ValueError("Inspection snapshot belongs to another URL")
        else:
            snapshot = None
        if snapshot is None or not snapshot.get("is_complete"):
            try:
                snapshot = await asyncio.wait_for(discover(url, obscura, llm), ScraperLimits.from_env().discovery_seconds)
            except asyncio.TimeoutError:
                recovered = partial_discovery(url, obscura)
                if recovered["chapters"] or snapshot is None:
                    snapshot = recovered
        atomic_json(snapshot_path, snapshot)
        obscura.metrics["discovery_seconds"] = time.monotonic() - discovery_started
        chapters = [ChapterLink.model_validate(item) for item in snapshot.get("chapters", [])]
        selected = select_chapters(chapters, chapter_indices, start_chapter, end_chapter)
        storage = NovelStorage(base_output_dir=destination, direct_output=True, include_frontmatter=include_frontmatter,
                               overwrite=overwrite, state_dir=state_dir / ("sources-" + hashlib.sha256(str(destination).encode()).hexdigest()[:12]))
        emit("validation", "Validating representative chapter layouts", total=len(selected))
        pending = [chapter for chapter in selected if not checkpoint.verified(chapter.url, destination)]
        sample_html, plans, pre_results = {}, {}, {}
        if pending:
            positions = list(dict.fromkeys([0, len(pending)//2, len(pending)-1]))
            boundaries = [index for index in range(1, len(pending)) if pending[index].volume and pending[index].volume != pending[index-1].volume]
            positions = list(dict.fromkeys(positions + boundaries + [len(pending)//4, len(pending)*3//4]))[:5]
            for position in positions:
                if checkpoint.cancelled():
                    break
                sample = pending[position]
                try:
                    html = await obscura.fetch_html(sample.url)
                    sample_html[sample.url] = html
                    orchestrator = ReviewLoopOrchestrator(analyzer=ChapterAnalyzer(llm), llm_client=llm)
                    reviewed = await asyncio.wait_for(orchestrator.run(html, sample.url), ScraperLimits.from_env().discovery_seconds)
                    if not reviewed.approved or not reviewed.verification.success:
                        html = await obscura.fetch_html(sample.url, force_browser=True)
                        sample_html[sample.url] = html
                        reviewed = await asyncio.wait_for(orchestrator.run(html, sample.url), ScraperLimits.from_env().discovery_seconds)
                    if not reviewed.approved or not reviewed.verification.success:
                        error = getattr(reviewed.verification, "error", None) or "Sample chapter validation rejected"
                        pre_results[sample.url] = ("blocked" if getattr(reviewed.verification, "is_paywalled", False) or "unsupported" in error.lower() else "failed", error)
                        continue
                except PermissionError as error:
                    pre_results[sample.url] = ("blocked", safe_error(error))
                    continue
                except Exception as error:
                    pre_results[sample.url] = ("failed", safe_error(error))
                    continue
                layout = fingerprint(html)
                plans[layout] = reviewed.plan
                from src.agent.domain_memory import domain_memory
                if reviewed.iterations:
                    domain_memory.save_recipe(url, snapshot.get("toc_url") or "", sample.url, snapshot.get("extraction_strategy") or "dom_heuristic",
                                             reviewed.plan, quality_score=reviewed.review.quality_score, sample_html=html)
                if reviewed.iterations == 0:
                    obscura.metrics["recipe_hits"] = obscura.metrics.get("recipe_hits", 0) + 1
            plan = next(iter(plans.values()), DOMStructurePlan(title_selector="h1", content_selector="article"))
        else:
            plan = DOMStructurePlan(title_selector="h1", content_selector="article")
        runner = BatchScraperRunner(snapshot["novel_title"], plan, obscura_client=obscura, storage=storage,
                    concurrency=concurrency, llm_client=llm, checkpoint=checkpoint, sample_html=sample_html,
                    on_progress=lambda current, total, title, percent: emit("downloading", title, current, total, percent),
                    on_stage=lambda stage, message: emit(stage, message, len(runner.outcomes), len(selected), len(runner.outcomes)/len(selected)*100))
        runner._layout_plans.update(plans)
        runner.require_layout_approval = True
        runner.pre_results = pre_results
        summary = await runner.run(selected, url, author=snapshot.get("author"), description=snapshot.get("description"))
        status = ("cancelled" if summary["cancelled"] or checkpoint.cancelled() else
                  "paused" if summary["blocked"] else
                  "completed" if not summary["failed"] and snapshot.get("is_complete") else "partial")
        return {"success": status == "completed", "status": status, **summary,
                "files": [item["file"] for item in summary["outcomes"] if item.get("file") and item["success"]],
                "total_selected": len(selected), "output_dir": str(destination),
                "is_complete": snapshot.get("is_complete", False),
                "completeness_evidence": snapshot.get("completeness_evidence", []), "issues": snapshot.get("issues", [])}
    except Exception as error:
        records = list(checkpoint.records.values())
        files = [item["file"] for item in records if item.get("file") and item.get("success")]
        return {"success": False, "status": "paused" if isinstance(error, (PermissionError, BlockingIOError)) else "partial" if files else "failed",
                "error": safe_error(error), "files": files, "completed": len(files),
                "total_selected": len(records), "failed": 0, "blocked": int(isinstance(error, PermissionError))}
    finally:
        destination_lock.release()
        await obscura.close()


def main():
    parser = argparse.ArgumentParser(description="Novel scraper JSON worker")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--url", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--url", required=True)
    extract.add_argument("--dest", required=True)
    extract.add_argument("--indices")
    extract.add_argument("--start", type=int)
    extract.add_argument("--end", type=int)
    extract.add_argument("--concurrency", type=int, default=3)
    extract.add_argument("--frontmatter", action="store_true")
    extract.add_argument("--overwrite", action="store_true")
    extract.add_argument("--state-dir")
    extract.add_argument("--job-id")
    extract.add_argument("--snapshot-file")
    args = parser.parse_args()
    if args.command == "inspect":
        print(json.dumps(asyncio.run(inspect_novel_url(args.url)), ensure_ascii=False), flush=True)
    else:
        args.job_id = args.job_id or uuid.uuid4().hex
        indices = [int(value) for value in args.indices.split(",")] if args.indices is not None else None
        result = asyncio.run(extract_novel_chapters(args.url, args.dest, indices, args.start, args.end,
            args.concurrency, args.frontmatter,
            on_progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
            overwrite=args.overwrite, state_dir=args.state_dir, job_id=args.job_id, snapshot_file=args.snapshot_file))
        encoded = json.dumps({"type": "completed", **result}, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 48000:
            state_dir = Path(args.state_dir) if args.state_dir else Path(args.dest).resolve() / ".scraper"
            result_path = state_dir / "results" / f"{args.job_id or 'standalone'}.json"
            atomic_json(result_path, result)
            encoded = json.dumps({"type": "completed", "status": result["status"], "success": result["success"], "result_file": str(result_path.resolve())})
        print(encoded, flush=True)


if __name__ == "__main__":
    main()

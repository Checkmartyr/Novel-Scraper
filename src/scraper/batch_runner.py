import asyncio
import random
import time
import logging
import re
from urllib.parse import urljoin, urlparse
from typing import List, Optional, Callable, Dict, Any
from bs4 import BeautifulSoup

from src.agent.classifier import ChapterLink
from src.agent.ch import (
    DOMStructurePlan,
    ChapterCodeGenerator,
    ExtractedChapter,
    SelfHealer,
)
from src.core.obscura_client import ObscuraClient
from src.scraper.storage import NovelStorage
from src.config import DEFAULT_CONCURRENCY, MIN_DELAY_SECONDS, MAX_DELAY_SECONDS

logger = logging.getLogger("scraper.batch")

class BatchScraperRunner:
    """Orchestrates polite concurrent batch downloading with self-healing and progress tracking."""

    def __init__(
        self,
        novel_title: str,
        initial_plan: DOMStructurePlan,
        obscura_client: Optional[ObscuraClient] = None,
        storage: Optional[NovelStorage] = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        min_delay: float = MIN_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        on_progress: Optional[Callable[[int, int, str, float], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        previous_interaction_id: Optional[str] = None,
        llm_client: Optional[Any] = None,
    ):
        self.novel_title = novel_title
        self.plan = initial_plan
        self.obscura = obscura_client or ObscuraClient()
        self.storage = storage or NovelStorage()
        self.concurrency = max(1, min(concurrency, 10))
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.on_progress = on_progress
        self.on_log = on_log
        self.previous_interaction_id = previous_interaction_id

        self.healer = SelfHealer(llm_client=llm_client) if llm_client is not None else SelfHealer()
        self._is_paused = False
        self._is_cancelled = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused by default

    def log(self, message: str, level: str = "info") -> None:
        """Emit log to console and registered UI callback."""
        if self.on_log:
            self.on_log(message, level)
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def pause(self) -> None:
        """Pause the batch scraper."""
        self._is_paused = True
        self._pause_event.clear()
        self.log("Batch scraper paused.", "warning")

    def resume(self) -> None:
        """Resume the batch scraper."""
        self._is_paused = False
        self._pause_event.set()
        self.log("Batch scraper resumed.", "info")

    def cancel(self) -> None:
        """Cancel ongoing scrape."""
        self._is_cancelled = True
        self._pause_event.set()
        self.log("Batch scraper cancelled by user.", "warning")

    async def _scrape_single_chapter(
        self,
        link: ChapterLink,
        semaphore: asyncio.Semaphore,
    ) -> bool:
        """Scrape, extract, and save a single chapter with polite delay."""
        async with semaphore:
            await self._pause_event.wait()
            if self._is_cancelled:
                return False

            # Polite jitter delay
            delay = random.uniform(self.min_delay, self.max_delay)
            await asyncio.sleep(delay)

            # 1. Fetch HTML using ObscuraClient with polite retry
            html = None
            last_err = None
            for attempt in range(2):
                try:
                    html = await self.obscura.fetch_html(link.url)
                    if html and len(html) > 200:
                        break
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        await asyncio.sleep(2.0)

            if not html:
                self.log(f"Failed to fetch {link.title} ({link.url}): {last_err or 'Empty response'}", "error")
                return False

            # 2. Extract content with current plan
            generator = ChapterCodeGenerator(self.plan)
            result = generator.extract(html)

            # 3. If extraction failed or yielded too short text, trigger self-healer
            if not result.success:
                self.log(f"Extraction failed on '{link.title}'. Triggering self-heal...", "warning")
                healed, updated_plan, healed_result = await self.healer.attempt_heal(
                    url=link.url,
                    html=html,
                    current_plan=self.plan,
                    previous_interaction_id=self.previous_interaction_id,
                )
                if healed and healed_result:
                    self.plan = updated_plan
                    result = healed_result
                    healer_llm = getattr(self.healer, "llm", None)
                    if healer_llm and getattr(healer_llm, "last_interaction_id", None):
                        self.previous_interaction_id = healer_llm.last_interaction_id
                    self.log(f"Self-healed! Updated extractor for '{link.title}'.", "info")
                    try:
                        from src.agent.domain_memory import domain_memory
                        domain_memory.update_chapter_plan(link.url, updated_plan, sample_url=link.url)
                    except Exception as e:
                        logger.warning(f"Failed syncing healed plan to domain memory: {e}")
                else:
                    self.log(f"Could not extract content for '{link.title}'. Saving placeholder.", "error")

            # 4. Stitch sub-pages if present (e.g. ?page=2, 下一页)
            if result.success:
                result = await self._stitch_subpages(link.url, html, result)

            # 5. Save to disk
            try:
                saved_path = await self.storage.save_chapter(
                    novel_title=self.novel_title,
                    chapter_index=link.index,
                    chapter_title=link.title,
                    chapter=result,
                    source_url=link.url
                )
                return result.success
            except Exception as e:
                self.log(f"Failed saving chapter {link.index}: {e}", "error")
                return False

    async def _stitch_subpages(
        self,
        initial_url: str,
        initial_html: str,
        initial_result: ExtractedChapter
    ) -> ExtractedChapter:
        """If a chapter is split across multiple pages (?page=2, 下一页), fetch and stitch them together."""
        current_html = initial_html
        current_url = initial_url
        generator = ChapterCodeGenerator(self.plan)
        combined_markdown = [initial_result.content_markdown] if initial_result.content_markdown else []
        visited_urls = {initial_url}
        max_subpages = 10

        for _ in range(max_subpages):
            soup = BeautifulSoup(current_html, "lxml")
            next_subpage_url = None

            # Find next page link (e.g. text "下一页" or "next page", or href with ?page=)
            for a in soup.find_all("a", href=True):
                text = a.get_text(strip=True)
                href = a["href"].strip()
                if not href or href.startswith("javascript:") or href.startswith("#"):
                    continue

                # Check for "下一页" or "next page" (distinct from "下一章" / "next chapter")
                is_subpage = False
                if any(k in text for k in ["下一页", "下页"]) or re.search(r"\bnext\s*page\b", text, re.I):
                    is_subpage = True
                elif ("?page=" in href or "&page=" in href) and text in ["2", "3", "4", "5", "6", "7", "8", "9"]:
                    is_subpage = True

                if is_subpage:
                    cand_url = urljoin(current_url, href)
                    parsed_base = urlparse(initial_url)
                    parsed_cand = urlparse(cand_url)
                    # Verify it stays on the same chapter base URL
                    if parsed_base.path == parsed_cand.path and cand_url not in visited_urls:
                        next_subpage_url = cand_url
                        break

            if not next_subpage_url:
                break

            visited_urls.add(next_subpage_url)
            try:
                sub_delay = random.uniform(0.3, 0.7)
                await asyncio.sleep(sub_delay)
                current_html = await self.obscura.fetch_html(next_subpage_url)
                sub_res = generator.extract(current_html)
                if sub_res.content_markdown:
                    combined_markdown.append(sub_res.content_markdown)
                    current_url = next_subpage_url
                else:
                    break
            except Exception as e:
                self.log(f"Error fetching subpage {next_subpage_url}: {e}", "warning")
                break

        final_content = "\n\n".join(combined_markdown)
        clean_title = re.sub(r"\s*[（\(\[]\s*\d+\s*/\s*\d+\s*[）\)\]]", "", initial_result.title).strip()
        words = len(re.findall(r"\w+", final_content))
        char_count = len(final_content)
        success = char_count > 50
        return ExtractedChapter(
            title=clean_title or initial_result.title,
            content_markdown=final_content,
            word_count=words,
            char_count=char_count,
            success=success,
            error=None if success else "Extracted text content too short (< 50 chars).",
        )

    async def run(
        self,
        chapters: List[ChapterLink],
        source_url: str,
        author: Optional[str] = None,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """Run batch scraper across chapter list with progress reporting."""
        total = len(chapters)
        completed = 0
        failed = 0
        start_time = time.time()

        self.log(f"Starting batch scrape for '{self.novel_title}' ({total} chapters, concurrency={self.concurrency})", "info")

        semaphore = asyncio.Semaphore(self.concurrency)
        chapter_records: List[Dict[str, Any]] = []

        async def worker(link: ChapterLink):
            nonlocal completed, failed
            success = await self._scrape_single_chapter(link, semaphore)
            if success:
                completed += 1
            else:
                failed += 1

            elapsed = max(time.time() - start_time, 0.1)
            speed = (completed / elapsed) * 60.0  # chapters per minute

            if self.on_progress:
                self.on_progress(completed + failed, total, link.title, speed)

            chapter_records.append({
                "index": link.index,
                "title": link.title,
                "url": link.url,
                "downloaded": success,
                "success": success
            })

        # Launch all tasks
        tasks = [asyncio.create_task(worker(ch)) for ch in chapters]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Update metadata.json
        from src.agent.llm import token_tracker
        tokens_summary = token_tracker.get_summary()
        await self.storage.save_metadata(
            novel_title=self.novel_title,
            source_url=source_url,
            author=author,
            description=description,
            total_chapters=total,
            completed_chapters=completed,
            chapter_index_list=chapter_records,
            token_usage=tokens_summary,
        )

        total_time = time.time() - start_time
        summary = {
            "novel_title": self.novel_title,
            "total": total,
            "completed": completed,
            "failed": failed,
            "elapsed_seconds": round(total_time, 2),
            "token_usage": tokens_summary,
        }
        self.log(f"Batch scrape finished in {round(total_time, 1)}s. Completed: {completed}/{total}", "info")
        return summary

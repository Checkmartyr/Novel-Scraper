"""High-level TocAgent wrapper executing the compiled LangGraph workflow."""

import logging
import asyncio
import time
from src.scraper.runtime import ScraperLimits
from typing import Optional, List, Callable
from pathlib import Path

from src.agent.toc.state import TocState
from src.agent.toc.graph import TocGraphWorkflow
from src.agent.classifier import ChapterLink, ClassificationResult
from src.core.obscura_client import ObscuraClient

logger = logging.getLogger("agent.toc.agent")


class TocAgent:
    """Agentic Table of Contents extractor powered by LangGraph, Playwright, and Obscura.
    
    Autonomously resolves collapsed accordions, 'load more' triggers, SSR Apollo/Redux hydration,
    multi-page pagination, and chapter count discrepancies without human-in-the-loop.
    """

    def __init__(
        self,
        obscura_client: Optional[ObscuraClient] = None,
        max_iterations: Optional[int] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        llm_client=None,
    ):
        self.obscura = obscura_client or ObscuraClient()
        self.limits = ScraperLimits.from_env()
        self.max_iterations = max_iterations or self.limits.toc_cycles
        self.on_log = on_log
        self.workflow = TocGraphWorkflow(obscura_client=self.obscura, on_log=on_log, llm_client=llm_client)
        self.app = self.workflow.build()

    async def extract_toc(self, url: str, html: Optional[str] = None) -> TocState:
        """Run the LangGraph workflow to extract all chapters from a TOC page."""
        if not html:
            logger.info(f"TocAgent fetching initial page HTML for: {url}")
            html = await self.obscura.fetch_html(url)

        # Check Domain Memory for cached recipe
        try:
            from src.agent.domain_memory import domain_memory
            from src.agent.toc.tools import ClaimInspector, TocAuditor
            recipe = domain_memory.get_recipe(url)
            if recipe:
                claimed, title, author, desc = ClaimInspector.inspect(html, url)
                ok, chapters = domain_memory.test_toc_recipe(recipe, html, url)
                if ok and len(chapters) >= 1:
                    is_complete, conf, unexpanded, pagination, issues = TocAuditor.audit(chapters, claimed, html)
                    # Valid if confidence high or if no claimed count and no unexpanded accordions/pagination
                    if is_complete and not pagination and not unexpanded:
                        log_msg = f"[DomainMemory] Successfully extracted {len(chapters)} chapters using saved recipe for '{recipe.domain}' (Confidence: {conf})."
                        logger.info(log_msg)
                        if self.on_log:
                            try:
                                self.on_log(log_msg, "info")
                            except Exception:
                                pass
                        domain_memory.record_usage(recipe.domain)
                        return {
                            "url": url,
                            "html": html,
                            "novel_title": title,
                            "author": author,
                            "description": desc,
                            "claimed_chapter_count": claimed,
                            "extracted_chapters": chapters,
                            "extraction_strategy": "remembered_recipe",
                            "confidence_score": conf,
                            "has_unexpanded_sections": False,
                            "has_pagination": False,
                            "pagination_urls": [],
                            "is_complete": True,
                            "completeness_evidence": ["Advertised count covered" if claimed else "Observed catalog navigation exhausted"],
                            "iteration": 0,
                            "max_iterations": self.max_iterations,
                            "issues": [],
                            "logs": [log_msg],
                        }
                    else:
                        logger.warning(f"[DomainMemory] Saved recipe for '{recipe.domain}' confidence {conf} < 0.8. Falling back to LangGraph.")
        except Exception as e:
            logger.debug(f"Domain memory check exception: {e}")

        initial_state: TocState = {
            "url": url,
            "html": html,
            "novel_title": "",
            "author": None,
            "description": None,
            "claimed_chapter_count": None,
            "extracted_chapters": [],
            "extraction_strategy": "initial",
            "confidence_score": 0.0,
            "has_unexpanded_sections": False,
            "has_pagination": False,
            "pagination_urls": [],
            "is_complete": False,
            "iteration": 0,
            "max_iterations": self.max_iterations,
            "issues": [],
            "logs": [],
            "deadline": time.monotonic() + self.limits.discovery_seconds,
            "last_count": -1,
            "stagnant_attempts": 0,
            "expanded": False,
            "completeness_evidence": [],
        }

        logger.info(f"TocAgent launching LangGraph execution on {url}...")
        try:
            final_state: TocState = await asyncio.wait_for(self.app.ainvoke(initial_state, config={"recursion_limit": max(50, self.max_iterations * 8)}), timeout=self.limits.discovery_seconds)
        except asyncio.TimeoutError:
            final_state = getattr(self.workflow, "last_state", initial_state).copy()
            final_state["is_complete"] = False
            final_state["issues"] = final_state.get("issues", []) + ["Discovery deadline exhausted"]
        logger.info(
            f"TocAgent LangGraph completed! Total Chapters: {len(final_state['extracted_chapters'])}, "
            f"Strategy: {final_state['extraction_strategy']}, Confidence: {final_state['confidence_score']}"
        )

        conf = final_state.get("confidence_score", 0.0)
        ch_count = len(final_state.get("extracted_chapters", []))
        if final_state.get("is_complete") and ch_count > 0:
            try:
                from src.agent.domain_memory import domain_memory
                domain_memory.update_toc_plan(
                    domain_or_url=url,
                    toc_strategy=final_state.get("extraction_strategy", "dom_heuristic"),
                    container_selector=final_state.get("custom_container_selector"),
                    link_selector=final_state.get("custom_link_selector"),
                    sample_url=url,
                )
            except Exception as e:
                logger.debug(f"Failed to auto-update TOC recipe in domain memory: {e}")

        return final_state

    async def to_classification_result(self, url: str, html: Optional[str] = None) -> ClassificationResult:
        """Convert LangGraph TOC output to standard ClassificationResult for the scraper pipeline."""
        state = await self.extract_toc(url, html)
        return ClassificationResult(
            page_type="TOC" if state["extracted_chapters"] else "CHAPTER",
            novel_title=state["novel_title"],
            author=state["author"],
            description=state["description"],
            chapter_links=state["extracted_chapters"],
        )

    @staticmethod
    def export_to_text(state: TocState, output_path: str | Path) -> Path:
        """Write extracted chapter list to a human-readable text file."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        chapters = state.get("extracted_chapters", [])

        with open(out, "w", encoding="utf-8") as f:
            f.write(f"Novel Title   : {state.get('novel_title', 'Untitled')}\n")
            if state.get("author"):
                f.write(f"Author        : {state['author']}\n")
            f.write(f"Source URL    : {state.get('url')}\n")
            f.write(f"Total Chapters: {len(chapters)}\n")
            if state.get("claimed_chapter_count"):
                f.write(f"Claimed Count : {state['claimed_chapter_count']}\n")
            f.write(f"Strategy      : {state.get('extraction_strategy')}\n")
            f.write(f"Confidence    : {state.get('confidence_score')}\n")
            f.write("=" * 90 + "\n\n")

            for ch in chapters:
                f.write(f"{ch.index:04d}. {ch.title}\n")
                f.write(f"      {ch.url}\n\n")

        logger.info(f"Exported {len(chapters)} chapters to {out}")
        return out

"""High-level TocAgent wrapper executing the compiled LangGraph workflow."""

import logging
import asyncio
import time
from src.scraper.runtime import ScraperLimits, canonical_url
from typing import Optional, List, Callable
from pathlib import Path

from src.agent.toc.state import TocState
from src.agent.toc.graph import TocGraphWorkflow
from src.agent.toc.discovery import replay_api_catalog, capture_api_recipe_candidates
from src.agent.classifier import ChapterLink, ClassificationResult
from src.core.obscura_client import ObscuraClient
from src.scraper.recovery import recovery_scope, before_deadline, current_scope
from src.core.page_analysis import PageAnalysis

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

    async def extract_toc(self, url: str, html: Optional[str] = None, *, page_analysis=None) -> TocState:
        previous_deadline = getattr(self.obscura, "discovery_deadline", None)
        try:
            with recovery_scope(url, self.obscura) as scope:
                scope.deadline = min(scope.deadline, time.monotonic() + self.limits.discovery_seconds)
                self.obscura.discovery_deadline = scope.deadline
                if html is None:
                    html = await before_deadline(self.obscura.fetch_html(url), scope.deadline)
                page_analysis = page_analysis or PageAnalysis(html)
                await scope.observe_catalog(url, html, self.obscura)
                return await self._extract_toc(url, html, page_analysis=page_analysis)
        finally:
            if previous_deadline is None:
                self.obscura.__dict__.pop("discovery_deadline", None)
            else:
                self.obscura.discovery_deadline = previous_deadline

    async def _extract_toc(self, url: str, html: Optional[str] = None, *, page_analysis=None) -> TocState:
        """Run the LangGraph workflow to extract all chapters from a TOC page."""
        if not html:
            logger.info(f"TocAgent fetching initial page HTML for: {url}")
            html = await self.obscura.fetch_html(url)
        page_analysis = page_analysis or PageAnalysis(html)

        # Check Domain Memory for cached recipe
        try:
            from src.agent.domain_memory import domain_memory
            from src.agent.toc.tools import ClaimInspector, TocAuditor, DomLinkExtractor, PaginatedTocCrawler
            recipe = domain_memory.get_recipe(url)
            if recipe:
                claimed, title, author, desc = ClaimInspector.inspect(html, url, page_analysis=page_analysis)
                # A warm API recipe is evidence only when its current response
                # still maps cleanly and agrees exactly with the live DOM.
                dom_chapters = DomLinkExtractor.extract(html, url, page_analysis=page_analysis)
                for api_recipe in recipe.api_recipes:
                    if "chapters" not in api_recipe.response_mapping:
                        continue
                    response = await self.obscura.replay_api_recipe(api_recipe)
                    api_chapters = replay_api_catalog(api_recipe, response, url) if response is not None else None
                    if not api_chapters:
                        continue
                    api_urls = {canonical_url(c.url) for c in api_chapters}
                    dom_urls = {canonical_url(c.url) for c in dom_chapters}
                    if current_scope():
                        from src.scraper.runtime import belongs_to_observed_novel
                        current_scope().catalogs.add(canonical_url(api_recipe.endpoint))
                        current_scope().approved_links.update(canonical_url(c.url) for c in api_chapters
                            if belongs_to_observed_novel(url, c.url))
                    api_complete, api_conf, api_unexpanded, api_pagination, api_issues = TocAuditor.audit(
                        api_chapters, claimed, html, page_analysis=page_analysis)
                    if (api_urls == dom_urls and api_complete and not api_unexpanded and not api_pagination
                            and not (current_scope() and current_scope().unresolved)):
                        return {
                            "url": url, "html": html, "novel_title": title, "author": author,
                            "description": desc, "claimed_chapter_count": claimed,
                            "extracted_chapters": api_chapters,
                            "extraction_strategy": "remembered_api_recipe",
                            "confidence_score": api_conf, "has_unexpanded_sections": False,
                            "has_pagination": False, "pagination_urls": [],
                            "pagination_requests": [], "api_recipe_candidates": [],
                            "is_complete": True,
                            "completeness_evidence": ["Replayed API catalog mapping agrees with live DOM"],
                            "evidence_observations": [
                                {"source": "remembered_api_recipe", "url": canonical_url(c.url),
                                 "label": c.title, "index": c.index, "volume": c.volume,
                                 "endpoint": api_recipe.endpoint}
                                for c in api_chapters
                            ] + [
                                {"source": "dom", "url": canonical_url(c.url), "label": c.title,
                                 "index": c.index, "volume": c.volume}
                                for c in dom_chapters
                            ],
                            "iteration": 0, "max_iterations": self.max_iterations,
                            "issues": [], "logs": ["Replayed saved API recipe and cross-checked the live DOM."],
                        }
                ok, chapters = domain_memory.test_toc_recipe(recipe, html, url)
                if ok and len(chapters) >= 1:
                    # A warm recipe is one observation; compare it with the live DOM.
                    recipe_urls = {canonical_url(c.url) for c in chapters}
                    dom_urls = {canonical_url(c.url) for c in dom_chapters}
                    pagination_urls = PaginatedTocCrawler.detect_pagination_urls(html, url, page_analysis=page_analysis)
                    scope = current_scope()
                    is_complete, conf, unexpanded, pagination, issues = TocAuditor.audit(
                        chapters, claimed, html, page_analysis=page_analysis)
                    if recipe_urls != dom_urls:
                        is_complete = False
                        issues.append("Saved recipe and current DOM expose different chapter URLs.")
                    if is_complete and not pagination and not pagination_urls and not unexpanded and not (scope and scope.unresolved):
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
                            "completeness_evidence": ["Recipe and DOM chapter URLs agree", "Advertised count covered" if claimed else "Observed catalog navigation exhausted"],
                            "evidence_observations": [
                                {"source": "recipe", "url": canonical_url(c.url), "label": c.title,
                                 "index": c.index, "volume": c.volume}
                                for c in chapters
                            ] + [
                                {"source": "dom", "url": canonical_url(c.url), "label": c.title,
                                 "index": c.index, "volume": c.volume}
                                for c in dom_chapters
                            ],
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
            "pagination_requests": [],
            "api_recipe_candidates": [],
            "is_complete": False,
            "iteration": 0,
            "max_iterations": self.max_iterations,
            "issues": [],
            "logs": [],
            "deadline": current_scope().deadline,
            "last_count": -1,
            "stagnant_attempts": 0,
            "expanded": False,
            "completeness_evidence": [],
            "evidence_observations": [],
        }

        logger.info(f"TocAgent launching LangGraph execution on {url}...")
        self.workflow.page_analysis = page_analysis
        try:
            final_state: TocState = await before_deadline(self.app.ainvoke(initial_state, config={"recursion_limit": max(50, self.max_iterations * 8)}), initial_state["deadline"])
        except asyncio.TimeoutError:
            final_state = getattr(self.workflow, "last_state", initial_state).copy()
            final_state["is_complete"] = False
            final_state["issues"] = final_state.get("issues", []) + ["Discovery deadline exhausted"]
        logger.info(
            f"TocAgent LangGraph completed! Total Chapters: {len(final_state['extracted_chapters'])}, "
            f"Strategy: {final_state['extraction_strategy']}, Confidence: {final_state['confidence_score']}"
        )
        captured = capture_api_recipe_candidates(getattr(self.obscura, "network_responses", []), url)
        existing_candidates = final_state.get("api_recipe_candidates", [])
        candidate_ids = {(item.get("method"), item.get("endpoint"),
                          str(item.get("request_body"))) for item in existing_candidates}
        for candidate in captured:
            marker = (candidate.method, candidate.endpoint, str(candidate.request_body))
            if marker not in candidate_ids:
                candidate_ids.add(marker)
                existing_candidates.append(candidate.model_dump())
        final_state["api_recipe_candidates"] = existing_candidates

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

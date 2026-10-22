"""LangGraph StateGraph workflow for autonomous TOC extraction."""

import logging
import asyncio
import json
import time
import re
from bs4 import BeautifulSoup
from urllib.parse import urlsplit
from src.scraper.runtime import ScraperLimits, canonical_url, belongs_to_observed_novel
from src.scraper.recovery import current_scope
from src.agent.toc.discovery import expand_catalog, network_chapters
from typing import Dict, Any, List, Optional, Callable
from langgraph.graph import StateGraph, END

from src.agent.toc.state import TocState
from src.agent.toc.tools import (
    ClaimInspector,
    EmbeddedStateExtractor,
    DomLinkExtractor,
    InteractiveDomExpander,
    PaginatedTocCrawler,
    TocAuditor,
    TocSynthesizer,
)
from src.core.obscura_client import ObscuraClient
from src.utils.title_cleaner import clean_chapter_title, detect_and_fix_reverse_order

logger = logging.getLogger("agent.toc.graph")


class TocGraphWorkflow:
    """Encapsulates and compiles the LangGraph state machine for autonomous TOC extraction."""

    def __init__(
        self,
        obscura_client: Optional[ObscuraClient] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        llm_client=None,
    ):
        self.obscura = obscura_client or ObscuraClient()
        self.on_log = on_log
        self.llm = llm_client
        self.limits = ScraperLimits.from_env()

    def _emit_log(self, msg: str, level: str = "info") -> None:
        """Log to standard logger and trigger optional UI callback."""
        if level == "warning":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.info(msg)
        if self.on_log:
            try:
                self.on_log(msg, level)
            except Exception:
                pass

    def build(self) -> Any:
        """Construct and compile the LangGraph StateGraph."""
        workflow = StateGraph(TocState)

        # Register Nodes
        workflow.add_node("inspect_metadata", self._node_inspect_metadata)
        workflow.add_node("extract_embedded_state", self._node_extract_embedded_state)
        workflow.add_node("extract_dom", self._node_extract_dom)
        workflow.add_node("audit", self._node_audit)
        workflow.add_node("interactive_expand", self._node_interactive_expand)
        workflow.add_node("self_heal_toc", self._node_self_heal_toc)
        workflow.add_node("crawl_pagination", self._node_crawl_pagination)
        workflow.add_node("finalize", self._node_finalize)

        # Set Entry Point
        workflow.set_entry_point("inspect_metadata")

        # Edges
        workflow.add_edge("inspect_metadata", "extract_embedded_state")

        # Conditional route after embedded state extraction
        workflow.add_conditional_edges(
            "extract_embedded_state",
            self._route_after_embedded,
            {
                "audit": "audit",
                "extract_dom": "extract_dom",
            }
        )

        # DOM extraction always feeds into audit
        workflow.add_edge("extract_dom", "audit")

        # Conditional routing after audit (Critic loop)
        workflow.add_conditional_edges(
            "audit",
            self._route_after_audit,
            {
                "finalize": "finalize",
                "interactive_expand": "interactive_expand",
                "self_heal_toc": "self_heal_toc",
                "crawl_pagination": "crawl_pagination",
            }
        )

        # After expanding DOM, re-extract from enriched HTML
        workflow.add_edge("interactive_expand", "extract_dom")

        # After self-healing, re-audit
        workflow.add_edge("self_heal_toc", "audit")

        # After crawling pagination, re-audit
        workflow.add_edge("crawl_pagination", "audit")

        # Finalize ends the execution
        workflow.add_edge("finalize", END)

        return workflow.compile()

    # --- Node Implementations ---

    async def _node_inspect_metadata(self, state: TocState) -> Dict[str, Any]:
        """Inspect page text, badges, and JSON for claimed chapter count and novel title."""
        claimed, title, author, desc = ClaimInspector.inspect(state["html"], state["url"])
        logs = list(state.get("logs", []))
        log_msg = f"[Inspect] Novel Title: '{title}', Claimed Chapters: {claimed}"
        self._emit_log(log_msg)
        logs.append(log_msg)

        # Also detect pagination links
        pag_urls = PaginatedTocCrawler.detect_pagination_urls(state["html"], state["url"])

        return {
            "novel_title": title,
            "author": author or state.get("author"),
            "description": desc or state.get("description"),
            "claimed_chapter_count": claimed,
            "has_pagination": len(pag_urls) > 0,
            "pagination_urls": pag_urls,
            "logs": logs,
        }

    async def _node_extract_embedded_state(self, state: TocState) -> Dict[str, Any]:
        """Extract chapters from Apollo, Redux, Nuxt, or JSON-LD if present."""
        logs = list(state.get("logs", []))
        chapters = EmbeddedStateExtractor.extract(state["html"], state["url"])
        
        if chapters:
            log_msg = f"[EmbeddedState] Found {len(chapters)} chapters via embedded state!"
            self._emit_log(log_msg)
            logs.append(log_msg)
            return {
                "extracted_chapters": chapters,
                "extraction_strategy": "embedded_state",
                "logs": logs,
            }
        
        no_emb = "[EmbeddedState] No embedded state chapters found. Proceeding to DOM..."
        self._emit_log(no_emb)
        logs.append(no_emb)
        return {"logs": logs}

    async def _node_extract_dom(self, state: TocState) -> Dict[str, Any]:
        """Extract chapter links from the DOM."""
        logs = list(state.get("logs", []))
        dom_chapters = DomLinkExtractor.extract(state["html"], state["url"])
        
        current_chapters = state.get("extracted_chapters", [])
        merged = {canonical_url(c.url): c for c in current_chapters}
        merged.update({canonical_url(c.url): c for c in dom_chapters})
        dom_chapters = list(merged.values())
        if len(dom_chapters) > len(current_chapters):
            log_msg = f"[DomExtractor] Extracted {len(dom_chapters)} chapters from DOM (upgraded from {len(current_chapters)})."
            self._emit_log(log_msg)
            logs.append(log_msg)
            return {
                "extracted_chapters": dom_chapters,
                "extraction_strategy": "dom_heuristic",
                "logs": logs,
            }

        dom_msg = f"[DomExtractor] DOM yielded {len(dom_chapters)} chapters (kept {len(current_chapters)})."
        self._emit_log(dom_msg)
        logs.append(dom_msg)
        return {"logs": logs}

    async def _node_audit(self, state: TocState) -> Dict[str, Any]:
        """Auditor / Critic: evaluates whether extraction is complete or truncated."""
        self.last_state = state.copy()
        self.obscura.last_toc_state = state.copy()
        logs = list(state.get("logs", []))
        chapters = [chapter for chapter in state.get("extracted_chapters", []) if belongs_to_observed_novel(state["url"], chapter.url)]
        claimed = state.get("claimed_chapter_count")
        # Check if pagination URLs are present
        has_pag = state.get("has_pagination", False) and len(state.get("pagination_urls", [])) > 0

        is_complete, conf, unexpanded, pagination, issues = TocAuditor.audit(
            chapters=chapters,
            claimed_count=claimed,
            html=state["html"],
            check_pagination=has_pag,
        )
        if state.get("expanded"):
            if not claimed and chapters and not has_pag and not unexpanded:
                is_complete = True
                conf = 0.8
        if state.get("crawl_issues"):
            is_complete = False
            issues.extend(state["crawl_issues"])
        if state.get("expansion_incomplete"):
            is_complete = False
            issues.append("Browser expansion budget exhausted before catalog closure")
        if current_scope() and current_scope().unresolved:
            is_complete = False
            issues.append(f"Unverified migrated catalog links: {len(current_scope().unresolved)}")

        log_msg = f"[Audit] Complete: {is_complete}, Confidence: {conf}, Chapters: {len(chapters)}/{claimed or '?'}, Issues: {issues}"
        self._emit_log(log_msg)
        logs.append(log_msg)

        iteration = state.get("iteration", 0) + 1
        count = len(chapters)
        stagnant = state.get("stagnant_attempts", 0) + 1 if count == state.get("last_count", -1) else 0

        return {
            "is_complete": is_complete,
            "extracted_chapters": chapters,
            "confidence_score": conf,
            "has_unexpanded_sections": unexpanded,
            "issues": issues,
            "iteration": iteration,
            "last_count": count,
            "stagnant_attempts": stagnant,
            "logs": logs,
        }

    async def _node_interactive_expand(self, state: TocState) -> Dict[str, Any]:
        """Action: uses Playwright to click accordions, click 'load more', and scroll."""
        logs = list(state.get("logs", []))
        log_msg = f"[InteractiveExpand] Running Playwright interaction on {state['url']}..."
        self._emit_log(log_msg)
        logs.append(log_msg)

        try:
            expansion = await expand_catalog(state["url"], self.obscura, DomLinkExtractor.extract, self.llm)
            expanded_html, observed, next_urls = expansion
            merged = {canonical_url(c.url): c for c in state.get("extracted_chapters", [])}
            merged.update({canonical_url(c.url): c for c in observed})
            exp_msg = f"[InteractiveExpand] Successfully expanded DOM (size: {len(expanded_html)} bytes)."
            self._emit_log(exp_msg)
            logs.append(exp_msg)
            return {
                "html": expanded_html,
                "has_unexpanded_sections": False,
                "extracted_chapters": list(merged.values()),
                "pagination_urls": next_urls,
                "has_pagination": bool(next_urls),
                "expanded": True,
                "expansion_incomplete": not expansion.exhausted,
                "logs": logs,
            }
        except Exception as e:
            err_msg = f"[InteractiveExpand] Failed: {e}"
            self._emit_log(err_msg, "warning")
            logs.append(err_msg)
            return {"logs": logs}

    async def _node_self_heal_toc(self, state: TocState) -> Dict[str, Any]:
        """Self-heal TOC extraction using DOM synthesis clustering."""
        logs = list(state.get("logs", []))
        self._emit_log("[SelfHealTOC] Attempting DOM clustering synthesis...")
        logs.append("[SelfHealTOC] Attempting DOM clustering synthesis...")

        chapters, c_sel, l_sel = TocSynthesizer.synthesize(
            state["html"], state["url"], state.get("claimed_chapter_count")
        )
        current = state.get("extracted_chapters", [])
        if len(chapters) > len(current):
            heal_msg = f"[SelfHealTOC] Synthesizer recovered {len(chapters)} chapters (container='{c_sel}', link='{l_sel}')."
            self._emit_log(heal_msg)
            logs.append(heal_msg)
            return {
                "extracted_chapters": chapters,
                "custom_container_selector": c_sel,
                "custom_link_selector": l_sel,
                "extraction_strategy": "self_healed_dom",
                "heal_attempted": True,
                "logs": logs,
            }
        else:
            fail_msg = f"[SelfHealTOC] Synthesizer found {len(chapters)} chapters (not better than current {len(current)})."
            self._emit_log(fail_msg)
            logs.append(fail_msg)
            return {
                "heal_attempted": True,
                "logs": logs,
            }

    async def _node_crawl_pagination(self, state: TocState) -> Dict[str, Any]:
        """Crawl the recursive frontier; unresolved pages remain explicit evidence gaps."""
        all_chapters = {canonical_url(c.url): c for c in state.get("extracted_chapters", [])}
        frontier = list(state.get("pagination_urls", []))
        visited = {canonical_url(state["url"])}
        issues = []
        pages = 1
        while frontier and pages < self.limits.toc_pages and time.monotonic() < state.get("deadline", float("inf")):
            batch_size = min(3, self.limits.toc_pages - pages)
            batch, frontier = frontier[:batch_size], frontier[batch_size:]
            async def fetch(url):
                try:
                    html = await self.obscura.fetch_html(url, wait_until="domcontentloaded")
                    return url, html, None
                except Exception as error:
                    return url, "", str(error)
            batch = [u for u in batch if canonical_url(u) not in visited and urlsplit(u).netloc == urlsplit(state["url"]).netloc]
            visited.update(canonical_url(u) for u in batch)
            pages += len(batch)
            if not batch:
                continue
            for page_url, html, error in await asyncio.gather(*(fetch(u) for u in batch)):
                if error:
                    issues.append(f"Unresolved catalog page {page_url}: {error}")
                    continue
                if current_scope():
                    await current_scope().observe_catalog(page_url, html, self.obscura)
                try:
                    data = json.loads(html)
                    chapters, next_urls = network_chapters([{"url": page_url, "data": data}], page_url)
                except (ValueError, TypeError):
                    chapters = EmbeddedStateExtractor.extract(html, page_url) + DomLinkExtractor.extract(html, page_url)
                    next_urls = PaginatedTocCrawler.detect_pagination_urls(html, page_url)
                    if not chapters:
                        try:
                            rendered = await self.obscura.fetch_html(page_url, force_browser=True)
                            if current_scope():
                                await current_scope().observe_catalog(page_url, rendered, self.obscura)
                            chapters = EmbeddedStateExtractor.extract(rendered, page_url) + DomLinkExtractor.extract(rendered, page_url)
                            next_urls += PaginatedTocCrawler.detect_pagination_urls(rendered, page_url)
                        except Exception as error:
                            issues.append(f"Unable to render catalog page {page_url}: {error}")
                if not chapters:
                    issues.append(f"No chapter evidence on catalog page {page_url}")
                for chapter in chapters:
                    all_chapters.setdefault(canonical_url(chapter.url), chapter)
                frontier.extend(u for u in next_urls if canonical_url(u) not in visited and u not in frontier)
        if frontier:
            issues.append("Catalog frontier exceeds discovery budget")
        ordered, _ = detect_and_fix_reverse_order(list(all_chapters.values()))
        for index, chapter in enumerate(ordered, 1):
            chapter.index = index
            chapter.title = clean_chapter_title(chapter.title)
        return {
            "extracted_chapters": ordered,
            "has_pagination": False,
            "pagination_urls": [],
            "extraction_strategy": "paginated_dom",
            "crawl_issues": issues,
            "completeness_evidence": [f"Visited {pages} catalog pages"] if not issues else [],
            "logs": state.get("logs", []) + issues,
        }

    async def _node_finalize(self, state: TocState) -> Dict[str, Any]:
        """Finalize chapter list with deduplication, reverse order fix, sequence re-indexing, and clean titles."""
        logs = list(state.get("logs", []))
        raw_chapters = state.get("extracted_chapters", [])
        soup = BeautifulSoup(state.get("html", ""), "lxml")
        from urllib.parse import urljoin
        volumes = {}
        for link in soup.select("a[href]"):
            heading = link.find_previous(["h2", "h3"])
            label = heading.get_text(" ", strip=True) if heading else ""
            if re.search(r"volume|vol\.|巻|部|卷|เล่ม", label, re.I):
                volumes[canonical_url(urljoin(state["url"], link["href"]))] = label
        
        seen = set()
        clean_list = []
        for ch in raw_chapters:
            if ch.url not in seen:
                seen.add(ch.url)
                clean_list.append(ch)
                ch.volume = ch.volume or volumes.get(canonical_url(ch.url))

        # Detect and fix reverse order (e.g. latest chapter first)
        clean_list, rev_detected = detect_and_fix_reverse_order(clean_list)
        if rev_detected:
            rev_msg = "[Finalize] Detected reverse chronological order. Inverted to ascending reading order."
            self._emit_log(rev_msg)
            logs.append(rev_msg)

        # Sort and re-index 1..N, clean titles
        for idx, ch in enumerate(clean_list, 1):
            ch.index = idx
            ch.title = clean_chapter_title(ch.title)

        log_msg = f"[Finalize] Outputting {len(clean_list)} clean chapters (Strategy: {state.get('extraction_strategy')}, Confidence: {state.get('confidence_score')})."
        self._emit_log(log_msg)
        logs.append(log_msg)

        return {
            "extracted_chapters": clean_list,
            "is_complete": bool(clean_list) and state.get("is_complete", False),
            "completeness_evidence": state.get("completeness_evidence", []) + (["Advertised count covered" if state.get("claimed_chapter_count") else "Observed catalog navigation exhausted"] if state.get("is_complete") and clean_list else []),
            "logs": logs,
        }

    # --- Routing Conditions ---

    def _route_after_embedded(self, state: TocState) -> str:
        chapters = state.get("extracted_chapters", [])
        claimed = state.get("claimed_chapter_count")
        # If embedded state yielded a full set matching claim or >= 10 chapters, go straight to audit
        if claimed and len(chapters) >= claimed:
            return "audit"
        if len(chapters) >= 10:
            return "audit"
        return "extract_dom"

    def _route_after_audit(self, state: TocState) -> str:
        if state.get("iteration", 0) >= state.get("max_iterations", 8):
            return "finalize"
        if time.monotonic() >= state.get("deadline", float("inf")) or state.get("stagnant_attempts", 0) >= self.limits.no_progress_attempts:
            return "finalize"
        # If multi-page pagination URLs are pending, crawl them first!
        if state.get("has_pagination") and state.get("pagination_urls"):
            return "crawl_pagination"

        if state.get("is_complete", False):
            return "finalize"

        # Try interactive expand if unexpanded sections are explicitly flagged
        if state.get("has_unexpanded_sections") and not state.get("expanded"):
            return "interactive_expand"

        # Self-heal TOC via synthesizer if incomplete and not attempted yet
        if not state.get("heal_attempted", False):
            claimed = state.get("claimed_chapter_count")
            count = len(state.get("extracted_chapters", []))
            if count == 0 or (claimed and count < claimed):
                return "self_heal_toc"

        iteration = state.get("iteration", 0)
        max_iter = state.get("max_iterations", 3)
        if iteration >= max_iter:
            logger.info(f"Reached max iterations ({max_iter}). Finalizing best-effort.")
            return "finalize"

        # If discrepancy or zero chapters found and iterations remain, try expand
        claimed = state.get("claimed_chapter_count")
        count = len(state.get("extracted_chapters", []))
        if count == 0 or (claimed and count < claimed):
            return "interactive_expand"

        return "finalize"

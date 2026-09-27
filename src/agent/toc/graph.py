"""LangGraph StateGraph workflow for autonomous TOC extraction."""

import logging
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
)
from src.core.obscura_client import ObscuraClient

logger = logging.getLogger("agent.toc.graph")


class TocGraphWorkflow:
    """Encapsulates and compiles the LangGraph state machine for autonomous TOC extraction."""

    def __init__(
        self,
        obscura_client: Optional[ObscuraClient] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ):
        self.obscura = obscura_client or ObscuraClient()
        self.on_log = on_log

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
                "crawl_pagination": "crawl_pagination",
            }
        )

        # After expanding DOM, re-extract from enriched HTML
        workflow.add_edge("interactive_expand", "extract_dom")

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
        # Keep whichever set is larger / more complete
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
        logs = list(state.get("logs", []))
        chapters = state.get("extracted_chapters", [])
        claimed = state.get("claimed_chapter_count")
        # Check if pagination URLs are present
        has_pag = state.get("has_pagination", False) and len(state.get("pagination_urls", [])) > 0

        is_complete, conf, unexpanded, pagination, issues = TocAuditor.audit(
            chapters=chapters,
            claimed_count=claimed,
            html=state["html"],
            check_pagination=has_pag,
        )

        log_msg = f"[Audit] Complete: {is_complete}, Confidence: {conf}, Chapters: {len(chapters)}/{claimed or '?'}, Issues: {issues}"
        self._emit_log(log_msg)
        logs.append(log_msg)

        iteration = state.get("iteration", 0) + 1

        return {
            "is_complete": is_complete,
            "confidence_score": conf,
            "has_unexpanded_sections": unexpanded,
            "issues": issues,
            "iteration": iteration,
            "logs": logs,
        }

    async def _node_interactive_expand(self, state: TocState) -> Dict[str, Any]:
        """Action: uses Playwright to click accordions, click 'load more', and scroll."""
        logs = list(state.get("logs", []))
        log_msg = f"[InteractiveExpand] Running Playwright interaction on {state['url']}..."
        self._emit_log(log_msg)
        logs.append(log_msg)

        try:
            expanded_html = await InteractiveDomExpander.expand(state["url"], self.obscura)
            exp_msg = f"[InteractiveExpand] Successfully expanded DOM (size: {len(expanded_html)} bytes)."
            self._emit_log(exp_msg)
            logs.append(exp_msg)
            return {
                "html": expanded_html,
                "has_unexpanded_sections": False,
                "logs": logs,
            }
        except Exception as e:
            err_msg = f"[InteractiveExpand] Failed: {e}"
            self._emit_log(err_msg, "warning")
            logs.append(err_msg)
            return {"logs": logs}

    async def _node_crawl_pagination(self, state: TocState) -> Dict[str, Any]:
        """Action: Crawls subsequent TOC pages concurrently if multi-page TOC is detected."""
        import asyncio
        import httpx
        logs = list(state.get("logs", []))
        all_chapters = list(state.get("extracted_chapters", []))
        seen_urls = {c.url for c in all_chapters}
        pag_urls = state.get("pagination_urls", [])

        crawl_start_msg = f"[PaginationCrawler] Crawling {len(pag_urls)} pagination pages concurrently (5 workers)..."
        self._emit_log(crawl_start_msg)
        logs.append(crawl_start_msg)
        
        sem = asyncio.Semaphore(5)
        cookies = self.obscura.get_cookies() if hasattr(self.obscura, "get_cookies") else {}
        cached_ua = self.obscura.get_user_agent() if hasattr(self.obscura, "get_user_agent") else None
        headers = {
            "User-Agent": cached_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
            "Referer": state["url"],
        }

        async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=15.0, follow_redirects=True) as http_client:
            async def fetch_page(p_url: str):
                async with sem:
                    # Fast path: direct async HTTP
                    try:
                        resp = await http_client.get(p_url)
                        if resp.status_code == 200 and len(resp.text) > 500:
                            return p_url, resp.text, None
                    except Exception:
                        pass
                    
                    # Fallback: full Obscura browser fetch
                    try:
                        p_html = await self.obscura.fetch_html(p_url, stealth=True, wait_until="domcontentloaded", timeout=15)
                        return p_url, p_html, None
                    except Exception as e:
                        return p_url, None, str(e)

            results = await asyncio.gather(*(fetch_page(u) for u in pag_urls))

        # Process results in strict pagination order to maintain sequential integrity
        for p_url, p_html, err in results:
            if p_html:
                page_chapters = DomLinkExtractor.extract(p_html, p_url)
                added = 0
                for ch in page_chapters:
                    if ch.url not in seen_urls:
                        seen_urls.add(ch.url)
                        all_chapters.append(ch)
                        added += 1
                page_log = f"[PaginationCrawler] {p_url}: found {len(page_chapters)} chapters (+{added} new)."
                logs.append(page_log)
            else:
                fail_log = f"[PaginationCrawler] Failed {p_url}: {err}"
                self._emit_log(fail_log, "warning")
                logs.append(fail_log)

        # Check if chapters are in reverse chronological order (e.g. latest chapter first)
        import re
        if len(all_chapters) >= 2:
            m_first = re.search(r"(\d+)", all_chapters[0].title) or re.search(r"/(\d+)/?$", all_chapters[0].url)
            m_last = re.search(r"(\d+)", all_chapters[-1].title) or re.search(r"/(\d+)/?$", all_chapters[-1].url)
            if m_first and m_last:
                if int(m_first.group(1)) > int(m_last.group(1)):
                    inv_msg = "[PaginationCrawler] Detected reverse chronological order. Inverting to reading order..."
                    self._emit_log(inv_msg)
                    logs.append(inv_msg)
                    all_chapters.reverse()

        # Re-index chapters monotonically 1..N
        for idx, ch in enumerate(all_chapters, start=1):
            ch.index = idx

        # Update claimed chapter count if pagination discovery revealed full catalog
        claimed = state.get("claimed_chapter_count")
        if not claimed or len(all_chapters) > claimed:
            claimed = len(all_chapters)

        tot_msg = f"[PaginationCrawler] Total chapters after pagination: {len(all_chapters)}"
        self._emit_log(tot_msg)
        logs.append(tot_msg)
        return {
            "extracted_chapters": all_chapters,
            "claimed_chapter_count": claimed,
            "has_pagination": False,
            "pagination_urls": [],
            "extraction_strategy": "paginated_dom",
            "logs": logs,
        }

    async def _node_finalize(self, state: TocState) -> Dict[str, Any]:
        """Finalize chapter list with deduplication, sequence re-indexing, and clean titles."""
        logs = list(state.get("logs", []))
        raw_chapters = state.get("extracted_chapters", [])
        
        seen = set()
        clean_list = []
        for ch in raw_chapters:
            if ch.url not in seen:
                seen.add(ch.url)
                clean_list.append(ch)

        # Sort and re-index 1..N
        for idx, ch in enumerate(clean_list, 1):
            ch.index = idx

        log_msg = f"[Finalize] Outputting {len(clean_list)} clean chapters (Strategy: {state.get('extraction_strategy')}, Confidence: {state.get('confidence_score')})."
        self._emit_log(log_msg)
        logs.append(log_msg)

        return {
            "extracted_chapters": clean_list,
            "is_complete": True,
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
        # If multi-page pagination URLs are pending, crawl them first!
        if state.get("has_pagination") and state.get("pagination_urls"):
            return "crawl_pagination"

        if state.get("is_complete", False):
            return "finalize"

        iteration = state.get("iteration", 0)
        max_iter = state.get("max_iterations", 3)
        if iteration >= max_iter:
            logger.info(f"Reached max iterations ({max_iter}). Finalizing best-effort.")
            return "finalize"

        # If discrepancy, unexpanded sections, or zero chapters found on first try
        claimed = state.get("claimed_chapter_count")
        count = len(state.get("extracted_chapters", []))
        if (claimed and count < claimed) or state.get("has_unexpanded_sections") or (count == 0 and iteration < max_iter):
            return "interactive_expand"

        return "finalize"

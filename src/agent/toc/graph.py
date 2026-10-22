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
from src.scraper.telemetry import stage_enabled
from src.agent.toc.discovery import (
    expand_catalog, network_chapters, network_pagination_requests,
    infer_api_response_mapping, capture_observed_api_recipe,
)
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
from src.core.page_analysis import PageAnalysis
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
        self.page_analysis = None

    def _analysis(self, html):
        if self.page_analysis is None or self.page_analysis.html != html:
            self.page_analysis = PageAnalysis(html)
        return self.page_analysis

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
        analysis = self._analysis(state["html"])
        claimed, title, author, desc = ClaimInspector.inspect(state["html"], state["url"], page_analysis=analysis)
        logs = list(state.get("logs", []))
        log_msg = f"[Inspect] Novel Title: '{title}', Claimed Chapters: {claimed}"
        self._emit_log(log_msg)
        logs.append(log_msg)

        # Also detect pagination links
        pag_urls = PaginatedTocCrawler.detect_pagination_urls(state["html"], state["url"], page_analysis=analysis)

        return {
            "novel_title": title,
            "author": author or state.get("author"),
            "description": desc or state.get("description"),
            "claimed_chapter_count": claimed,
            "has_pagination": len(pag_urls) > 0,
            "pagination_urls": pag_urls,
            "pagination_requests": list(state.get("pagination_requests", [])),
            "logs": logs,
        }

    async def _node_extract_embedded_state(self, state: TocState) -> Dict[str, Any]:
        """Extract chapters from Apollo, Redux, Nuxt, or JSON-LD if present."""
        logs = list(state.get("logs", []))
        chapters = EmbeddedStateExtractor.extract(state["html"], state["url"], page_analysis=self._analysis(state["html"]))
        
        if chapters:
            log_msg = f"[EmbeddedState] Found {len(chapters)} chapters via embedded state!"
            self._emit_log(log_msg)
            logs.append(log_msg)
            return {
                "extracted_chapters": chapters,
                "extraction_strategy": "embedded_state",
                "evidence_observations": list(state.get("evidence_observations", [])) + [
                    {"source": "embedded_state", "url": canonical_url(c.url), "label": c.title,
                     "index": c.index, "volume": c.volume, "catalog_url": canonical_url(state["url"])}
                    for c in chapters
                ],
                "logs": logs,
            }
        
        no_emb = "[EmbeddedState] No embedded state chapters found. Proceeding to DOM..."
        self._emit_log(no_emb)
        logs.append(no_emb)
        return {"logs": logs}

    async def _node_extract_dom(self, state: TocState) -> Dict[str, Any]:
        """Extract chapter links from the DOM."""
        logs = list(state.get("logs", []))
        dom_chapters = DomLinkExtractor.extract(state["html"], state["url"], page_analysis=self._analysis(state["html"]))
        dom_observations = [
            {"source": "dom", "url": canonical_url(c.url), "label": c.title,
             "index": c.index, "volume": c.volume, "catalog_url": canonical_url(state["url"])}
            for c in dom_chapters
        ]
        
        current_chapters = state.get("extracted_chapters", [])
        merged = {canonical_url(c.url): c for c in current_chapters}
        merged.update({canonical_url(c.url): c for c in dom_chapters})
        dom_chapters = list(merged.values())
        # Keep both independent observations even when their cardinalities match.
        # Equal counts do not establish that they refer to the same chapter URLs.
        observations = list(state.get("evidence_observations", [])) + dom_observations
        if len(dom_chapters) != len(current_chapters) or any(
            canonical_url(c.url) not in {canonical_url(old.url) for old in current_chapters}
            for c in dom_chapters
        ):
            log_msg = f"[DomExtractor] Reconciled {len(dom_chapters)} DOM chapters with {len(current_chapters)} existing chapters."
            self._emit_log(log_msg)
            logs.append(log_msg)
            return {
                "extracted_chapters": dom_chapters,
                "extraction_strategy": "reconciled_evidence",
                "evidence_observations": observations,
                "logs": logs,
            }

        dom_msg = f"[DomExtractor] DOM yielded {len(dom_chapters)} chapters (kept {len(current_chapters)})."
        self._emit_log(dom_msg)
        logs.append(dom_msg)
        return {"evidence_observations": observations, "logs": logs}

    async def _node_audit(self, state: TocState) -> Dict[str, Any]:
        """Auditor / Critic: evaluates whether extraction is complete or truncated."""
        self.last_state = state.copy()
        self.obscura.last_toc_state = state.copy()
        logs = list(state.get("logs", []))
        chapters = [chapter for chapter in state.get("extracted_chapters", []) if belongs_to_observed_novel(state["url"], chapter.url)]
        claimed = state.get("claimed_chapter_count")
        # Check if pagination URLs are present
        pending_frontier = list(state.get("pagination_urls", []))
        pending_requests = list(state.get("pagination_requests", []))
        has_pag = bool(state.get("has_pagination", False) or pending_frontier or pending_requests)

        is_complete, conf, unexpanded, pagination, issues = TocAuditor.audit(
            chapters=chapters,
            claimed_count=claimed,
            html=state["html"],
            check_pagination=has_pag,
            page_analysis=self._analysis(state["html"]),
            check_expansion=not (state.get("expanded") and not state.get("expansion_incomplete")),
        )
        if state.get("expanded"):
            if not claimed and chapters and not has_pag and not unexpanded:
                is_complete = True
                conf = 0.8
        if state.get("crawl_issues"):
            is_complete = False
            issues.extend(state["crawl_issues"])
        source_sets = {}
        for observation in state.get("evidence_observations", []):
            if observation.get("kind", "chapter") != "chapter":
                continue
            page = observation.get("catalog_url", canonical_url(state["url"]))
            source_sets.setdefault((page, observation.get("source")), set()).add(observation.get("url"))
        by_page = {}
        for (page, _source), urls in source_sets.items():
            by_page.setdefault(page, []).append(urls)
        conflicting_pages = [sets for sets in by_page.values()
                             if len(sets) > 1 and any(
                                 not (left <= right or right <= left)
                                 for index, left in enumerate(sets)
                                 for right in sets[index + 1:]
                             )]
        if conflicting_pages:
            is_complete = False
            issues.append("Conflicting chapter URLs were observed across TOC evidence sources.")
        if pending_frontier or pending_requests:
            is_complete = False
            issues.append(f"Unvisited catalog pages remain: {len(pending_frontier) + len(pending_requests)}")
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
            # Carry the frontier explicitly through every audit update. A
            # completed node may not erase failed or budget-deferred requests.
            "pagination_urls": pending_frontier,
            "pagination_requests": pending_requests,
            "has_pagination": bool(state.get("has_pagination") or pending_frontier or pending_requests),
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
            expansion = await expand_catalog(
                state["url"], self.obscura,
                lambda html, url: DomLinkExtractor.extract(html, url, page_analysis=self._analysis(html)),
                self.llm,
            )
            expanded_html, observed, next_urls = expansion
            merged = {canonical_url(c.url): c for c in state.get("extracted_chapters", [])}
            merged.update({canonical_url(c.url): c for c in observed})
            observations = list(state.get("evidence_observations", [])) + [
                {"source": "api_chapter_response" if canonical_url(c.url) in set(expansion.api_chapter_urls) else "interactive_dom",
                 "url": canonical_url(c.url), "label": c.title,
                 "index": c.index, "volume": c.volume, "catalog_url": canonical_url(state["url"])}
                for c in observed
            ]
            exp_msg = f"[InteractiveExpand] Successfully expanded DOM (size: {len(expanded_html)} bytes)."
            self._emit_log(exp_msg)
            logs.append(exp_msg)
            return {
                "html": expanded_html,
                "has_unexpanded_sections": False,
                "extracted_chapters": list(merged.values()),
                "pagination_urls": list(dict.fromkeys(list(state.get("pagination_urls", [])) + next_urls)),
                "pagination_requests": list(state.get("pagination_requests", [])) + [request for request in expansion.pagination_requests
                    if request.get("method") != "GET" or request.get("url") not in next_urls],
                "api_recipe_candidates": list(state.get("api_recipe_candidates", [])) + [
                    recipe.model_dump() for recipe in expansion.api_recipe_candidates],
                "has_pagination": bool(state.get("pagination_urls") or next_urls or state.get("pagination_requests") or expansion.pagination_requests),
                "expanded": True,
                "expansion_incomplete": not expansion.exhausted,
                "pending_catalog_controls": expansion.pending_controls,
                "crawl_issues": list(state.get("crawl_issues", [])) + expansion.issues,
                "evidence_observations": observations,
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
            state["html"], state["url"], state.get("claimed_chapter_count"),
            page_analysis=self._analysis(state["html"]),
        )
        current = state.get("extracted_chapters", [])
        if len(chapters) > len(current):
            merged = {canonical_url(c.url): c for c in current}
            merged.update({canonical_url(c.url): c for c in chapters})
            heal_msg = f"[SelfHealTOC] Synthesizer recovered {len(chapters)} chapters (container='{c_sel}', link='{l_sel}')."
            self._emit_log(heal_msg)
            logs.append(heal_msg)
            return {
                "extracted_chapters": list(merged.values()),
                "custom_container_selector": c_sel,
                "custom_link_selector": l_sel,
                "extraction_strategy": "self_healed_dom",
                "heal_attempted": True,
                "evidence_observations": list(state.get("evidence_observations", [])) + [
                    {"source": "synthesized_dom", "url": canonical_url(c.url), "label": c.title,
                     "index": c.index, "volume": c.volume, "catalog_url": canonical_url(state["url"])}
                    for c in chapters
                ],
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
        observations = list(state.get("evidence_observations", []))
        frontier = list(state.get("pagination_urls", []))
        request_frontier = list(state.get("pagination_requests", []))
        api_candidates = list(state.get("api_recipe_candidates", []))
        visited = {canonical_url(state["url"])}
        visited_requests = set()
        issues = []
        pages = 1
        async def browser_fallback(request):
            source_url = request.get("source_page_url") or state["url"]
            rendered = await self.obscura.fetch_html(source_url, force_browser=True)
            if current_scope():
                await current_scope().observe_catalog(source_url, rendered, self.obscura)
            analysis = self._analysis(rendered)
            found = (EmbeddedStateExtractor.extract(rendered, source_url, page_analysis=analysis) +
                     DomLinkExtractor.extract(rendered, source_url, page_analysis=analysis))
            next_urls = PaginatedTocCrawler.detect_pagination_urls(rendered, source_url, page_analysis=analysis)
            return source_url, found, next_urls
        while (frontier or request_frontier) and pages < self.limits.toc_pages and time.monotonic() < state.get("deadline", float("inf")):
            batch_size = min(3, self.limits.toc_pages - pages)
            batch, frontier = frontier[:batch_size], frontier[batch_size:]
            request_batch, request_frontier = request_frontier[:max(0, batch_size - len(batch))], request_frontier[max(0, batch_size - len(batch)):]
            async def fetch(url):
                try:
                    html = await self.obscura.fetch_html(url, wait_until="domcontentloaded")
                    return "url", url, html, None
                except Exception as error:
                    return "url", url, "", str(error)
            async def fetch_request(request):
                try:
                    data = await self.obscura.request_api_json(request["method"], request["url"],
                        headers=request.get("headers"), json_body=request.get("body"),
                        trusted_read_only=request.get("trusted_read_only", False))
                    return "request", request, data, None
                except Exception as error:
                    return "request", request, None, str(error)
            batch = [u for u in batch if canonical_url(u) not in visited and urlsplit(u).netloc == urlsplit(state["url"]).netloc]
            visited.update(canonical_url(u) for u in batch)
            valid_requests = []
            for request in request_batch:
                marker = json.dumps([request.get("method"), request.get("url"), request.get("body")],
                                    sort_keys=True, separators=(",", ":"))
                if marker not in visited_requests and urlsplit(request.get("url", "")).netloc == urlsplit(state["url"]).netloc:
                    visited_requests.add(marker)
                    valid_requests.append(request)
            pages += len(batch) + len(valid_requests)
            if not batch and not valid_requests:
                continue
            failed_this_batch = False
            pending_fetches = [fetch(u) for u in batch] + [fetch_request(request) for request in valid_requests]
            for kind, item, payload, error in await asyncio.gather(*pending_fetches):
                request = item if kind == "request" else None
                page_url = request.get("url") if request else item
                if request:
                    try:
                        html = json.dumps(payload, ensure_ascii=False)
                    except (TypeError, ValueError):
                        html = ""
                else:
                    html = payload
                if error:
                    if request:
                        try:
                            source_url, fallback_chapters, fallback_urls = await browser_fallback(request)
                            if fallback_chapters:
                                for chapter in fallback_chapters:
                                    all_chapters.setdefault(canonical_url(chapter.url), chapter)
                                    observations.append({"kind": "chapter", "source": "pagination_rendered_dom",
                                        "url": canonical_url(chapter.url), "label": chapter.title,
                                        "index": chapter.index, "volume": chapter.volume,
                                        "catalog_url": canonical_url(source_url)})
                                frontier.extend(u for u in fallback_urls if canonical_url(u) not in visited and u not in frontier)
                                request_frontier.append(request)
                                issues.append(f"API catalog request remains unverified after browser fallback: {page_url}")
                                failed_this_batch = True
                                continue
                        except Exception as fallback_error:
                            error = f"{error}; browser fallback failed: {fallback_error}"
                        request_frontier.append(request)
                    else:
                        frontier.append(page_url)
                    issues.append(f"Unresolved catalog page {page_url}: {error}")
                    failed_this_batch = True
                    continue
                if current_scope() and not request:
                    try:
                        await current_scope().observe_catalog(page_url, html, self.obscura)
                    except Exception as error:
                        issues.append(f"Unable to verify catalog identity for {page_url}: {error}")
                evidence_page_url = (request.get("source_page_url") or state["url"]) if request else page_url
                try:
                    data = payload if request else json.loads(html)
                    response_observation = {"url": page_url, "data": data}
                    if request:
                        response_observation.update({"method": request.get("method"),
                            "post_data": request.get("body"), "headers": request.get("headers"),
                            "source_page_url": request.get("source_page_url"),
                            "trusted_read_only": request.get("trusted_read_only", False)})
                    chapters, next_urls = network_chapters([response_observation], evidence_page_url)
                    next_requests = network_pagination_requests([response_observation])
                    page_source = "pagination_api"
                except (ValueError, TypeError):
                    analysis = self._analysis(html)
                    chapters = (EmbeddedStateExtractor.extract(html, page_url, page_analysis=analysis) +
                                DomLinkExtractor.extract(html, page_url, page_analysis=analysis))
                    next_urls = PaginatedTocCrawler.detect_pagination_urls(html, page_url, page_analysis=analysis)
                    next_requests = []
                    page_source = "pagination_dom_or_embedded"
                    if not chapters:
                        try:
                            rendered = await self.obscura.fetch_html(page_url, force_browser=True)
                            if current_scope():
                                try:
                                    await current_scope().observe_catalog(page_url, rendered, self.obscura)
                                except Exception as error:
                                    issues.append(f"Unable to verify rendered catalog identity for {page_url}: {error}")
                            rendered_analysis = self._analysis(rendered)
                            chapters = (EmbeddedStateExtractor.extract(rendered, page_url, page_analysis=rendered_analysis) +
                                        DomLinkExtractor.extract(rendered, page_url, page_analysis=rendered_analysis))
                            page_source = "pagination_rendered_dom"
                            next_urls += PaginatedTocCrawler.detect_pagination_urls(
                                rendered, page_url, page_analysis=rendered_analysis)
                        except Exception as error:
                            issues.append(f"Unable to render catalog page {page_url}: {error}")
                if not chapters:
                    if request:
                        try:
                            source_url, fallback_chapters, fallback_urls = await browser_fallback(request)
                            if fallback_chapters:
                                for chapter in fallback_chapters:
                                    all_chapters.setdefault(canonical_url(chapter.url), chapter)
                                    observations.append({"kind": "chapter", "source": "pagination_rendered_dom",
                                        "url": canonical_url(chapter.url), "label": chapter.title,
                                        "index": chapter.index, "volume": chapter.volume,
                                        "catalog_url": canonical_url(source_url)})
                                frontier.extend(u for u in fallback_urls if canonical_url(u) not in visited and u not in frontier)
                                request_frontier.append(request)
                                issues.append(f"API catalog mapping remains unverified after browser fallback: {page_url}")
                                failed_this_batch = True
                                continue
                        except Exception as fallback_error:
                            issues.append(f"Browser fallback failed for {request.get('source_page_url')}: {fallback_error}")
                        request_frontier.append(request)
                    else:
                        issues.append(f"No chapter evidence on catalog page {page_url}")
                        frontier.append(page_url)
                    failed_this_batch = True
                    continue
                if request and current_scope():
                    from src.scraper.runtime import belongs_to_observed_novel
                    current_scope().catalogs.add(canonical_url(page_url))
                    current_scope().catalogs.add(canonical_url(request.get("source_page_url") or state["url"]))
                    current_scope().approved_links.update(canonical_url(c.url) for c in chapters
                        if belongs_to_observed_novel(state["url"], c.url))
                mapping_info = infer_api_response_mapping(data) if page_source == "pagination_api" else None
                if mapping_info:
                    recipe = capture_observed_api_recipe(method=request.get("method", "GET") if request else "GET",
                        url=page_url, headers=request.get("headers") if request else {},
                        request_body=request.get("body") if request else None,
                        response_body=data, page_url=request.get("source_page_url", state["url"]) if request else state["url"],
                        mapping=mapping_info["mapping"], representation=mapping_info["representation"],
                        handler=__import__("src.handlers", fromlist=["registry"]).registry.find_handler(state["url"]))
                    if recipe:
                        api_candidates.append(recipe.model_dump())
                for chapter in chapters:
                    all_chapters.setdefault(canonical_url(chapter.url), chapter)
                    observations.append({
                        "kind": "chapter", "source": page_source,
                        "url": canonical_url(chapter.url), "label": chapter.title,
                        "index": chapter.index, "volume": chapter.volume,
                        "catalog_url": canonical_url(evidence_page_url),
                        **({"endpoint": page_url} if request else {}),
                    })
                observations.extend({"kind": "navigation", "source": page_source,
                                     "url": canonical_url(next_url), "label": "pagination",
                                     "catalog_url": canonical_url(evidence_page_url)}
                                    for next_url in next_urls)
                frontier.extend(u for u in next_urls if canonical_url(u) not in visited and u not in frontier)
                request_frontier.extend(request for request in next_requests
                    if json.dumps([request.get("method"), request.get("url"), request.get("body")],
                                  sort_keys=True, separators=(",", ":")) not in visited_requests
                    and (request.get("method") != "GET" or request.get("url") not in next_urls)
                    and request not in request_frontier)
            # Do not retry failed URLs in this run, but keep them in the returned
            # frontier so audit/finalization can report the unresolved coverage.
            if failed_this_batch:
                break
        if frontier or request_frontier:
            issues.append("Catalog frontier exceeds discovery budget")
        ordered, _ = detect_and_fix_reverse_order(list(all_chapters.values()))
        for index, chapter in enumerate(ordered, 1):
            chapter.index = index
            chapter.title = clean_chapter_title(chapter.title)
        return {
            "extracted_chapters": ordered,
            "has_pagination": bool(frontier or request_frontier),
            "pagination_urls": list(dict.fromkeys(frontier)),
            "pagination_requests": request_frontier,
            "api_recipe_candidates": api_candidates,
            "extraction_strategy": "paginated_dom",
            "crawl_issues": issues,
            "evidence_observations": observations,
            "completeness_evidence": list(state.get("completeness_evidence", [])) + ([f"Visited {pages} catalog pages"] if not issues else []),
            "logs": state.get("logs", []) + issues,
        }

    async def _node_finalize(self, state: TocState) -> Dict[str, Any]:
        """Finalize chapter list with deduplication, reverse order fix, sequence re-indexing, and clean titles."""
        logs = list(state.get("logs", []))
        raw_chapters = state.get("extracted_chapters", [])
        soup = self._analysis(state.get("html", "")).soup
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
            if canonical_url(ch.url) not in seen:
                seen.add(canonical_url(ch.url))
                clean_list.append(ch)
                ch.volume = ch.volume or volumes.get(canonical_url(ch.url))

        from src.utils.title_cleaner import chapter_number_bounds
        labels = [chapter_number_bounds(ch.title) for ch in clean_list]
        if clean_list and all(labels) and not any(ch.volume for ch in clean_list):
            if len({bounds[0] for bounds in labels}) == len(labels):
                clean_list = [ch for _, ch in sorted(zip(labels, clean_list), key=lambda pair: pair[0][0])]

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
            "is_complete": bool(clean_list) and state.get("is_complete", False)
                and not state.get("pagination_urls") and not state.get("pagination_requests")
                and not state.get("crawl_issues"),
            "pagination_urls": list(dict.fromkeys(state.get("pagination_urls", []))),
            "pagination_requests": list(state.get("pagination_requests", [])),
            "api_recipe_candidates": list(state.get("api_recipe_candidates", [])),
            "has_pagination": bool(state.get("pagination_urls") or state.get("pagination_requests") or state.get("has_pagination")),
            "completeness_evidence": state.get("completeness_evidence", []) + (["Advertised count covered" if state.get("claimed_chapter_count") else "Observed catalog navigation exhausted"] if state.get("is_complete") and clean_list else []),
            "logs": logs,
        }

    # --- Routing Conditions ---

    def _route_after_embedded(self, state: TocState) -> str:
        # Embedded state is an observation, never a reason to skip the DOM cross-check.
        return "extract_dom"

    def _route_after_audit(self, state: TocState) -> str:
        if state.get("iteration", 0) >= state.get("max_iterations", 8):
            return "finalize"
        if time.monotonic() >= state.get("deadline", float("inf")):
            return "finalize"
        if state.get("crawl_issues"):
            return "finalize"
        # If multi-page pagination URLs are pending, crawl them first!
        if state.get("has_pagination") and (state.get("pagination_urls") or state.get("pagination_requests")):
            return "crawl_pagination"
        if state.get("stagnant_attempts", 0) >= self.limits.no_progress_attempts:
            return "finalize"

        if state.get("is_complete", False):
            return "finalize"

        if not stage_enabled("browser"):
            return "self_heal_toc" if not state.get("heal_attempted") else "finalize"

        # Try interactive expand if unexpanded sections are explicitly flagged
        if state.get("has_unexpanded_sections") and not state.get("expanded"):
            return "interactive_expand"

        if not state.get("expanded") and any(
            "Conflicting chapter URLs" in issue for issue in state.get("issues", [])
        ):
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

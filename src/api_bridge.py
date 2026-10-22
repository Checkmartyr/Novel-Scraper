"""JSON-lines worker contract for automatic discovery and resumable extraction."""
import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from src.core.obscura_client import ObscuraClient
from src.core.page_analysis import PageAnalysis, PageAnalysisCache, call_with_page_analysis
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
from src.scraper.recovery import before_deadline, recovery_scope, current_scope, operation_scope
from src.scraper.telemetry import traced_job, stage, stage_enabled, recovery_ceiling
from src.scraper.telemetry import mark_validated

logger = logging.getLogger("api_bridge")


def _obscura_client(state_dir=None):
    """Construct retrieval clients while keeping legacy zero-argument test/custom factories valid."""
    if state_dir is None:
        return ObscuraClient()
    import inspect
    try:
        parameters = inspect.signature(ObscuraClient).parameters.values()
        accepts_state = any(item.name == "state_dir" or item.kind == inspect.Parameter.VAR_KEYWORD
                            for item in parameters)
    except (TypeError, ValueError):
        accepts_state = True
    return ObscuraClient(state_dir=state_dir) if accepts_state else ObscuraClient()


def _confirmed_chapter(html, url, *, page_analysis=None):
    from src.agent.ch.candidates import rank_candidates
    from src.agent.ch.code_generator import extract_paragraphs
    page_analysis = page_analysis or PageAnalysis(html)
    soup = page_analysis.soup
    heading = soup.select_one(".reader-header-chapter, .chapter-title, .episode-title, .p-novel__title, .widget-episodeTitle")
    heading = heading or next((node for node in soup.select("h1, h2") if re.match(
        r"\s*(?:chapter|episode|prologue|epilogue)\b|\s*第.+[話章]|\s*ตอน", node.get_text(" ", strip=True), re.I)), None)
    route = re.search(r"/(?:chapter[-/]|episodes/|read/.+/chapter-|n[a-z0-9]+/\d+/)", url, re.I)
    candidates = rank_candidates(html, limit=1, page_analysis=page_analysis)
    if not candidates or not (heading or route):
        return False
    candidate = candidates[0]
    region = page_analysis.select_one(candidate.selector)
    return candidate.signature["link_density"] < 0.2 and bool(extract_paragraphs(region))


def _scope_chapters(url, chapters):
    from src.agent.domain_memory import domain_memory
    recipe = domain_memory.get_recipe(url)
    if recipe:
        chapters = [chapter for chapter in chapters if belongs_to_recipe_novel(
            url, chapter.url, recipe.sample_toc_url, recipe.sample_chapter_url) is not False or
            (current_scope() and current_scope().allows(url, chapter.url))]
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
    scope = getattr(obscura, "novel_scope", None)
    chapters = list({canonical_url(chapter.url): chapter for chapter in state.get("extracted_chapters", [])
                     if belongs_to_observed_novel(url, chapter.url) or (scope and scope.allows(url, chapter.url))}.values())
    return {"url": url, "success": bool(chapters), "novel_title": state.get("novel_title") or "Unknown Novel",
            "chapters": [chapter.model_dump() for chapter in chapters], "total_chapters": len(chapters),
            "linked_entry_count": len(chapters), "source_scope": scope.export() if scope else {},
            "claimed_chapter_count": state.get("claimed_chapter_count"),
            "pending_catalog_controls": state.get("pending_catalog_controls", []),
            "is_complete": False, "issues": state.get("issues", []) + ["Discovery deadline exhausted"],
            "completeness_evidence": state.get("completeness_evidence", [])}


def incomplete_catalog_result(snapshot, files=()):
    """A shared gate for batch entrypoints; discovery evidence remains resumable."""
    return {"success": False, "status": "paused", "is_complete": False,
            "completed": len(files), "files": list(files), "saved": 0, "failed": 0, "blocked": 1,
            "total_selected": 0, "total_chapters": len(snapshot.get("chapters", snapshot.get("extracted_chapters", []))),
            "claimed_chapter_count": snapshot.get("claimed_chapter_count"),
            "pending_catalog_controls": snapshot.get("pending_catalog_controls", []),
            "issues": list(snapshot.get("issues", [])) or [snapshot.get("error") or "Catalog coverage is unresolved"],
            "completeness_evidence": snapshot.get("completeness_evidence", []),
            "error": "Full catalog verification is required before chapter extraction"}


async def discover(url, obscura, llm):
    previous_deadline = getattr(obscura, "discovery_deadline", None)
    try:
        with recovery_scope(url, obscura) as scope:
            deadline = previous_deadline or time.monotonic() + ScraperLimits.from_env().discovery_seconds
            obscura.discovery_deadline = scope.deadline = min(scope.deadline, deadline)
            with stage("catalog_audit") as measurement:
                result = await before_deadline(_discover(url, obscura, llm), scope.deadline)
                mark_validated(measurement, ("catalog:" + chapter["url"] for chapter in result.get("chapters", [])))
                measurement.update(outcome="complete" if result.get("is_complete") else "incomplete",
                                   reason="; ".join(result.get("issues", [])) or "Observed catalog coverage resolved")
                return result
    finally:
        if previous_deadline is None:
            obscura.__dict__.pop("discovery_deadline", None)
        else:
            obscura.discovery_deadline = previous_deadline


async def _discover(url, obscura, llm):
    limits = ScraperLimits.from_env()
    callback = getattr(obscura, "on_progress", None)
    def log_discovery(message, level="info"):
        if callback:
            callback({"type": "progress", "stage": "discovery", "message": message, "current": 0, "total": 0})
    log_discovery("Retrieving catalog evidence")
    toc = TocAgent(obscura_client=obscura, llm_client=llm, on_log=log_discovery)
    classifier = PageClassifier(llm_client=llm, obscura_client=obscura, toc_agent=toc)
    requested_url = url
    html = await obscura.fetch_html(url)
    record = getattr(obscura, "fetch_records", {}).get(url)
    if record is not None:
        url = record.final_url
    page_analyses = PageAnalysisCache(max_snapshots=12)
    page_analysis = page_analyses.get(canonical_url(url), html)
    scope = current_scope()
    await scope.observe_catalog(url, html, obscura)
    with recovery_ceiling("deterministic"):
        classification = await call_with_page_analysis(
            classifier.classify, url, html, page_analysis=page_analysis)
    if classification.page_type == "CHAPTER" and not _confirmed_chapter(html, url, page_analysis=page_analysis) and not classification.toc_url:
        if stage_enabled("browser"):
            try:
                html = await obscura.fetch_html(url, force_browser=True)
                page_analysis = page_analyses.get(canonical_url(url), html)
                await scope.observe_catalog(url, html, obscura)
                with recovery_ceiling("deterministic"):
                    classification = await call_with_page_analysis(
                        classifier.classify, url, html, page_analysis=page_analysis)
            except Exception as error:
                logger.info("Uncertain page rendering failed: %s", safe_error(error))
        if classification.page_type == "CHAPTER" and not _confirmed_chapter(html, url, page_analysis=page_analysis) and stage_enabled("model"):
            classification = await call_with_page_analysis(
                classifier.classify, url, html, page_analysis=page_analysis)
    state = classification.toc_state or {}
    catalog_url = url if classification.page_type == "TOC" else None
    if classification.page_type == "TOC":
        if not state:
            state = await call_with_page_analysis(
                toc.extract_toc, url, html=html, page_analysis=page_analysis)
        count = len(state.get("extracted_chapters", []))
        if stage_enabled("browser") and (not count or state.get("has_unexpanded_sections") or
                (state.get("claimed_chapter_count") and count < state["claimed_chapter_count"])):
            # Expansion already opens a browser. Avoid a second navigation when
            # the HTTP snapshot witnesses the controls that need interaction.
            if state.get("has_unexpanded_sections"):
                state = await call_with_page_analysis(toc.extract_toc, url, html=html, page_analysis=page_analysis)
            else:
                rendered = await obscura.fetch_html(url, force_browser=True, readiness="catalog", wait_until="domcontentloaded")
                page_analysis = page_analyses.get(canonical_url(url), rendered)
                await scope.observe_catalog(url, rendered, obscura)
                state = await call_with_page_analysis(
                    toc.extract_toc, url, html=rendered, page_analysis=page_analysis)
    else:
        navigation = observed_navigation(html, url)
        witnessed = {canonical_url(urljoin(url, anchor["href"])) for anchor in page_analysis.select("a[href]")}
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
            before, after, seen = [], [], {canonical_url(url)}
            issues = [] if _confirmed_chapter(html, url) else ["Page identity remains ambiguous; chapter or catalog evidence is unresolved"]
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
                    target_analysis = page_analyses.get(canonical_url(target), page_html)
                    if not _confirmed_chapter(page_html, target, page_analysis=target_analysis):
                        issues.append("Unconfirmed chapter identity in observed navigation")
                        break
                    page_url = target
                    soup = target_analysis.soup
                    heading = soup.select_one("h1, h2")
                    destination.append(ChapterLink(index=1, title=heading.get_text(" ", strip=True) if heading else "Chapter", url=target))
                else:
                    issues.append("Chapter navigation budget exhausted")
            state = {"extracted_chapters": list(reversed(before)) + ([current] if _confirmed_chapter(html, url) else []) + after,
                     "is_complete": not issues, "issues": issues,
                     "completeness_evidence": ["Observed previous/next chapter chain exhausted"] if not issues else [],
                     "extraction_strategy": "chapter_navigation", "novel_title": classification.novel_title}
    chapters = _scope_chapters(url, state.get("extracted_chapters", classification.chapter_links))
    issues = list(state.get("issues", []))
    claimed_count = state.get("claimed_chapter_count")
    coverage_complete = bool(chapters) and not (
        classification.page_type == "TOC" and claimed_count and len(chapters) < claimed_count
    )
    if classification.page_type == "TOC":
        if not chapters:
            issues.append("No in-scope chapter links were found in the catalog.")
        elif claimed_count and len(chapters) < claimed_count:
            issue = f"Chapter count discrepancy after scope validation: extracted {len(chapters)} of {claimed_count}."
            if issue not in issues:
                issues.append(issue)
        if not coverage_complete:
            state["is_complete"] = False
    if scope.unresolved:
        state["is_complete"] = False
        issues.append(f"Unresolved cross-domain catalog coverage: {len(scope.unresolved)} observed links.")
        issues.extend(f"Unverified chapter source: {cause}" for cause in dict.fromkeys(scope.unresolved.values()))
    from src.utils.title_cleaner import chapter_number_bounds
    ranges = [{"url": chapter.url, "start": bounds[0], "end": bounds[1]}
              for chapter in chapters if (bounds := chapter_number_bounds(chapter.title))]
    observed_bounds = [bounds for title in scope.observed_links.values() if (bounds := chapter_number_bounds(title))]
    return {
        "success": bool(chapters), "url": requested_url, "final_url": url, "page_type": classification.page_type,
        "novel_title": state.get("novel_title") or classification.novel_title or "Unknown Novel",
        "author": state.get("author") or classification.author,
        "description": state.get("description") or classification.description,
        "total_chapters": len(chapters),
        "linked_entry_count": len(chapters),
        "observed_linked_entry_count": len(scope.observed_links),
        "highest_observed_chapter_number": max((bounds[1] for bounds in observed_bounds), default=None),
        "highest_chapter_number": max((bounds["end"] for bounds in ranges), default=None),
        "numbered_chapter_ranges": ranges,
        "source_scope": scope.export(),
        "chapters": [chapter.model_dump() for chapter in chapters],
        "is_complete": coverage_complete and bool(state.get("is_complete")),
        "claimed_chapter_count": claimed_count,
        "confidence_score": state.get("confidence_score", 0),
        "completeness_evidence": state.get("completeness_evidence", []),
        "api_recipe_candidates": state.get("api_recipe_candidates", []),
        "pending_catalog_controls": state.get("pending_catalog_controls", []),
        "issues": issues, "extraction_strategy": state.get("extraction_strategy"),
        "toc_url": catalog_url,
    }


@traced_job
async def inspect_novel_url(url: str, *, refresh: bool = False, state_dir=None, on_progress=None):
    obscura = _obscura_client(Path(state_dir) if state_dir else None)
    obscura.refresh_mode = refresh
    obscura.on_progress = on_progress
    llm = LLMClient()
    try:
        start = time.monotonic()
        try:
            result = await discover(url, obscura, llm)
        except asyncio.TimeoutError:
            result = partial_discovery(url, obscura)
        result["metrics"] = {**obscura.metrics, "discovery_seconds": time.monotonic() - start,
                             "model_calls": getattr(llm, "call_count", 0)}
        if on_progress:
            on_progress({"type": "progress", "stage": "discovery", "current": result.get("total_chapters", 0),
                "total": result.get("claimed_chapter_count") or 0,
                "message": "Catalog verified" if result.get("is_complete") else "Catalog incomplete: " + "; ".join(result.get("issues", []))})
        return result
    except Exception as error:
        if on_progress:
            on_progress({"type": "progress", "stage": "discovery", "message": "Discovery failed: " + safe_error(error), "current": 0, "total": 0})
        return {"success": False, "url": url, "error": safe_error(error), "total_chapters": 0, "chapters": [], "is_complete": False}
    finally:
        await obscura.close()


@traced_job
async def extract_novel_chapters(url, output_dir, chapter_indices=None, start_chapter=None, end_chapter=None,
                                 concurrency=3, include_frontmatter=False, on_progress=None, overwrite=False,
                                 state_dir=None, job_id=None, snapshot_file=None, *, refresh=False):
    destination = Path(output_dir).resolve()
    state_dir = Path(state_dir) if state_dir else destination / ".scraper"
    obscura = _obscura_client(state_dir)
    obscura.refresh_mode = refresh
    obscura.on_progress = on_progress
    job_id = job_id or uuid.uuid4().hex
    checkpoint = ChapterCheckpoint(state_dir, job_id)
    refresh_state_path = state_dir / "refresh-generations" / f"{job_id}.json"
    refresh_state = None
    if refresh:
        previous_generation = None
        if refresh_state_path.is_file():
            try:
                previous_generation = json.loads(refresh_state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous_generation = None
        if previous_generation and previous_generation.get("status") == "running":
            refresh_state = previous_generation
        else:
            refresh_state = {"generation": uuid.uuid4().hex, "status": "running",
                             "started_at": time.time()}
            atomic_json(refresh_state_path, refresh_state)
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
        prior_snapshot = snapshot
        if refresh or snapshot is None or not snapshot.get("is_complete"):
            try:
                snapshot = await discover(url, obscura, llm)
            except asyncio.TimeoutError:
                recovered = partial_discovery(url, obscura)
                if recovered["chapters"] or snapshot is None:
                    snapshot = recovered
                else:
                    snapshot = {**snapshot, "is_complete": False,
                                "issues": list(snapshot.get("issues", [])) + ["Discovery deadline exhausted"]}
            except Exception as error:
                recovered = partial_discovery(url, obscura)
                snapshot = {**(snapshot or recovered), "is_complete": False,
                            "issues": ["Discovery failed: " + safe_error(error)]}
        atomic_json(snapshot_path, snapshot)
        if refresh_state is not None:
            refresh_state["snapshot_sha256"] = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            atomic_json(refresh_state_path, refresh_state)
        obscura.metrics["discovery_seconds"] = time.monotonic() - discovery_started
        if not snapshot.get("is_complete") or not snapshot.get("chapters"):
            emit("discovery", "Catalog incomplete; chapter extraction paused")
            files = [item["file"] for item in checkpoint.records.values() if item.get("file") and item.get("success")]
            if refresh_state is not None:
                refresh_state["status"] = "paused"
                atomic_json(refresh_state_path, refresh_state)
            return {**incomplete_catalog_result(snapshot, files), "output_dir": str(destination)}
        chapters = [ChapterLink.model_validate(item) for item in snapshot.get("chapters", [])]
        if refresh and prior_snapshot and (chapter_indices is not None or start_chapter is not None or end_chapter is not None):
            prior_chapters = [ChapterLink.model_validate(item) for item in prior_snapshot.get("chapters", [])]
            prior_selected = select_chapters(prior_chapters, chapter_indices, start_chapter, end_chapter)
            current_by_url = {canonical_url(ch.url): ch for ch in chapters}
            selected = [current_by_url.get(canonical_url(ch.url), ch) for ch in prior_selected]
        else:
            selected = select_chapters(chapters, chapter_indices, start_chapter, end_chapter)
        storage = NovelStorage(base_output_dir=destination, direct_output=True, include_frontmatter=include_frontmatter,
                               overwrite=overwrite, state_dir=state_dir / ("sources-" + hashlib.sha256(str(destination).encode()).hexdigest()[:12]))
        emit("validation", "Validating representative chapter layouts", total=len(selected))
        pending = selected if refresh else [chapter for chapter in selected if not checkpoint.verified(chapter.url, destination)]
        sample_html, sample_page_analyses, plans, pre_results = {}, {}, {}, {}
        if pending:
            positions = list(dict.fromkeys([0, len(pending)//2, len(pending)-1]))
            boundaries = [index for index in range(1, len(pending)) if pending[index].volume and pending[index].volume != pending[index-1].volume]
            positions = list(dict.fromkeys(positions + boundaries + [len(pending)//4, len(pending)*3//4]))[:5]
            for position in positions:
                if checkpoint.cancelled():
                    break
                sample = pending[position]
                try:
                    sample_deadline = time.monotonic() + ScraperLimits.from_env().discovery_seconds
                    html = await before_deadline(obscura.fetch_html(sample.url, refresh=refresh), sample_deadline)
                    sample_html[sample.url] = html
                    page_analysis = PageAnalysis(html)
                    sample_page_analyses[canonical_url(sample.url)] = page_analysis
                    orchestrator = ReviewLoopOrchestrator(analyzer=ChapterAnalyzer(llm), llm_client=llm)
                    with recovery_ceiling("deterministic"):
                        reviewed = await before_deadline(orchestrator.run(html, sample.url, page_analysis=page_analysis), sample_deadline)
                    if (not reviewed.approved or not reviewed.verification.success) and stage_enabled("browser"):
                        from src.agent.ch.candidates import readiness_selector
                        html = await before_deadline(obscura.fetch_html(sample.url, force_browser=True, wait_until="domcontentloaded",
                            content_selector=readiness_selector(html, getattr(getattr(reviewed, "plan", None), "content_selector", None)), readiness="chapter"), sample_deadline)
                        sample_html[sample.url] = html
                        page_analysis = PageAnalysis(html)
                        sample_page_analyses[canonical_url(sample.url)] = page_analysis
                        reviewed = await before_deadline(orchestrator.run(html, sample.url, page_analysis=page_analysis), sample_deadline)
                    if not reviewed.approved or not reviewed.verification.success:
                        error = (getattr(reviewed.verification, "error", None) or
                                 "; ".join(getattr(getattr(reviewed, "review", None), "issues", [])) or
                                 "Sample chapter validation rejected")
                        pre_results[sample.url] = ("blocked" if getattr(reviewed.verification, "is_paywalled", False) or "unsupported" in error.lower() else "failed", error)
                        continue
                except PermissionError as error:
                    pre_results[sample.url] = ("blocked", safe_error(error))
                    continue
                except Exception as error:
                    pre_results[sample.url] = ("failed", safe_error(error))
                    continue
                layout = fingerprint(html, page_analysis=page_analysis)
                plans[layout] = reviewed.plan
                from src.agent.domain_memory import domain_memory
                from src.agent.toc.discovery import capture_chapter_api_candidates
                if snapshot.get("is_complete") and any(
                    canonical_url(item.url) == canonical_url(sample.url) for item in chapters
                ):
                    for candidate in snapshot.get("api_recipe_candidates", []):
                        domain_memory.promote_api_recipe(
                            sample.url, candidate, toc_complete=True,
                            novel_identity_verified=True,
                            quality_score=reviewed.review.quality_score,
                        )
                    for api_recipe in capture_chapter_api_candidates(
                        getattr(obscura, "network_responses", []), sample.url
                    ):
                        domain_memory.promote_api_recipe(
                            sample.url, api_recipe, toc_complete=True,
                            novel_identity_verified=True,
                            quality_score=reviewed.review.quality_score,
                        )
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
                    sample_page_analyses=sample_page_analyses,
                    refresh=refresh,
                    on_progress=lambda current, total, title, percent: emit("downloading", title, current, total, percent),
                    on_stage=lambda stage, message: emit(stage, message, len(runner.outcomes), len(selected), len(runner.outcomes)/len(selected)*100))
        runner._layout_plans.update(plans)
        runner.require_layout_approval = True
        runner.pre_results = pre_results
        if refresh_state is not None:
            runner.refresh_generation = refresh_state["generation"]
        summary = await runner.run(selected, url, author=snapshot.get("author"), description=snapshot.get("description"))
        summary.setdefault("metrics", {}).update({"model_calls": getattr(llm, "call_count", 0),
            "browser_launches": obscura.metrics.get("browser_launches", 0), "browser_renders": obscura.metrics.get("rendered", 0)})
        status = ("cancelled" if summary["cancelled"] or checkpoint.cancelled() else
                  "paused" if summary["blocked"] else
                  "completed" if not summary["failed"] and snapshot.get("is_complete") else "partial")
        if refresh_state is not None:
            refresh_state["status"] = "complete"
            refresh_state["completed_at"] = time.time()
            atomic_json(refresh_state_path, refresh_state)
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
    inspect.add_argument("--refresh", action="store_true")
    inspect.add_argument("--state-dir")
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
    extract.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.command == "inspect":
        print(json.dumps(asyncio.run(inspect_novel_url(args.url, refresh=args.refresh, state_dir=args.state_dir)), ensure_ascii=False), flush=True)
    else:
        args.job_id = args.job_id or uuid.uuid4().hex
        indices = [int(value) for value in args.indices.split(",")] if args.indices is not None else None
        result = asyncio.run(extract_novel_chapters(args.url, args.dest, indices, args.start, args.end,
            args.concurrency, args.frontmatter,
            on_progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
            overwrite=args.overwrite, state_dir=args.state_dir, job_id=args.job_id, snapshot_file=args.snapshot_file,
            refresh=args.refresh))
        encoded = json.dumps({"type": "completed", **result}, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 48000:
            state_dir = Path(args.state_dir) if args.state_dir else Path(args.dest).resolve() / ".scraper"
            result_path = state_dir / "results" / f"{args.job_id or 'standalone'}.json"
            atomic_json(result_path, result)
            encoded = json.dumps({"type": "completed", "status": result["status"], "success": result["success"], "result_file": str(result_path.resolve())})
        print(encoded, flush=True)


if __name__ == "__main__":
    main()

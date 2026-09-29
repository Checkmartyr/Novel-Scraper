"""
NouSetsu API Bridge for Novel-Scraper.
Provides headless URL inspection, TOC discovery, and batch chapter extraction
both programmatically and via JSON-streaming CLI for subprocess execution.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from src.core.binary_manager import ensure_obscura
from src.core.obscura_client import ObscuraClient
from src.agent.classifier import PageClassifier, ChapterLink
from src.agent.llm import LLMClient
from src.agent.toc.agent import TocAgent
from src.agent.ch.analyzer import ChapterAnalyzer
from src.agent.ch.review_loop import ReviewLoopOrchestrator
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.storage import NovelStorage
from src.config import DEFAULT_CONCURRENCY
from src.utils.novel_url import belongs_to_recipe_novel

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("api_bridge")


def _scope_chapters(url: str, chapters: List[ChapterLink]) -> List[ChapterLink]:
    """Do not send links provably belonging to another novel to the batch runner."""
    from src.agent.domain_memory import domain_memory

    recipe = domain_memory.get_recipe(url)
    if not recipe:
        return chapters
    scoped = [
        chapter for chapter in chapters
        if belongs_to_recipe_novel(
            url, chapter.url, recipe.sample_toc_url, recipe.sample_chapter_url
        ) is not False
    ]
    for index, chapter in enumerate(scoped, start=1):
        chapter.index = index
    return scoped


async def inspect_novel_url(url: str) -> Dict[str, Any]:
    """Inspect and classify a novel URL (TOC or chapter), returning metadata and discovered chapter links."""
    ensure_obscura()
    obscura = ObscuraClient()
    try:
        llm_client = LLMClient()
        toc_agent = TocAgent(obscura_client=obscura)
        classifier = PageClassifier(
            llm_client=llm_client,
            obscura_client=obscura,
            toc_agent=toc_agent,
        )

        html = await obscura.fetch_html(url, stealth=True)
        classification = await classifier.classify(url, html)

        chapter_list = classification.chapter_links

        if classification.page_type == "TOC":
            toc_state = classification.toc_state
            if not toc_state:
                toc_state = await toc_agent.extract_toc(url, html=html)
                if toc_state and toc_state.get("extracted_chapters"):
                    chapter_list = toc_state["extracted_chapters"]
                    classification.chapter_links = chapter_list
        elif classification.page_type == "CHAPTER" and classification.toc_url:
            toc_state = await toc_agent.extract_toc(classification.toc_url)
            if toc_state and toc_state.get("extracted_chapters"):
                chapter_list = toc_state["extracted_chapters"]
                classification.novel_title = toc_state.get("novel_title") or classification.novel_title

        chapter_list = _scope_chapters(url, chapter_list)
        if not chapter_list and classification.page_type == "CHAPTER":
            chapter_list = [
                ChapterLink(index=1, title=classification.chapter_title or "Chapter 1", url=url)
            ]

        chapters_payload = [
            {"index": ch.index, "title": ch.title, "url": ch.url}
            for ch in chapter_list
        ]

        return {
            "success": True,
            "url": url,
            "page_type": classification.page_type,
            "novel_title": classification.novel_title or "Unknown Novel",
            "author": classification.author,
            "description": classification.description,
            "total_chapters": len(chapters_payload),
            "chapters": chapters_payload,
        }
    except Exception as e:
        logger.exception("Failed to inspect novel URL: %s", url)
        return {
            "success": False,
            "url": url,
            "error": str(e),
            "total_chapters": 0,
            "chapters": [],
        }
    finally:
        await obscura.close()


async def extract_novel_chapters(
    url: str,
    output_dir: str | Path,
    chapter_indices: Optional[List[int]] = None,
    start_chapter: Optional[int] = None,
    end_chapter: Optional[int] = None,
    concurrency: int = 3,
    include_frontmatter: bool = False,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Extract chapters from a novel URL directly into output_dir.
    Reports progress via on_progress callback if provided.
    """
    ensure_obscura()
    obscura = ObscuraClient()
    dest_path = Path(output_dir).resolve()
    dest_path.mkdir(parents=True, exist_ok=True)

    storage = NovelStorage(
        base_output_dir=dest_path,
        include_frontmatter=include_frontmatter,
        direct_output=True,
    )

    try:
        llm_client = LLMClient()
        toc_agent = TocAgent(obscura_client=obscura)
        classifier = PageClassifier(
            llm_client=llm_client,
            obscura_client=obscura,
            toc_agent=toc_agent,
        )
        analyzer = ChapterAnalyzer(llm_client=llm_client)

        html = await obscura.fetch_html(url, stealth=True)
        classification = await classifier.classify(url, html)

        chapter_list = classification.chapter_links
        if classification.page_type == "TOC":
            toc_state = classification.toc_state
            if not toc_state:
                toc_state = await toc_agent.extract_toc(url, html=html)
                if toc_state and toc_state.get("extracted_chapters"):
                    chapter_list = toc_state["extracted_chapters"]
        elif classification.page_type == "CHAPTER" and classification.toc_url:
            toc_state = await toc_agent.extract_toc(classification.toc_url)
            if toc_state and toc_state.get("extracted_chapters"):
                chapter_list = toc_state["extracted_chapters"]
                classification.novel_title = toc_state.get("novel_title") or classification.novel_title

        chapter_list = _scope_chapters(url, chapter_list)
        if not chapter_list and classification.page_type == "TOC":
            raise ValueError("No chapters belonging to the requested novel were found")
        if not chapter_list:
            chapter_list = [
                ChapterLink(index=1, title=classification.chapter_title or "Chapter 1", url=url)
            ]

        # Filter chapters according to requested indices / range
        selected_indices_set = set(chapter_indices) if chapter_indices else None
        filtered_chapters: List[ChapterLink] = []

        for ch in chapter_list:
            if selected_indices_set is not None:
                if ch.index in selected_indices_set:
                    filtered_chapters.append(ch)
            else:
                if start_chapter is not None and ch.index < start_chapter:
                    continue
                if end_chapter is not None and ch.index > end_chapter:
                    continue
                filtered_chapters.append(ch)

        if not filtered_chapters:
            filtered_chapters = chapter_list

        total_selected = len(filtered_chapters)
        sample_url = filtered_chapters[0].url

        # Analyze sample chapter with ReviewLoopOrchestrator
        orchestrator = ReviewLoopOrchestrator(
            analyzer=analyzer,
            llm_client=llm_client,
        )
        sample_html = await obscura.fetch_html(sample_url, stealth=True)
        loop_result = await orchestrator.run(sample_html, sample_url)

        plan = loop_result.plan

        # Update domain memory recipe if possible
        try:
            from src.agent.domain_memory import domain_memory
            domain_memory.save_recipe(
                domain_or_url=url,
                sample_toc_url=url if classification.page_type == "TOC" else (classification.toc_url or url),
                sample_chapter_url=sample_url,
                toc_strategy=(toc_state or {}).get("extraction_strategy", "dom_heuristic") if "toc_state" in locals() and toc_state else "dom_heuristic",
                chapter_plan=plan,
                quality_score=loop_result.review.quality_score if loop_result.review else 1.0,
            )
        except Exception:
            pass

        # Progress tracking wrapper
        saved_files: List[str] = []
        completed_count = 0

        def batch_progress_cb(current: int, total: int, ch_title: str, pct: float):
            nonlocal completed_count
            completed_count = current
            if on_progress:
                on_progress({
                    "type": "progress",
                    "current": current,
                    "total": total,
                    "title": ch_title,
                    "percent": round(pct, 1),
                })

        runner = BatchScraperRunner(
            novel_title=classification.novel_title,
            initial_plan=plan,
            obscura_client=obscura,
            storage=storage,
            concurrency=concurrency,
            on_progress=batch_progress_cb,
            previous_interaction_id=loop_result.last_interaction_id,
            llm_client=llm_client,
        )

        summary = await runner.run(
            chapters=filtered_chapters,
            source_url=url,
            author=classification.author,
            description=classification.description,
        )

        # Collect saved files
        for ch in filtered_chapters:
            # Check files matching chapter index
            pattern = f"{ch.index:04d} - *.md"
            matches = list(dest_path.glob(pattern))
            for m in matches:
                if m.name not in saved_files:
                    saved_files.append(m.name)

        return {
            "success": True,
            "novel_title": classification.novel_title,
            "total_selected": total_selected,
            "completed": summary.get("completed", len(saved_files)),
            "failed": summary.get("failed", 0),
            "files": saved_files,
            "output_dir": str(dest_path),
        }
    except Exception as e:
        logger.exception("Extraction failed for URL: %s", url)
        return {
            "success": False,
            "error": str(e),
            "total_selected": 0,
            "completed": 0,
            "failed": 0,
            "files": [],
        }
    finally:
        await obscura.close()


def main():
    parser = argparse.ArgumentParser(description="NouSetsu Scraper API Bridge CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Inspect subcommand
    inspect_parser = subparsers.add_parser("inspect", help="Inspect URL and return TOC JSON")
    inspect_parser.add_argument("--url", type=str, required=True, help="URL to inspect")

    # Extract subcommand
    extract_parser = subparsers.add_parser("extract", help="Extract chapters and save to destination")
    extract_parser.add_argument("--url", type=str, required=True, help="URL to scrape")
    extract_parser.add_argument("--dest", type=str, required=True, help="Destination directory path")
    extract_parser.add_argument("--indices", type=str, default="", help="Comma-separated chapter indices")
    extract_parser.add_argument("--start", type=int, default=None, help="Start chapter index")
    extract_parser.add_argument("--end", type=int, default=None, help="End chapter index")
    extract_parser.add_argument("--concurrency", type=int, default=3, help="Concurrent workers")
    extract_parser.add_argument("--frontmatter", action="store_true", help="Include YAML frontmatter")

    args = parser.parse_args()

    if args.command == "inspect":
        res = asyncio.run(inspect_novel_url(args.url))
        print(json.dumps(res, ensure_ascii=False))

    elif args.command == "extract":
        indices = None
        if args.indices.strip():
            try:
                indices = [int(x.strip()) for x in args.indices.split(",") if x.strip()]
            except ValueError:
                indices = None

        def stream_progress(evt: Dict[str, Any]):
            print(json.dumps(evt, ensure_ascii=False), flush=True)

        res = asyncio.run(extract_novel_chapters(
            url=args.url,
            output_dir=args.dest,
            chapter_indices=indices,
            start_chapter=args.start,
            end_chapter=args.end,
            concurrency=args.concurrency,
            include_frontmatter=args.frontmatter,
            on_progress=stream_progress,
        ))
        print(json.dumps({"type": "completed", **res}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

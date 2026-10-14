import argparse
import asyncio
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console

from src.core.binary_manager import ensure_obscura
from src.core.obscura_client import ObscuraClient
from src.agent.classifier import PageClassifier, ChapterLink
from src.agent.toc.agent import TocAgent
from src.agent.ch.analyzer import ChapterAnalyzer
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.ch.review_loop import ReviewLoopOrchestrator
import logging
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.storage import NovelStorage
from src.config import DEFAULT_CONCURRENCY, LOGS_DIR
from src.utils.logger import setup_file_logging, clean_markup

console = Console()

async def run_headless(url: str, concurrency: int = DEFAULT_CONCURRENCY) -> dict:
    """Use the same autonomous, resumable pipeline as NouSetsu's worker."""
    import hashlib
    from src.api_bridge import inspect_novel_url, extract_novel_chapters
    from src.scraper.runtime import atomic_json, canonical_url
    setup_file_logging(logs_dir=LOGS_DIR, session_prefix="cli")
    inspection = await inspect_novel_url(url)
    if not inspection.get("success"):
        raise RuntimeError(inspection.get("error") or "No accessible chapter catalog")
    storage = NovelStorage()
    destination = storage.get_novel_dir(inspection["novel_title"])
    state_dir = destination / ".scraper"
    job_id = hashlib.sha256(canonical_url(url).encode()).hexdigest()[:32]
    snapshot = state_dir / "inspections" / f"{job_id}.json"
    atomic_json(snapshot, inspection)
    def report(event):
        console.print(f"[cyan]{event.get('stage', 'downloading')}[/cyan]: "
                      f"{event.get('message', '')} ({event.get('current', 0)}/{event.get('total', 0)})")
    result = await extract_novel_chapters(url, destination, concurrency=concurrency,
        state_dir=state_dir, job_id=job_id, snapshot_file=snapshot, on_progress=report)
    color = "green" if result["status"] == "completed" else "yellow"
    console.print(f"[{color}]Extraction {result['status']}: {result.get('completed', 0)} verified chapters in {destination}[/{color}]")
    return result

def main():
    parser = argparse.ArgumentParser(description="Novel Scraping Agent powered by Obscura and Gemini")
    parser.add_argument("--url", type=str, default="", help="Novel TOC or chapter URL")
    parser.add_argument("--auto", action="store_true", help="Run in headless CLI mode without TUI")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Number of concurrent workers")
    args = parser.parse_args()

    if args.auto:
        if not args.url:
            parser.error("--url is required when running in headless mode (--auto).")
        result = asyncio.run(run_headless(args.url, concurrency=args.concurrency))
        if result["status"] != "completed":
            raise SystemExit(1)
    else:
        # Launch Textual TUI
        from src.ui.app import NovelScraperApp
        app = NovelScraperApp(initial_url=args.url, initial_concurrency=args.concurrency)
        app.run()

if __name__ == "__main__":
    main()

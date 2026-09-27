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

async def run_headless(url: str, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    """Run scraper in headless CLI mode without Textual TUI."""
    session_log, latest_log = setup_file_logging(logs_dir=LOGS_DIR, session_prefix="cli")
    cli_logger = logging.getLogger("cli")
    cli_logger.info(f"CLI headless scrape started for URL: {url}")

    console.print(f"[cyan]Ensuring Obscura binary...[/cyan]")
    ensure_obscura()
    
    obscura = ObscuraClient()
    try:
        def cli_log_step(msg: str, level: str = "info"):
            color = "green" if level == "info" else ("yellow" if level == "warning" else "red")
            console.print(f"[{color}][{level.upper()}][/{color}] {msg}")
            clean_text = clean_markup(msg)
            if level == "error":
                cli_logger.error(clean_text)
            elif level in ("warning", "warn"):
                cli_logger.warning(clean_text)
            else:
                cli_logger.info(clean_text)

        toc_agent = TocAgent(obscura_client=obscura, on_log=cli_log_step)
        classifier = PageClassifier(obscura_client=obscura, toc_agent=toc_agent)
        analyzer = ChapterAnalyzer()
        storage = NovelStorage()
        
        console.print(f"[cyan]Fetching and classifying URL:[/cyan] {url}")
        html = await obscura.fetch_html(url, stealth=True)
        classification = await classifier.classify(url, html)
        
        console.print(f"[green]Page Classified:[/green] {classification.page_type}")
        console.print(f"[green]Novel Title:[/green] {classification.novel_title}")
        
        sample_url = url
        chapter_list = classification.chapter_links
        
        if classification.page_type == "TOC":
            toc_state = classification.toc_state
            if not toc_state:
                toc_state = await toc_agent.extract_toc(url, html=html)
                if toc_state and toc_state.get("extracted_chapters"):
                    chapter_list = toc_state["extracted_chapters"]
                    classification.chapter_links = chapter_list
            strategy = (toc_state or {}).get("extraction_strategy", "dom")
            confidence = (toc_state or {}).get("confidence_score", 1.0)
            claimed = (toc_state or {}).get("claimed_chapters")
            audit_passed = (toc_state or {}).get("audit_passed", True)
            claimed_str = f" (Claimed: {claimed})" if claimed else ""
            console.print(f"[bold green]TOC Extracted:[/bold green] Strategy={strategy} (Confidence: {int(confidence * 100)}%), Chapters={len(chapter_list)}{claimed_str}, Audit={'PASSED' if audit_passed else 'WARNING'}")
        elif classification.page_type == "CHAPTER" and classification.toc_url:
            console.print(f"[cyan]Found TOC link:[/cyan] {classification.toc_url}")
            toc_state = await toc_agent.extract_toc(classification.toc_url)
            if toc_state and toc_state.get("extracted_chapters"):
                chapter_list = toc_state["extracted_chapters"]
                classification.novel_title = toc_state.get("novel_title") or classification.novel_title
                console.print(f"[bold green]TOC Extracted from Chapter Link:[/bold green] {len(chapter_list)} chapters discovered.")
                
        if chapter_list:
            sample_url = chapter_list[0].url
        else:
            chapter_list = [ChapterLink(index=1, title=classification.chapter_title or "Chapter 1", url=url)]
            
        # Automatically create novel directory and save metadata.json with all chapter links
        from src.agent.llm import token_tracker
        novel_folder = storage.get_novel_dir(classification.novel_title)
        meta_file = await storage.save_metadata(
            novel_title=classification.novel_title,
            source_url=url,
            author=classification.author,
            description=classification.description,
            total_chapters=len(chapter_list),
            completed_chapters=0,
            chapter_index_list=chapter_list,
            token_usage=token_tracker.get_summary(),
            extraction_strategy=(toc_state or {}).get("extraction_strategy") if "toc_state" in locals() and toc_state else None,
            audit_passed=(toc_state or {}).get("audit_passed") if "toc_state" in locals() and toc_state else None,
        )
        console.print(f"[bold green]Novel directory created:[/bold green] {novel_folder}")
        console.print(f"[bold green]Saved initial metadata.json ({len(chapter_list)} chapters):[/bold green] {meta_file.name}")

        orchestrator = ReviewLoopOrchestrator(analyzer=analyzer)

        def log_review_step(iteration: int, max_iter: int, p, v, r):
            status_tag = "[green]APPROVED[/green]" if r.is_accurate else "[yellow]REFINING[/yellow]"
            console.print(f"[bold cyan][Review Loop {iteration}/{max_iter}][/bold cyan] Observer Score: {r.quality_score}/1.0 - {status_tag}")
            if r.issues:
                for issue in r.issues:
                    console.print(f"  [yellow]Observer Issue:[/yellow] {issue}")
            if r.recommended_fixes and not r.is_accurate:
                for fix in r.recommended_fixes:
                    console.print(f"  [cyan]Observer Recommendation:[/cyan] {fix}")

        console.print(f"[cyan]Analyzing sample chapter with Observer Review Loop:[/cyan] {sample_url}")
        sample_html = await obscura.fetch_html(sample_url, stealth=True)
        loop_result = await orchestrator.run(sample_html, sample_url, on_progress=log_review_step)
        
        plan = loop_result.plan
        verification = loop_result.verification
        review = loop_result.review
        
        if not verification.success:
            console.print(f"[red]Selector verification failed: {verification.error}[/red]")
            sys.exit(1)
            
        approval_status = "Approved" if loop_result.approved else "Best Effort"
        console.print(f"[green]Observer Review Complete ({approval_status})![/green] Title: '{verification.chapter_title}', Words: {verification.word_count}, Score: {review.quality_score}/1.0")

        # Save or update domain recipe for next time
        try:
            from src.agent.domain_memory import domain_memory
            saved_recipe = domain_memory.save_recipe(
                domain_or_url=url,
                sample_toc_url=url if classification.page_type == "TOC" else (classification.toc_url or url),
                sample_chapter_url=sample_url,
                toc_strategy=(toc_state or {}).get("extraction_strategy", "dom_heuristic"),
                chapter_plan=plan,
                quality_score=review.quality_score,
            )
            console.print(f"[bold cyan]Domain recipe saved for '{saved_recipe.domain}':[/bold cyan] recipes/{saved_recipe.domain}.json & .py")
        except Exception as e:
            cli_logger.warning(f"Failed to save domain recipe: {e}")

        console.print(f"[cyan]Starting batch download of {len(chapter_list)} chapters...[/cyan]")
        
        runner = BatchScraperRunner(
            novel_title=classification.novel_title,
            initial_plan=plan,
            obscura_client=obscura,
            storage=storage,
            concurrency=concurrency,
            on_log=lambda msg, lvl: console.print(f"[{lvl}]{msg}[/{lvl}]"),
            previous_interaction_id=loop_result.last_interaction_id,
        )
        
        summary = await runner.run(
            chapters=chapter_list,
            source_url=url,
            author=classification.author,
            description=classification.description
        )
        novel_dir = storage.get_novel_dir(classification.novel_title)
        console.print(f"[bold green]Batch download complete! {summary['completed']}/{summary['total']} chapters saved to {novel_dir}[/bold green]")
    finally:
        await obscura.close()

def main():
    parser = argparse.ArgumentParser(description="Novel Scraping Agent powered by Obscura and Gemini")
    parser.add_argument("--url", type=str, default="", help="Novel TOC or chapter URL")
    parser.add_argument("--auto", action="store_true", help="Run in headless CLI mode without TUI")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Number of concurrent workers")
    args = parser.parse_args()

    if args.auto:
        if not args.url:
            parser.error("--url is required when running in headless mode (--auto).")
        asyncio.run(run_headless(args.url, concurrency=args.concurrency))
    else:
        # Launch Textual TUI
        from src.ui.app import NovelScraperApp
        app = NovelScraperApp(initial_url=args.url, initial_concurrency=args.concurrency)
        app.run()

if __name__ == "__main__":
    main()

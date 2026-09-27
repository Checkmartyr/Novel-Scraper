import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from textual.widgets import DataTable, RichLog, Static, Button, Input, TabbedContent
from src.ui.app import NovelScraperApp
from src.agent.classifier import ChapterLink, ClassificationResult
from src.agent.analyzer import DOMStructurePlan
from src.agent.code_generator import ParserVerificationResult
from src.agent.observer import ExtractionReview
from src.agent.review_loop import ReviewLoopResult
from src.ui.widgets.reader import RichMarkdownReader

@pytest.mark.asyncio
async def test_tui_composition():
    """Verify that all core widgets, tabs, details, and DataTable are mounted."""
    app = NovelScraperApp(initial_url="https://example.com/novel")
    async with app.run_test() as pilot:
        # Header and Top bar
        assert app.query_one("#url-input", Input).value == "https://example.com/novel"
        assert app.query_one("#btn-analyze", Button) is not None
        assert app.query_one("#concurrency-input", Input) is not None
        
        # Details panel
        assert app.query_one("#detail-title", Static) is not None
        assert app.query_one("#detail-author", Static) is not None
        assert app.query_one("#detail-type", Static) is not None
        assert app.query_one("#detail-chapters", Static) is not None
        assert app.query_one("#detail-strategy", Static) is not None
        assert app.query_one("#detail-audit", Static) is not None
        assert app.query_one("#detail-tokens", Static) is not None
        
        # Table of Contents DataTable
        table = app.query_one("#toc-table", DataTable)
        assert table is not None
        assert len(table.columns) == 3
        
        # Preview and Logs
        assert app.query_one("#reader-widget") is not None
        assert app.query_one("#code-widget", Static) is not None
        assert app.query_one("#log-widget", RichLog) is not None

@pytest.mark.asyncio
async def test_tui_datatable_population():
    """Verify DataTable can be cleared and populated with chapter rows."""
    app = NovelScraperApp()
    async with app.run_test() as pilot:
        table = app.query_one("#toc-table", DataTable)
        table.clear()
        
        mock_chapters = [
            ChapterLink(index=1, title="Chapter 1: The Beginning", url="https://example.com/ch1"),
            ChapterLink(index=2, title="Chapter 2: The Adventure", url="https://example.com/ch2"),
            ChapterLink(index=3, title="Chapter 3: The Climax", url="https://example.com/ch3"),
        ]
        for ch in mock_chapters:
            table.add_row(str(ch.index), ch.title, ch.url)
            
        assert table.row_count == 3

@pytest.mark.asyncio
async def test_tui_log_msg():
    """Verify log_msg writes formatted messages to RichLog widget when log tab is active."""
    app = NovelScraperApp()
    async with app.run_test() as pilot:
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-logs"
        await pilot.pause()

        app.log_msg("Test info message", "info")
        app.log_msg("Test warning message", "warning")
        app.log_msg("Test error message", "error")
        await pilot.pause()
        
        log_widget = app.query_one("#log-widget", RichLog)
        assert log_widget is not None
        assert len(log_widget.lines) >= 3

@pytest.mark.asyncio
async def test_tui_action_analyze_toc(tmp_path):
    """Verify action_analyze populates TOC details, strategy, audit, DataTable, and creates metadata.json."""
    app = NovelScraperApp(initial_url="https://example.com/novel/toc")
    app.storage.base_output_dir = tmp_path
    
    mock_chapters = [
        ChapterLink(index=1, title="Chapter 1: Genesis", url="https://example.com/ch1"),
        ChapterLink(index=2, title="Chapter 2: Exodus", url="https://example.com/ch2"),
    ]
    
    mock_toc_state = {
        "extracted_chapters": mock_chapters,
        "novel_title": "Test Odyssey",
        "author": "Test Author",
        "extraction_strategy": "paginated_dom",
        "confidence_score": 0.95,
        "audit_passed": True,
        "claimed_chapters": 2,
    }
    
    mock_classification = ClassificationResult(
        page_type="TOC",
        novel_title="Test Odyssey",
        author="Test Author",
        chapter_links=mock_chapters,
        toc_state=mock_toc_state,
    )
    
    mock_plan = DOMStructurePlan(
        title_selector="h1",
        content_selector=".content",
        unwanted_selectors=[],
        next_button_selector=None,
    )
    mock_verification = ParserVerificationResult(
        success=True,
        word_count=500,
        chapter_title="Chapter 1: Genesis",
        generated_code="print('parsed')",
        preview_markdown="# Chapter 1: Genesis\n\nBody text here...",
    )
    mock_review = ExtractionReview(
        is_accurate=True,
        quality_score=0.98,
        issues=[],
        recommended_fixes=[],
    )
    mock_loop_result = ReviewLoopResult(
        plan=mock_plan,
        verification=mock_verification,
        review=mock_review,
        iterations=1,
        review_history=[mock_review],
        approved=True,
    )

    with patch.object(app.obscura, "fetch_html", new=AsyncMock(return_value="<html><body>Mock</body></html>")), \
         patch.object(app.classifier, "classify", new=AsyncMock(return_value=mock_classification)), \
         patch("src.ui.app.ReviewLoopOrchestrator.run", new=AsyncMock(return_value=mock_loop_result)):
         
        async with app.run_test() as pilot:
            await app.action_analyze()
            await pilot.pause()
            
            # Verify details updated
            assert "Test Odyssey" in str(app.query_one("#detail-title", Static).content)
            assert "Test Author" in str(app.query_one("#detail-author", Static).content)
            assert "Chapters Found: 2" in str(app.query_one("#detail-chapters", Static).content)
            assert "paginated_dom" in str(app.query_one("#detail-strategy", Static).content)
            assert "Passed" in str(app.query_one("#detail-audit", Static).content)
            
            # Verify DataTable populated
            table = app.query_one("#toc-table", DataTable)
            assert table.row_count == 2
            
            # Verify Approve button enabled
            assert app.query_one("#btn-approve", Button).disabled is False
            
            # Verify folder and metadata.json created with all chapter links
            import json
            novel_dir = app.storage.get_novel_dir("Test Odyssey")
            meta_path = novel_dir / "metadata.json"
            assert meta_path.is_file()
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            assert meta_data["novel_title"] == "Test Odyssey"
            assert meta_data["author"] == "Test Author"
            assert meta_data["total_chapters"] == 2
            assert meta_data["completed_chapters"] == 0
            assert len(meta_data["chapters"]) == 2
            assert meta_data["chapters"][0]["url"] == "https://example.com/ch1"
            assert meta_data["chapters"][1]["url"] == "https://example.com/ch2"


@pytest.mark.asyncio
async def test_tui_file_logging(tmp_path):
    """Verify that NovelScraperApp initializes and writes logs to local files in logs/."""
    custom_logs_dir = tmp_path / "tui_logs"
    app = NovelScraperApp(logs_dir=custom_logs_dir)
    assert app.session_log_path.exists()
    assert app.latest_log_path.exists()

    async with app.run_test() as pilot:
        app.log_msg("TUI test log line for file persistence", "info")
        app.log_msg("TUI test warning line [bold yellow]alert[/bold yellow]", "warning")
        await pilot.pause()

        # Flush handlers
        import logging
        for h in logging.getLogger().handlers:
            h.flush()

        session_text = app.session_log_path.read_text(encoding="utf-8")
        assert "TUI test log line for file persistence" in session_text
        assert "TUI test warning line alert" in session_text
        assert "[ui.app]" in session_text


@pytest.mark.asyncio
async def test_tui_safe_call_and_batch_execution(tmp_path):
    """Verify that safe_call executes safely on the main thread and batch execution callbacks don't raise RuntimeError."""
    custom_logs_dir = tmp_path / "tui_batch_logs"
    app = NovelScraperApp(logs_dir=custom_logs_dir)
    app.storage.base_output_dir = tmp_path

    # Mock classification and plan so action_start_batch can proceed
    app.classification = ClassificationResult(
        page_type="TOC",
        novel_title="Batch Test Novel",
        chapter_links=[ChapterLink(index=1, title="Ch 1", url="https://example.com/ch1")],
    )
    app.plan = DOMStructurePlan(title_selector="h1", content_selector="#content")

    async with app.run_test() as pilot:
        # 1. Test direct safe_call on main thread (should not raise RuntimeError)
        results = []
        app.safe_call(lambda x: results.append(x * 2), 21)
        assert results == [42]

        # 2. Test mock batch runner invoking callbacks from main asyncio task
        async def mock_run(*args, **kwargs):
            # Emulate batch runner invoking on_progress and on_log
            if app.batch_runner.on_progress:
                app.batch_runner.on_progress(1, 1, "Ch 1", 10.0)
            if app.batch_runner.on_log:
                app.batch_runner.on_log("Batch chapter saved successfully", "info")
            return {"completed": 1, "failed": 0, "total": 1, "errors": []}

        with patch("src.scraper.batch_runner.BatchScraperRunner.run", new=AsyncMock(side_effect=mock_run)):
            await app.action_start_batch()
            await pilot.pause()
            # Wait briefly for batch task to finish
            await asyncio.sleep(0.1)
            await pilot.pause()

        # Check logs for any batch execution error
        import logging
        for h in logging.getLogger().handlers:
            h.flush()
        session_text = app.session_log_path.read_text(encoding="utf-8")
        assert "call_from_thread" not in session_text
        assert "Batch execution error" not in session_text
        assert "Batch chapter saved successfully" in session_text


@pytest.mark.asyncio
async def test_tui_toc_row_selected_preview(tmp_path):
    """Verify that selecting rows in DataTable switches to preview tab and loads chapter preview."""
    app = NovelScraperApp(logs_dir=tmp_path / "logs")
    app.storage.base_output_dir = tmp_path

    chapters = [
        ChapterLink(index=1, title="Chapter 1: Genesis", url="https://example.com/ch1"),
        ChapterLink(index=2, title="Chapter 2: Exodus", url="https://example.com/ch2"),
    ]
    app.sample_chapter_url = "https://example.com/ch1"
    app.sample_chapter_title = "Chapter 1: Genesis"
    app.verification = ParserVerificationResult(
        success=True,
        word_count=500,
        chapter_title="Chapter 1: Genesis",
        generated_code="print('ok')",
        preview_markdown="# Chapter 1: Genesis\n\nGenesis content here...",
    )
    app.plan = DOMStructurePlan(
        title_selector="h1",
        content_selector="#chapter-content",
    )

    async with app.run_test() as pilot:
        # Populate table
        app.populate_toc_table(chapters)
        await pilot.pause()

        table = app.query_one("#toc-table", DataTable)
        tabs = app.query_one("#tabs", TabbedContent)
        reader = app.query_one("#reader-widget", RichMarkdownReader)

        # 1. Select row 0 (Sample chapter)
        table.action_select_cursor()
        await pilot.pause()

        assert tabs.active == "tab-preview"
        assert "Genesis content here..." in reader.markdown_text

        # 2. Select row 1 (Different chapter requiring dynamic fetch)
        mock_ch2_html = "<html><body><h1>Chapter 2: Exodus</h1><div id='chapter-content'><p>Exodus chapter body text. This is a longer paragraph that provides enough novel text to exceed fifty characters comfortably.</p></div></body></html>"
        with patch.object(app.obscura, "fetch_html", new=AsyncMock(return_value=mock_ch2_html)):
            table.move_cursor(row=1)
            table.action_select_cursor()
            await pilot.pause()
            await asyncio.sleep(0.1)
            await pilot.pause()

            assert tabs.active == "tab-preview"
            assert "Exodus chapter body text." in reader.markdown_text
        assert "Chapter 2" in str(app.query_one("#chapter-preview-header", Static).content)


@pytest.mark.asyncio
async def test_tui_single_chapter_fallback_populates_toc_and_preview(tmp_path):
    """Verify single chapter page without TOC populates TOC table with fallback row and switches to preview."""
    app = NovelScraperApp(initial_url="https://example.com/ch1", logs_dir=tmp_path / "logs")
    app.storage.base_output_dir = tmp_path

    mock_classification = ClassificationResult(
        page_type="CHAPTER",
        novel_title="Single Chapter Novel",
        author="Solo Writer",
        chapter_title="Chapter 1: The Lonely Hero",
        chapter_links=[],
        toc_url=None,
    )
    mock_plan = DOMStructurePlan(title_selector="h1", content_selector=".content")
    mock_verification = ParserVerificationResult(
        success=True,
        word_count=350,
        chapter_title="Chapter 1: The Lonely Hero",
        generated_code="print('ok')",
        preview_markdown="# Chapter 1: The Lonely Hero\n\nOnce upon a time...",
    )
    mock_review = ExtractionReview(is_accurate=True, quality_score=1.0, issues=[])
    mock_loop_result = ReviewLoopResult(
        plan=mock_plan,
        verification=mock_verification,
        review=mock_review,
        iterations=1,
        review_history=[mock_review],
        approved=True,
    )

    with patch.object(app.obscura, "fetch_html", new=AsyncMock(return_value="<html><body>Mock</body></html>")), \
         patch.object(app.classifier, "classify", new=AsyncMock(return_value=mock_classification)), \
         patch("src.ui.app.ReviewLoopOrchestrator.run", new=AsyncMock(return_value=mock_loop_result)):

        async with app.run_test() as pilot:
            await app.action_analyze()
            await pilot.pause()

            table = app.query_one("#toc-table", DataTable)
            assert table.row_count == 1

            tabs = app.query_one("#tabs", TabbedContent)
            assert tabs.active == "tab-preview"

            reader = app.query_one("#reader-widget", RichMarkdownReader)
            assert "Once upon a time..." in reader.markdown_text


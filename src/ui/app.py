import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional, List, Callable, Any
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Header,
    Footer,
    Input,
    Button,
    Static,
    RichLog,
    TabbedContent,
    TabPane,
    Checkbox,
    DataTable,
)
from rich.syntax import Syntax

from src.core.obscura_client import ObscuraClient
from src.agent.classifier import PageClassifier, ClassificationResult, ChapterLink
from src.agent.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.toc.agent import TocAgent
from src.agent.code_generator import ChapterCodeGenerator, ParserVerificationResult
from src.agent.review_loop import ReviewLoopOrchestrator
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.storage import NovelStorage
from src.ui.widgets.reader import RichMarkdownReader
from src.ui.widgets.progress import BatchProgressWidget
from src.config import DEFAULT_CONCURRENCY, LOGS_DIR
from src.utils.logger import setup_file_logging, clean_markup

class NovelScraperApp(App):
    """Textual TUI for Obscura Agentic Novel Scraper."""
    
    TITLE = "Novel Scraping Agent"
    SUB_TITLE = "Obscura Headless Engine & Gemini Agent"
    CSS = """
    Screen {
        layout: vertical;
        background: #1a1b26;
        color: #c0caf5;
    }
    
    #top-bar {
        height: 4;
        width: 100%;
        padding: 0 1;
        background: #16161e;
        border-bottom: solid #7aa2f7;
        align: center middle;
    }

    #mascot-badge {
        width: auto;
        margin-right: 1;
        color: #bb9af7;
        text-style: bold;
    }
    
    #url-input {
        width: 50%;
        margin-right: 1;
        background: #24283b;
        border: tall #3b4261;
        color: #c0caf5;
    }
    #url-input:focus {
        border: tall #7aa2f7;
    }
    
    #btn-analyze {
        width: 14%;
        margin-right: 1;
        background: #7aa2f7;
        color: #1a1b26;
        text-style: bold;
    }
    #btn-analyze:hover {
        background: #7dcfff;
    }
    
    #concurrency-input {
        width: 10%;
        margin-right: 1;
        background: #24283b;
        border: tall #3b4261;
        color: #c0caf5;
    }
    
    #stealth-check {
        width: 12%;
        color: #9ece6a;
    }
    
    #main-content {
        height: 1fr;
        width: 100%;
    }
    
    #left-panel {
        width: 32%;
        height: 100%;
        padding: 1;
        border-right: solid #7aa2f7;
        background: #16161e;
    }
    
    #right-panel {
        width: 68%;
        height: 100%;
        background: #1a1b26;
    }
    
    .panel-box {
        border: round #3b4261;
        background: #1f2335;
        padding: 1;
        margin-bottom: 1;
    }
    
    .card-header {
        color: #bb9af7;
        text-style: bold;
        border-bottom: solid #3b4261;
        margin-bottom: 1;
        padding-bottom: 0;
    }

    .step-item {
        padding-left: 1;
        height: 1;
    }

    .step-active {
        color: #7dcfff;
        text-style: bold;
    }

    .step-done {
        color: #9ece6a;
    }

    .step-pending {
        color: #565f89;
    }
    
    .btn-action {
        width: 100%;
        margin-bottom: 1;
        text-style: bold;
    }

    #toc-search-input {
        width: 100%;
        margin-bottom: 1;
        background: #24283b;
        border: tall #3b4261;
        color: #c0caf5;
    }
    #toc-search-input:focus {
        border: tall #7aa2f7;
    }

    #toc-table {
        height: 1fr;
        width: 100%;
        background: #1a1b26;
    }

    #code-widget {
        padding: 1 2;
        background: #1a1b26;
        height: 100%;
        overflow-y: scroll;
    }

    #recipes-table {
        height: 100%;
        width: 100%;
        background: #1a1b26;
    }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("f5", "analyze", "Analyze URL"),
        ("ctrl+d", "start_batch", "Download"),
    ]

    def __init__(
        self,
        initial_url: str = "",
        initial_concurrency: int = DEFAULT_CONCURRENCY,
        logs_dir: Optional[Path] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.initial_url = initial_url
        self.initial_concurrency = initial_concurrency
        self.logs_dir = Path(logs_dir or LOGS_DIR)
        self.session_log_path, self.latest_log_path = setup_file_logging(
            logs_dir=self.logs_dir, session_prefix="tui"
        )
        self.logger = logging.getLogger("ui.app")

        self.obscura = ObscuraClient()
        self.toc_agent = TocAgent(obscura_client=self.obscura, on_log=self.log_msg)
        self.classifier = PageClassifier(obscura_client=self.obscura, toc_agent=self.toc_agent)
        self.analyzer = ChapterAnalyzer()
        self.storage = NovelStorage()
        
        # State
        self.current_url: str = ""
        self.classification: Optional[ClassificationResult] = None
        self.toc_state: Optional[dict] = None
        self.plan: Optional[DOMStructurePlan] = None
        self.verification: Optional[ParserVerificationResult] = None
        self.batch_runner: Optional[BatchScraperRunner] = None
        self.sample_html: str = ""
        self.sample_chapter_url: str = ""
        self.sample_chapter_title: str = ""
        self.last_interaction_id: Optional[str] = None
        self.all_chapter_links: List[ChapterLink] = []

    def compose(self) -> ComposeResult:
        yield Header()
        
        with Horizontal(id="top-bar"):
            yield Static("(=^･ω･^=) Cristina: IDLE", id="mascot-badge")
            yield Input(
                placeholder="Enter novel TOC or chapter URL (Enter to analyze)...",
                value=self.initial_url,
                id="url-input"
            )
            yield Button("🔍 Analyze", variant="primary", id="btn-analyze")
            yield Input(
                placeholder="Workers",
                value=str(self.initial_concurrency),
                id="concurrency-input"
            )
            yield Checkbox("🛡️ Stealth", value=True, id="stealth-check")
            
        with Horizontal(id="main-content"):
            with Vertical(id="left-panel"):
                with Vertical(classes="panel-box", id="stepper-box"):
                    yield Static("Scraping Pipeline", classes="card-header text-bold")
                    yield Static("① Target URL", id="step-url", classes="step-item step-active")
                    yield Static("② TOC Discovery", id="step-toc", classes="step-item step-pending")
                    yield Static("③ DOM Synthesis", id="step-analysis", classes="step-item step-pending")
                    yield Static("④ Batch Scrape", id="step-batch", classes="step-item step-pending")

                with Vertical(classes="panel-box"):
                    yield Static("Novel Details", classes="card-header text-bold")
                    yield Static("Title: -", id="detail-title")
                    yield Static("Author: -", id="detail-author")
                    yield Static("Page Type: -", id="detail-type")
                    yield Static("Chapters Found: 0", id="detail-chapters")
                    yield Static("Strategy: -", id="detail-strategy")
                    yield Static("Audit: -", id="detail-audit")
                    yield Static("Tokens: 0", id="detail-tokens")
                
                with Vertical(classes="panel-box"):
                    yield Static("Actions", classes="card-header text-bold")
                    yield Button("🚀 Approve & Download", variant="success", id="btn-approve", classes="btn-action", disabled=True)
                    yield Button("⏸️ Pause / Resume", variant="warning", id="btn-pause", classes="btn-action", disabled=True)
                    yield Button("⏹️ Cancel", variant="error", id="btn-cancel", classes="btn-action", disabled=True)
                    yield Button("📂 Open Folder", variant="default", id="btn-open-folder", classes="btn-action", disabled=True)

            with Vertical(id="right-panel"):
                with TabbedContent(id="tabs"):
                    with TabPane("Table of Contents", id="tab-toc"):
                        yield Input(placeholder="🔎 Filter chapters by title or index...", id="toc-search-input")
                        yield DataTable(id="toc-table", cursor_type="row")
                    with TabPane("Sample Chapter Preview", id="tab-preview"):
                        yield RichMarkdownReader(id="reader-widget")
                    with TabPane("Generated Parser Code", id="tab-code"):
                        yield Static("# Parser code will appear here after analysis", id="code-widget")
                    with TabPane("Live Logs", id="tab-logs"):
                        yield RichLog(id="log-widget", max_lines=1000, highlight=True, markup=True)
                    with TabPane("Domain Recipes", id="tab-recipes"):
                        yield DataTable(id="recipes-table", cursor_type="row")
                        
        yield BatchProgressWidget(id="progress-widget")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize DataTable columns, log session info, and setup UI state on mount."""
        table = self.query_one("#toc-table", DataTable)
        table.add_columns("#", "Chapter Title", "URL")

        recipes_table = self.query_one("#recipes-table", DataTable)
        recipes_table.add_columns("Domain", "Strategy", "Title Sel", "Content Sel", "Quality", "Used")
        self.load_recipes_table()

        self.log_msg(
            f"Local file logging active: {self.session_log_path.name} & {self.latest_log_path.name} (in folder {self.logs_dir.name}/)",
            "info",
        )

    def log_msg(self, msg: str, level: str = "info") -> None:
        """Write log message to Textual RichLog widget and persistent log file."""
        # 1. Update RichLog in UI
        try:
            log_widget = self.query_one("#log-widget", RichLog)
            color = "green" if level == "info" else ("yellow" if level == "warning" else "red")
            log_widget.write(f"[{color}][{level.upper()}][/{color}] {msg}")
        except Exception:
            pass

        # 2. Write clean plain-text message to persistent log file
        try:
            clean_text = clean_markup(msg)
            lvl_lower = level.lower()
            if lvl_lower == "error":
                self.logger.error(clean_text)
            elif lvl_lower in ("warning", "warn"):
                self.logger.warning(clean_text)
            else:
                self.logger.info(clean_text)
        except Exception:
            pass

    def safe_call(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute a UI callback safely whether called from main asyncio thread or a worker thread."""
        try:
            if hasattr(self, "_thread_id") and self._thread_id == threading.get_ident():
                return callback(*args, **kwargs)
            else:
                return self.call_from_thread(callback, *args, **kwargs)
        except Exception:
            pass

    def update_token_display(self) -> None:
        """Update tokens widget from central token tracker."""
        from src.agent.token_tracker import token_tracker
        summary = token_tracker.get_summary()
        tot = summary["total_tokens"]
        inp = summary["prompt_tokens"]
        out = summary["completion_tokens"]
        self.query_one("#detail-tokens", Static).update(f"Tokens: {tot:,} (In: {inp:,} | Out: {out:,})")

    def set_pipeline_step(self, step_num: int) -> None:
        """Update visual pipeline stepper (1=URL, 2=TOC, 3=DOM, 4=Batch)."""
        steps = [
            ("#step-url", "① Target URL"),
            ("#step-toc", "② TOC Discovery"),
            ("#step-analysis", "③ DOM Synthesis"),
            ("#step-batch", "④ Batch Scrape"),
        ]
        for idx, (elem_id, label) in enumerate(steps, start=1):
            try:
                el = self.query_one(elem_id, Static)
                el.remove_class("step-active", "step-done", "step-pending")
                if idx < step_num:
                    el.add_class("step-done")
                    el.update(f"[bold green]✓[/bold green] {label}")
                elif idx == step_num:
                    el.add_class("step-active")
                    el.update(f"[bold cyan]▶[/bold cyan] [bold]{label}[/bold]")
                else:
                    el.add_class("step-pending")
                    el.update(f"[dim]○ {label}[/dim]")
            except Exception:
                pass

    def set_mascot_status(self, status: str) -> None:
        """Update mascot badge in top bar."""
        try:
            badge = self.query_one("#mascot-badge", Static)
            badge.update(f"(=^･ω･^=) Cristina: {status}")
        except Exception:
            pass

    def load_recipes_table(self) -> None:
        """Populate recipes table with learned domain recipes."""
        try:
            from src.agent.domain_memory import domain_memory
            table = self.query_one("#recipes-table", DataTable)
            table.clear()
            recipes = domain_memory.list_recipes()
            for r in recipes:
                table.add_row(
                    r["domain"],
                    r["strategy"],
                    r["title_selector"][:25],
                    r["content_selector"][:25],
                    f"{r['quality_score']*100:.0f}%",
                    str(r["times_used"]),
                )
        except Exception:
            pass

    def action_open_folder(self) -> None:
        """Open the downloaded novel directory in the OS file explorer."""
        if not self.classification or not self.classification.novel_title:
            return
        novel_folder = self.storage.get_novel_dir(self.classification.novel_title)
        import os
        import subprocess
        try:
            if os.name == "nt":
                os.startfile(str(novel_folder))
            else:
                subprocess.Popen(["xdg-open", str(novel_folder)])
            self.log_msg(f"Opened novel folder: '{novel_folder}'", "info")
        except Exception as e:
            self.log_msg(f"Novel folder path: '{novel_folder}' ({e})", "info")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle real-time search filtering in TOC table."""
        if event.input.id == "toc-search-input":
            query = event.value.strip().lower()
            table = self.query_one("#toc-table", DataTable)
            table.clear()
            matching = [
                ch for ch in self.all_chapter_links
                if query in ch.title.lower() or query in str(ch.index) or query in ch.url.lower()
            ] if query else self.all_chapter_links
            for ch in matching:
                table.add_row(str(ch.index), ch.title, ch.url)
            if matching:
                try:
                    table.move_cursor(row=0)
                except Exception:
                    pass

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit URL when Enter is pressed in #url-input."""
        if event.input.id == "url-input":
            await self.action_analyze()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle UI button clicks."""
        if event.button.id == "btn-analyze":
            await self.action_analyze()
        elif event.button.id == "btn-approve":
            await self.action_start_batch()
        elif event.button.id == "btn-pause":
            self.action_toggle_pause()
        elif event.button.id == "btn-cancel":
            self.action_cancel_scrape()
        elif event.button.id == "btn-open-folder":
            self.action_open_folder()

    def populate_toc_table(self, chapter_links: List[ChapterLink]) -> None:
        """Populate DataTable with chapter links and highlight first row if available."""
        self.all_chapter_links = list(chapter_links)
        table = self.query_one("#toc-table", DataTable)
        table.clear()
        for ch in chapter_links:
            table.add_row(str(ch.index), ch.title, ch.url)
        if chapter_links:
            try:
                table.move_cursor(row=0)
            except Exception:
                pass

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle chapter selection in TOC table: switch to preview tab and load chapter preview."""
        if event.data_table.id != "toc-table":
            return
        try:
            row = event.data_table.get_row(event.row_key)
            if not row or len(row) < 3:
                return
            index_str, title, chapter_url = str(row[0]), str(row[1]), str(row[2])
            
            # Switch to preview tab
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.active = "tab-preview"
            
            reader = self.query_one("#reader-widget", RichMarkdownReader)
            
            # If this is the sample chapter already verified, display immediately
            if self.sample_chapter_url and chapter_url == self.sample_chapter_url and self.verification and self.verification.preview_markdown:
                reader.update_markdown(self.verification.preview_markdown, title=f"Chapter {index_str}: {title}")
                return
                
            # If we have an extraction plan, load the chapter preview dynamically
            if self.plan:
                reader.set_loading(f"Fetching Chapter {index_str}: {title}...")
                self.run_worker(self._load_chapter_preview(index_str, title, chapter_url), exclusive=False)
            else:
                reader.update_markdown(
                    f"*URL: {chapter_url}*\n\nPlease click 'Analyze URL' first before previewing chapters.",
                    title=f"Chapter {index_str}: {title}"
                )
        except Exception as e:
            self.log_msg(f"Error selecting chapter row: {e}", "warning")

    async def _load_chapter_preview(self, index_str: str, title: str, chapter_url: str) -> None:
        """Fetch and extract chapter preview for selected chapter."""
        self.log_msg(f"Loading preview for Chapter {index_str}: {title} ({chapter_url})...", "info")
        stealth = self.query_one("#stealth-check", Checkbox).value
        try:
            ch_html = await self.obscura.fetch_html(chapter_url, stealth=stealth)
            if not ch_html:
                reader = self.query_one("#reader-widget", RichMarkdownReader)
                reader.update_markdown(f"*Failed to load HTML for chapter: {chapter_url}*", title=f"Chapter {index_str}: {title}")
                return
                
            generator = ChapterCodeGenerator(self.plan)
            extracted = generator.extract(ch_html)
            reader = self.query_one("#reader-widget", RichMarkdownReader)
            if extracted.success and extracted.content_markdown:
                md_text = f"# {extracted.title or title}\n\n{extracted.content_markdown}"
                reader.update_markdown(md_text, title=f"Chapter {index_str}: {extracted.title or title}")
                self.log_msg(f"Chapter {index_str} preview loaded ({extracted.word_count} words).", "info")
            else:
                reader.update_markdown(
                    f"# {extracted.title or title}\n\n*Extraction warning: {extracted.error or 'No content found'}*",
                    title=f"Chapter {index_str}: {title}"
                )
        except Exception as e:
            self.log_msg(f"Failed to load chapter preview: {e}", "error")
            reader = self.query_one("#reader-widget", RichMarkdownReader)
            reader.update_markdown(f"*Error loading chapter preview:*\n```\n{e}\n```", title=f"Chapter {index_str}: {title}")

    async def action_analyze(self) -> None:
        """Analyze provided novel URL."""
        url_input = self.query_one("#url-input", Input)
        url = url_input.value.strip()
        if not url:
            self.log_msg("Please enter a valid URL.", "warning")
            return
            
        self.current_url = url
        stealth = self.query_one("#stealth-check", Checkbox).value
        
        progress = self.query_one("#progress-widget", BatchProgressWidget)
        progress.set_status("Analyzing page structure with Obscura...")
        self.set_pipeline_step(2)
        self.set_mascot_status("ANALYZING 🔄")
        self.log_msg(f"Fetching URL via Obscura (stealth={stealth}): {url}")
        
        # Switch to logs tab so user can monitor real-time analysis progress
        try:
            self.query_one("#tabs", TabbedContent).active = "tab-logs"
        except Exception:
            pass

        # Disable analyze button during work
        btn_analyze = self.query_one("#btn-analyze", Button)
        btn_analyze.disabled = True
        
        try:
            # 1. Fetch target page HTML
            html = await self.obscura.fetch_html(url, stealth=stealth)
            self.log_msg(f"Page fetched successfully ({len(html)} bytes). Classifying...", "info")
            
            # 2. Classify page
            self.classification = await self.classifier.classify(url, html)
            self.update_token_display()
            self.log_msg(f"Classified as: {self.classification.page_type} | Novel: '{self.classification.novel_title}'", "info")
            
            # Update left panel details
            self.query_one("#detail-title", Static).update(f"Title: {self.classification.novel_title}")
            self.query_one("#detail-author", Static).update(f"Author: {self.classification.author or 'Unknown'}")
            self.query_one("#detail-type", Static).update(f"Page Type: {self.classification.page_type}")
            
            sample_chapter_url = ""
            
            if self.classification.page_type == "TOC":
                self.toc_state = self.classification.toc_state
                if not self.toc_state:
                    self.log_msg("Invoking autonomous TocAgent...", "info")
                    self.toc_state = await self.toc_agent.extract_toc(url, html=html)
                    if self.toc_state and self.toc_state.get("extracted_chapters"):
                        self.classification.chapter_links = self.toc_state["extracted_chapters"]
                        if self.toc_state.get("novel_title"):
                            self.classification.novel_title = self.toc_state["novel_title"]
                            self.query_one("#detail-title", Static).update(f"Title: {self.classification.novel_title}")
                        if self.toc_state.get("author"):
                            self.classification.author = self.toc_state["author"]
                            self.query_one("#detail-author", Static).update(f"Author: {self.classification.author}")

                total_ch = len(self.classification.chapter_links)
                self.query_one("#detail-chapters", Static).update(f"Chapters Found: {total_ch}")
                
                strategy = (self.toc_state or {}).get("extraction_strategy", "dom")
                confidence = (self.toc_state or {}).get("confidence_score", 1.0)
                audit_passed = (self.toc_state or {}).get("audit_passed", True)
                claimed = (self.toc_state or {}).get("claimed_chapters")
                
                self.query_one("#detail-strategy", Static).update(f"Strategy: {strategy} ({int(confidence * 100)}%)")
                audit_str = "Passed" if audit_passed else "Warning"
                if claimed:
                    audit_str += f" ({total_ch}/{claimed})"
                self.query_one("#detail-audit", Static).update(f"Audit: {audit_str}")
                
                # Populate DataTable
                self.populate_toc_table(self.classification.chapter_links)
                
                if total_ch > 0:
                    sample_chapter_url = self.classification.chapter_links[0].url
                else:
                    self.log_msg("TOC detected but 0 chapters parsed.", "warning")
                    return
            else:
                # Single chapter page
                self.query_one("#detail-chapters", Static).update("Single Chapter Link")
                self.query_one("#detail-strategy", Static).update("Strategy: single_chapter")
                self.query_one("#detail-audit", Static).update("Audit: N/A")
                sample_chapter_url = url
                
                # Check if TOC found to expand
                if self.classification.toc_url:
                    self.log_msg(f"Found TOC link: {self.classification.toc_url}. Extracting TOC index...", "info")
                    self.toc_state = await self.toc_agent.extract_toc(self.classification.toc_url)
                    if self.toc_state and self.toc_state.get("extracted_chapters"):
                        self.classification.chapter_links = self.toc_state["extracted_chapters"]
                        if self.toc_state.get("novel_title"):
                            self.classification.novel_title = self.toc_state["novel_title"]
                            self.query_one("#detail-title", Static).update(f"Title: {self.classification.novel_title}")
                        if self.toc_state.get("author"):
                            self.classification.author = self.toc_state["author"]
                            self.query_one("#detail-author", Static).update(f"Author: {self.classification.author}")
                        total_ch = len(self.classification.chapter_links)
                        self.query_one("#detail-chapters", Static).update(f"Chapters Found: {total_ch}")
                        strategy = self.toc_state.get("extraction_strategy", "default")
                        confidence = self.toc_state.get("confidence_score", 1.0)
                        self.query_one("#detail-strategy", Static).update(f"Strategy: {strategy} ({int(confidence * 100)}%)")
                        self.query_one("#detail-audit", Static).update("Audit: Passed" if self.toc_state.get("audit_passed") else "Audit: Warning")
                        
                        self.populate_toc_table(self.classification.chapter_links)
                        self.log_msg(f"TOC loaded! Discovered {total_ch} chapters.", "info")

            # If chapter_links is empty for single chapter page, fallback to single entry
            if not self.classification.chapter_links and sample_chapter_url:
                self.classification.chapter_links = [
                    ChapterLink(index=1, title=self.classification.chapter_title or "Chapter 1", url=sample_chapter_url)
                ]
                self.populate_toc_table(self.classification.chapter_links)

            self.sample_chapter_url = sample_chapter_url
            self.sample_chapter_title = (
                self.classification.chapter_title
                or (self.classification.chapter_links[0].title if self.classification.chapter_links else "Sample Chapter")
            )

            # Automatically create novel directory and save metadata.json with all chapter links
            from src.agent.token_tracker import token_tracker
            novel_folder = self.storage.get_novel_dir(self.classification.novel_title)
            meta_path = await self.storage.save_metadata(
                novel_title=self.classification.novel_title,
                source_url=self.current_url,
                author=self.classification.author,
                description=self.classification.description,
                total_chapters=len(self.classification.chapter_links),
                completed_chapters=0,
                chapter_index_list=self.classification.chapter_links,
                token_usage=token_tracker.get_summary(),
                extraction_strategy=(self.toc_state or {}).get("extraction_strategy"),
                audit_passed=(self.toc_state or {}).get("audit_passed"),
            )
            self.log_msg(f"Novel folder created: '{novel_folder}'", "info")
            self.log_msg(f"Saved initial metadata.json with {len(self.classification.chapter_links)} chapters: '{meta_path.name}'", "info")
            try:
                self.query_one("#btn-open-folder", Button).disabled = False
            except Exception:
                pass

            # 3. Fetch sample chapter HTML
            self.log_msg(f"Fetching sample chapter for selector synthesis: {sample_chapter_url}", "info")
            if sample_chapter_url == url:
                self.sample_html = html
            else:
                self.sample_html = await self.obscura.fetch_html(sample_chapter_url, stealth=stealth)
                
            # 4. Analyze sample chapter DOM with Observer Review Loop
            self.set_pipeline_step(3)
            self.log_msg("Initiating Observer Review & Refinement Loop...", "info")
            orchestrator = ReviewLoopOrchestrator(analyzer=self.analyzer)

            def tui_review_callback(iteration: int, max_iter: int, p, v, r):
                status_text = "APPROVED" if r.is_accurate else "REFINING"
                self.log_msg(f"[Review Loop {iteration}/{max_iter}] Observer Score: {r.quality_score}/1.0 ({status_text})", "info")
                if r.issues:
                    for issue in r.issues:
                        self.log_msg(f"  Defect: {issue}", "warning")

            loop_result = await orchestrator.run(
                self.sample_html,
                sample_chapter_url,
                on_progress=tui_review_callback,
            )
            self.plan = loop_result.plan
            self.verification = loop_result.verification
            self.last_interaction_id = loop_result.last_interaction_id
            self.update_token_display()
            
            # Update detail card with observer score badge
            observer_badge = f"Quality: {int(loop_result.review.quality_score * 100)}%"
            if loop_result.approved:
                observer_badge += " (Approved)"
            self.query_one("#detail-type", Static).update(f"Page Type: {self.classification.page_type} | {observer_badge}")
            self.log_msg(f"Observer Review Complete: Score={loop_result.review.quality_score}/1.0 | Selectors Verified.", "info")
            
            # Log cumulative token usage
            from src.agent.token_tracker import token_tracker
            tok_summary = token_tracker.get_summary()
            if tok_summary["total_tokens"] > 0:
                self.log_msg(f"Tokens consumed so far: {tok_summary['total_tokens']:,} (Prompt: {tok_summary['prompt_tokens']:,} | Output: {tok_summary['completion_tokens']:,})", "info")
                await self.storage.save_metadata(
                    novel_title=self.classification.novel_title,
                    source_url=self.current_url,
                    token_usage=tok_summary,
                )
            
            # Update Preview widget
            reader = self.query_one("#reader-widget", RichMarkdownReader)
            preview_title = (
                self.verification.chapter_title
                or self.sample_chapter_title
                or "Sample Chapter Preview"
            )
            reader.update_markdown(self.verification.preview_markdown, title=preview_title)
            
            # Update Code widget
            code_view = self.query_one("#code-widget", Static)
            code_view.update(Syntax(self.verification.generated_code, "python", theme="monokai", line_numbers=True))
            
            if self.verification.success:
                try:
                    from src.agent.domain_memory import domain_memory
                    saved_recipe = domain_memory.save_recipe(
                        domain_or_url=self.current_url,
                        sample_toc_url=self.current_url if self.classification.page_type == "TOC" else (self.classification.toc_url or self.current_url),
                        sample_chapter_url=sample_chapter_url,
                        toc_strategy=(self.toc_state or {}).get("extraction_strategy", "dom_heuristic"),
                        chapter_plan=self.plan,
                        quality_score=loop_result.review.quality_score,
                    )
                    self.log_msg(f"Domain recipe saved: recipes/{saved_recipe.domain}.json & .py", "info")
                    full_script = domain_memory.generate_full_standalone_script(saved_recipe)
                    code_view.update(Syntax(full_script, "python", theme="monokai", line_numbers=True))
                    self.load_recipes_table()
                except Exception as e:
                    self.log_msg(f"Failed saving domain recipe: {e}", "warning")

                self.set_pipeline_step(4)
                self.set_mascot_status("READY ✅")
                self.log_msg(f"Verification passed! Chapter words: {self.verification.word_count}. Ready for approval.", "info")
                self.query_one("#btn-approve", Button).disabled = False
                progress.set_status("Ready. Please review preview and click Approve & Download.")

                # Switch to appropriate tab: TOC table if multi-chapter, Preview if single chapter
                tabs = self.query_one("#tabs", TabbedContent)
                if self.classification.chapter_links and len(self.classification.chapter_links) > 1:
                    tabs.active = "tab-toc"
                    self.log_msg("Table of Contents ready! Click or select any chapter to preview.", "info")
                else:
                    tabs.active = "tab-preview"
                    self.log_msg("Chapter Preview ready! Click Approve & Download to start scraping.", "info")
            else:
                self.log_msg(f"Verification warning: {self.verification.error}", "warning")
                progress.set_status(f"Warning: {self.verification.error}")
                try:
                    self.query_one("#tabs", TabbedContent).active = "tab-preview"
                except Exception:
                    pass
                
        except Exception as e:
            self.log_msg(f"Analysis failed: {e}", "error")
            progress.set_status("Analysis failed.")
        finally:
            btn_analyze.disabled = False

    async def action_start_batch(self) -> None:
        """Start batch scraping after approval."""
        if not self.classification or not self.plan:
            return
            
        btn_approve = self.query_one("#btn-approve", Button)
        btn_approve.disabled = True
        self.query_one("#btn-pause", Button).disabled = False
        self.query_one("#btn-cancel", Button).disabled = False
        self.set_mascot_status("DOWNLOADING ⚡")
        
        # Read workers concurrency setting
        try:
            conc_input = self.query_one("#concurrency-input", Input).value.strip()
            concurrency = int(conc_input)
        except ValueError:
            concurrency = DEFAULT_CONCURRENCY
            
        progress_widget = self.query_one("#progress-widget", BatchProgressWidget)
        
        # Setup batch runner
        self.batch_runner = BatchScraperRunner(
            novel_title=self.classification.novel_title,
            initial_plan=self.plan,
            obscura_client=self.obscura,
            storage=self.storage,
            concurrency=concurrency,
            on_progress=lambda comp, tot, title, spd: self.safe_call(
                progress_widget.update_progress, comp, tot, title, spd
            ),
            on_log=lambda msg, lvl: self.safe_call(self.log_msg, msg, lvl),
            previous_interaction_id=self.last_interaction_id,
        )
        
        chapter_list = self.classification.chapter_links
        # If single chapter link without TOC list, create single item
        if not chapter_list:
            chapter_list = [
                ChapterLink(index=1, title=self.classification.chapter_title or "Chapter 1", url=self.current_url)
            ]
            
        asyncio.create_task(self._execute_batch(chapter_list))

    async def _execute_batch(self, chapter_list: List[ChapterLink]) -> None:
        """Run batch scraper task in background."""
        progress_widget = self.query_one("#progress-widget", BatchProgressWidget)
        try:
            summary = await self.batch_runner.run(
                chapters=chapter_list,
                source_url=self.current_url,
                author=self.classification.author,
                description=self.classification.description,
            )
            progress_widget.set_status(f"Completed! {summary['completed']}/{summary['total']} chapters saved.")
            novel_dest = self.storage.get_novel_dir(self.classification.novel_title)
            self.log_msg(f"Novel saved successfully to '{novel_dest}'!", "info")
            self.set_mascot_status("COMPLETED ★ (=^･ω･^=)")
        except Exception as e:
            self.log_msg(f"Batch execution error: {e}", "error")
            progress_widget.set_status("Batch execution error.")
            self.set_mascot_status("ERROR ❌")
        finally:
            self.query_one("#btn-pause", Button).disabled = True
            self.query_one("#btn-cancel", Button).disabled = True

    def action_toggle_pause(self) -> None:
        """Toggle pause/resume state."""
        if not self.batch_runner:
            return
        btn = self.query_one("#btn-pause", Button)
        if self.batch_runner._is_paused:
            self.batch_runner.resume()
            btn.label = "Pause"
        else:
            self.batch_runner.pause()
            btn.label = "Resume"

    def action_cancel_scrape(self) -> None:
        """Cancel batch scrape."""
        if self.batch_runner:
            self.batch_runner.cancel()

    async def on_unmount(self) -> None:
        """Cleanup resources on exit."""
        if hasattr(self, "logger") and self.logger:
            self.logger.info("TUI session ended.")
        if hasattr(self, "obscura") and self.obscura:
            await self.obscura.close()


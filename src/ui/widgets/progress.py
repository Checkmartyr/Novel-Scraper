from textual.app import ComposeResult
from textual.widgets import Static, ProgressBar
from textual.containers import Vertical, Horizontal

class BatchProgressWidget(Vertical):
    """Compound widget displaying batch download metrics and progress bar."""
    
    DEFAULT_CSS = """
    BatchProgressWidget {
        height: 5;
        width: 100%;
        background: #16161e;
        border-top: solid #7aa2f7;
        padding: 0 2;
    }
    
    .metrics-header {
        height: 1;
        width: 100%;
        margin-top: 1;
    }
    
    #status-val {
        width: 25%;
        color: #7dcfff;
        text-style: bold;
    }
    
    #count-val {
        width: 30%;
        color: #e0af68;
        text-align: center;
        text-style: bold;
    }
    
    #current-val {
        width: 45%;
        color: #bb9af7;
        text-align: right;
    }
    
    #scrape-bar {
        width: 100%;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(classes="metrics-header"):
            yield Static("● Idle", id="status-val")
            yield Static("0 / 0 (0%)", id="count-val")
            yield Static("-", id="current-val")
            
        yield ProgressBar(id="scrape-bar", total=100, show_eta=True)

    def update_progress(self, completed: int, total: int, current_title: str, speed: float) -> None:
        """Update metrics and progress bar."""
        bar = self.query_one("#scrape-bar", ProgressBar)
        if total > 0:
            pct = int((completed / total) * 100)
            bar.update(total=total, progress=completed)
            self.query_one("#count-val", Static).update(f"📊 {completed} / {total} ({pct}%)")
        else:
            self.query_one("#count-val", Static).update(f"📊 {completed} chapters")
            
        short_title = current_title[:35] + ("..." if len(current_title) > 35 else "")
        self.query_one("#current-val", Static).update(f"⚡ {speed:.1f} ch/min | {short_title}")
        self.query_one("#status-val", Static).update("⚡ Downloading...")

    def set_status(self, status: str) -> None:
        """Set high-level status string."""
        self.query_one("#status-val", Static).update(status)


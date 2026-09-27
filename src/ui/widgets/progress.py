from textual.app import ComposeResult
from textual.widgets import Static, ProgressBar
from textual.containers import Vertical, Horizontal

class BatchProgressWidget(Vertical):
    """Compound widget displaying batch download metrics and progress bar."""
    
    DEFAULT_CSS = """
    BatchProgressWidget {
        height: 6;
        width: 100%;
        background: $surface-darken-1;
        border-top: solid $primary;
        padding: 0 1;
    }
    
    .metric-row {
        height: 1;
        width: 100%;
    }
    
    .metric-label {
        width: 30%;
        color: $text-muted;
    }
    
    .metric-value {
        width: 70%;
        color: $accent;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(classes="metric-row"):
            yield Static("Status:", classes="metric-label")
            yield Static("Idle", id="status-val", classes="metric-value")
        with Horizontal(classes="metric-row"):
            yield Static("Progress:", classes="metric-label")
            yield Static("0 / 0 (0%)", id="count-val", classes="metric-value")
        with Horizontal(classes="metric-row"):
            yield Static("Speed / Chapter:", classes="metric-label")
            yield Static("-", id="current-val", classes="metric-value")
            
        yield ProgressBar(id="scrape-bar", total=100, show_eta=True)

    def update_progress(self, completed: int, total: int, current_title: str, speed: float) -> None:
        """Update metrics and progress bar."""
        bar = self.query_one("#scrape-bar", ProgressBar)
        if total > 0:
            pct = int((completed / total) * 100)
            bar.update(total=total, progress=completed)
            self.query_one("#count-val", Static).update(f"{completed} / {total} ({pct}%)")
        else:
            self.query_one("#count-val", Static).update(f"{completed} chapters")
            
        self.query_one("#current-val", Static).update(f"{speed:.1f} ch/min | {current_title[:40]}")
        self.query_one("#status-val", Static).update("Downloading...")

    def set_status(self, status: str) -> None:
        """Set high-level status string."""
        self.query_one("#status-val", Static).update(status)

import re
from textual.app import ComposeResult
from textual.containers import VerticalScroll, Horizontal, Vertical
from textual.widgets import Static, Button
from rich.markdown import Markdown


def calculate_reading_stats(text: str) -> tuple[int, int, str]:
    """Calculate word count, character count, and estimated reading time."""
    if not text:
        return 0, 0, "< 1 min"
    words = len(re.findall(r"\b\w+\b", text))
    chars = len(re.sub(r"\s+", "", text))
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    if cjk_chars > 50:
        minutes = max(1, round(cjk_chars / 500))
    else:
        minutes = max(1, round(words / 200)) if words > 0 else 1
    return words, chars, f"{minutes} min"


class RichMarkdownReader(VerticalScroll):
    """Textual widget that renders Markdown content using Rich inside a scrollable container."""

    DEFAULT_CSS = """
    RichMarkdownReader {
        width: 100%;
        height: 100%;
        padding: 1 2;
        background: #1a1b26;
        color: #c0caf5;
        border: round #7aa2f7;
    }
    #reader-toolbar {
        width: 100%;
        height: auto;
        border-bottom: solid #3b4261;
        padding-bottom: 1;
        margin-bottom: 1;
    }
    #chapter-preview-header {
        text-style: bold;
        color: #7dcfff;
        width: 100%;
    }
    #reader-stats {
        color: #9ece6a;
        margin-top: 1;
    }
    #md-display {
        margin-top: 1;
        width: 100%;
    }
    """

    def __init__(
        self,
        initial_markdown: str = "*No chapter analyzed yet. Enter a URL above and click Analyze.*",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.can_focus = True
        self.markdown_text = initial_markdown
        self.title_text = "Chapter Preview"

    def compose(self) -> ComposeResult:
        with Vertical(id="reader-toolbar"):
            yield Static(self.title_text, id="chapter-preview-header")
            yield Static("📖 0 words  •  0 chars  •  ⏱️ < 1 min", id="reader-stats")
        yield Static(Markdown(self.markdown_text), id="md-display")

    def set_loading(self, message: str = "Loading chapter preview...") -> None:
        """Set loading message in reader."""
        try:
            header = self.query_one("#chapter-preview-header", Static)
            header.update("Loading Preview...")
            stats = self.query_one("#reader-stats", Static)
            stats.update("[bold yellow]⏳ Fetching & parsing chapter DOM...[/bold yellow]")
            display = self.query_one("#md-display", Static)
            display.update(Markdown(f"*{message}*"))
        except Exception:
            pass

    def update_markdown(self, markdown_text: str, title: str = "") -> None:
        """Update rendered markdown text, reading metrics, and scroll to top."""
        self.markdown_text = markdown_text
        if title:
            self.title_text = f"Preview: {title}"
        try:
            header = self.query_one("#chapter-preview-header", Static)
            header.update(self.title_text)

            words, chars, est_time = calculate_reading_stats(markdown_text)
            stats = self.query_one("#reader-stats", Static)
            stats.update(
                f"[bold cyan]📖 {words:,} words[/bold cyan]  •  "
                f"[bold yellow]{chars:,} chars[/bold yellow]  •  "
                f"[bold green]⏱️ ~{est_time} read[/bold green]"
            )

            display = self.query_one("#md-display", Static)
            display.update(Markdown(self.markdown_text))
            self.scroll_home(animate=False)
        except Exception:
            pass

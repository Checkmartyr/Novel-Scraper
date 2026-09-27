from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static
from rich.markdown import Markdown

class RichMarkdownReader(VerticalScroll):
    """Textual widget that renders Markdown content using Rich inside a scrollable container."""
    
    DEFAULT_CSS = """
    RichMarkdownReader {
        width: 100%;
        height: 100%;
        padding: 1 2;
        background: $surface;
        color: $text;
        border: round $primary;
    }
    #chapter-preview-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
        border-bottom: solid $primary;
        padding-bottom: 1;
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
        yield Static(self.title_text, id="chapter-preview-header")
        yield Static(Markdown(self.markdown_text), id="md-display")

    def set_loading(self, message: str = "Loading chapter preview...") -> None:
        """Set loading message in reader."""
        try:
            header = self.query_one("#chapter-preview-header", Static)
            header.update("Loading Preview...")
            display = self.query_one("#md-display", Static)
            display.update(Markdown(f"*{message}*"))
        except Exception:
            pass

    def update_markdown(self, markdown_text: str, title: str = "") -> None:
        """Update rendered markdown text and scroll to top."""
        self.markdown_text = markdown_text
        if title:
            self.title_text = f"Preview: {title}"
        try:
            header = self.query_one("#chapter-preview-header", Static)
            header.update(self.title_text)
            display = self.query_one("#md-display", Static)
            display.update(Markdown(self.markdown_text))
            self.scroll_home(animate=False)
        except Exception:
            pass

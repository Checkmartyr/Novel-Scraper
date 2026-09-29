# Novel Scraping Agent

An autonomous, agentic web novel scraping system powered by **Obscura** anti-detect headless browser, **Google Gemini Interactions API**, **LangChain / LangGraph**, and **Textual / Rich**.

The agent follows an **Analyze-Once, Synthesize Deterministically, Self-Heal on Failure** pattern: it leverages Gemini and LangGraph to semantically understand novel DOM structures, executes an Actor-Critic Observer review loop to guarantee clean extraction, generates deterministic BeautifulSoup parsers, caches domain extraction recipes to eliminate redundant LLM calls, handles paginated multi-page chapters, and downloads entire novels with polite concurrency.

---

## Key Features

- **Domain Memory & Recipe Cache**: Caches TOC strategies and chapter extraction plans in `src/recipes/<domain>.json`, with generated extractor scripts in `src/recipes/<domain>.py`. Recipes are checked against the current page before reuse, avoiding repeated model work when the cached plan still matches.
- **Autonomous Table of Contents (TOC) LangGraph Agent**: [`TocAgent`](src/agent/toc/agent.py) coordinates metadata inspection, embedded-state extraction (including Apollo and JSON-LD), DOM link discovery, completeness audits, pagination, and interactive expansion.
- **Specialized Platform Handlers**: Built-in support and bypasses for complex novel platforms including:
  - **Dek-D** ([`src/handlers/dekd.py`](src/handlers/dekd.py)): API-backed TOC handling and synthetic HTML for Dek-D pages.
  - **Nekopost** ([`src/handlers/nekopost.py`](src/handlers/nekopost.py)): Project and chapter data handling, including encrypted chapter content.
  - **Kakuyomu**: Next.js Apollo state deserialization and WorkTocSection title cleaning.
  - **Syosetu (小説家になろう)**, **FreeWebNovel**, **EmpireNovel**, and **XSZJ**.
- **Obscura Anti-Detect Browser Engine**: Integrated Rust-based headless browser with stealth fingerprinting, Cloudflare/Turnstile/Akamai bypass, and CDP server support.
- **Gemini Interactions API & LangChain**: Native multi-turn state chaining via `previous_interaction_id` to preserve reasoning context across analysis, refinement, and self-healing turns.
- **Actor-Critic Quality Review Loop**: [`ChapterAnalyzer`](src/agent/ch/analyzer.py) and [`ExtractionObserver`](src/agent/ch/observer.py) review a sample extraction for title accuracy, clutter, and content quality before batch scraping.
- **Interactive Textual TUI**: Modern terminal interface featuring:
  - **Interactive TOC Table**: Dynamic `DataTable` where clicking or pressing `Enter` on any chapter instantly switches to the preview tab and displays that chapter's parsed markdown.
  - **Automatic Tab Navigation**: Seamlessly shifts to `Live Logs` during analysis and switches to `Table of Contents` or `Chapter Preview` upon completion.
  - **Focusable Scrollable Reader**: Custom [`RichMarkdownReader`](src/ui/widgets/reader.py) displays chapter previews in a scrollable Markdown view.
  - **Generated Code View**: Displays the synthesized Python parser with syntax highlighting.
- **Paginated Multi-Page Chapter Stitching**: Seamlessly detects and stitches multi-page chapters (`下一页`, `next page`, `?page=N`) into a unified markdown chapter while stripping pagination badges (e.g. `（1/3）`) from titles.
- **Self-Healing Batch Scraper**: Resilient scraper that monitors chapter length and syntax during batch extraction, triggering Gemini with prior reasoning state to patch shifted DOM selectors on-the-fly.
- **Persistent Multi-Level File Logging**: Automatically logs all TUI and CLI sessions with ANSI-stripped plain text into `logs/tui_<timestamp>.log` and `logs/scraper_<timestamp>.log` alongside symlinked `latest` logs.
- **Universal Multi-Language Romanization**: Automatically romanizes novel folder names across Japanese (Hepburn Romaji via `pykakasi`), Chinese (Pinyin via `anyascii`), Korean (Romaja via `anyascii`), and Russian/Cyrillic (via `anyascii`).
- **Clean Raw Markdown Output**: Saves chapters as zero-padded clean markdown files (`0001 - <Title>.md`) without cluttering YAML frontmatter headers (YAML frontmatter can be optionally toggled via `INCLUDE_FRONTMATTER=true`).
- **Full Token Accounting**: Real-time tracking of prompt, completion, thought, and total tokens across all LLM operations, persisted into `metadata.json`.

---

## Architecture & Workflow Guides

For detailed technical explanations, Mermaid diagrams, and pipeline breakdowns, see:
- [Agent Workflow](docs/AGENT_WORKFLOW.md): End-to-end scrape lifecycle from URL input through persistence.
- [Architecture and Project Structure](docs/agent_architecture.md): Current source tree, package responsibilities, runtime flow, and extension points.

---

## Installation & Quickstart

### 1. Prerequisites

- Python `>= 3.10`
- [uv](https://github.com/astral-sh/uv) (recommended) or standard `pip`

### 2. Install Package

```bash
# Clone the repository
git clone https://github.com/MasterMayoi/Novel_scraping_agent.git
cd Novel_scraping_agent

# Install dependencies using uv (recommended):
uv pip install -e .

# Or using pip:
pip install -e .

# Or install globally as a CLI tool:
uv tool install --editable .
```

### 3. Configure Environment

Copy `.env.example` to `.env` and provide your Google Gemini API key:

```bash
copy .env.example .env
```

Edit `.env`:
```ini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash
```

When Novel-Scraper runs inside NouSetsu Desktop, it uses NouSetsu's provider routing and shared API keys. `NOVEL_SCRAPER_MODEL` optionally overrides the scraper route; when unset, the scraper inherits `NOVEL_MODEL` (or NouSetsu's default). `NOVEL_FALLBACK_MODEL` is used for provider failover. Standalone Novel-Scraper continues to use `GEMINI_API_KEY` and `GEMINI_MODEL`.

---

## Configuration Reference

All settings can be customized in `.env` or passed as environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(Required)* | Google Gemini API key for agent reasoning and code generation |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model ID (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-3.5-flash-lite`) |
| `OBSCURA_BIN_PATH` | *(auto)* | Custom path to `obscura.exe` binary. Auto-downloads into `./src/bin/` if empty |
| `NAVIGATION_TIMEOUT` | `30` | Obscura page fetch timeout in seconds |
| `WAIT_UNTIL` | `networkidle` | Navigation wait condition (`networkidle`, `domcontentloaded`, `load`) |
| `OUTPUT_DIR` | `./novels` | Directory where scraped novels and metadata are saved |
| `LOGS_DIR` | `./logs` | Directory for persistent session log files (`tui_*.log`, `scraper_*.log`) |
| `RECIPES_DIR` | `./src/recipes` | Directory for cached domain recipes (`*.json`, `*.py`) |
| `DEFAULT_CONCURRENCY` | `3` | Default number of concurrent scraper worker tasks (clamped 1-10) |
| `MIN_DELAY_SECONDS` | `0.5` | Minimum polite jitter delay between requests (in seconds) |
| `MAX_DELAY_SECONDS` | `1.5` | Maximum polite jitter delay between requests (in seconds) |
| `INCLUDE_FRONTMATTER` | `false` | When `true`, prepends YAML frontmatter to chapter markdown files |
| `ROMANIZE_FOLDER` | `true` | When `true`, romanizes folder names to Latin script (`GuiMiZhiZhu`) |
| `MAX_REVIEW_ITERATIONS`| `32` | Maximum review iterations for the Actor-Critic Observer loop |
| `MIN_QUALITY_SCORE` | `0.85` | Minimum quality score (0.0 - 1.0) required for Observer approval |
| `OBSCURA_GITHUB_REPO` | `h4ckf0r0day/obscura` | GitHub repository for downloading Obscura releases |

---

## Usage

### Interactive Textual TUI

Launch the full interactive TUI application:

```powershell
novel-scraper
```

Or run directly with `uv`:

```powershell
uv run novel-scraper
```

**TUI Features:**
- **URL & Parameter Controls**: Input TOC or individual chapter URL (auto-discovers TOC index), configure worker concurrency, and toggle anti-detect stealth mode.
- **Novel Detail Card**: Live status, title, author, chapter count, extraction strategy, and cumulative token metrics.
- **Table of Contents Tab (`tab-toc`)**: Displays complete list of discovered chapters. Selecting or double-clicking any row automatically loads that chapter's preview.
- **Chapter Preview Tab (`tab-preview`)**: Focusable, scrollable Rich Markdown reader showing parsed title and content.
- **Generated Parser Code Tab (`tab-code`)**: Displays the synthesized standalone Python extractor with Monokai syntax highlighting.
- **Live Logs Tab (`tab-logs`)**: Real-time worker output and observer review feedback.
- **Compound Progress Bar**: Shows chapter completion, percentage, download speed (ch/min), and active chapter title.

### Headless CLI Mode

For automated workflows, CI/CD, or background batch scraping:

```powershell
# Scrape novel automatically in headless mode:
novel-scraper --url "https://kakuyomu.jp/works/16816927860264024346" --auto

# Customize worker concurrency:
novel-scraper --url "https://example.com/novel-toc" --auto --concurrency 4
```

**CLI Flags:**
- `--url <URL>`: Novel Table of Contents (TOC) URL or individual chapter URL.
- `--auto`: Run headless pipeline without launching the Textual TUI.
- `--concurrency <N>`: Number of concurrent worker tasks (default: `3`).

---

## Output Structure

Downloaded novels are stored under `novels/<Romanized_Title>/`:

```
novels/
└── Akuyaku Kizoku Niyoru Jakushou Ryouchi No Kakumei Kaitaku/
    ├── metadata.json
    ├── 0001 - 悪役貴族、姉妹ができる.md
    ├── 0002 - 悪役貴族、ドライヤーという名の火炎放射器を作る.md
    ├── 0003 - 悪役貴族、ブレーキ無しバイクを作る.md
    └── ...
```

### Chapter File Format

By default (`INCLUDE_FRONTMATTER=false`), chapters contain clean raw Markdown:

```markdown
# 0001 - 悪役貴族、姉妹ができる

「は、話がある」夕食時、挙動不審な父上が唐突にそう言った。

僕の名前はクノウ・ドラーナ。十歳。ガルダナキア王国の辺境にあるドラーナ男爵家の嫡男である。

...
```

### `metadata.json` Format

```json
{
  "novel_title": "悪役貴族による弱小領地の革命開拓",
  "romanized_title": "Akuyaku Kizoku Niyoru Jakushou Ryouchi No Kakumei Kaitaku",
  "author": "Author Name",
  "description": "Novel synopsis...",
  "source_url": "https://kakuyomu.jp/works/...",
  "total_chapters": 120,
  "completed_chapters": 120,
  "extraction_strategy": "embedded_state",
  "audit_passed": true,
  "token_usage": {
    "prompt_tokens": 12450,
    "completion_tokens": 2890,
    "thought_tokens": 850,
    "total_tokens": 16190,
    "call_count": 3
  },
  "created_at": "2026-09-17T12:00:00+00:00",
  "last_updated": "2026-09-17T12:00:00+00:00",
  "chapters": [
    {
      "index": 1,
      "title": "悪役貴族、姉妹ができる",
      "url": "https://kakuyomu.jp/works/.../episodes/...",
      "downloaded": true,
      "success": true
    }
  ]
}
```

---

## Project Structure

The README shows the main packages; the [Architecture and Project Structure guide](docs/agent_architecture.md) documents the current source tree and each module in more detail.

```text
Novel_scraping_agent/
├── docs/                      # Architecture and end-to-end workflow guides
├── logs/                      # Runtime session logs
├── novels/                    # Downloaded chapters and metadata
├── scripts/
│   └── repair_scraped_novels.py
├── src/
│   ├── bin/                   # Obscura binary location
│   ├── recipes/               # Cached domain recipes and generated extractors
│   ├── config.py              # Environment-backed configuration and paths
│   ├── main.py                # CLI entrypoint and headless pipeline
│   ├── agent/
│   │   ├── ch/                # Chapter analysis, parser, review, and self-healing
│   │   ├── llm/               # Gemini/LangChain client and token tracking
│   │   ├── toc/               # LangGraph table-of-contents extraction
│   │   ├── classifier.py      # Page classification and TOC/chapter metadata
│   │   ├── domain_memory.py   # Validated per-domain extraction recipes
│   │   └── *.py               # Public exports and backward-compatibility shims
│   ├── core/                  # Obscura browser and binary management
│   ├── handlers/              # Dek-D, Nekopost, and WebNovel integrations
│   ├── scraper/               # Concurrent batch scraping and output storage
│   ├── ui/                    # Textual application and reader/progress widgets
│   └── utils/                 # Logging, romanization, and title/content cleanup
├── tests/                     # Unit, integration, site-specific, and TUI tests
├── .env.example               # Environment configuration template
├── pyproject.toml             # Package metadata, dependencies, and CLI script
├── uv.lock                    # Locked dependency versions
└── README.md
```

`dist/`, `.venv/`, and cache directories are generated or local development artifacts and are not part of the source package.

---

## Testing

Run the test suite from the repository root:

```bash
uv run pytest
```

Tests cover classification and TOC extraction, chapter parser generation and review, domain recipes, platform handlers, batch scraping and storage, shared utilities, and the Textual interface. See [`tests/`](tests/) for the current test modules.

---

## License

This project is licensed under the Apache-2.0 License.

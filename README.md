# Novel Scraping Agent

An autonomous, agentic web novel scraping system powered by **Obscura** anti-detect headless browser, **Google Gemini Interactions API**, **LangChain / LangGraph**, and **Textual / Rich**.

The agent follows an **Analyze-Once, Synthesize Deterministically, Self-Heal on Failure** pattern: it leverages Gemini and LangGraph to semantically understand novel DOM structures, executes an Actor-Critic Observer review loop to guarantee clean extraction, generates deterministic BeautifulSoup parsers, caches domain extraction recipes to eliminate redundant LLM calls, handles paginated multi-page chapters, and downloads entire novels with polite concurrency.

---

## Key Features

- **Domain Memory & Recipe Cache (0-Token Fast Path)**: Automatically caches validated TOC strategies and chapter extraction plans into `recipes/<domain>.json` and standalone Python scripts (`recipes/<domain>.py`). Subsequent visits to previously analyzed domains bypass LLM calls entirely for maximum speed and cost efficiency.
- **Autonomous Table of Contents (TOC) LangGraph Agent**: Self-healing cyclic StateGraph ([`TocAgent`](file:///D:/Code/Novel_scraping_agent/src/agent/toc/agent.py)) featuring claim inspection, embedded state hydration extraction (Next.js Apollo `__NEXT_DATA__`, JSON-LD), DOM link heuristics, and strict audit loops to guarantee 100% chapter completeness without human-in-the-loop.
- **Specialized Platform Handlers**: Built-in support and bypasses for complex novel platforms including:
  - **Dek-D** ([`dekd_handler.py`](file:///D:/Code/Novel_scraping_agent/src/core/dekd_handler.py)): Native API handler and synthetic HTML generator for writer.dek-d.com & novel.dek-d.com.
  - **Nekopost** ([`nekopost_handler.py`](file:///D:/Code/Novel_scraping_agent/src/core/nekopost_handler.py)): Automated JSON project detail decoding and episode parsing.
  - **Kakuyomu**: Next.js Apollo state deserialization and WorkTocSection title cleaning.
  - **Syosetu (小説家になろう)**, **FreeWebNovel**, **EmpireNovel**, and **XSZJ**.
- **Obscura Anti-Detect Browser Engine**: Integrated Rust-based headless browser with stealth fingerprinting, Cloudflare/Turnstile/Akamai bypass, and CDP server support.
- **Gemini Interactions API & LangChain**: Native multi-turn state chaining via `previous_interaction_id` to preserve reasoning context across analysis, refinement, and self-healing turns.
- **Actor-Critic Quality Review Loop**: Generator ([`ChapterAnalyzer`](file:///D:/Code/Novel_scraping_agent/src/agent/analyzer.py)) and Critic ([`ExtractionObserver`](file:///D:/Code/Novel_scraping_agent/src/agent/observer.py)) iteratively audit sample extractions (scoring 0.0 to 1.0) to eliminate residual ads, navigation buttons, and incorrect chapter titles before batch scraping.
- **Interactive Textual TUI**: Modern terminal interface featuring:
  - **Interactive TOC Table**: Dynamic `DataTable` where clicking or pressing `Enter` on any chapter instantly switches to the preview tab and displays that chapter's parsed markdown.
  - **Automatic Tab Navigation**: Seamlessly shifts to `Live Logs` during analysis and switches to `Table of Contents` or `Chapter Preview` upon completion.
  - **Focusable Scrollable Reader**: Custom [`RichMarkdownReader`](file:///D:/Code/Novel_scraping_agent/src/ui/widgets/reader.py) inheriting from `VerticalScroll` with full keyboard arrow, PageUp/PageDown, and mouse-wheel scrolling.
  - **Generated Code View**: Displays the synthesized Python parser with Monokai syntax highlighting.
- **Paginated Multi-Page Chapter Stitching**: Seamlessly detects and stitches multi-page chapters (`下一页`, `next page`, `?page=N`) into a unified markdown chapter while stripping pagination badges (e.g. `（1/3）`) from titles.
- **Self-Healing Batch Scraper**: Resilient scraper that monitors chapter length and syntax during batch extraction, triggering Gemini with prior reasoning state to patch shifted DOM selectors on-the-fly.
- **Persistent Multi-Level File Logging**: Automatically logs all TUI and CLI sessions with ANSI-stripped plain text into `logs/tui_<timestamp>.log` and `logs/scraper_<timestamp>.log` alongside symlinked `latest` logs.
- **Universal Multi-Language Romanization**: Automatically romanizes novel folder names across Japanese (Hepburn Romaji via `pykakasi`), Chinese (Pinyin via `anyascii`), Korean (Romaja via `anyascii`), and Russian/Cyrillic (via `anyascii`).
- **Clean Raw Markdown Output**: Saves chapters as zero-padded clean markdown files (`0001 - <Title>.md`) without cluttering YAML frontmatter headers (YAML frontmatter can be optionally toggled via `INCLUDE_FRONTMATTER=true`).
- **Full Token Accounting**: Real-time tracking of prompt, completion, thought, and total tokens across all LLM operations, persisted into `metadata.json`.

---

## Architecture & Workflow Guides

For detailed technical explanations, Mermaid diagrams, and pipeline breakdowns, see:
- 📖 [Agent Workflow & Architecture Guide](docs/AGENT_WORKFLOW.md): Step-by-step lifecycle from ingestion to persistence.
- 🏗️ [Agent Architecture & Pipeline Documentation](docs/agent_architecture.md): Deep-dive into TocAgent LangGraph, ReviewLoop, Domain Memory, and batch mechanics.

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

---

## Configuration Reference

All settings can be customized in `.env` or passed as environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(Required)* | Google Gemini API key for agent reasoning and code generation |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model ID (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-3.5-flash-lite`) |
| `OBSCURA_BIN_PATH` | *(auto)* | Custom path to `obscura.exe` binary. Auto-downloads into `./bin/` if empty |
| `NAVIGATION_TIMEOUT` | `30` | Obscura page fetch timeout in seconds |
| `WAIT_UNTIL` | `networkidle` | Navigation wait condition (`networkidle`, `domcontentloaded`, `load`) |
| `OUTPUT_DIR` | `./novels` | Directory where scraped novels and metadata are saved |
| `LOGS_DIR` | `./logs` | Directory for persistent session log files (`tui_*.log`, `scraper_*.log`) |
| `RECIPES_DIR` | `./recipes` | Directory for cached domain recipes (`*.json`, `*.py`) |
| `DEFAULT_CONCURRENCY` | `3` | Default number of concurrent scraper worker tasks (clamped 1-10) |
| `MIN_DELAY_SECONDS` | `0.5` | Minimum polite jitter delay between requests (in seconds) |
| `MAX_DELAY_SECONDS` | `1.5` | Maximum polite jitter delay between requests (in seconds) |
| `INCLUDE_FRONTMATTER` | `false` | When `true`, prepends YAML frontmatter to chapter markdown files |
| `ROMANIZE_FOLDER` | `true` | When `true`, romanizes folder names to Latin script (`GuiMiZhiZhu`) |
| `MAX_REVIEW_ITERATIONS`| `3` | Maximum review iterations for the Actor-Critic Observer loop |
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
  "token_usage": {
    "prompt_tokens": 12450,
    "completion_tokens": 2890,
    "thought_tokens": 850,
    "total_tokens": 16190,
    "call_count": 3
  },
  "last_updated": "2026-09-17T12:00:00+00:00",
  "chapters": [
    {
      "index": 1,
      "title": "悪役貴族、姉妹ができる",
      "url": "https://kakuyomu.jp/works/.../episodes/...",
      "success": true
    }
  ]
}
```

---

## Project Structure

```
Novel_scraping_agent/
├── bin/                       # Automatically downloaded Obscura binaries (obscura.exe)
├── docs/
│   ├── AGENT_WORKFLOW.md      # High-level architecture & sequence diagrams
│   └── agent_architecture.md  # In-depth technical breakdown of TocAgent & ReviewLoop
├── logs/                      # Persistent session log files (tui_*.log, scraper_*.log)
├── novels/                    # Output directory for downloaded novels and metadata
├── recipes/                   # Cached domain extraction recipes (*.json, *.py)
├── src/
│   ├── config.py              # Central environment and configuration settings
│   ├── main.py                # CLI entrypoint and headless pipeline runner
│   ├── agent/                 # Agent reasoning and LLM orchestration layer
│   │   ├── analyzer.py        # DOM structure analysis and selector planning
│   │   ├── classifier.py      # TOC vs Chapter classification and smart link prioritization
│   │   ├── code_generator.py  # Deterministic Python BeautifulSoup parser synthesis
│   │   ├── domain_memory.py   # Domain recipe persistence and 0-token bypass manager
│   │   ├── interactions_model.py # Native Gemini Interactions API LangChain wrapper
│   │   ├── llm_client.py      # LLM client with LCEL pipelines and state tracking
│   │   ├── observer.py        # Actor-Critic quality reviewer and clutter auditor
│   │   ├── review_loop.py     # Generator <-> Critic review & refinement loop
│   │   ├── self_healer.py     # Runtime selector self-healing with prior interaction state
│   │   ├── token_tracker.py   # Thread-safe prompt, completion, and thought token tracking
│   │   └── toc/               # Autonomous LangGraph TOC extraction agent
│   │       ├── agent.py       # TocAgent coordinator and domain memory integration
│   │       ├── graph.py       # Cyclic StateGraph definition and node routers
│   │       ├── state.py       # TocState TypedDict definition
│   │       └── tools.py       # ClaimInspector, EmbeddedStateExtractor, DomLinkExtractor, TocAuditor
│   ├── core/                  # Headless browsing & platform handlers
│   │   ├── binary_manager.py  # Automatic Obscura binary download and verification
│   │   ├── dekd_handler.py    # Specialized Dek-D API decoder and synthetic HTML builder
│   │   ├── nekopost_handler.py# Specialized Nekopost project decoder and chapter resolver
│   │   └── obscura_client.py  # Obscura CLI subprocess driver and CDP Playwright integration
│   ├── scraper/               # Polite concurrent batch execution & persistence
│   │   ├── batch_runner.py    # Async queue, subpage stitching, pause/resume, and jitter
│   │   └── storage.py         # Markdown output formatting, frontmatter toggle, and metadata.json
│   ├── ui/                    # Textual Terminal User Interface (TUI)
│   │   ├── app.py             # Main Textual application with tabs, controls, and details
│   │   └── widgets/
│   │       ├── progress.py    # Compound progress widget with rate calculation and status
│   │       └── reader.py      # Focusable, scrollable Rich Markdown reader preview
│   └── utils/
│       ├── logger.py          # Session file logging, latest symlink, and clean markup
│       └── romanizer.py       # Multi-language romanizer (Japanese, Chinese, Korean, Russian)
├── tests/                     # Comprehensive test suite (92 tests across 16 test modules)
├── .env.example               # Complete environment variable template
├── pyproject.toml             # Project metadata, dependencies, and script entrypoints
└── README.md                  # Project overview and documentation
```

---

## Testing

The project maintains a rigorous automated test suite (**92 tests across 16 test modules**) verifying every component from network resilience to DOM heuristics and TUI interactivity:

```powershell
# Run the entire test suite:
pytest tests/
```

### Module Breakdown:

| Test Module | Coverage Area |
| :--- | :--- |
| [`test_dekd_handler.py`](file:///D:/Code/Novel_scraping_agent/tests/test_dekd_handler.py) | Dek-D URL detection, API pagination decoding, synthetic HTML construction |
| [`test_domain_memory.py`](file:///D:/Code/Novel_scraping_agent/tests/test_domain_memory.py) | Recipe saving, loading, validation, and standalone script synthesis |
| [`test_e2e.py`](file:///D:/Code/Novel_scraping_agent/tests/test_e2e.py) | End-to-end headless pipeline execution with mock local HTTP server |
| [`test_empirenovel_toc.py`](file:///D:/Code/Novel_scraping_agent/tests/test_empirenovel_toc.py) | EmpireNovel accordion unpacking, pagination, and TOC claim auditing |
| [`test_freewebnovel.py`](file:///D:/Code/Novel_scraping_agent/tests/test_freewebnovel.py) | FreeWebNovel multi-page TOC discovery and chapter index synthesis |
| [`test_kakuyomu.py`](file:///D:/Code/Novel_scraping_agent/tests/test_kakuyomu.py) | Kakuyomu Apollo state hydration, episode extraction, and title cleaning |
| [`test_logger.py`](file:///D:/Code/Novel_scraping_agent/tests/test_logger.py) | Multi-level file logging, clean markup stripping, and session persistence |
| [`test_nekopost.py`](file:///D:/Code/Novel_scraping_agent/tests/test_nekopost.py) | Nekopost project API handling, episode link mapping, and live TOC verification |
| [`test_observer.py`](file:///D:/Code/Novel_scraping_agent/tests/test_observer.py) | Actor-Critic review loop iterations, clutter auditing, and self-healing |
| [`test_romanizer.py`](file:///D:/Code/Novel_scraping_agent/tests/test_romanizer.py) | Romanization across Japanese (Romaji), Chinese (Pinyin), Korean, and Cyrillic |
| [`test_scraper.py`](file:///D:/Code/Novel_scraping_agent/tests/test_scraper.py) | Obscura binary download, batch queue jitter, and storage formatting |
| [`test_syosetu_toc.py`](file:///D:/Code/Novel_scraping_agent/tests/test_syosetu_toc.py) | Syosetu multi-page episode pagination and subtitle selector extraction |
| [`test_toc_agent.py`](file:///D:/Code/Novel_scraping_agent/tests/test_toc_agent.py) | LangGraph cyclic StateGraph, claim inspection, and audit edge routing |
| [`test_token_tracker.py`](file:///D:/Code/Novel_scraping_agent/tests/test_token_tracker.py) | Prompt, completion, and thought token tracking across LLM calls |
| [`test_tui.py`](file:///D:/Code/Novel_scraping_agent/tests/test_tui.py) | Textual app composition, TOC row selection, dynamic preview, and safe_call |
| [`test_xszj.py`](file:///D:/Code/Novel_scraping_agent/tests/test_xszj.py) | Multi-page subpage chapter stitching (`下一页`) and title cleaning |

---

## License

This project is licensed under the Apache-2.0 License.

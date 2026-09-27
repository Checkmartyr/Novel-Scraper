# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Domain Memory & Recipe Caching (`src/agent/domain_memory.py`)**:
  - Validated extraction plans and TOC strategies are now automatically persisted to `src/recipes/<domain>.json`.
  - Generates standalone, self-contained Python extractors (`src/recipes/<domain>.py`) for external or offline use.
  - Implemented 0-token fast-path bypass for known domains, skipping LLM classification and selector synthesis entirely.
- **Autonomous TocAgent with LangGraph (`src/agent/toc/`)**:
  - Implemented cyclic StateGraph workflow with `ClaimInspector`, `EmbeddedStateExtractor`, `DomLinkExtractor`, `InteractiveDomExpander`, and `TocAuditor`.
  - Added support for Next.js Apollo state hydration extraction (`__NEXT_DATA__`) and JSON-LD schema scraping.
- **Specialized Platform Handlers (`src/core/`)**:
  - `dekd_handler.py`: Added native API decoding and synthetic HTML generation for Thai novels on `writer.dek-d.com` and `novel.dek-d.com`.
  - `nekopost_handler.py`: Added JSON project detail endpoint decoding for `nekopost.net`.
- **Persistent Multi-Level Session Logging (`src/utils/logger.py`)**:
  - Structured local file logging to `logs/tui_<timestamp>.log` and `logs/scraper_<timestamp>.log`.
  - Created `tui_latest.log` and `scraper_latest.log` symlinks for easy real-time tailing.
  - Implemented `clean_markup()` to strip Textual/Rich markup tags before persisting logs.
- **Interactive TUI Enhancements (`src/ui/app.py`, `src/ui/widgets/reader.py`)**:
  - Added interactive `on_data_table_row_selected` handler to TOC `DataTable`. Clicking or selecting any chapter row immediately displays the chapter preview in `tab-preview`.
  - Added dynamic background chapter loading for non-sample chapters selected in the TOC table.
  - Upgraded `RichMarkdownReader` to inherit from `VerticalScroll` with `can_focus = True`, enabling keyboard arrow, PageUp/PageDown, and mouse-wheel scrolling.
  - Added `#chapter-preview-header` displaying the currently previewed chapter title.
  - Added automated tab switching (`tab-logs` during analysis, `tab-toc` or `tab-preview` upon completion).
  - Added Monokai syntax-highlighted Python code view in `tab-code`.
- **Comprehensive Automated Test Suite**:
  - Added test modules: `test_dekd_handler.py`, `test_domain_memory.py`, `test_empirenovel_toc.py`, `test_freewebnovel.py`, `test_logger.py`, `test_nekopost.py`, `test_syosetu_toc.py`, `test_toc_agent.py`.
  - Test coverage expanded to 92 passing automated tests across 16 test modules.

### Changed
- Refactored `safe_call()` in `src/ui/app.py` to prevent `RuntimeError: The 'call_from_thread' method must run in a different thread from the app` when invoked on the main asyncio thread.
- Single-chapter pages without detected TOC links now cleanly populate the TOC `DataTable` with a fallback row.
- Updated `ObscuraClient` with automatic private network allowance (`--allow-private-network`) for seamless local mock server testing.

### Fixed
- Fixed bug where clicking or pressing Enter on rows in the TOC table failed to display chapter previews.
- Fixed issue where single-chapter novel analysis left the TOC table completely empty.
- Fixed Windows console encoding issues when printing multi-byte CJK text by configuring standard output streams for UTF-8.
- Fixed `test_live_nekopost_toc_and_agent` test assertion to gracefully handle newly published ongoing chapters.

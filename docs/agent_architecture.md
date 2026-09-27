# 🏗️ Novel Scraping Agent — Architecture & Pipeline Documentation

> Comprehensive technical documentation of the autonomous TOC extraction and Chapter scraping pipeline.

---

## 1. High-Level System Overview

The Novel Scraping Agent is a multi-stage, LLM-powered pipeline that autonomously scrapes web novels from any website. It takes a single URL as input and outputs a complete, organized collection of chapter Markdown files.

```mermaid
flowchart LR
    A["🌐 URL Input"] --> B["🔍 PageClassifier"]
    B -->|TOC Page| C["📑 TocAgent\n(LangGraph)"]
    B -->|Chapter Page| D["🔗 Discover TOC URL"]
    D --> C
    C --> E["🔬 ReviewLoop\nOrchestrator"]
    E --> F["📥 BatchScraper\nRunner"]
    F --> G["💾 NovelStorage\n(novels/)"]

    style A fill:#4fc3f7,color:#000
    style B fill:#7c4dff,color:#fff
    style C fill:#ff7043,color:#fff
    style D fill:#ffb74d,color:#000
    style E fill:#66bb6a,color:#fff
    style F fill:#42a5f5,color:#fff
    style G fill:#ab47bc,color:#fff
```

### Key Components at a Glance

| Stage | Component | File | Purpose |
|-------|-----------|------|---------|
| 1 | **PageClassifier** | [classifier.py](file:///D:/Code/Novel_scraping_agent/src/agent/classifier.py) | Classify URL as TOC or Chapter |
| 2 | **TocAgent** | [agent.py](file:///D:/Code/Novel_scraping_agent/src/agent/toc/agent.py) | Extract complete chapter list via LangGraph |
| 3 | **ReviewLoopOrchestrator** | [review_loop.py](file:///D:/Code/Novel_scraping_agent/src/agent/review_loop.py) | Iterative Generator ↔ Observer refinement |
| 4 | **BatchScraperRunner** | [batch_runner.py](file:///D:/Code/Novel_scraping_agent/src/scraper/batch_runner.py) | Concurrent chapter downloading |
| 5 | **NovelStorage** | [storage.py](file:///D:/Code/Novel_scraping_agent/src/scraper/storage.py) | File I/O, metadata.json persistence |
| ⚡ | **DomainMemoryManager** | [domain_memory.py](file:///D:/Code/Novel_scraping_agent/src/agent/domain_memory.py) | 0-token fast-path for known domains |
| 🌐 | **ObscuraClient** | [obscura_client.py](file:///D:/Code/Novel_scraping_agent/src/core/obscura_client.py) | HTTP + headless browser fetching |
| 🧠 | **LLMClient** | [llm_client.py](file:///D:/Code/Novel_scraping_agent/src/agent/llm_client.py) | Gemini LLM via LangChain LCEL |

---

## 2. The Full Pipeline — Step by Step

```mermaid
flowchart TD
    URL["🌐 User provides URL"] --> FETCH["ObscuraClient.fetch_html()"]
    FETCH --> DM_CHECK{"Domain Memory\nRecipe exists?"}

    DM_CHECK -->|Yes ✅| DM_TOC["Test TOC recipe"]
    DM_TOC -->|Pass| DM_CH["Test Chapter recipe"]
    DM_CH -->|Pass| DM_BYPASS["⚡ Full 0-token bypass\n(No LLM calls)"]
    DM_BYPASS --> BATCH

    DM_CHECK -->|No ❌| CLASSIFY["PageClassifier.classify()"]
    DM_TOC -->|Fail| CLASSIFY
    DM_CH -->|Fail| REVIEW

    CLASSIFY -->|TOC| TOC_AGENT["TocAgent.extract_toc()\n(LangGraph State Machine)"]
    CLASSIFY -->|Chapter| DISCOVER_TOC["Find TOC link → Fetch → TocAgent"]
    DISCOVER_TOC --> TOC_AGENT

    TOC_AGENT --> REVIEW["ReviewLoopOrchestrator.run()\n(Generator ↔ Observer)"]

    REVIEW -->|Approved ✅| SAVE_RECIPE["domain_memory.save_recipe()\n→ recipes/{domain}.json\n→ recipes/{domain}.py"]
    SAVE_RECIPE --> BATCH["BatchScraperRunner.run()\n(Concurrent Download)"]
    REVIEW -->|Best Effort ⚠️| BATCH

    BATCH --> STORAGE["NovelStorage\n(novels/{title}/)"]
    STORAGE --> DONE["✅ metadata.json\n+ Chapter .md files"]

    style DM_BYPASS fill:#4caf50,color:#fff
    style CLASSIFY fill:#7c4dff,color:#fff
    style TOC_AGENT fill:#ff7043,color:#fff
    style REVIEW fill:#66bb6a,color:#fff
    style BATCH fill:#42a5f5,color:#fff
    style SAVE_RECIPE fill:#ffb74d,color:#000
```

---

## 3. Stage 1 — Page Classification

**File:** [classifier.py](file:///D:/Code/Novel_scraping_agent/src/agent/classifier.py)
**Class:** `PageClassifier`

The classifier determines whether a given URL is a **Table of Contents (TOC)** page or a **single Chapter** page.

### Classification Flow

```mermaid
flowchart TD
    HTML["Input: URL + HTML"] --> DM{"Domain Memory\nrecipe exists?"}
    DM -->|Yes| TEST_TOC["test_toc_recipe()"]
    TEST_TOC -->|chapters ≥ 1 & conf ≥ 0.8| RETURN_TOC["Return TOC\n(remembered_recipe)"]
    TEST_TOC -->|Fail| TEST_CH["test_chapter_recipe()"]
    TEST_CH -->|content found| RETURN_CH["Return CHAPTER"]
    TEST_CH -->|Fail| LLM

    DM -->|No| LLM["LLM Classification\n(Gemini via LangChain)"]
    LLM -->|Success| SANITIZE["Sanitize chapter links\n+ Apollo state merge"]
    LLM -->|Fail| HEURISTIC["_heuristic_classify()\n(Deterministic fallback)"]

    SANITIZE --> IS_TOC{"page_type == TOC?"}
    HEURISTIC --> IS_TOC

    IS_TOC -->|Yes| TOC_AGENT["TocAgent.extract_toc()\n(Refine chapter list)"]
    IS_TOC -->|No| RESULT

    TOC_AGENT --> RESULT["ClassificationResult"]
    RETURN_TOC --> RESULT
    RETURN_CH --> RESULT

    style RETURN_TOC fill:#4caf50,color:#fff
    style RETURN_CH fill:#4caf50,color:#fff
    style LLM fill:#7c4dff,color:#fff
    style HEURISTIC fill:#ffb74d,color:#000
```

### Key Data Models

```python
class ClassificationResult(BaseModel):
    page_type: str       # "TOC" or "CHAPTER"
    novel_title: str
    author: Optional[str]
    description: Optional[str]
    chapter_links: List[ChapterLink]   # Populated if TOC
    toc_url: Optional[str]             # Discovered if CHAPTER
    toc_state: Optional[dict]          # Full TocAgent state if TOC

class ChapterLink(BaseModel):
    index: int       # 1-based sequential index
    title: str       # Clean chapter title
    url: str         # Absolute URL
```

### What happens internally

1. **Domain Memory Bypass** — Checks `DomainMemoryManager` for a saved recipe. If found, tests both TOC and chapter extraction. On success, returns immediately with **zero LLM tokens**.
2. **LLM Path** — Sends a condensed page summary (title, H1s, sample links) to Gemini. The LLM returns structured JSON matching `ClassificationResult`.
3. **Apollo/Next.js Merge** — If `__NEXT_DATA__` script is present (e.g., Kakuyomu), extracts episode list from Apollo state and merges it.
4. **Heuristic Fallback** — If LLM is unavailable, uses regex pattern matching on link text/URLs to detect chapter links (≥4 links = TOC).
5. **TocAgent Refinement** — If classified as TOC, delegates to `TocAgent` for a more thorough extraction.

---

## 4. Stage 2 — TOC Extraction (LangGraph State Machine)

**Files:**
- [agent.py](file:///D:/Code/Novel_scraping_agent/src/agent/toc/agent.py) — `TocAgent` wrapper
- [graph.py](file:///D:/Code/Novel_scraping_agent/src/agent/toc/graph.py) — `TocGraphWorkflow` (LangGraph `StateGraph`)
- [state.py](file:///D:/Code/Novel_scraping_agent/src/agent/toc/state.py) — `TocState` TypedDict
- [tools.py](file:///D:/Code/Novel_scraping_agent/src/agent/toc/tools.py) — Extraction tools

### LangGraph State Machine Architecture

```mermaid
stateDiagram-v2
    [*] --> inspect_metadata
    inspect_metadata --> extract_embedded_state

    extract_embedded_state --> audit : chapters found ≥ 10\nor matches claimed count
    extract_embedded_state --> extract_dom : no/few embedded chapters

    extract_dom --> audit

    audit --> finalize : is_complete == True
    audit --> interactive_expand : has_unexpanded_sections\nor count < claimed
    audit --> crawl_pagination : has_pagination URLs
    audit --> finalize : max_iterations reached

    interactive_expand --> extract_dom : re-extract from\nenriched HTML

    crawl_pagination --> audit : re-audit with\nmerged chapters

    finalize --> [*]

    note right of inspect_metadata
        ClaimInspector: finds
        chapter count badges,
        novel title, author
    end note

    note right of extract_embedded_state
        Checks: Apollo/Redux,
        Nuxt, JSON-LD,
        Nekopost API
    end note

    note right of extract_dom
        DomLinkExtractor:
        Platform-specific CSS +
        generic semantic heuristics
    end note

    note right of audit
        TocAuditor: completeness
        verification, confidence
        scoring, gap detection
    end note

    note right of interactive_expand
        Playwright: click
        accordions, scroll,
        load-more buttons
    end note

    note right of crawl_pagination
        Concurrent HTTP fetch
        of paginated TOC pages
        (5 workers)
    end note
```

### TocState — The Graph State

```python
class TocState(TypedDict):
    url: str                           # Source TOC URL
    html: str                          # Current HTML (updated after expand/pagination)
    novel_title: str                   # Extracted novel name
    author: Optional[str]
    description: Optional[str]
    claimed_chapter_count: Optional[int]  # From badges like "全111話"
    extracted_chapters: List[ChapterLink] # Current chapter list
    extraction_strategy: str           # "embedded_state" | "dom_heuristic" | "paginated_dom"
    confidence_score: float            # 0.0 → 1.0
    has_unexpanded_sections: bool      # Collapsed accordions detected
    has_pagination: bool               # Multi-page TOC detected
    pagination_urls: List[str]         # URLs of subsequent TOC pages
    is_complete: bool                  # Auditor verdict
    iteration: int                     # Current audit loop iteration
    max_iterations: int                # Safety cap (default: 3)
    issues: List[str]                  # Auditor-reported issues
    logs: List[str]                    # Execution trace
```

### Node Details

#### 1. `inspect_metadata` — ClaimInspector
Scans HTML for chapter count badges (e.g., `"全111話"`, `"111 chapters"`), novel title from `<h1>`/meta tags, author, and description. Also detects pagination URLs.

#### 2. `extract_embedded_state` — EmbeddedStateExtractor
Extracts chapter lists from JavaScript framework state:
- **Next.js / Apollo** — `__NEXT_DATA__` script tag → `__APOLLO_STATE__` → episode unions
- **Nuxt.js** — `__NUXT_DATA__` or `window.__NUXT__`
- **JSON-LD** — `<script type="application/ld+json">`
- **Nekopost** — Custom API endpoint

#### 3. `extract_dom` — DomLinkExtractor
Extracts chapter links from the DOM using a two-tier approach:
1. **Platform-specific CSS selectors** — Kakuyomu (`a.widget-toc-episode-episodeTitle`), Syosetu (`dl.novel_sublist2 a`), etc.
2. **Generic semantic heuristic** — Finds all `<a>` tags, filters by chapter URL/text patterns, removes action buttons, sorts numerically.

#### 4. `audit` — TocAuditor (The Critic)
Evaluates extraction completeness:
- Compares `len(chapters)` vs `claimed_count`
- Detects unexpanded accordion sections
- Checks for pagination indicators
- Outputs `confidence_score` (0.0–1.0)

#### 5. `interactive_expand` — InteractiveDomExpander
Uses Playwright via Obscura to interact with the page:
- Clicks accordion toggles, "Show more" / "Load more" buttons
- Scrolls to bottom for infinite scroll
- Returns enriched HTML for re-extraction

#### 6. `crawl_pagination` — PaginatedTocCrawler
Concurrently fetches subsequent TOC pages (5 workers):
- Fast HTTP first, Obscura browser fallback
- Extracts chapters from each page via `DomLinkExtractor`
- Deduplicates, re-indexes, and handles reverse chronological ordering

#### 7. `finalize`
Deduplicates by URL, re-indexes chapters 1..N, outputs final clean list.

### Conditional Routing Logic

```mermaid
flowchart TD
    EMB{"_route_after_embedded"}
    EMB -->|"chapters ≥ claimed OR ≥ 10"| AUDIT["→ audit"]
    EMB -->|"few/no chapters"| DOM["→ extract_dom"]

    AUD{"_route_after_audit"}
    AUD -->|"has_pagination URLs"| CRAWL["→ crawl_pagination"]
    AUD -->|"is_complete == True"| FIN["→ finalize"]
    AUD -->|"iteration ≥ max"| FIN
    AUD -->|"count < claimed\nOR unexpanded"| EXPAND["→ interactive_expand"]
    AUD -->|"else"| FIN
```

---

## 5. Stage 3 — Review Loop (Generator ↔ Observer Pattern)

**File:** [review_loop.py](file:///D:/Code/Novel_scraping_agent/src/agent/review_loop.py)
**Class:** `ReviewLoopOrchestrator`

This stage determines **how** to extract chapter content (title, body text) from a single chapter page. It uses an iterative refinement loop between a **Generator** (ChapterAnalyzer) and an **Observer** (ExtractionObserver).

### Review Loop Architecture

```mermaid
flowchart TD
    START["Sample Chapter HTML"] --> DM{"Domain Memory\nchapter recipe?"}
    DM -->|"recipe verified ✅"| FAST["⚡ Return approved result\n(0 iterations, 0 tokens)"]
    DM -->|"No / Failed"| ITER_START

    subgraph loop ["Iterative Refinement Loop (max 3 iterations)"]
        ITER_START["Iteration N"] --> GEN{"Iteration == 1?"}
        GEN -->|Yes| ANALYZE["ChapterAnalyzer.analyze()\n→ DOMStructurePlan"]
        GEN -->|No| REFINE["ChapterAnalyzer.refine()\n(uses Observer feedback)"]

        ANALYZE --> CODEGEN["ChapterCodeGenerator\n.test_and_verify()"]
        REFINE --> CODEGEN

        CODEGEN --> OBSERVER["ExtractionObserver.review()\n(Quality Control Critic)"]

        OBSERVER --> CHECK{"quality ≥ 0.7 AND\nis_accurate == True?"}
        CHECK -->|"Yes ✅"| APPROVED["ReviewLoopResult\n(approved=True)"]
        CHECK -->|"No ❌"| NEXT{"iteration < max?"}
        NEXT -->|Yes| ITER_START
        NEXT -->|No| BEST["Return best-effort result\n(approved=False)"]
    end

    FAST --> OUTPUT["ReviewLoopResult"]
    APPROVED --> OUTPUT
    BEST --> OUTPUT

    style FAST fill:#4caf50,color:#fff
    style APPROVED fill:#4caf50,color:#fff
    style BEST fill:#ffb74d,color:#000
    style OBSERVER fill:#e91e63,color:#fff
```

### Sub-Components

#### ChapterAnalyzer ([analyzer.py](file:///D:/Code/Novel_scraping_agent/src/agent/analyzer.py))

| Method | Purpose |
|--------|---------|
| `analyze(html, url)` | First pass: sends DOM skeleton to LLM → returns `DOMStructurePlan` |
| `refine(html, url, plan, review)` | Subsequent passes: sends Observer feedback → returns improved plan |
| `_heuristic_analyze(html)` | LLM-free fallback using common CSS selectors |
| `_prepare_dom_skeleton(html)` | Creates compact representation with title candidates + large text containers |

```python
class DOMStructurePlan(BaseModel):
    title_selector: str          # CSS selector for chapter title heading
    content_selector: str        # CSS selector for main novel body container
    remove_selectors: List[str]  # CSS selectors for clutter (ads, nav, scripts)
    clean_paragraphs: bool       # Whether to normalize paragraphs
```

#### ChapterCodeGenerator ([code_generator.py](file:///D:/Code/Novel_scraping_agent/src/agent/code_generator.py))

| Method | Purpose |
|--------|---------|
| `generate_code_string()` | Generates standalone Python `extract_chapter(html)` function |
| `extract(html)` | Runs extraction directly → returns `ExtractedChapter` |
| `test_and_verify(html)` | Extracts + generates preview → returns `ParserVerificationResult` |

#### ExtractionObserver ([observer.py](file:///D:/Code/Novel_scraping_agent/src/agent/observer.py))

The "critic" agent that reviews extracted content quality:

| Check | What It Looks For |
|-------|-------------------|
| **Title Check** | Is the title the real chapter title, or a generic label? |
| **Clutter Check** | Did nav buttons, social widgets, or copyright leak into text? |
| **Completeness** | Does the story start and end coherently? |
| **Scoring** | Rates `quality_score` (0.0–1.0), sets `is_accurate` flag |

---

## 6. Stage 4 — Batch Downloading

**File:** [batch_runner.py](file:///D:/Code/Novel_scraping_agent/src/scraper/batch_runner.py)
**Class:** `BatchScraperRunner`

```mermaid
flowchart TD
    CHAPTERS["Chapter List\n(ChapterLink[])"] --> SEM["Semaphore\n(concurrency cap)"]

    subgraph workers ["Concurrent Workers"]
        SEM --> W1["Worker 1"]
        SEM --> W2["Worker 2"]
        SEM --> WN["Worker N"]
    end

    W1 --> FETCH_CH["ObscuraClient.fetch_html()"]
    FETCH_CH --> EXTRACT["ChapterCodeGenerator.extract()"]
    EXTRACT --> SUCCESS{"success?"}
    SUCCESS -->|Yes| STITCH["_stitch_subpages()\n(multi-page chapters)"]
    SUCCESS -->|No| HEAL["SelfHealer.attempt_heal()"]
    HEAL -->|Recovered| STITCH
    HEAL -->|Failed| SAVE_ERR["Save placeholder"]

    STITCH --> SAVE["NovelStorage.save_chapter()\n→ 0001 - Title.md"]
    SAVE_ERR --> PROGRESS["Report progress"]
    SAVE --> PROGRESS

    PROGRESS --> META["NovelStorage.save_metadata()\n→ metadata.json"]

    style HEAL fill:#ff9800,color:#000
    style STITCH fill:#26c6da,color:#000
```

### Key Features

| Feature | Description |
|---------|-------------|
| **Polite Jitter** | Random delay between `MIN_DELAY` and `MAX_DELAY` per request |
| **HTTP 429 Backoff** | Exponential backoff on rate limit responses |
| **Self-Healing** | If extraction fails, `SelfHealer` re-analyzes DOM + Observer reviews |
| **Sub-page Stitching** | Detects `?page=2` / `下一页` links, fetches and concatenates content |
| **Pause/Resume/Cancel** | Supports user control via `asyncio.Event` |
| **Progress Tracking** | Reports chapters/minute speed to UI callback |

---

## 7. Self-Healing Engine

**File:** [self_healer.py](file:///D:/Code/Novel_scraping_agent/src/agent/self_healer.py)
**Class:** `SelfHealer`

Triggered during batch download when a chapter's HTML layout differs from the expected plan.

```mermaid
flowchart TD
    FAIL["Extraction failed\non chapter HTML"] --> REANALYZE["ChapterAnalyzer.analyze()\n(fresh DOM analysis)"]
    REANALYZE --> TEST["ChapterCodeGenerator\n.test_and_verify()"]
    TEST --> CHECK{"success?"}
    CHECK -->|No| GIVE_UP["Return original plan\n(save placeholder)"]
    CHECK -->|Yes| OBSERVER["ExtractionObserver.review()"]
    OBSERVER --> ACCURATE{"is_accurate?"}
    ACCURATE -->|Yes ✅| HEALED["Return new plan\n(update global plan)"]
    ACCURATE -->|No| REFINE_PLAN["ChapterAnalyzer.refine()\n(Observer-guided)"]
    REFINE_PLAN --> RETEST["Re-extract with\nrefined plan"]
    RETEST --> HEALED_2{"success?"}
    HEALED_2 -->|Yes| HEALED
    HEALED_2 -->|No| GIVE_UP

    style HEALED fill:#4caf50,color:#fff
    style GIVE_UP fill:#f44336,color:#fff
```

---

## 8. Domain Memory & Recipe System (0-Token Fast Path)

**File:** [domain_memory.py](file:///D:/Code/Novel_scraping_agent/src/agent/domain_memory.py)
**Class:** `DomainMemoryManager`

After successfully scraping a website for the first time, the system saves a **domain recipe** — both as structured JSON and as a standalone Python script. On subsequent scrapes from the same domain, the recipe enables **instant extraction with zero LLM calls**.

### 3-Stage Bypass Architecture

```mermaid
flowchart TD
    URL["New URL from same domain"] --> S1{"Stage 1: PageClassifier\nrecipe exists?"}

    S1 -->|Yes| T1["test_toc_recipe()\ntest_chapter_recipe()"]
    T1 -->|Both pass ✅| BYPASS1["⚡ Skip LLM classification"]
    T1 -->|Fail ❌| LLM1["Run normal LLM classification"]

    BYPASS1 --> S2{"Stage 2: TocAgent\nrecipe exists?"}
    LLM1 --> S2

    S2 -->|Yes| T2["test_toc_recipe() + TocAuditor"]
    T2 -->|conf ≥ 0.8 ✅| BYPASS2["⚡ Skip LangGraph\nstate machine"]
    T2 -->|Fail ❌| LANGGRAPH["Run full LangGraph\nworkflow"]

    BYPASS2 --> S3{"Stage 3: ReviewLoop\nchapter recipe?"}
    LANGGRAPH --> S3

    S3 -->|Yes| T3["test_chapter_recipe()"]
    T3 -->|verified ✅| BYPASS3["⚡ Skip Observer loop\n(0 iterations)"]
    T3 -->|Fail ❌| OBSERVER["Run Generator ↔\nObserver loop"]

    BYPASS3 --> BATCH["BatchScraperRunner"]
    OBSERVER --> BATCH

    style BYPASS1 fill:#4caf50,color:#fff
    style BYPASS2 fill:#4caf50,color:#fff
    style BYPASS3 fill:#4caf50,color:#fff
```

### Recipe File Structure

```
recipes/
├── freewebnovel.com.json    # Structured recipe metadata
└── freewebnovel.com.py      # Standalone executable script
```

#### JSON Recipe (`DomainRecipe`)
```json
{
  "domain": "freewebnovel.com",
  "created_at": "2026-09-17T00:00:00+00:00",
  "updated_at": "2026-09-17T00:00:00+00:00",
  "sample_toc_url": "https://freewebnovel.com/the-novel/...",
  "sample_chapter_url": "https://freewebnovel.com/the-novel/chapter-1.html",
  "toc_config": {
    "strategy": "dom_heuristic",
    "link_selector": "a[href]",
    "container_selector": null
  },
  "chapter_config": {
    "title_selector": ".chapter-title",
    "content_selector": "#chapter-content",
    "remove_selectors": ["script", "style", ".ad", "nav"],
    "quality_score": 0.95
  },
  "times_used": 5,
  "last_used_at": "2026-09-17T08:00:00+00:00"
}
```

#### Python Script (Standalone CLI)
The generated `.py` file contains two functions:
- `extract_toc(html, base_url)` → returns `list[dict]` of chapters
- `extract_chapter(html)` → returns `dict` with title, content, word_count

Can be run independently: `python recipes/freewebnovel.com.py --toc <url>`

---

## 9. Infrastructure Layer

### ObscuraClient ([obscura_client.py](file:///D:/Code/Novel_scraping_agent/src/core/obscura_client.py))

The HTTP and browser fetching layer with a 3-tier strategy:

```mermaid
flowchart TD
    REQ["fetch_html(url)"] --> NEKO{"Is Nekopost URL?"}
    NEKO -->|Yes| API["Direct API handler"]
    NEKO -->|No| HTTP["Fast HTTP via httpx\n(with cached cookies/UA)"]

    HTTP -->|"200 + len > 800\n+ no CF challenge"| DONE["✅ Return HTML"]
    HTTP -->|"429 Rate Limit"| BACKOFF["Exponential backoff\n(up to 3 retries)"]
    BACKOFF --> HTTP
    HTTP -->|"Fail / short / CF"| BROWSER["Playwright via\nObscura CDP"]

    API -->|Success| DONE
    API -->|Fail| BROWSER

    BROWSER -->|"Success + no CF"| DONE
    BROWSER -->|"Cloudflare Turnstile"| CHROME["System Chrome\n(headed, no automation flags)"]
    CHROME --> DONE

    style DONE fill:#4caf50,color:#fff
    style CHROME fill:#ff9800,color:#000
```

| Feature | Details |
|---------|---------|
| **Ad Blocking** | Routes matching `doubleclick|taboola|outbrain|...` are aborted |
| **CDP Connection** | Connects to Obscura via `ws://127.0.0.1:9222` WebSocket |
| **Cloudflare Bypass** | Falls back to headed system Chrome with anti-automation flags |
| **Cookie Caching** | Saves `cf_clearance` cookies for subsequent HTTP requests |

### LLMClient ([llm_client.py](file:///D:/Code/Novel_scraping_agent/src/agent/llm_client.py))

| Method | Purpose |
|--------|---------|
| `generate_json(prompt, schema)` | LangChain LCEL: `prompt \| chat_model \| PydanticOutputParser` |
| `generate_text(prompt)` | Raw text generation via Gemini |
| `get_chat_model(previous_id)` | Returns `ChatGeminiInteractions` with Interaction chaining |

Uses the **Gemini Interactions API** for multi-turn context persistence without resending full conversation history.

---

## 10. Entry Points

### CLI Headless Mode ([main.py](file:///D:/Code/Novel_scraping_agent/src/main.py))

```bash
python -m src.main --url "https://example.com/novel" --auto --concurrency 3
```

### Textual TUI ([app.py](file:///D:/Code/Novel_scraping_agent/src/ui/app.py))

```bash
python -m src.main --url "https://example.com/novel"
```

Both modes execute the same pipeline:
1. `ObscuraClient.fetch_html()` → Get page HTML
2. `PageClassifier.classify()` → Determine page type
3. `TocAgent.extract_toc()` → Extract all chapters
4. `NovelStorage.save_metadata()` → Create novel folder + initial metadata.json
5. `ReviewLoopOrchestrator.run()` → Determine extraction selectors
6. `domain_memory.save_recipe()` → Persist recipe for future use
7. `BatchScraperRunner.run()` → Download all chapters
8. `NovelStorage.save_metadata()` → Update final metadata.json

---

## 11. Output File Structure

```
novels/
└── The Harem System Rewards Me For Everything/
    ├── metadata.json          # Novel metadata + chapter index
    ├── 0001 - Chapter 1.md    # Chapter markdown files
    ├── 0002 - Chapter 2.md
    ├── ...
    └── 0027 - Chapter 27.md

recipes/
├── freewebnovel.com.json      # Saved domain recipe
└── freewebnovel.com.py        # Standalone Python scraper
```

---

## 12. Complete Data Flow Sequence Diagram

```mermaid
sequenceDiagram
    actor User
    participant Main as main.py
    participant Obscura as ObscuraClient
    participant DM as DomainMemory
    participant Classifier as PageClassifier
    participant TocAgent as TocAgent (LangGraph)
    participant ReviewLoop as ReviewLoopOrchestrator
    participant Analyzer as ChapterAnalyzer
    participant CodeGen as ChapterCodeGenerator
    participant Observer as ExtractionObserver
    participant Batch as BatchScraperRunner
    participant Storage as NovelStorage
    participant LLM as Gemini LLM

    User->>Main: provide URL
    Main->>Obscura: fetch_html(url)
    Obscura-->>Main: HTML

    Main->>Classifier: classify(url, html)
    Classifier->>DM: get_recipe(url)

    alt Recipe exists & verified
        DM-->>Classifier: recipe ✅
        Classifier-->>Main: ClassificationResult (fast-path)
    else No recipe
        DM-->>Classifier: None
        Classifier->>LLM: classify page type
        LLM-->>Classifier: TOC/CHAPTER
        Classifier->>TocAgent: extract_toc(url, html)
        TocAgent->>TocAgent: LangGraph: inspect → embed → DOM → audit → finalize
        TocAgent-->>Classifier: TocState (chapters)
        Classifier-->>Main: ClassificationResult
    end

    Main->>Storage: save_metadata() (initial)

    Main->>ReviewLoop: run(sample_html, sample_url)
    ReviewLoop->>DM: check chapter recipe

    alt Chapter recipe verified
        DM-->>ReviewLoop: verified ✅ (0 iterations)
    else No recipe
        loop max 3 iterations
            ReviewLoop->>Analyzer: analyze/refine(html)
            Analyzer->>LLM: generate DOMStructurePlan
            LLM-->>Analyzer: plan
            ReviewLoop->>CodeGen: test_and_verify(html)
            CodeGen-->>ReviewLoop: verification
            ReviewLoop->>Observer: review(plan, verification)
            Observer->>LLM: critique extraction
            LLM-->>Observer: ExtractionReview
            Observer-->>ReviewLoop: review
        end
    end

    ReviewLoop-->>Main: ReviewLoopResult (plan)

    Main->>DM: save_recipe(domain, plan)

    Main->>Batch: run(chapters, plan)
    loop for each chapter
        Batch->>Obscura: fetch_html(ch.url)
        Obscura-->>Batch: HTML
        Batch->>CodeGen: extract(html)
        alt Extraction failed
            Batch->>Batch: SelfHealer.attempt_heal()
        end
        Batch->>Storage: save_chapter() → .md file
    end
    Batch->>Storage: save_metadata() (final)
    Batch-->>Main: summary

    Main-->>User: ✅ Done!
```

---

## 13. Technology Stack

| Layer | Technology |
|-------|-----------|
| **LLM** | Google Gemini (via Interactions API) |
| **LLM Framework** | LangChain LCEL (prompt \| model \| parser) |
| **State Machine** | LangGraph (`StateGraph`) |
| **Browser Automation** | Playwright + Obscura (Chromium CDP) |
| **HTTP Client** | httpx (async) |
| **HTML Parsing** | BeautifulSoup4 + lxml |
| **Data Models** | Pydantic v2 |
| **TUI** | Textual (Rich-based) |
| **CLI** | argparse + Rich Console |
| **Async Runtime** | asyncio |
| **File I/O** | aiofiles |
| **Testing** | pytest + pytest-asyncio |

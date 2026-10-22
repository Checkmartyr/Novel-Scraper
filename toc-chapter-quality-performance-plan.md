# Plan: Improve TOC and Chapter Extraction

## Summary

Implement TOC cross-checking and gap repair alongside shared page analysis first. After those pass an integration gate, add reusable API recipes and explicit incremental refresh.

Confirmed choices: refresh is explicit; ordinary download and resume remain compatible; refresh checks selected chapters; overwrite is required to replace existing files; locally edited files are protected. API recipes support observed GET requests and verified read-only POST/GraphQL queries. Reuse installed dependencies and existing recovery budgets.

## Implementation contracts

### TOC reconciliation

- Retain observations from DOM listings, embedded state, observed API responses, and witnessed chapter navigation, including provenance, original labels, volume, and order.
- Reconcile evidence before declaring completeness, including recipe fast paths. A claimed count or ten embedded entries does not skip DOM cross-checking.
- Separate linked-entry totals from chapter numbers, ranges, decimals, and interludes. Number gaps do not justify inventing URLs.
- Preserve pending and failed discovery requests through audit/finalization. Repair discrepancies using observed pagination, controls, and chapter links.
- Preserve unresolved conflicts and access failures; catalog coverage and selected-chapter verification are separate requirements.

### Shared page analysis

- Introduce one `PageAnalysis` per response snapshot, holding an untouched Beautiful Soup/lxml tree and reusable selector results/candidate statistics.
- Pass it through classification, TOC tools, chapter ranking, extraction, verification, and fingerprinting via optional internal arguments; keep HTML-string interfaces working.
- Reuse traversal results instead of serializing/reparsing subtrees. Cleaning uses isolated copies or non-mutating exclusion views; source evidence remains independent of proposed removals.
- Bound the runtime working set to 12 snapshots. Never persist parsed trees. Maintain runtime/generated-parser parity.

### Reusable API recipes

- Capture request method, exact endpoint, relevant body/query bindings, response mappings, and originating page evidence using Playwright network APIs.
- Persist sanitized recipe structures only; resolve credentials from the current session. Never persist authentication headers, cookies, tokens, or secret-bearing request values.
- Permit GET, trusted-handler-declared read-only REST POST, and a conservatively validated single GraphQL query. Reject mutations, subscriptions, batching, fragments, multiple operations, and opaque persisted-only queries; retain browser fallback.
- Support plain text, ordered paragraph arrays, and validated HTML chapter fields without flattening prose or incorrectly escaping HTML.
- Promote recipes only after novel identity and existing TOC/chapter quality checks pass. Preserve handler precedence and record actual API provenance.

### Incremental refresh

- Add keyword-only `refresh=False` and optional inspection `state_dir`, plus `--refresh` on worker inspection/extraction and headless CLI. Forward overwrite consistently.
- Persist response validators, catalog dependencies, discovery frontiers, and evidence generations under the existing state directory.
- Apply HTTP conditional GET semantics. A root 304 does not establish freshness of later catalog pages or chapter continuations: revalidate every contributing dependency.
- Partition cached responses by exact request, session, and representation. Honor Vary and Cache-Control; corrupt/unmatched entries trigger normal retrieval. POST and browser results require actual re-observation.
- Limit project cache to 256 MiB and stored responses to 2 MB. Oversized responses remain extractable; eviction only causes refetching.
- Resolve explicit selections against their supplied inspection/job snapshot before refresh, then retain canonical source identities. “All chapters” selects the current catalog.
- Keep existing source-to-filename ownership; do not delete or renumber files. Newly inserted sources with occupied indices produce an explicit conflict.
- Compare semantic title/prose digests separately from file hashes. Without overwrite, source changes produce `source_update_available`; local edits produce `local_modified`. Both preserve files and make the run partial. With overwrite, update only unchanged, source-owned files atomically.
- Resume interrupted refreshes within their evidence generation; each new refresh revalidates prior observations.

## Dependency-aware tasks

| ID | Depends on | Location | Description | Validation |
|---|---|---|---|---|
| **T1** | `[]` | `tests`, existing benchmark modules | Save this reviewed plan; establish offline baseline, independent fixtures, parsing/request counters. | Existing suite baseline recorded; gold expectations independent of extractor output. |
| **T2** | `[T1]` | New `src/core/page_analysis.py`; chapter subsystem | Implement shared analysis and integrate chapter ranking, extraction, verification. | Reduced parsing work; unchanged text, endings, generated-parser behavior; no mutation leakage. |
| **T3** | `[T1]` | `src/agent/toc` | Implement evidence reconciliation, targeted repair, retained discovery frontiers. Keep shared-analysis wiring for T4. | Conflicts, missing pages, ranges, volumes, navigation loops, and warm recipes behave correctly. |
| **T4** | `[T2, T3]` | Shared callers, including `src/api_bridge.py` | Wire shared analysis through TOC/classifier/retrieval/fingerprinting; update role contracts and caller checks. Freeze request-aware frontier contracts for the next phase. | Full offline suite and phase-one benchmark pass. Gate before API/cache work. |
| **T5** | `[T4]` | `src/core` | Implement bounded response caching and conditional GET transport. | Correct 200/304, metadata merge, session isolation, corruption recovery, concurrent publication. |
| **T6** | `[T4]` | TOC discovery, domain memory, new API replay helper | Implement observed API recipes and deterministic replay. Defer retrieval-client wiring to T7 to avoid T5 overlap. | GET/POST/query fixtures, cursor progress, schema/identity rejection, representation fidelity, secret exclusion. |
| **T7** | `[T5, T6]` | Retrieval and extraction integration | Connect API capture/replay, recipe promotion, provenance, caching, and normal auditing. | Warm API reuse preserves quality; invalid recipes fall back; POST cursors remain distinct requests. |
| **T8** | `[T7]` | Worker/CLI, batch runner, storage, checkpoints | Add explicit refresh, dependency revalidation, stable selections, file-conflict handling, refresh recovery. | Changed sources, local edits, inserted chapters, split chapters, interrupted refresh follow contracts. |
| **T9** | `[T8]` | Tests, benchmarks, instructions, documentation | Run final validation and document interfaces, measured improvements, limitations. | Full suite passes; benchmark comparison meets acceptance criteria. |

Execution waves: **T1 → (T2 ∥ T3) → T4 → (T5 ∥ T6) → T7 → T8 → T9**.

## Test and acceptance criteria

- TOC fixtures cover conflicting sources, equal counts with different entries, failed pagination, virtualized listings, combined chapters, and unrelated links.
- Chapter fixtures cover mixed inline/block prose, repeated passages, missing endings, destructive removal proposals, and multi-page continuations.
- API fixtures cover GraphQL HTTP-200 errors/partial data, missing/repeated cursors, expired sessions, changed schemas, and unsupported query syntax.
- Refresh fixtures cover root 304 with changed descendants, missing cached bodies, deleted sources, selection stability, local modifications, index collisions, and interrupted writes.
- Run `uv run pytest` and the existing offline benchmark/replay harness.
- Require zero false completions, preserved chapter fidelity, and no catalog precision/recall regressions. Verify fewer parse builds and reduced unchanged-response transfer deterministically; report timing separately.
- Live acceptance remains opt-in through the existing environment contract and authorized URLs.

## Assumptions and delivery

- Changes stay in this repository; no parent web-app changes, translation, OCR, or new refresh UI.
- Existing recipe JSON and worker result/status protocols remain compatible through additive fields.
- Begin with a writable test/cache directory; the planning test run did not complete.

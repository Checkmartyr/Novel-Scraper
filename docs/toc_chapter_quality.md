# TOC and chapter quality improvements

This change improves discovery coverage, reduces repeated chapter-page parsing, and adds an explicit source refresh path.

## Shared page analysis

`PageAnalysis` owns one parsed tree for a response snapshot and memoizes selector results and candidate statistics. `PageAnalysisCache` bounds the runtime working set to 12 snapshots. Internal agent entry points accept `page_analysis=` while their HTML-string interfaces remain available. Parsed trees are runtime-only and are not serialized.

## TOC evidence

TOC completion compares embedded data, live DOM links, pagination, expanded controls, and witnessed navigation. Evidence retains labels, order, volume, and source provenance. A saved recipe must match current DOM URLs before it can use the fast path. Failed or pending pagination requests remain visible in the final audit. Number gaps are evidence for review, never a source of invented URLs.

## Explicit refresh

The worker API supports `inspect_novel_url(url, *, refresh=False, state_dir=None)` and `extract_novel_chapters(..., refresh=False)`. Worker inspection and extraction commands accept `--refresh`; extraction preserves `--overwrite`. The headless CLI also accepts `--refresh` and `--overwrite`.

Refresh revalidates GET responses through conditional requests when a state directory is supplied. It refreshes the catalog and selected chapter source URLs. Explicit selections stay tied to the supplied inspection snapshot; an unfiltered run uses the refreshed catalog. A persisted job generation lets an interrupted refresh resume already verified chapters; a later refresh starts a new generation and revalidates them. Existing chapter filenames remain tied to their source receipts. A changed source without overwrite returns `source_update_available`; a locally edited file returns `local_modified`, even when overwrite is enabled. Overwrite replaces only an unchanged source-owned file. Semantic title/prose digests are stored separately from the output-file hash.

## API recipes

API recipes are additive domain-memory data. Captured exchanges retain safe request shape, response mappings, representation, and page evidence. Credentials are resolved from the current session and excluded from recipes. GET requests are eligible; POST requires an explicit handler declaration that the exact request is read-only. GraphQL query validation rejects mutations, subscriptions, fragments, batching, and persisted-only requests. Cursor pagination retains method and request body in the additive `pagination_requests` frontier. TOC recipes must agree with current DOM URLs; chapter recipes are replayed through the ordinary extraction verifier and fall back to HTML when their mapping or identity is stale. API provenance is recorded on successful outcomes.

## Offline benchmark

The final synthetic benchmark compared 12 runs against an untouched `HEAD` snapshot on the same fixture corpus. Both runs had 100% success and zero false completions. Mean elapsed time was 0.3171 seconds at baseline and 0.2376 seconds after implementation (about 25% lower). Shared-analysis tests also assert one tree build per snapshot and selector-result reuse.

The frozen-capture replay compared the current build with the repository's prior replay report across 16 measured runs (four sites, cold/warm recipes, four recovery ceilings). Catalog precision/recall and chapter precision/recall matched the prior report exactly; both reports had zero false completions. The replay's success rate remains 0% because its frozen fixtures continue to fail existing chapter-boundary/access checks; this change did not improve those outcomes. One site remains unmeasured because its independent annotations are not reviewed. Synthetic timing is not a live-site performance guarantee, and API replay/cache transfer savings are not included in the timing comparison.

The complete reviewed implementation plan is in [`toc-chapter-quality-performance-plan.md`](../toc-chapter-quality-performance-plan.md).

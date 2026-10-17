# Completing the research sequence

See the [consolidated result summary](benchmarks/research-v2-summary.json) for
the final counts, source fingerprint, call usage, and unresolved failures.

The implementation retains the original synthetic regression corpus and adds a
separate protocol for frozen website evidence and isolated live acceptance.
Results from synthetic layouts, captured websites, live retrieval, and visual
control trials are reported separately.

## Recovery order and evidence

The chapter coordinator verifies saved recipes, evaluates deterministic DOM
candidates, refreshes browser evidence, and then permits model selection and
contextual repair. The batch healer uses the same approval path. Parser execution
uses its explicit plan; selecting a different container requires another audited
candidate. A short prologue is valid without a minimum character requirement.

Independent evidence examines adjacent prose and the surrounding article before
proposed removal selectors. Another chapter heading or article forms a boundary.
Saved signatures include semantic attributes, nearby headings, ancestor context,
and paragraph distribution. Old recipes remain readable.

Ranking identical HTML and structural signatures uses a 12-entry bounded cache;
callers receive independent copies. Source changes invalidate the lookup. The
source audit also rejects extra prose outside its confirmed chapter boundary.
Known advertisement containers are excluded by shared structural rules in both
the parser and source audit. A live regression showed that an adjacent banner's
label could otherwise be mistaken for a repeated chapter ending; the minimized
offline test and the original captured samples now pass without weakening order
or ending checks.

Inspection and extraction add `metrics.recovery_stages` without changing existing
result fields or call signatures. Each event includes its stage, attempt count,
elapsed seconds, outcome, reason, and newly verified items. Repeated verification
of the same item within one job does not count as new progress. DOM discovery is
reported separately from verification. Model proposals await deterministic audit.

An ambiguous landing page cannot become a complete one-chapter novel simply
because it has no previous/next links. Rendering and paginated observations
refresh job-local scope evidence. Fetch history retains unsuccessful response
statuses, redirects, transport, and errors; status zero means no status was
observed. Capture indexes persist only selected non-sensitive response headers.

## Frozen website protocol

Raw HTML and complete chapter gold text stay under ignored `.evaluation/`.
[The manifest](benchmarks/evaluation-manifest.json) records source URLs, catalog
entries, independently selected boundaries, capture/text hashes, annotation
methods, and unavailable samples. Gold expectations are authored from publisher
source regions, independently of application parser output. These are agent
annotations, not a separately commissioned human-labelled dataset.

Every referenced file is checked against its stored-byte hash before replay;
paths must remain inside the capture root. Unreviewed annotations, missing first,
middle, or last text, and modified captures remain explicitly unmeasured. The
Wandering Inn's final selected entry requires Patreon access; no complete text
is inferred from its access notice. Some native browser annotations timed out;
the manifest identifies independent frozen-HTTP paragraph annotations used
instead. Raw source files were preserved when correcting Windows newline hashes.

Run from the repository root with Python asyncio localhost access permitted:

```powershell
$env:NOVEL_SCRAPER_RECOVERY_MODE = 'maximum'
uv run python -m src.scraper.evaluation replay --manifest docs/benchmarks/evaluation-manifest.json --root .evaluation/captures --output .evaluation/replay.json
```

Replay compares deterministic, browser, model, and repair ceilings against the
same evidence, each with separate cold/warm recipe directories. `--llm` enables
configured-provider calls; without it, model effectiveness is unmeasured. A
capture containing only HTML views cannot replay an unrecorded interactive
transition or network response. Such recovery remains incomplete, rather than
using live traffic during replay. Browser launch/render metrics are simulated in
replay and actual process counters in live runs.

Capture additional supplied pages only with external acceptance enabled:

```powershell
$env:NOVEL_SCRAPER_LIVE_TESTS = '1'
uv run python -m src.scraper.evaluation capture --root .evaluation/new-captures --readiness catalog --url https://example.com/catalog
```

Prepare an independent catalog annotation and first/middle/last text annotations
before marking a site's `annotation_status` as `reviewed`. Capture files alone
are not gold. The committed manifest deliberately references ignored local data;
another checkout must supply the corresponding files to reproduce these results.

## Live acceptance and model budget

[The live matrix](benchmarks/live-evaluation-matrix.json) initially retained six
development sites and reserved Royal Road, The Wandering Inn, and BC Novels.
After the initial evaluation, runtime measurements on Royal Road and BC Novels
guided the ranking cache, so both now belong to development coverage. The
Wandering Inn remains reserved, with an inaccessible last sample. These results
do not establish a successful unseen-site extraction rate. The initial source
freeze and pre-cache reports are retained alongside subsequent revisions.

```powershell
$env:NOVEL_SCRAPER_LIVE_TESTS = '1'
$env:NOVEL_SCRAPER_RECOVERY_MODE = 'maximum'
uv run python -m src.scraper.evaluation live --llm --manifest docs/benchmarks/live-evaluation-matrix.json --root .evaluation/live --output .evaluation/live-results.json
uv run python -m src.scraper.evaluation visual --llm --root .evaluation/visual --output .evaluation/visual-results.json
```

Each live site runs in a separate worker with isolated recipes, output, and
checkpoints. The outer timeout is four discovery budgets plus cleanup and ten
seconds of process overhead. Partial inspection is persisted before sample
validation. On expiry, only that worker and its descendants are terminated;
partial results remain available. The default application recovery profile is
unchanged. The recorded live run used maximum recovery with an explicit
180-second discovery override.

Configured evaluation calls share `.evaluation/model-budget.json` across replay,
live, and visual commands. The ledger permits at most 30 application model
requests, including at most six screenshot requests. Failed requests consume the
budget. Ordinary application jobs do not use this evaluation ledger. Keep the
ledger for the entire evaluation; starting a new experiment requires a new
explicit `--budget-file`. Model credentials, image support, or exhausted budgets
must be reported as unavailable/unmeasured when applicable.

Visual trials compare identical synthetic controls using DOM-only evidence and
optional screenshot assistance. Screenshot suggestions select witnessed controls;
chapter text still comes from DOM source. The first trial encountered browser
content-initialization timeouts and therefore established no visual accuracy.
After the harness initialized its synthetic DOM directly, DOM-only selection
failed on both fixtures and screenshot assistance succeeded on both. This retry
used six model requests, including two screenshots. The result concerns two
synthetic controls, not general website or chapter extraction accuracy.

## Reading the reports

- [Pre-change baseline](benchmarks/research-v2-baseline.json) and
  [current synthetic results](benchmarks/research-v2-current.json): both 12/12
  successful cold/warm runs with zero false completions. The earlier 10/12 result
  belongs to the previous implementation and is preserved separately.
- [Source freeze](benchmarks/research-v2-freeze.json): source fingerprint, offline
  suite result, reserved domains, and evaluation limits.
- [Captured-site stage comparison](benchmarks/research-v2-captured.json): gold
  precision/recall, titles, endings, publication, failures, and per-stage events.
- [Visual trial](benchmarks/research-v2-visual.json): per-fixture outcomes and
  actual model/screenshot counts; initialization failures are unmeasured.
- [Corrected visual trial](benchmarks/research-v2-visual-retry.json): measured
  DOM-only 0/2 and screenshot-assisted 2/2 control recovery.
- [Initial live matrix](benchmarks/research-v2-live.json): 2/9 successful sites.
  NovelBin discovered 133 entries and Syosetu discovered 2,649; both published
  first/middle/last samples. The seven other sites remain incomplete or blocked.
- [Initial captured-site results](benchmarks/research-v2-captured-initial.json)
  and [profiling evidence](benchmarks/research-v2-profile.json): 0/16 successful
  cold/warm stage runs, zero false completions, and a repeated ranking bottleneck.
- [Cache measurement](benchmarks/research-v2-cache.json): identical candidate
  results; one measured uncached call took 1.69 seconds and its warm repeat
  approximately 0.0001 seconds. This is not an end-to-end speed guarantee.
- [Configured-model replay](benchmarks/research-v2-captured-model.json) records
  whether model stages were reached. Zero calls cannot establish model efficacy.
- [Final NovelBin acceptance](benchmarks/research-v2-novelbin-final.json): the final
  advertisement fix retains all 133 catalog entries and three saved samples.
  [The diagnosis](benchmarks/research-v2-ad-boundary.json) records the original
  failure and the cache/order/ad-container probes.

The final offline suite passed 211 tests, with ten external tests skipped by
default. Historical reports carry their own source fingerprints; the final
source freeze identifies the current implementation.

The broader cases expose remaining extraction defects: catalog controls outside
the relevant listing, extra same-novel links, misread count metadata, and chapter
cleanup/title disagreements. WordPress also has parked migration targets,
EmpireNovel presented an access challenge, and the final Wandering Inn sample
requires Patreon access. Missing captured URLs and unrecorded browser transitions
are replay evidence gaps. Each failure remains in the reports; no site was made
an expected-success case simply because its error was reported correctly.

A complete extraction requires full catalog coverage and verified selected
chapters. Expected access blockers and correctly reported errors are failures.
Live pipeline completion without independent gold cannot establish text precision
or catalog recall; use the corresponding frozen-evidence report for those claims.
The source auditor remains heuristic and is evaluated against the independent
gold, rather than being treated as ground truth itself.

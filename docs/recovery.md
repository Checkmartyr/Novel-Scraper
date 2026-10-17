# Recovery on unfamiliar websites

Recovery escalates through existing recipes, structured data and DOM extraction,
browser/network evidence, model-selected grounded candidates, and contextual
selector repair. Approval still requires the existing quality threshold. Raw
files retain atomic publication, source receipts, and per-chapter outcomes.

## Retrieval and novel identity

`ObscuraClient.fetch_html()` still returns a string. Its internal `fetch_records`
mapping records the requested URL, final URL, status, response headers, redirect
chain, and transport. API inspection retains the requested `url` and adds the
resolved `final_url` and job-local `source_scope` evidence.

Scope is stored in a ContextVar for the active job, rather than a global domain
allowlist. Cross-domain catalog links require witnessed chapter labels and
matching headings plus reciprocal catalog backlinks on first, middle, and last
witnesses. Admission applies to the exact URLs listed by that catalog. A redirect
or verified backlink can establish the corresponding catalog URL. Unverified
foreign chapter links keep the result incomplete. Recommendations, comments,
and global navigation are excluded from candidate collection.

WordPress recovery follows the current document's advertised JSON resource in
HTML or a Link header. It does not guess endpoints or enumerate all posts on a
publisher's site. The fallback supplies the existing HTML parser with the
advertised document's rendered content.

## Chapter fidelity and repair

Candidate ranking combines prose amount, paragraph continuity, link density,
semantic attributes, and container context. The model receives a bounded set
with beginning and ending evidence. On rejection, the review loop tries other
observed regions and skips equivalent extraction outputs.

Verification independently selects a source region before applying a plan's
removal selectors. Missing prose, missing endings, changed order, and duplicated
blocks are explicit coverage defects. An incomplete plan cannot hide its own
omissions by removing them from the evidence. Saved approved chapter recipes
include a compact structural signature for contextual recovery after changes.

Inspection reports `linked_entry_count`, `highest_chapter_number`, and
`numbered_chapter_ranges` separately. A Chapter 11–12 post is one linked entry
with an upper chapter label of 12. Chapter-range links are not interpreted as
accordion count badges. `total_chapters` retains its existing linked-entry meaning.

`observed_linked_entry_count` and `highest_observed_chapter_number` also retain
the catalog's witnessed labels when migration verification fails. They describe
links seen on the source page, not accessible or verified chapter content.

## Budgets and optional visual recovery

Set `NOVEL_SCRAPER_RECOVERY_MODE=maximum` for the recovery preference discussed
in this task. Existing clients keep the standard default. Individual positive
`NOVEL_SCRAPER_<LIMIT>` overrides take precedence over the profile.

| Budget | Standard | Maximum |
| --- | ---: | ---: |
| Discovery seconds | 600 | 900 |
| TOC cycles | 8 | 16 |
| Repair attempts per layout | 3 | 5 |
| Ranked candidates | 6 | 10 |
| Visual attempts per expansion | 1 | 2 |
| Cleanup seconds | 5 | 5 |

Maximum recovery allows 60 seconds for default browser retrieval operations.
Discovery shares an absolute deadline across its stages. Each representative
chapter shares one budget across fetch, browser retry, and review. Cancellation
and cleanup have bounded waits; owned browser processes are killed if cleanup
times out. Synchronous parsing cannot be interrupted mid-call, and these bounds
are not an OS process watchdog.

Visual recovery is an explicit opt-in:
`NOVEL_SCRAPER_VISUAL_RECOVERY=1` requires a configured image-capable provider.
After text-based control selection fails, a viewport screenshot can help choose
from the same currently witnessed controls. The DOM is observed again after each
click. Visual suggestions cannot invent selectors, run arbitrary code, or supply
chapter prose. Gemini message conversion preserves inline image blocks; external
provider routing receives standard LangChain image blocks. Screenshots stay in
memory and are sent to the configured model only with this option enabled.

## Benchmark and acceptance

Run from the repository root:

```powershell
uv run python -m src.scraper.benchmark --output report.json
uv run python -m src.scraper.benchmark --compare-trafilatura --output comparison.json
```

The benchmark uses six fixed synthetic website families with separate recipe
directories per family, then repeats cold and warm runs with fresh output.
`development` and `held_out` labels identify layout families, not a collection
of independently sampled live websites. `--llm` explicitly enables the configured
model on fixture HTML; the default requires no model key or live site traffic.
Trafilatura is an optional comparator, not a runtime dependency. If absent, its
result is `not_installed` rather than a fabricated comparison.

The gold catalog and chapter text are fixed independently of the extractor. The
report records catalog precision/recall, ordered token precision/recall, original
title accuracy, actual ending preservation, false completion, runtime, simulated
browser launches/renders, and model calls. Fixture browser metrics describe the
simulator; production inspection records actual browser process launches.

The recorded baseline and current reports under `docs/benchmarks/` use the same
corpus fingerprint. End-to-end success requires the complete gold catalog and
verified first, middle, and last saved chapter files. Baseline: 10/12 runs, with
two false completions. Current: 12/12 runs, with zero false completions. These
deterministic fixture results do not establish a live-site accuracy percentage.

Live matrix entries now require complete discovery and representative chapter
publication. An expected blocker must be declared explicitly using
`expected_blocker`; an arbitrary error no longer passes accessible-site acceptance.
Use `NOVEL_SCRAPER_LIVE_TESTS=1` and supply URLs through
`NOVEL_SCRAPER_LIVE_MATRIX`. Tests use temporary recipes, output, and state.

### Supplied URLs: live observations

- NovelBin: 133 linked entries, matching the advertised 133. Strict live acceptance
  passed with catalog entries 1, 67, and 133 published through the normal pipeline.
  See [catalog count and sample receipts](benchmarks/novelbin-live.json).
- The supplied WordPress page: 286 distinct catalog links, with a highest chapter
  label of 293 (some posts combine chapter ranges). Of these, 246 links lead to
  `isekailunatic.com`, whose sampled response serves a ParkLogic parking redirect.
  The scraper retains 40 same-domain entries and reports incomplete coverage with
  the parking cause. This is a failed extraction acceptance, not an extraction
  success. See [live blocker evidence](benchmarks/wordpress-live.json).

These checks used heuristic mode without model credentials. Visual assistance
has offline transport/control tests only; no live visual accuracy claim is made.

## Research basis

- [Mind2Web](https://osu-nlp-group.github.io/Mind2Web/): relevant-element selection and evaluation across websites.
- [Playwright navigation](https://playwright.dev/python/docs/navigations) and [network observations](https://playwright.dev/python/docs/network): readiness and observed responses.
- [WordPress discovery](https://developer.wordpress.org/rest-api/using-the-rest-api/discovery/): advertised document resources.
- [Trafilatura extraction](https://trafilatura.readthedocs.io/en/stable/extraction-overview.html) and [evaluation](https://raw.githubusercontent.com/adbar/trafilatura/master/tests/README.rst): fallback candidates and independent text segments.
- [Erratum](https://arxiv.org/abs/2106.04916): contextual locator repair.
- [WebLINX](https://arxiv.org/abs/2402.05930): visual/DOM evidence and remaining unseen-website generalization limits.
- [Python cancellation](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.wait_for): cancellation can exceed a nominal wait_for timeout.

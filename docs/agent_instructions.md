# Agent instructions and skills

When changing an agent's goal, skills, rules, or completion criteria, update
`src/agent/instructions.py`. This module is the source of truth for role
instructions. `agent_system_instruction(role)` includes the shared evidence
rules and only the active role in a model call. `LLMClient.generate_json`
adds the requested JSON schema. These are application instructions, loaded
directly by the scraper, without a Codex skill loader or additional model calls.

| Role | Invocation and skills | Completion owner |
| --- | --- | --- |
| `classifier` | Classify metadata, headings, and observed links when a validated recipe or deterministic classification is insufficient. | Returns classification; TOC auditor decides catalog coverage. |
| `toc` | Inspect counts, extract embedded state and DOM links, follow observed pagination, expand witnessed controls, and audit coverage. The optional browser model receives this role's action constraints. | TOC graph preserves unresolved audits through finalization. |
| `chapter_analyzer` | Propose heading, complete story container, and narrow clutter selectors from a bounded DOM summary. | Deterministic selector validation followed by extraction and independent review. |
| `chapter_refiner` | Repair specific review findings using the current plan and current DOM candidates. | Revised selectors must exist and preserve the title and story container. |
| `observer` | Compare source and extracted beginning, middle, ending, title, and coverage counts. | Returns explicit defects and review flags consistent with the configured quality threshold. |
| `code_generator` | Apply the selector plan, preserve mixed prose, generate deterministic extraction code, and collect verification evidence. | Code returns extraction status and evidence; Observer approves quality. |
| `review_loop` | Validate recipes, audit deterministic candidates, refresh browser evidence, then use model selection and contextual repair. | Verify independent surrounding story boundaries; skip equivalent plans and outputs; rejected best results stay unapproved. |
| `self_healer` | Reuse analyzer, refiner, generator, and Observer skills after layout failure. | Adopt only a successfully extracted and reviewed repair meeting the configured threshold. |
| `domain_memory` | Validate and reuse approved recipes by route and structural fingerprint. | Return a verified recipe hit or fall back to analysis. |
| `api_recipe` | Record only witnessed request/response shapes; keep credentials in the active session; allow replay only for GET or explicitly trusted read-only POST/query requests. | Promote only after novel identity and TOC/chapter quality audits; reject stale mappings and fall back to browser evidence. |

The last four roles run deterministic Python and reuse the model roles where
needed. Their entries document implementation contracts rather than adding
extra prompting stages. Site handlers remain deterministic API and decoding
adapters under `src/handlers/`.

## Checks when changing instructions

1. Exercise the actual calling agent with a mocked model; inspect the submitted
   instructions and evidence, then verify the returned result.
2. Include missing selectors, destructive removal proposals, invented links,
   and repeated rejected plans when changing those boundaries.
3. Run the offline suite with `uv run pytest`. Live site checks remain opt-in
   under the repository's existing live matrix contract.
4. For refresh changes, cover 304 dependency revalidation, stable source identity,
   local file ownership, semantic source changes, and interrupted-job recovery.
5. For API recipes, verify credential exclusion, exact request/cursor identity,
   response representation fidelity, promotion gates, and invalid-recipe fallback.

Instruction changes guide model behavior; they do not establish a measured
speed or accuracy improvement. Compare model-call counts on fixed fixtures
before claiming savings. Repeated-plan termination is enforced by code and
its regression test counts the avoided Observer calls.

TOC browser actions include `select` with a witnessed enabled option `value`,
alongside click, scroll, and stop. The executor revalidates the current control
and option before each action, retains intermediate catalogs, and does not
treat unchanged chapter counts as closure while options remain unvisited.
Recent-list headings are not advertised totals; conflicting total claims keep
the audit incomplete. Batch entrypoints pause before chapter validation or
downloads until catalog coverage is complete. Incomplete snapshots remain
available for rediscovery on retry. Inspection accepts an optional progress
callback; the JSON inspection worker still writes one final response to stdout.
Styled dropdowns may hide their native select. A fixed DOM fallback can dispatch
input/change events when the browser's selection helper fails, after rechecking
the witnessed selector, select element, enabled option, and exact option value.
Model-generated scripts are never executed.
Interactive catalog pages probe native select support. If the CDP engine lacks
it, the client can use an already-installed Chrome, Edge, or Playwright Chromium
with the same authorized storage state and page/deadline limits. No browser is
downloaded. Owned fallback contexts and browsers close with the client; if no
standard browser is installed, existing bounded recovery remains available.

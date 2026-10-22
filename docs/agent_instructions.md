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

Instruction changes guide model behavior; they do not establish a measured
speed or accuracy improvement. Compare model-call counts on fixed fixtures
before claiming savings. Repeated-plan termination is enforced by code and
its regression test counts the avoided Observer calls.

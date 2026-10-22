# Harness² assessment for Novel Scraping Agent

Date: 2026-10-19. Scope: first-party README, documentation, and source inspection; no installation or external code execution. Links target the inspected upstream `main`, which can change.

## Finding

**Recommendation: retain the scraper runtime and consider Harness² as an optional optimization layer.** Harness² optimizes the system surrounding a fixed model through task execution experiments and accumulated lessons. Its supplied executors are OpenCode and Codex; its packaged workloads are benchmarks. It is not a ready-made replacement for this repository's browser, catalog discovery, parsers, handlers, checkpointing, or chapter publishing. The replacement assessment is an architectural inference from the integration contracts below. [README](https://github.com/google-research/harness2/blob/main/README.md), [adapter contract](https://github.com/google-research/harness2/blob/main/harness2/core/spec.py)

## What it actually does

- **Propose:** create general and domain-specific changes to the execution harness. **Probe:** rerun the same task under candidate harnesses. **Compose:** combine changes using contrasts between actual executions. Parallel mode explores `k` general plus `k` domain candidates, then executes `k` compositions. Sequential mode performs `k` successive rounds and carries the current harness and accumulated evidence forward. Invalid edits get one retry, then inherit the current harness. [harness2/core/recursion.py](https://github.com/google-research/harness2/blob/main/harness2/core/recursion.py)
- The editable surface includes instructions, memory, tool guidance, skills, scripts, delegation, and middleware. OpenCode's `guardrails.md` is always-on prompt text; plugin middleware acts within its tool loop. Codex uses `AGENTS.md`, skills, scripts, and lifecycle hooks. **Inference:** editable “guardrails” do not provide an immutable authorization boundary; scraper authorization, session access, parser restrictions, and publication gates should remain outside the optimized surface. [harness2/substrate/opencode.py](https://github.com/google-research/harness2/blob/main/harness2/substrate/opencode.py), [harness2/substrate/codex.py](https://github.com/google-research/harness2/blob/main/harness2/substrate/codex.py)
- The library stores the full adopted harness plus `EXPERIENCE.md` and `INDEX.md` per domain. Persistence requires a parsed lesson containing cue, lesson, and evidence. Earlier same-domain entries are provided as context for later improvement; this is not the scraper's deterministic recipe lookup. [harness2/core/library.py](https://github.com/google-research/harness2/blob/main/harness2/core/library.py)

## Cost and adoption semantics

Nominal cost **per task, per mode** is `4k` executor rollouts: `k` reference runs + `2k` probes + `k` composed runs; plus `3k` improvement stages. Selection and reflection are outside that accounting, and retries add invocations. Thus `k=1` already means four executions, three editing stages, and normally one reflection session when a library is active; it is not one model request. Running both modes shares the reference executions. Dollar cost depends on model use inside every execution. [harness2/core/ledger.py](https://github.com/google-research/harness2/blob/main/harness2/core/ledger.py), [harness2/core/schedule.py](https://github.com/google-research/harness2/blob/main/harness2/core/schedule.py)

The selection pool contains only composed finals: reference outputs are excluded. At `k=1`, the single surviving final is adopted without a selector session. At larger budgets, selection compares anonymized finals; it does not enforce superiority to the baseline. Benchmark scoring occurs after selection. **Inference:** production use needs a separate regression/promotion gate; “adopted” does not establish correctness or improvement. [harness2/core/selection.py](https://github.com/google-research/harness2/blob/main/harness2/core/selection.py), [score contract](https://github.com/google-research/harness2/blob/main/harness2/core/spec.py)

## Integration work

The core separates method, substrate, and benchmark. A custom adapter must supply stable tasks/domain grouping, clean task prompts/source files, idempotent `execute(...)`, delivery detection, readable trajectories, extracted deliverables, a base harness, harness validation, and post-selection scoring. A substrate materializes the editable tree and delegates execution to a supplied launcher. The CLI currently enumerates only `jb`, `lab`, `wb` and `oc`, `cx`; a scraper adapter requires implementation and an entry point. [harness2/core/spec.py](https://github.com/google-research/harness2/blob/main/harness2/core/spec.py), [harness2/substrate/base.py](https://github.com/google-research/harness2/blob/main/harness2/substrate/base.py), [harness2/cli/_common.py](https://github.com/google-research/harness2/blob/main/harness2/cli/_common.py)

Provisional local mapping, based on the current checkout inspection coordinated with the parent investigation:

| Existing subsystem | Suggested relationship |
|---|---|
| Chapter review loop and self-healer | Explore candidate instructions/recovery plans on novel layouts; preserve deterministic verification and existing budgets. |
| Domain memory | Keep route/fingerprint recipe variants; store general lessons separately. |
| TOC audit, runtime limits, handlers, browser, storage | Retain as execution infrastructure and independent constraints. |
| `docs/research-evaluation.md` | Use frozen fixture gold and cold/warm evaluation to measure accuracy, coverage, latency, and tokens before promotion. |
| `src/agent/instructions.py` | Optimize only permitted declarative plans/instructions; preserve the existing prohibition on model-generated executable parser code. |

**Proposed first experiment, not implemented:** offline Propose–Probe–Compose over existing failure fixtures, followed by independent held-out validation. Promote only candidates that improve extraction without losing endings, inline text, catalog coverage, or access-status accuracy. Keep warm deterministic recipe runs outside this expensive loop. This is a design recommendation, not an upstream capability claim.

## Evidence limits and environment

The supported benchmarks cover professional work (JobBench), legal work (LAB), and office/web/code tasks (WorkBuddy). Published headline gains are benchmark-specific; they do not demonstrate novel extraction accuracy or throughput. [benchmark guides](https://github.com/google-research/harness2/blob/main/docs/BENCHMARKS.md), [reported results](https://github.com/google-research/harness2/blob/main/README.md)

Upstream documents Linux and Python 3.12 for benchmark runs (package also permits 3.13), with additional model/runtime dependencies. Codex's supplied launch contract disables approvals and sandboxing; network access is unrestricted by default. **Inference:** evaluate separately on Linux/WSL/container infrastructure with deliberately configured external controls, rather than treating this Windows scraper environment as an established supported deployment. [setup](https://github.com/google-research/harness2/blob/main/docs/SETUP.md), [network and experiment guide](https://github.com/google-research/harness2/blob/main/docs/EXPERIMENTS.md)

Validation: this assessment adds documentation only. Harness² was not installed or executed. The local full test suite could not complete: initial attempts encountered sandbox cache/temp permission errors; after redirecting temporary files into the workspace, the run was interrupted after four passing tests without further progress. No passing-suite or measured integration-performance claim is made.

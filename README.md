# skill-failure-auditor

Read-only reliability auditor and cross-repository symptom triage toolkit for LLM Skills, Prompts, Agent instructions, workflows, and already-produced runtime evidence.

`skill-failure-auditor` (SFA) focuses on two recurring engineering problems across an agent skill family:

1. **Preventing fake completion (`US-075`)** — catching workflows that look finished on the surface (exit code `0`, non-empty files, self-review by the same role, weakened assertions, or unverified evidence shards) while the real user goal remains unmet.
2. **Cross-repository symptom attribution (`US-074`)** — pinpointing the primary responsible repository when an error spans host discovery, `agent-method-registry`, `skill-family-foundation`, `artifact-graph`, `release-skill`, `skill-family-test`, external domain skill plugins (`domain-skill-plugin`), or a domain business skill, without mutating the target workspace.

**One authoritative core, four platform projections:** Claude Code, Codex, Kimi Code, and WorkBuddy/CodeBuddy. Every projection carries identical `FM-01`…`FM-28` rules, schemas, evidence tools, symptom triage scripts, and fail-closed validation semantics. Platform differences are limited to installation manifests, discovery paths, and client version constraints.

## Layout

- `plugin-src/core/` — platform-independent core: failure-mode registry (`failure-modes.jsonl`), JSON Schemas, evidence chunking and coverage tools, append-only attempt records, result validation, report rendering, and `triage_symptom.py` / `triage-symptom.mjs`.
- `plugin-src/platforms/<platform>/` — thin host manifests, discovery rules, and client constraints. No business rules or execution topology live here.
- `spec/` — platform support and equivalence contracts, Foundation integration pins, symptom triage bundle schema, public-boundary rules, and release/Pages/Hub design.
- `docs/llm-academy/` — companion static course (17 files, CC BY 4.0). See `README_zh.md` for the full chapter index.
- `scripts/` — deterministic platform projection builders and package-level `triage-symptom.mjs` CLI wrapper.

## Product boundary

SFA is an auditor, not an executor. `static` reviews definitions; `runtime` reviews logs, tool outputs, and receipts that already exist; `combined` reviews both input classes together. No mode starts the audited target Skill, delegates work to subagents, waits for or retries a target task, or joins its live control loop.

Active trials belong to an independent evaluator that freezes raw evidence first; SFA then audits that frozen evidence as ordinary read-only data. No executor or orchestration product holds privileged status or a custom runtime interface inside SFA.

## Entry points

The `skill-failure-auditor` entry routes requests into four intents:

- **Help (`HELP`)** — explains purpose, required inputs, the three audit modes, read-only boundaries, cross-repository symptom triage, prerequisites, and minimal invocation, then exits without creating audit artifacts.
- **Setup check (`SETUP_CHECK`)** — inspects Python, the `SFA_FOUNDATION_NODE` canonical real path (no symlinks) and exact Node.js pin match, input availability, and a non-existent output directory without installing or modifying anything.
- **Adoption analysis (`ADOPTION_ANALYSIS`)** — reports satisfied prerequisites, remaining gaps, and trade-offs without scaffolding or mutating the target project.
- **Reliability failure audit (`AUDIT`)** — runs the applicability gate, deterministic rule selection, chunk coverage verification, schema/domain result validation, and Markdown report rendering.

Explicitly requested modes are preserved; if required material is missing, SFA reports the gap instead of silently switching modes. See [`plugin-src/core/references/usage-guide.md`](plugin-src/core/references/usage-guide.md) and [`plugin-src/core/references/cross-repo-triage.md`](plugin-src/core/references/cross-repo-triage.md).

<!-- release-skill:external-write-boundary -->

External-write boundary: SFA reads the audited target and writes only to a caller-selected new audit-output directory (or `--output-bundle` directory) outside that target. It never modifies the audited source tree.

## Cross-repository symptom triage (`triage-symptom`)

When a deterministic error occurs across multiple ecosystem packages, run the read-only triage CLI to attribute the primary owner (`skill-family-foundation`, `artifact-graph`, `agent-method-registry`, `skill-family-test`, `release-skill`, `skill-failure-auditor`, `host-configuration`, `domain-skill-plugin`, or `business-skill`) and optionally export a redacted minimal reproduction bundle:

```sh
node scripts/triage-symptom.mjs \
  --symptom "prepare blocked: STALE_REVIEW in traceability-version-lock.json" \
  --workspace /absolute/project/path \
  --output-bundle /absolute/path/to/new-triage-bundle
```

## Install

The public marketplace source is `ifoohoo/skill-family-hub`. Add it with your host's marketplace manager, then install `skill-failure-auditor@skill-family-hub`. This repository provides per-host manifests without publishing a separate standalone marketplace index.

- **Claude Code**: copy `platforms/claude-code/skill/` into a skills directory (for example `.claude/skills/skill-failure-auditor`), then invoke `/skill-failure-auditor <target> <static|runtime|combined>`.
- **WorkBuddy**: copy `platforms/workbuddy/skill/` to `~/.workbuddy/skills/skill-failure-auditor/` (default `<CODEBUDDY_CONFIG_DIR>/skills` discovery root; do not place this projection under `.claude/skills`).
- **Kimi Code**: `kimi.plugin.json` is the authoritative manifest; `.kimi-plugin/plugin.json` is the mechanically generated Hub-compatible projection.
- **Codex / WorkBuddy (CodeBuddy)**: install the matching `platforms/<id>/` projection; see `spec/platforms/support-matrix.json` for verified client versions and status.

<!-- release-skill:safe-first-command -->

Safe first command: a one-shot static audit (`/skill-failure-auditor <target> static`) or symptom triage (`node scripts/triage-symptom.mjs --symptom "<error>"`) reads inputs without running the target skill. Release lifecycle operations (assess/prepare/approve/publish/reconcile/verify) are governed by `npx release-skill`.

For offline verification of historical release records, pass explicit `plan`, `approval`, `target-run`, every `source-run`, expected `unit`, and version to `release-skill` `verify-records` (`CONSISTENT`=0, `CONTRADICTED`=1, `INSUFFICIENT`=2; `historicalTerminalStatus` is reported independently without network or file writes).

## Minimal example

```sh
# one-shot static audit; writes only to a caller-selected new output directory
/skill-failure-auditor /absolute/project/path/skills/my-skill static
```

## Troubleshooting

All tools fail closed: schema violations, incomplete chunk coverage, digest mismatches, symlinked inputs, or non-empty bundle targets exit non-zero with actionable diagnostics. For cross-package errors (`SFC*`, `SFH*`, `SFEK*`, `ACA*`, `AG_*`, `STALE_REVIEW`, `AMR*`, `SFT*`, `RS_*`, `HOOK_SIDE_EFFECT`, `SFA*`, `PYTHON_OVERRIDE_INVALID`, or custom prefixes configured via `SFA_CUSTOM_TRIAGE_PREFIXES`), run `triage-symptom` first to locate the owning repository before modifying code.

## License

Code: Apache-2.0 (Copyright 2026 skill-failure-auditor contributors; Copyright 2026 Guangzhou Fenghe Technology Co., Ltd.). Course content under `docs/llm-academy/`: CC BY 4.0 (see NOTICE).

## Status

The authoritative product version is defined in `package.json`. Host runtime verification status is recorded in `spec/platforms/support-matrix.json`.

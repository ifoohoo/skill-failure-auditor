# skill-failure-auditor

Audits Skills, Prompts, Agent instructions, workflows, and their real runtime evidence for reliability failure modes — especially “looks completed but the real goal was not achieved” (fake completion, proxy evidence, self-verification, context/evidence/shard/responsibility-isolation failures).

**One authoritative core, four platform projections:** Claude Code, Codex, Kimi Code, WorkBuddy/CodeBuddy. All platforms run the same business rules (FM-01…FM-28), the same task-package/result contracts, and the same fail-closed semantics; only manifests, dispatch syntax, and receipt normalization differ.

## Layout

- `plugin-src/core/` — the single platform-independent core: rule registry (`failure-modes.jsonl`), schemas, evidence/attempt/evaluation tools, orchestration engine (`orchestration_engine.py`: prepare-run / write-result / validate-result-set / finalize-run).
- `plugin-src/platforms/<platform>/` — thin projections: manifests, prompt bindings, dispatch mappings. No business rules live here.
- `spec/` — support matrix, orchestration protocol and schemas, public-boundary policy, release/Pages/Hub design.
- `docs/llm-academy/` — companion static course (17 files, CC BY 4.0). See `README_zh.md` for the chapter index.
- `tests/`, `scripts/build/` — structural/deterministic build and gate tooling.

## Orchestration contract (summary)

Six semantic roles: `scope-routing`, `static-audit`, `runtime-evidence`, `evaluation-integrity`, `adversarial-challenge`, `result-synthesis`. Modes: `static` (5 roles), `runtime` (5), `combined` (6). The engine is the only writer of result files; missing/duplicated/extra/mismatched outputs, schema failures, non-zero exits and timeouts fail closed. Candidates and implementers may only submit diagnostics (`SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW`); formal acceptance requires independent review.

<!-- release-skill:external-write-boundary -->

External-write boundary: the audit engine writes only diagnostic evidence under the audited target's evidence directory; the audited source tree is never modified.

## Install

- Claude Code: copy (or symlink) `platforms/claude-code/skill/` into a skills directory (e.g. project `.claude/skills/skill-failure-auditor`), then invoke `/skill-failure-auditor <target> <static|runtime|combined>`.
- WorkBuddy: copy `platforms/workbuddy/skill/` to `~/.workbuddy/skills/skill-failure-auditor/`. This is the WorkBuddy app's default `<CODEBUDDY_CONFIG_DIR>/skills` discovery root; do not install this projection under `.claude/skills`.
- Kimi Code: use `kimi.plugin.json` (authoritative); `.kimi-plugin/plugin.json` is a mechanically generated Hub-compat projection (field-identical).
- Codex / WorkBuddy (CodeBuddy): install the corresponding `platforms/<id>/` projection; see `spec/platforms/support-matrix.json` for verified runtimes and honest status.

<!-- release-skill:safe-first-command -->

Safe first command: a one-shot audit (`/skill-failure-auditor <target> static`) is read-only — it inspects the target and writes only diagnostic evidence; nothing else in the workspace is modified. The release-time state machine (assess/prepare/approve/publish/reconcile/verify) is governed by the release-skill pipeline (`npx release-skill`).

## Minimal example

```sh
# one-shot static audit; release-skill governs the release-time state machine
/skill-failure-auditor packages/flow-architect static
```

## Troubleshooting

Audits fail closed: schema violations, missing/duplicated/mismatched outputs, non-zero exits, and timeouts surface as explicit diagnostics (e.g. `GATE_FAILED`-style gate failures) rather than silent success. If an audit fails, inspect the run evidence under the target's evidence directory, re-run with a single role (`static`) to isolate the failing role, then consult `spec/` for the failure-mode registry (FM-01…FM-28).

## License

Code: Apache-2.0 (Copyright 2026 skill-failure-auditor contributors; Copyright 2026 Guangzhou Fenghe Technology Co., Ltd.). Course content under `docs/llm-academy/`: CC BY 4.0 (see NOTICE).

## Status

Productization candidate `1.0.0-candidate` — formal status `FORMAL_ACCEPTANCE_BLOCKED` pending independent acceptance (W12) and release authorization (W13). Platform verification: Claude Code / Codex / WorkBuddy black-box passed; Kimi Code pending runtime authentication.

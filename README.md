# skill-failure-auditor

Audits Skills, Prompts, Agent instructions, workflows, and already-produced runtime evidence for reliability failure modes — especially “looks completed but the real goal was not achieved” (fake completion, proxy evidence, self-verification, and context, evidence, shard, or responsibility-isolation failures).

**One authoritative audit core, four platform projections:** Claude Code, Codex, Kimi Code, and WorkBuddy/CodeBuddy. Every projection carries the same FM-01…FM-28 rules, audit schemas, evidence tools, and fail-closed semantics. Platform differences are limited to installation manifests, discovery paths, and client constraints.

## Layout

- `plugin-src/core/` — the platform-independent audit core: rule registry (`failure-modes.jsonl`), audit schemas, evidence indexing, attempt records, result validation, and report rendering.
- `plugin-src/platforms/<platform>/` — thin platform sources containing manifests, discovery rules, and client constraints. No business rules or execution topology live here.
- `spec/` — audit equivalence and support contracts, Foundation integration, public-boundary policy, and release/Pages/Hub design.
- `docs/llm-academy/` — companion static course (17 files, CC BY 4.0). See `README_zh.md` for the chapter index.
- `scripts/build/` — deterministic projection builders. Workspace-only integration and release tests are intentionally excluded from the public snapshot.

## Product boundary

SFA is an auditor, not an executor. `static` reviews definitions; `runtime` reviews logs, tool output, and receipts that already exist; `combined` reviews both input classes. No mode starts the target Skill, delegates work to subagents, waits for or retries the target task, or joins its live control loop.

An independent evaluator may run active trials and freeze the resulting evidence. SFA can then audit that evidence as ordinary input. No executor or orchestration product has a privileged integration, runtime role, or product-specific interface in SFA; any such system is either ordinary audited input or an external consumer.

<!-- release-skill:external-write-boundary -->

External-write boundary: SFA reads the audited target and writes only to a caller-selected new audit-output directory outside that target. It does not modify the audited source tree.

## Install

- Claude Code: copy (or symlink) `platforms/claude-code/skill/` into a skills directory (e.g. project `.claude/skills/skill-failure-auditor`), then invoke `/skill-failure-auditor <target> <static|runtime|combined>`.
- WorkBuddy: copy `platforms/workbuddy/skill/` to `~/.workbuddy/skills/skill-failure-auditor/`. This is the WorkBuddy app's default `<CODEBUDDY_CONFIG_DIR>/skills` discovery root; do not install this projection under `.claude/skills`.
- Kimi Code: use `kimi.plugin.json` (authoritative); `.kimi-plugin/plugin.json` is a mechanically generated Hub-compat projection (field-identical).
- Codex / WorkBuddy (CodeBuddy): install the corresponding `platforms/<id>/` projection; see `spec/platforms/support-matrix.json` for verified runtimes and honest status.

<!-- release-skill:safe-first-command -->

Safe first command: a one-shot audit (`/skill-failure-auditor <target> static`) reads the target without running it and writes only to a new audit-output directory. The release-time state machine (assess/prepare/approve/publish/reconcile/verify) is governed by the release-skill pipeline (`npx release-skill`).

## Minimal example

```sh
# one-shot static audit; release-skill governs the release-time state machine
/skill-failure-auditor packages/flow-architect static
```

## Troubleshooting

Audits fail closed: schema violations, incomplete coverage, missing or duplicate evidence, and digest drift surface as explicit diagnostics rather than silent success. Inspect the caller-selected audit-output directory and the frozen input bindings, then consult the rule registry (FM-01…FM-28). Retrying or repairing the audited target remains the responsibility of its owner or an external evaluator.

## License

Code: Apache-2.0 (Copyright 2026 skill-failure-auditor contributors; Copyright 2026 Guangzhou Fenghe Technology Co., Ltd.). Course content under `docs/llm-academy/`: CC BY 4.0 (see NOTICE).

## Status

The source version is defined by `package.json`. Runtime support and verification status are recorded in `spec/platforms/support-matrix.json`; this static README does not claim that an unpublished candidate or an unverified host is available.

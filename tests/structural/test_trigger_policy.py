"""Candidate 16：共享触发策略、适用性门禁和平台原生语法回归。"""
from __future__ import annotations

import json
import re
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads(
    (PACKAGE_ROOT / "plugin-src" / "core" / "trigger-policy.json").read_text(
        encoding="utf-8"
    )
)
CASES = json.loads(
    (PACKAGE_ROOT / "spec" / "trigger-policy-cases.json").read_text(encoding="utf-8")
)
GENERATED = PACKAGE_ROOT / "generated" / "platforms"
PLATFORMS = ["claude-code", "codex", "kimi-code", "workbuddy"]
SIGNALS = POLICY["positive_signal_terms"]


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "missing YAML frontmatter"
    result = {}
    for line in match.group(1).splitlines():
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def _matches_fixture(text: str) -> bool:
    return any(signal in text for signal in SIGNALS)


def test_all_generated_entries_share_exact_trigger_and_gate() -> None:
    for platform in PLATFORMS:
        entry = (GENERATED / platform / "skill" / "SKILL.md").read_text(encoding="utf-8")
        meta = _frontmatter(entry)
        assert meta["name"] == POLICY["name"]
        assert meta["description"] == POLICY["description"]
        assert entry.count(POLICY["applicability_gate"]) == 1
        frontmatter_end = re.match(r"^---\n.*?\n---\n", entry, re.DOTALL)
        assert frontmatter_end
        assert entry[frontmatter_end.end():].lstrip().startswith(POLICY["applicability_gate"])


def test_trigger_fixtures_separate_reliability_signals_from_ordinary_work() -> None:
    assert all(signal in POLICY["description"] for signal in SIGNALS)
    assert all(_matches_fixture(case) for case in CASES["positive"])
    assert not any(_matches_fixture(case) for case in CASES["negative"])
    for excluded in ("普通技能", "提示词编写", "常规代码审查", "调试", "安装兼容",
                     "单次测试失败", "一般工作流设计"):
        assert excluded in POLICY["description"]


def test_platform_entry_templates_can_only_receive_shared_policy_by_tokens() -> None:
    for platform in PLATFORMS:
        source = (
            PACKAGE_ROOT / "plugin-src" / "platforms" / platform / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert source.count("{{SHARED_SKILL_NAME}}") == 1
        assert source.count("{{SHARED_TRIGGER_DESCRIPTION}}") == 1
        assert source.count("{{SHARED_APPLICABILITY_GATE}}") == 1
        assert POLICY["description"] not in source


def test_platform_native_orchestration_does_not_leak() -> None:
    claude = (GENERATED / "claude-code" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    claude += (GENERATED / "claude-code" / "skill" / "references" /
               "claude-code-orchestration.md").read_text(encoding="utf-8")
    codex = (GENERATED / "codex" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    workbuddy = (GENERATED / "workbuddy" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "Agent({" in claude and "collaboration.spawn_agent" not in claude
    assert "collaboration.spawn_agent" in codex and "subagent_type" not in codex
    assert "Agent({" in workbuddy and "collaboration.spawn_agent" not in workbuddy
    assert "context: fork" not in workbuddy
    assert "${CLAUDE_SKILL_DIR}" not in workbuddy
    assert "${CODEBUDDY_SKILL_DIR}" in workbuddy

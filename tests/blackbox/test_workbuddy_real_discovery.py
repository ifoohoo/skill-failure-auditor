"""WorkBuddy 配置根/skills 单一解析规则与可选真实 discovery 黑盒。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROBE = PACKAGE_ROOT / "scripts" / "build" / "workbuddy_discovery_probe.py"
GENERATED = PACKAGE_ROOT / "generated" / "platforms" / "workbuddy"
MANIFEST = PACKAGE_ROOT / "plugin-src" / "platforms" / "workbuddy" / "platform-manifest.json"
SUPPORT = PACKAGE_ROOT / "spec" / "platforms" / "support-matrix.json"
ENTRY = PACKAGE_ROOT / "plugin-src" / "platforms" / "workbuddy" / "SKILL.md"
ORCHESTRATION = (
    PACKAGE_ROOT / "plugin-src" / "platforms" / "workbuddy" / "references"
    / "workbuddy-orchestration.md"
)
CODEBUDDY = Path(
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
)
PROBE_SPEC = importlib.util.spec_from_file_location("workbuddy_discovery_probe", PROBE)
assert PROBE_SPEC is not None and PROBE_SPEC.loader is not None
PROBE_MODULE = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(PROBE_MODULE)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_probe(tmp_path: Path, *extra: str,
              manifest: Path = MANIFEST) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--skill-source", str(GENERATED / "skill"),
            "--platform-manifest", str(manifest),
            "--isolated-home", str(tmp_path / "home"),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=21 * 60,
    )


def tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use", "id": tool_id,
                "name": name, "input": tool_input,
            }]
        },
    }


def tool_result(tool_id: str, text: str, *, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result", "tool_use_id": tool_id,
                "is_error": is_error,
                "content": [{"type": "text", "text": text}],
            }]
        },
    }


def evaluate(events: list[dict], expected_path: Path) -> dict:
    entry = (GENERATED / "skill" / "SKILL.md").read_text(encoding="utf-8")
    policy = json.loads(
        (PACKAGE_ROOT / "plugin-src" / "core" / "trigger-policy.json")
        .read_text(encoding="utf-8")
    )
    return PROBE_MODULE.evaluate_discovery_evidence(
        events,
        expected_entry_path=expected_path,
        expected_entry=entry,
        expected_entry_sha256=hashlib.sha256(entry.encode("utf-8")).hexdigest(),
        shared_description=policy["description"],
        applicability_gate=policy["applicability_gate"],
    )


def numbered_read_output(text: str) -> str:
    return "\n".join(
        f"{index:4d}→{line}" for index, line in enumerate(text.splitlines(), start=1)
    )


def test_consumer_install_uses_manifest_config_root_skills_rule(tmp_path: Path) -> None:
    result = run_probe(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    expected = tmp_path / "home" / ".workbuddy" / "skills" / "skill-failure-auditor"
    assert Path(data["skill_install"]) == expected
    assert (expected / "SKILL.md").is_file()
    assert not (tmp_path / "home" / ".claude" / "skills").exists()
    assert not (tmp_path / "home" / ".codebuddy" / "skills").exists()
    source_root = GENERATED / "skill"
    source_files = {
        path.relative_to(source_root).as_posix(): sha256_file(path)
        for path in source_root.rglob("*") if path.is_file()
    }
    installed_files = {
        path.relative_to(expected).as_posix(): sha256_file(path)
        for path in expected.rglob("*") if path.is_file()
    }
    assert installed_files == source_files

    shutil.rmtree(expected)
    assert not expected.exists()
    assert not any(
        path.name == "skill-failure-auditor"
        for path in (tmp_path / "home").rglob("skill-failure-auditor")
    )


def test_discovery_manifest_drift_fails_closed(tmp_path: Path) -> None:
    drifted = json.loads(MANIFEST.read_text(encoding="utf-8"))
    drifted["discovery"]["skillRootRelativeToConfig"] = ".claude/skills"
    manifest = tmp_path / "drifted-platform-manifest.json"
    manifest.write_text(json.dumps(drifted), encoding="utf-8")

    result = run_probe(tmp_path, manifest=manifest)
    assert result.returncode != 0
    data = json.loads(result.stdout)
    assert data["status"] == "FAIL"
    assert data["reason"] == "WorkBuddy discovery manifest drift"


def test_manifest_support_contract_and_docs_share_discovery_rule() -> None:
    discovery = json.loads(MANIFEST.read_text(encoding="utf-8"))["discovery"]
    support = json.loads(SUPPORT.read_text(encoding="utf-8"))
    workbuddy = next(
        platform["toolAvailability"]
        for platform in support["platforms"]
        if platform["platformId"] == "workbuddy"
    )
    assert discovery["configRootEnv"] == "CODEBUDDY_CONFIG_DIR"
    assert discovery["skillRootRelativeToConfig"] == "skills"
    assert discovery["skillInstallRelativeToConfig"] == "skills/skill-failure-auditor"
    assert workbuddy["defaultAppSkillInstall"] == discovery["workbuddyAppDefaultSkillInstall"]
    assert "<CODEBUDDY_CONFIG_DIR>/skills" in workbuddy["skillsRootRule"]
    for path in (ENTRY, ORCHESTRATION):
        text = path.read_text(encoding="utf-8")
        assert "<CODEBUDDY_CONFIG_DIR>/skills/skill-failure-auditor" in text
        assert discovery["workbuddyAppDefaultSkillInstall"] in text


def test_discovery_parser_accepts_exact_skill_tool_event(tmp_path: Path) -> None:
    policy = json.loads(
        (PACKAGE_ROOT / "plugin-src" / "core" / "trigger-policy.json")
        .read_text(encoding="utf-8")
    )
    evidence = evaluate([
        tool_use("skill-1", "Skill", {"skill": "skill-failure-auditor"}),
        tool_result("skill-1", policy["applicability_gate"]),
    ], tmp_path / ".workbuddy" / "skills" / "skill-failure-auditor" / "SKILL.md")
    assert evidence["discovery_evidence_observed"] is True
    assert evidence["discovery_evidence_mode"] == "Skill"
    assert evidence["skill_tool_event_observed"] is True
    assert evidence["applicability_gate_text_observed"] is True


def test_discovery_parser_accepts_exact_installed_entry_read(tmp_path: Path) -> None:
    expected_path = (
        tmp_path / ".workbuddy" / "skills" / "skill-failure-auditor" / "SKILL.md"
    )
    entry = (GENERATED / "skill" / "SKILL.md").read_text(encoding="utf-8")
    evidence = evaluate([
        tool_use("read-1", "Read", {"file_path": str(expected_path)}),
        tool_result("read-1", numbered_read_output(entry)),
    ], expected_path)
    assert evidence["discovery_evidence_observed"] is True
    assert evidence["discovery_evidence_mode"] == "Read"
    assert evidence["installed_entry_read_observed"] is True
    assert evidence["read_tool_result_entry_sha256_verified"] is True
    assert evidence["read_tool_result_description_observed"] is True
    assert evidence["read_tool_result_applicability_gate_observed"] is True


def test_discovery_parser_rejects_wrong_path_read(tmp_path: Path) -> None:
    expected_path = (
        tmp_path / ".workbuddy" / "skills" / "skill-failure-auditor" / "SKILL.md"
    )
    entry = (GENERATED / "skill" / "SKILL.md").read_text(encoding="utf-8")
    evidence = evaluate([
        tool_use("read-wrong", "Read", {"file_path": str(tmp_path / "SKILL.md")}),
        tool_result("read-wrong", numbered_read_output(entry)),
    ], expected_path)
    assert evidence["discovery_evidence_observed"] is False
    assert evidence["discovery_evidence_mode"] is None
    assert evidence["installed_entry_read_observed"] is False


def test_discovery_parser_rejects_assistant_only_gate_restatement(tmp_path: Path) -> None:
    policy = json.loads(
        (PACKAGE_ROOT / "plugin-src" / "core" / "trigger-policy.json")
        .read_text(encoding="utf-8")
    )
    evidence = evaluate([{
        "type": "assistant",
        "message": {
            "content": [{
                "type": "text",
                "text": policy["description"] + "\n" + policy["applicability_gate"],
            }]
        },
    }], tmp_path / ".workbuddy" / "skills" / "skill-failure-auditor" / "SKILL.md")
    assert evidence["discovery_evidence_observed"] is False
    assert evidence["discovery_evidence_mode"] is None
    assert evidence["applicability_gate_text_observed"] is False

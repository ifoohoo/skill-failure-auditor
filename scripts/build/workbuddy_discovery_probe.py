#!/usr/bin/env python3
"""WorkBuddy 真实 discovery + 顶层 Agent 最小派发探针。

安装位置只从 WorkBuddy platform-manifest.json 的 discovery 规则解析。真实运行的
prompt 只使用技能名称与自然语言意图，不得给模型绝对 SKILL.md 路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PRODUCT = "skill-failure-auditor"
DEFAULT_CODEBUDDY = Path(
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
)
PROBE_MARKER = "WORKBUDDY_NATIVE_AGENT_DISPATCH_OK"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
TRIGGER_POLICY = PACKAGE_ROOT / "plugin-src" / "core" / "trigger-policy.json"
READ_LINE_PATTERN = re.compile(r"^\s*\d+→(.*)$")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_discovery(manifest_path: Path) -> dict:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("platformId") != "workbuddy":
        raise ValueError("manifest is not the WorkBuddy projection")
    discovery = data.get("discovery")
    expected = {
        "configRootEnv": "CODEBUDDY_CONFIG_DIR",
        "skillRootRelativeToConfig": "skills",
        "skillInstallRelativeToConfig": f"skills/{PRODUCT}",
        "workbuddyAppDefaultConfigRoot": "~/.workbuddy",
        "workbuddyAppDefaultSkillInstall": f"~/.workbuddy/skills/{PRODUCT}",
        "codebuddyCliDefaultConfigRoot": "~/.codebuddy",
    }
    if discovery != expected:
        raise ValueError("WorkBuddy discovery manifest drift")
    return discovery


def resolve_isolated_layout(manifest_path: Path, isolated_home: Path) -> dict[str, Path]:
    discovery = load_discovery(manifest_path)
    home = isolated_home.resolve()
    config_root = home / discovery["workbuddyAppDefaultConfigRoot"].removeprefix("~/")
    skill_root = config_root / discovery["skillRootRelativeToConfig"]
    skill_install = config_root / discovery["skillInstallRelativeToConfig"]
    if skill_install != skill_root / PRODUCT:
        raise ValueError("discovery root and install location disagree")
    return {"home": home, "config_root": config_root, "skill_install": skill_install}


def install_projection(skill_source: Path, manifest_path: Path,
                       isolated_home: Path) -> dict[str, Path]:
    layout = resolve_isolated_layout(manifest_path, isolated_home)
    destination = layout["skill_install"]
    if destination.exists():
        raise FileExistsError(f"isolated skill install already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_source, destination)
    return layout


def copy_auth_state(auth_home: Path, isolated_home: Path, config_root: Path) -> None:
    """只复制黑盒登录所需的显式允许项，不枚举或记录凭据内容。"""
    source_config = auth_home / ".workbuddy"
    for relative in ("local_storage", "user-state.json", "models.json"):
        source = source_config / relative
        target = config_root / relative
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    source_auth = (
        auth_home / "Library" / "Application Support" / "CodeBuddyExtension"
        / "Data" / "Public" / "auth"
    )
    if source_auth.is_dir():
        target_auth = (
            isolated_home / "Library" / "Application Support" / "CodeBuddyExtension"
            / "Data" / "Public" / "auth"
        )
        shutil.copytree(source_auth, target_auth, dirs_exist_ok=True)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _tool_result_text(item: dict) -> str:
    """提取一个真实 tool_result 自身携带的文本，不读取相邻 assistant 输出。"""
    content = item.get("content")
    if isinstance(content, str):
        return content
    texts = []
    for child in _walk(content):
        if child.get("type") == "text" and isinstance(child.get("text"), str):
            texts.append(child["text"])
    return "\n".join(texts)


def _read_content_matches_entry(tool_text: str, expected_entry: str) -> bool:
    """接受原文或 WorkBuddy Read 的 ``行号→正文`` 包装，并要求完整字节等价。"""
    candidates = {tool_text}
    if not tool_text.endswith("\n"):
        candidates.add(tool_text + "\n")
    numbered_lines = tool_text.splitlines()
    matches = [READ_LINE_PATTERN.fullmatch(line) for line in numbered_lines]
    if numbered_lines and all(matches):
        restored = "\n".join(match.group(1) for match in matches if match is not None)
        candidates.add(restored)
        candidates.add(restored + "\n")
    return expected_entry in candidates


def evaluate_discovery_evidence(events: list[dict], *, expected_entry_path: Path,
                                expected_entry: str, expected_entry_sha256: str,
                                shared_description: str,
                                applicability_gate: str) -> dict:
    """机械判定 Skill 或严格受限的自主 Read discovery 证据。"""
    if not expected_entry_path.is_absolute():
        raise ValueError("expected installed entry path must be absolute")
    expected_path = str(expected_entry_path)
    if hashlib.sha256(expected_entry.encode("utf-8")).hexdigest() != expected_entry_sha256:
        raise ValueError("frozen installed entry digest mismatch")
    if shared_description not in expected_entry or applicability_gate not in expected_entry:
        raise ValueError("installed entry does not contain the shared trigger policy")

    tool_uses = []
    tool_results: dict[str, list[dict]] = {}
    for event in events:
        for item in _walk(event):
            if item.get("type") == "tool_use":
                tool_uses.append(item)
            elif item.get("type") == "tool_result" and isinstance(item.get("tool_use_id"), str):
                tool_results.setdefault(item["tool_use_id"], []).append(item)

    skill_event = False
    skill_gate_loaded = False
    verified_read = False
    read_description_observed = False
    read_gate_observed = False
    for item in tool_uses:
        tool_id = item.get("id")
        tool_input = item.get("input")
        if item.get("name") == "Skill" and isinstance(tool_input, dict) \
                and tool_input.get("skill") == PRODUCT:
            skill_event = True
            for result in tool_results.get(tool_id, []):
                if result.get("is_error") is True:
                    continue
                if applicability_gate in _tool_result_text(result):
                    skill_gate_loaded = True
        if item.get("name") != "Read" or not isinstance(tool_input, dict) \
                or tool_input.get("file_path") != expected_path:
            continue
        for result in tool_results.get(tool_id, []):
            if result.get("is_error") is True:
                continue
            tool_text = _tool_result_text(result)
            content_matches = _read_content_matches_entry(tool_text, expected_entry)
            # Read 的行号包装会把多行 gate 切开；只有完整还原为冻结 entry 后，
            # 才从已核摘要的 entry 证明共享 description/gate，不能靠输出复述。
            this_description = content_matches and shared_description in expected_entry
            this_gate = content_matches and applicability_gate in expected_entry
            read_description_observed = read_description_observed or this_description
            read_gate_observed = read_gate_observed or this_gate
            if content_matches and this_description and this_gate:
                verified_read = True

    mode = "Skill" if skill_event else "Read" if verified_read else None
    return {
        "discovery_evidence_observed": skill_event or verified_read,
        "discovery_evidence_mode": mode,
        "skill_tool_event_observed": skill_event,
        "skill_tool_gate_observed": skill_gate_loaded,
        "installed_entry_read_observed": verified_read,
        "read_tool_result_entry_sha256_verified": verified_read,
        "read_tool_result_description_observed": read_description_observed,
        "read_tool_result_applicability_gate_observed": read_gate_observed,
        "applicability_gate_text_observed": skill_gate_loaded or read_gate_observed,
    }


def load_trigger_policy() -> dict:
    policy = json.loads(TRIGGER_POLICY.read_text(encoding="utf-8"))
    description = policy.get("description")
    gate = policy.get("applicability_gate")
    if not isinstance(description, str) or not description \
            or not isinstance(gate, str) or not gate:
        raise ValueError("shared trigger policy is invalid")
    return policy


def run_probe(codebuddy: Path, layout: dict[str, Path], model: str,
              stream_output: Path | None = None,
              stderr_output: Path | None = None) -> dict:
    prompt = """请使用已安装的 skill-failure-auditor 技能判断这个请求是否适用。
这是 WorkBuddy 技能发现与顶层原生派发探针，内容只有普通安装兼容检查，没有任何
LLM/Agent 可靠性失效信号。请遵守该技能的适用性门禁，随后回到普通工作流，调用一次
Agent 工具并使用 Plan 子智能体，让它只返回 WORKBUDDY_NATIVE_AGENT_DISPATCH_OK。
最后原样返回该标记，以及门禁原文规定退出技能流程后不得做的三件事；不要读取或猜测
任何绝对技能文件路径。"""
    entry_path = layout["skill_install"] / "SKILL.md"
    expected_entry_path = entry_path.resolve()
    expected_entry = entry_path.read_text(encoding="utf-8")
    expected_entry_sha256 = sha256_file(entry_path)
    policy = load_trigger_policy()
    for forbidden in ("SKILL.md", str(layout["skill_install"]), str(entry_path),
                      str(expected_entry_path)):
        if forbidden in prompt:
            raise ValueError("probe prompt must not point at a SKILL.md path")
    expected_install = layout["config_root"] / "skills" / PRODUCT / "SKILL.md"
    if entry_path != expected_install:
        raise ValueError("installed entry is outside the manifest-resolved discovery root")
    if policy["description"] not in expected_entry \
            or policy["applicability_gate"] not in expected_entry:
        raise ValueError("installed entry does not contain the shared trigger policy")
    if prompt.lstrip().startswith("/"):
        raise ValueError("probe must rely on natural-language skill discovery")
    env = {
        **os.environ,
        "HOME": str(layout["home"]),
        "CODEBUDDY_CONFIG_DIR": str(layout["config_root"]),
    }
    completed = subprocess.run(
        [
            str(codebuddy), "-p", prompt, "-y",
            "--output-format", "stream-json",
            "--no-session-persistence",
            "--subagent-permission-mode", "bypassPermissions",
            "--max-turns", "8",
            "--model", model,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20 * 60,
    )
    trace_bindings = {}
    for label, path, content in (
        ("stream", stream_output, completed.stdout),
        ("stderr", stderr_output, completed.stderr),
    ):
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        trace_bindings[f"{label}_output"] = {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
        }
    events = []
    invalid_lines = []
    for line_number, line in enumerate(completed.stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            invalid_lines.append(line_number)
    combined = completed.stdout + "\n" + completed.stderr
    dispatched = False
    for event in events:
        for item in _walk(event):
            if item.get("type") == "tool_use" and item.get("name") == "Agent" \
                    and isinstance(item.get("input"), dict) \
                    and item["input"].get("subagent_type") == "Plan":
                dispatched = True
    discovery = evaluate_discovery_evidence(
        events,
        expected_entry_path=expected_entry_path,
        expected_entry=expected_entry,
        expected_entry_sha256=expected_entry_sha256,
        shared_description=policy["description"],
        applicability_gate=policy["applicability_gate"],
    )
    terminal = PROBE_MARKER in combined
    auth_blocked = "Authentication required" in combined
    status = "PASS" if (
        completed.returncode == 0 and not invalid_lines
        and discovery["discovery_evidence_observed"] and dispatched
        and discovery["applicability_gate_text_observed"] and terminal and not auth_blocked
    ) else "FAIL"
    return {
        "status": status,
        "returncode": completed.returncode,
        "event_count": len(events),
        "invalid_json_lines": invalid_lines,
        "installed_entry_path": str(expected_entry_path),
        "installed_entry_sha256": expected_entry_sha256,
        "shared_description_sha256": hashlib.sha256(
            policy["description"].encode("utf-8")
        ).hexdigest(),
        "applicability_gate_sha256": hashlib.sha256(
            policy["applicability_gate"].encode("utf-8")
        ).hexdigest(),
        **discovery,
        "native_agent_dispatch_observed": dispatched,
        "terminal_marker_observed": terminal,
        "authentication_blocked": auth_blocked,
        **trace_bindings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-source", type=Path, required=True)
    parser.add_argument("--platform-manifest", type=Path, required=True)
    parser.add_argument("--isolated-home", type=Path, required=True)
    parser.add_argument("--codebuddy", type=Path, default=DEFAULT_CODEBUDDY)
    parser.add_argument("--auth-home", type=Path)
    parser.add_argument("--model", default="custom-local:mimo-v2.5-pro")
    parser.add_argument("--stream-output", type=Path)
    parser.add_argument("--stderr-output", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    try:
        layout = install_projection(
            args.skill_source.resolve(), args.platform_manifest.resolve(),
            args.isolated_home.resolve(),
        )
        if args.auth_home:
            copy_auth_state(args.auth_home.resolve(), layout["home"], layout["config_root"])
        result = {
            "status": "INSTALLED",
            "config_root": str(layout["config_root"]),
            "skill_install": str(layout["skill_install"]),
        }
        if args.run:
            result = {
                **result,
                **run_probe(
                    args.codebuddy.resolve(), layout, args.model,
                    args.stream_output.resolve() if args.stream_output else None,
                    args.stderr_output.resolve() if args.stderr_output else None,
                ),
            }
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {"status": "FAIL", "reason": str(error)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"INSTALLED", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

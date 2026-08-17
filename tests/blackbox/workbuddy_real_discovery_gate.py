#!/usr/bin/env python3
"""显式运行的 WorkBuddy 真实 discovery + 原生 Agent 派发必过门禁。

文件名故意不以 ``test_`` 开头：普通离线 pytest 套件不得用 skip 掩盖登录前置；
候选门禁以独立命令提供认证 HOME 和只新建的证据目录。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROBE = PACKAGE_ROOT / "scripts" / "build" / "workbuddy_discovery_probe.py"
GENERATED = PACKAGE_ROOT / "generated" / "platforms" / "workbuddy" / "skill"
MANIFEST = PACKAGE_ROOT / "plugin-src" / "platforms" / "workbuddy" / "platform-manifest.json"
DEFAULT_CODEBUDDY = Path(
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-home", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--codebuddy", type=Path, default=DEFAULT_CODEBUDDY)
    args = parser.parse_args()

    evidence_dir = args.evidence_dir.resolve()
    if evidence_dir.exists():
        print(json.dumps({"status": "FAIL", "reason": "EVIDENCE_DIR_EXISTS"}))
        return 1
    evidence_dir.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="sfa-workbuddy-discovery-") as temp:
        command = [
            sys.executable,
            str(PROBE),
            "--skill-source", str(GENERATED),
            "--platform-manifest", str(MANIFEST),
            "--isolated-home", str(Path(temp) / "home"),
            "--codebuddy", str(args.codebuddy.resolve()),
            "--auth-home", str(args.auth_home.resolve()),
            "--stream-output", str(evidence_dir / "stream.jsonl"),
            "--stderr-output", str(evidence_dir / "stderr.txt"),
            "--run",
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=21 * 60
        )

    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        probe = {"status": "FAIL", "reason": f"INVALID_PROBE_SUMMARY: {error}"}
    for name, content in (
        ("probe-summary-stdout.txt", completed.stdout),
        ("probe-driver-stderr.txt", completed.stderr),
    ):
        with (evidence_dir / name).open("x", encoding="utf-8") as handle:
            handle.write(content)
    required = {
        "status": "PASS",
        "returncode": 0,
        "invalid_json_lines": [],
        "discovery_evidence_observed": True,
        "applicability_gate_text_observed": True,
        "native_agent_dispatch_observed": True,
        "terminal_marker_observed": True,
        "authentication_blocked": False,
    }
    failures = {
        key: {"expected": expected, "actual": probe.get(key)}
        for key, expected in required.items()
        if probe.get(key) != expected
    }
    if probe.get("discovery_evidence_mode") not in {"Skill", "Read"}:
        failures["discovery_evidence_mode"] = {
            "expected": ["Skill", "Read"],
            "actual": probe.get("discovery_evidence_mode"),
        }
    installed_sha = probe.get("installed_entry_sha256")
    if not isinstance(installed_sha, str) or len(installed_sha) != 64 \
            or any(character not in "0123456789abcdef" for character in installed_sha):
        failures["installed_entry_sha256"] = {
            "expected": "64 lowercase hex characters",
            "actual": installed_sha,
        }
    result = {
        "status": "PASS" if completed.returncode == 0 and not failures else "FAIL",
        "probe_exit_code": completed.returncode,
        "failures": failures,
        "probe": probe,
    }
    result_path = evidence_dir / "result.json"
    with result_path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

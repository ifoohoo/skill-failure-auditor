"""平台 JSONL 快速失败只检查外层协议，不扫描普通内容。"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[4]
RUNNER = WORKSPACE / "scripts" / "blackbox" / "run-platform.mjs"
RELEASE_TREE = Path(os.environ.get(
    "SFA_PREFLIGHT_RELEASE_TREE",
    str(WORKSPACE / "packages" / "skill-failure-auditor" / "generated"),
))
CANDIDATE_DIGEST = "a" * 64


def _run_fake_kimi(tmp_path: Path, *, attempt: int, body: str,
                   timeout: float = 12) -> tuple[subprocess.CompletedProcess, float, dict]:
    fake_home = tmp_path / f"home-{attempt}"
    fake_bin = fake_home / ".kimi-code" / "bin"
    fake_bin.mkdir(parents=True)
    fake_kimi = fake_bin / "kimi"
    fake_kimi.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'kimi-fake 1.0'; exit 0; fi\n"
        + body,
        encoding="utf-8",
    )
    fake_kimi.chmod(0o755)

    target = tmp_path / f"target-{attempt}"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
    out = tmp_path / "black-box" / f"audit-output-kimi-code-{attempt}"
    env = {**os.environ, "HOME": str(fake_home)}

    started = time.monotonic()
    result = subprocess.run(
        [
            "node", str(RUNNER),
            "--platform", "kimi-code",
            "--release-tree", str(RELEASE_TREE),
            "--target", str(target),
            "--evidence-type", "skill",
            "--out", str(out),
            "--candidate-digest", CANDIDATE_DIGEST,
            "--allow-real-run",
        ],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    blocked_path = out.parent / f"attempt-kimi-code-{attempt}-blocked.json"
    blocked = json.loads(blocked_path.read_text(encoding="utf-8"))
    return result, elapsed, blocked


def _run_fake_codex(tmp_path: Path, *, attempt: int, body: str) -> tuple[subprocess.CompletedProcess, dict]:
    fake_home = tmp_path / f"codex-home-{attempt}"
    fake_bin = fake_home / "bin"
    fake_bin.mkdir(parents=True)
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'codex-fake 1.0'; exit 0; fi\n"
        + body,
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    target = tmp_path / f"codex-target-{attempt}"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
    out = tmp_path / "black-box" / f"audit-output-codex-{attempt}"
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        [
            "node", str(RUNNER),
            "--platform", "codex",
            "--release-tree", str(RELEASE_TREE),
            "--target", str(target),
            "--evidence-type", "skill",
            "--out", str(out),
            "--candidate-digest", CANDIDATE_DIGEST,
            "--allow-real-run",
        ],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=12,
    )
    blocked_path = out.parent / f"attempt-codex-{attempt}-blocked.json"
    return result, json.loads(blocked_path.read_text(encoding="utf-8"))


def test_malformed_outer_json_is_sealed_without_waiting_for_timeout(tmp_path: Path) -> None:
    result, elapsed, blocked = _run_fake_kimi(
        tmp_path,
        attempt=1,
        body="printf '%s\\n' '{\"type\":\"assistant\"'\nsleep 30\n",
    )

    assert result.returncode != 0
    assert elapsed < 10, f"损坏的外层 JSON 未快速失败，耗时 {elapsed:.2f}s"
    assert blocked["classification"] == "INVALID_JSON_FAIL_FAST"
    assert blocked["reasonCode"] == "INVALID_JSON_OBSERVED"


def test_explicit_protocol_json_decode_error_is_sealed_fast(tmp_path: Path) -> None:
    result, elapsed, blocked = _run_fake_kimi(
        tmp_path,
        attempt=2,
        body=(
            "printf '%s\\n' "
            "'{\"type\":\"error\",\"error\":\"json.decoder.JSONDecodeError: Expecting comma\"}'\n"
            "sleep 30\n"
        ),
    )

    assert result.returncode != 0
    assert elapsed < 10, f"明确的协议解析错误未快速失败，耗时 {elapsed:.2f}s"
    assert blocked["classification"] == "INVALID_JSON_FAIL_FAST"
    assert blocked["reasonCode"] == "INVALID_JSON_OBSERVED"


def test_json_decode_error_text_inside_valid_content_is_not_fail_fast(tmp_path: Path) -> None:
    result, elapsed, blocked = _run_fake_kimi(
        tmp_path,
        attempt=3,
        body=(
            "printf '%s\\n' "
            "'{\"type\":\"tool_result\",\"content\":\"except (OSError, json.JSONDecodeError)\"}'\n"
        ),
    )

    assert result.returncode != 0
    assert elapsed < 10
    assert blocked["classification"] != "INVALID_JSON_FAIL_FAST"


def test_valid_json_split_across_stdout_chunks_is_not_fail_fast(tmp_path: Path) -> None:
    result, elapsed, blocked = _run_fake_kimi(
        tmp_path,
        attempt=4,
        body=(
            "printf '%s' '{\"type\":\"tool_result\",\"content\":\"json.'\n"
            "sleep 1\n"
            "printf '%s\\n' 'JSONDecodeError is source text\"}'\n"
        ),
    )

    assert result.returncode != 0
    assert elapsed < 10
    assert blocked["classification"] != "INVALID_JSON_FAIL_FAST"


def test_codex_plain_stdout_is_not_treated_as_jsonl_protocol(tmp_path: Path) -> None:
    result, blocked = _run_fake_codex(
        tmp_path,
        attempt=5,
        body="printf '%s\\n' 'plain stdout mentioning JSONDecodeError'\n",
    )

    assert result.returncode != 0
    assert blocked["classification"] != "INVALID_JSON_FAIL_FAST"
    out = tmp_path / "black-box" / "audit-output-codex-5"
    assert (out / "stream.jsonl").is_file()
    assert not (out / "rollout.jsonl").exists()

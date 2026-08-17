#!/usr/bin/env python3
"""Black-box gate v2 夹具测试（R3 交付物）。

在 /tmp 构造合成证据树，调用 node scripts/gate.mjs --only-blackbox
验证正向与负向用例。使用真实引擎 prepare-run / write-result / finalize-run
生成合法任务包与结果文件，原始流用最小合法合成 JSONL。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[4]  # tests/blackbox -> tests -> skill-failure-auditor -> packages -> workspace
ENGINE = WORKSPACE / "packages" / "skill-failure-auditor" / "plugin-src" / "core" / "scripts" / "orchestration_engine.py"
GATE = WORKSPACE / "scripts" / "gate.mjs"
PROMPTS_ROOT = WORKSPACE / "packages" / "skill-failure-auditor" / "plugin-src" / "core" / "prompts"

PLATFORMS = ["claude-code", "codex", "kimi-code", "workbuddy"]
VERSION_ONLY_PATHS = {
    "claude-code": ["skill/.claude-plugin/plugin.json"],
    "kimi-code": [".kimi-plugin/plugin.json", "kimi.plugin.json"],
    "workbuddy": [".codebuddy-plugin/plugin.json"],
}
INHERITANCE_SOURCE_VERSION = "1.0.0-candidate.10"
INHERITANCE_TARGET_VERSION = "1.0.0-candidate.15"
STATIC_ROLES = ["scope-routing", "static-audit", "evaluation-integrity",
                "adversarial-challenge", "result-synthesis"]
CODEX_TASK_NAMES = {role: role.replace("-", "_") for role in [
    "scope-routing", "static-audit", "runtime-evidence", "evaluation-integrity",
    "adversarial-challenge", "result-synthesis",
]}

# 角色→原生类型映射
ROLE_NATIVE_TYPE = {
    "claude-code": {
        "scope-routing": "Plan", "static-audit": "Explore",
        "evaluation-integrity": "general-purpose", "adversarial-challenge": "general-purpose",
        "result-synthesis": "general-purpose",
    },
    "kimi-code": {
        "scope-routing": "plan", "static-audit": "explore",
        "evaluation-integrity": "coder", "adversarial-challenge": "coder",
        "result-synthesis": "coder",
    },
    "workbuddy": {
        "scope-routing": "Plan", "static-audit": "Explore",
        "evaluation-integrity": "general-purpose", "adversarial-challenge": "general-purpose",
        "result-synthesis": "general-purpose",
    },
    "codex": {
        # codex 没有原生类型映射，task_name 绑定角色
        "scope-routing": None, "static-audit": None,
        "evaluation-integrity": None, "adversarial-challenge": None,
        "result-synthesis": None,
    },
}

RECEIPT_KIND = {
    "claude-code": "claude-trace",
    "codex": "codex-collaboration-receipt",
    "kimi-code": "kimi-stream-json",
    "workbuddy": "workbuddy-stream-json",
}

FAKE_CANDIDATE_DIGEST = "a" * 64
SCHEMA_VERSION = "2.1"


# ─── helpers ──────────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(obj) -> bytes:
    """Python-canonical JSON matching common.py canonical_json_bytes."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def projection_tree(root: Path) -> tuple[str, list[dict]]:
    files = []
    for item in sorted(path for path in root.rglob("*") if path.is_file()):
        files.append({
            "path": item.relative_to(root).as_posix(),
            "sha256": sha256_file(item),
        })
    digest_input = b"".join(
        entry["path"].encode("utf-8") + b"\0"
        + entry["sha256"].encode("ascii") + b"\n"
        for entry in files
    )
    return sha256_bytes(digest_input), files


def refresh_candidate_manifest(candidate_root: Path, version: str,
                               candidate_digest: str) -> None:
    manifest_files = []
    platforms_root = candidate_root / "platforms"
    for platform_dir in sorted(path for path in platforms_root.iterdir() if path.is_dir()):
        platform = platform_dir.name
        _, files = projection_tree(platform_dir)
        manifest_files.extend({
            "path": f"platforms/{platform}/{entry['path']}",
            "sha256": entry["sha256"],
        } for entry in files)
    (candidate_root / "candidate-manifest.json").write_text(json.dumps({
        "version": version,
        "candidateDigest": candidate_digest,
        "files": sorted(manifest_files, key=lambda entry: entry["path"]),
    }, indent=2) + "\n", encoding="utf-8")


def create_candidate_projection_root(base: Path, version: str, candidate_digest: str,
                                     platforms: list[str]) -> Path:
    candidate_root = base / version
    for platform in platforms:
        projection = candidate_root / "platforms" / platform
        skill = projection / "skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"unchanged install projection for {platform}\n", encoding="utf-8")
        for manifest_path in VERSION_ONLY_PATHS[platform]:
            manifest = projection / manifest_path
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({
                "name": f"skill-failure-auditor-{platform}",
                "version": version,
            }, indent=2) + "\n", encoding="utf-8")
    refresh_candidate_manifest(candidate_root, version, candidate_digest)
    return candidate_root


def create_inheritance_manifest(path: Path, source_root: Path, target_root: Path,
                                source_digest: str, target_digest: str,
                                bb_root: Path) -> Path:
    bindings = []
    for platform in ["claude-code", "kimi-code", "workbuddy"]:
        source_projection = source_root / "platforms" / platform
        target_projection = target_root / "platforms" / platform
        source_projection_digest, _ = projection_tree(source_projection)
        target_projection_digest, _ = projection_tree(target_projection)
        attempts = sorted(bb_root.glob(f"audit-output-{platform}-*"))
        assert len(attempts) == 1, f"expected one {platform} attempt, got {attempts}"
        summary = attempts[0] / "platform-summary.json"
        bindings.append({
            "platform": platform,
            "sourceCandidateVersion": INHERITANCE_SOURCE_VERSION,
            "targetCandidateVersion": INHERITANCE_TARGET_VERSION,
            "sourceCandidateDigest": source_digest,
            "targetCandidateDigest": target_digest,
            "sourceProjectionRoot": str(source_projection),
            "targetProjectionRoot": str(target_projection),
            "sourceProjectionDigest": source_projection_digest,
            "targetProjectionDigest": target_projection_digest,
            "sourcePlatformSummarySha256": sha256_file(summary),
            "allowedVersionOnlyPaths": VERSION_ONLY_PATHS[platform],
        })
    path.write_text(json.dumps({
        "schemaVersion": "1.0",
        "targetCandidateDigest": target_digest,
        "freshPlatforms": ["codex"],
        "bindings": bindings,
    }, indent=2) + "\n", encoding="utf-8")
    return path


def run_engine(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ENGINE), *args],
        capture_output=True, text=True,
        cwd=cwd or WORKSPACE,
    )


def run_gate(bb_root: str, candidate_digest: str | None = None,
             extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["node", str(GATE), "--only-blackbox", "--blackbox-root", bb_root]
    if candidate_digest:
        cmd.extend(["--candidate-digest", candidate_digest])
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=WORKSPACE)


def parse_gate_output(result: subprocess.CompletedProcess) -> dict:
    """解析 gate JSON 输出。"""
    return json.loads(result.stdout)


def create_raw_stream(platform: str, role: str, work_raw_dir: Path) -> Path:
    """创建最小合法原始流 JSONL 文件。"""
    work_raw_dir.mkdir(parents=True, exist_ok=True)
    path = work_raw_dir / f"{role}.jsonl"
    if platform in ("claude-code", "workbuddy"):
        native_type = ROLE_NATIVE_TYPE[platform][role]
        events = [
            {"type": "system", "subtype": "init"},
            {"tool": "Agent", "input": {"subagent_type": native_type,
                                            "description": role,
                                            "run_in_background": False}},
        ]
    elif platform == "kimi-code":
        native_type = ROLE_NATIVE_TYPE[platform][role]
        events = [
            {"type": "system", "subtype": "init"},
            {"tool": "Agent", "input": {"subagent_type": native_type,
                                            "description": role,
                                            "run_in_background": False}},
        ]
    elif platform == "codex":
        task_name = CODEX_TASK_NAMES[role]
        events = [
            {"type": "tool_use", "name": "collaboration.spawn_agent",
             "input": {"task_name": task_name, "message": f"Execute {role}", "fork_turns": "none"}},
            {"type": "tool_use", "name": "collaboration.wait_agent",
             "input": {"timeout_ms": 30000}},
        ]
    else:
        events = []
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def create_raw_stream_wrong_case_kimi(role: str, work_raw_dir: Path) -> Path:
    """创建 Kimi 流但使用大写 Plan（应触发 RAW_STREAM_WRONG_CASE）。"""
    work_raw_dir.mkdir(parents=True, exist_ok=True)
    path = work_raw_dir / f"{role}.jsonl"
    # 用大写 Plan 而不是小写 plan
    events = [
        {"tool": "Agent", "input": {"subagent_type": "Plan", "description": role}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def create_raw_stream_missing_dispatch(role: str, work_raw_dir: Path) -> Path:
    """创建缺少派发事件的原始流（应触发 RAW_STREAM_MISSING_DISPATCH）。"""
    work_raw_dir.mkdir(parents=True, exist_ok=True)
    path = work_raw_dir / f"{role}.jsonl"
    events = [{"type": "system", "subtype": "init", "note": "no dispatch event"}]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def create_raw_stream_missing_foreground(role: str, work_raw_dir: Path) -> Path:
    """创建 Agent 派发但省略显式前台字段。"""
    work_raw_dir.mkdir(parents=True, exist_ok=True)
    path = work_raw_dir / f"{role}.jsonl"
    native_type = ROLE_NATIVE_TYPE["claude-code"][role]
    events = [{"tool": "Agent", "input": {"subagent_type": native_type,
                                              "description": role}}]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def create_raw_stream_unsafe_codex_task_name(role: str, work_raw_dir: Path) -> Path:
    """创建旧式连字符 task_name，验证安全映射门禁。"""
    work_raw_dir.mkdir(parents=True, exist_ok=True)
    path = work_raw_dir / f"{role}.jsonl"
    events = [
        {"type": "tool_use", "name": "collaboration.spawn_agent",
         "input": {"task_name": role, "message": f"Execute {role}", "fork_turns": "none"}},
        {"type": "tool_use", "name": "collaboration.wait_agent",
         "input": {"timeout_ms": 30000}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def create_raw_stream_codex_response_item(role: str, work_raw_dir: Path) -> Path:
    """创建 Codex 0.145+ 的 response_item.payload 原生调用格式。"""
    work_raw_dir.mkdir(parents=True, exist_ok=True)
    path = work_raw_dir / f"{role}.jsonl"
    task_name = CODEX_TASK_NAMES[role]
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "namespace": "collaboration",
                "arguments": json.dumps({
                    "task_name": task_name,
                    "message": f"Execute {role}",
                    "fork_turns": "none",
                }),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "namespace": "collaboration",
                "arguments": json.dumps({"timeout_ms": 30000}),
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def create_receipt(platform: str, task_id: str, role: str, raw_path: Path) -> dict:
    """创建 L1 原生回执对象。"""
    native_type = ROLE_NATIVE_TYPE[platform].get(role, "agent")
    return {
        "platform": platform,
        "task_id": task_id,
        "role": role,
        "kind": RECEIPT_KIND[platform],
        "native_agent_type": native_type or "collaboration-agent",
        "invocation_id": f"inv-{platform}-{role}-001",
        "raw_record": {
            "path": str(raw_path.resolve()),
            "sha256": sha256_file(raw_path),
        },
        "completion": {
            "kind": "exit_status",
            "value": 0,
        },
    }


def create_artifact(platform: str, task_id: str, role: str,
                    semantic_status: str = "PASS_WITHIN_FROZEN_SCOPE",
                    conclusion_ceiling: str = "PASS_WITHIN_FROZEN_SCOPE",
                    selected_rules: list[dict] | None = None,
                    evidence_ref: dict | None = None) -> dict:
    """创建 L2 职责成果对象（不含 artifact_sha256）。"""
    artifact = {
        "schema_version": "1.0",
        "task_id": task_id,
        "platform": platform,
        "role": role,
        "semantic_status": semantic_status,
        "conclusion_ceiling": conclusion_ceiling,
        "rule_results": [],
        "findings": [
            {
                "statement": f"Synthetic finding for {role}",
                "evidence_refs": [],
            }
        ],
    }
    if role in {"static-audit", "runtime-evidence"}:
        assert selected_rules is not None and evidence_ref is not None
        artifact["rule_results"] = [
            {
                "id": item["id"],
                "revision": item["revision"],
                "severity": item["severity"],
                "status": "NOT_HIT",
                "reason": "Synthetic black-box fixture checked the frozen target.",
                "evidence_refs": [dict(evidence_ref)],
            }
            for item in selected_rules
        ]
    return artifact


def write_artifact_file(artifact: dict, path: Path) -> Path:
    """写入成果文件并计算 artifact_sha256。"""
    unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    artifact["artifact_sha256"] = sha256_bytes(canonical_json(unsigned))
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def create_output_file(work_dir: Path, role: str) -> tuple[Path, str]:
    """创建输出文件并返回 (路径, sha256)。"""
    path = work_dir / f"{role}-output.txt"
    content = f"Synthetic output for {role}\n"
    path.write_text(content, encoding="utf-8")
    return path, sha256_bytes(content.encode("utf-8"))


def build_platform_attempt(bb_root: Path, platform: str, task_num: int,
                           candidate_digest: str = FAKE_CANDIDATE_DIGEST,
                           semantic_status: str = "PASS_WITHIN_FROZEN_SCOPE",
                           conclusion_ceiling: str = "PASS_WITHIN_FROZEN_SCOPE",
                           custom_stream_fn=None,
                           skip_roles: set[str] | None = None,
                           extra_roles: set[str] | None = None,
                           ) -> tuple[Path, dict]:
    """为一个平台构建完整的合成尝试目录。

    返回 (attempt_dir, info)。info 包含 platform_summary 等数据。
    """
    attempt_dir = bb_root / f"audit-output-{platform}-{task_num}"
    work_dir = attempt_dir / "work"
    work_raw_dir = work_dir / "raw"
    results_dir = attempt_dir / "agent-results"

    # 创建目标
    target_dir = bb_root / "target-skill"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "skill.md").write_text("# Test Skill\n", encoding="utf-8")

    task_id = f"AUDIT-BBTEST-{platform.upper().replace('-', '')}{task_num:03d}"

    # 运行 prepare-run
    r = run_engine(
        "prepare-run",
        "--task-id", task_id,
        "--platform", platform,
        "--mode", "static",
        "--target", str(target_dir),
        "--evidence-type", "text",
        "--output-root", str(attempt_dir),
        "--prompts-root", str(PROMPTS_ROOT),
    )
    if r.returncode != 0:
        raise RuntimeError(f"prepare-run failed for {platform}: {r.stderr}\n{r.stdout}")

    # 读取任务包
    pkg_path = attempt_dir / "task-package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    expected_roles = list(pkg["expected_roles"])
    selection = json.loads((attempt_dir / "selection.json").read_text(encoding="utf-8"))
    selected_rules = selection["selection_context"]["selected_rules"]
    evidence_index = json.loads(
        (attempt_dir / "evidence-index.json").read_text(encoding="utf-8")
    )
    indexed = evidence_index["files"][0]
    evidence_ref = {"path": indexed["path"], "sha256": indexed["sha256"]}

    roles_to_process = list(expected_roles)
    if skip_roles:
        roles_to_process = [r for r in roles_to_process if r not in skip_roles]

    # 按依赖序处理每个角色
    receipt_files = {}
    artifact_files = {}
    output_infos = {}

    for role in roles_to_process:
        # 1. 创建原始流
        if custom_stream_fn:
            raw_path = custom_stream_fn(role, work_raw_dir)
        else:
            raw_path = create_raw_stream(platform, role, work_raw_dir)

        # 2. 创建回执
        receipt = create_receipt(platform, task_id, role, raw_path)
        receipt_path = work_dir / f"{role}-receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        receipt_files[role] = receipt_path

        # 3. 创建成果
        artifact = create_artifact(platform, task_id, role,
                                   semantic_status=semantic_status,
                                   conclusion_ceiling=conclusion_ceiling,
                                   selected_rules=selected_rules,
                                   evidence_ref=evidence_ref)
        artifact_path = work_dir / f"{role}-artifact.json"
        write_artifact_file(artifact, artifact_path)
        artifact_files[role] = artifact_path

        # 4. 创建输出文件
        out_path, out_sha = create_output_file(work_dir, role)
        output_infos[role] = {"path": str(out_path.resolve()), "sha256": out_sha}

        # 5. 写入 outputs JSON 文件
        outputs_json_path = work_dir / f"{role}-outputs.json"
        outputs_json_path.write_text(
            json.dumps([output_infos[role]], ensure_ascii=False) + "\n", encoding="utf-8")

        # 6. 运行 write-result
        wr = run_engine(
            "write-result",
            "--task-package", str(pkg_path),
            "--role", role,
            "--status", "COMPLETED",
            "--receipt-file", str(receipt_path),
            "--artifact-file", str(artifact_path),
            "--outputs-file", str(outputs_json_path),
            "--attempt", "1",
        )
        if wr.returncode != 0:
            raise RuntimeError(
                f"write-result failed for {platform}/{role}: {wr.stderr}\n{wr.stdout}")

    # 添加额外角色（如果要求）
    if extra_roles:
        for role in extra_roles:
            raw_path = create_raw_stream(platform, role, work_raw_dir)
            receipt = create_receipt(platform, task_id, role, raw_path)
            receipt_path = work_dir / f"{role}-receipt.json"
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
            artifact = create_artifact(
                platform, task_id, role,
                selected_rules=selected_rules,
                evidence_ref=evidence_ref,
            )
            artifact_path = work_dir / f"{role}-artifact.json"
            write_artifact_file(artifact, artifact_path)
            out_path, out_sha = create_output_file(work_dir, role)
            outputs_json_path = work_dir / f"{role}-outputs.json"
            outputs_json_path.write_text(
                json.dumps([{"path": str(out_path.resolve()), "sha256": out_sha}]) + "\n",
                encoding="utf-8")
            wr = run_engine(
                "write-result",
                "--task-package", str(pkg_path),
                "--role", role,
                "--status", "COMPLETED",
                "--receipt-file", str(receipt_path),
                "--artifact-file", str(artifact_path),
                "--outputs-file", str(outputs_json_path),
                "--attempt", "1",
            )
            # 可能因 ROLE_NOT_EXPECTED 失败，这是预期行为

    # 平台级直接捕获流只有一份；职责回执继续绑定各自不可变的原始记录。
    platform_stream_path = attempt_dir / "stream.jsonl"
    with platform_stream_path.open("wb") as platform_stream:
        for role in expected_roles:
            raw_path = work_raw_dir / f"{role}.jsonl"
            if raw_path.is_file():
                platform_stream.write(raw_path.read_bytes())

    # 运行 finalize-run
    fin = run_engine(
        "finalize-run",
        "--task-package", str(pkg_path),
        "--results-dir", str(results_dir),
        "--output-root", str(attempt_dir),
    )
    # finalize 可能因语义失败而 returncode != 0，此时会写 machine-report.json
    # 对于正向用例，它应该成功

    # 创建 platform-summary.json
    result_set_for_digest = []
    for role in expected_roles:
        result_path = results_dir / f"{role}.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            art_path = result.get("artifact", {}).get("path", "")
            sem_status = None
            if art_path and Path(art_path).is_file():
                art = json.loads(Path(art_path).read_text(encoding="utf-8"))
                sem_status = art.get("semantic_status")
            result_set_for_digest.append({
                "role": role,
                "result_sha256": result.get("result_sha256"),
                "semantic_status": sem_status,
            })
    set_digest = sha256_bytes(canonical_json(result_set_for_digest))

    # 构建 nativeDispatchFacts
    native_dispatch_facts = {}
    for role in expected_roles:
        native_type = ROLE_NATIVE_TYPE[platform].get(role)
        raw_path = work_raw_dir / f"{role}.jsonl"
        native_dispatch_facts[role] = {
            "nativeAgentType": native_type,
            "rawRecordSha256": sha256_file(raw_path) if raw_path.is_file() else None,
            "dispatched": True,
        }

    platform_summary = {
        "platform": platform,
        "attempt": task_num,
        "candidateDigest": candidate_digest,
        "taskPackageSha256": sha256_file(pkg_path),
        "resultSetSha256": set_digest,
        "platformStreamSha256": sha256_file(platform_stream_path),
        "nativeDispatchFacts": native_dispatch_facts,
        "unifiedContractStatus": "PASS" if fin.returncode == 0 else "FAIL",
        "verdict": "PASS" if fin.returncode == 0 else "FAIL",
        "platformCliVersion": "test-synthetic",
    }
    summary_path = attempt_dir / "platform-summary.json"
    summary_path.write_text(json.dumps(platform_summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")

    return attempt_dir, {
        "task_id": task_id,
        "package": pkg,
        "platform_summary": platform_summary,
        "finalization_returncode": fin.returncode,
    }


def build_all_four_platforms(bb_root: Path, candidate_digest: str = FAKE_CANDIDATE_DIGEST,
                             platform_overrides: dict | None = None) -> dict:
    """为四个平台各构建一个合成尝试。"""
    infos = {}
    for i, platform in enumerate(PLATFORMS):
        overrides = (platform_overrides or {}).get(platform, {})
        attempt_dir, info = build_platform_attempt(
            bb_root, platform, i + 1,
            candidate_digest=candidate_digest,
            **overrides,
        )
        infos[platform] = info
    return infos


# ─── 正向测试 ─────────────────────────────────────────────────────────

class TestGateBlackboxPositive:
    """正向：四平台各一个合法合成尝试 → verdict PASS。"""

    def test_four_platform_pass(self, tmp_path):
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        infos = build_all_four_platforms(bb_root)

        # 验证所有 finalize 成功
        for pid, info in infos.items():
            assert info["finalization_returncode"] == 0, \
                f"finalize-run failed for {pid}: check engine output"

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)

        assert output["verdict"] == "PASS", \
            f"Expected PASS but got FAIL: {result.stderr}\nstdout: {result.stdout}"

    def test_gate_does_not_mutate_registered_role_results(self, tmp_path):
        """平台流复验只读；已登记职责结果在门禁前后字节不变。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root)
        result_paths = sorted(bb_root.glob("audit-output-*/agent-results/*.json"))
        before = {str(path): sha256_file(path) for path in result_paths}

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)

        assert result.returncode == 0, result.stderr
        after = {str(path): sha256_file(path) for path in result_paths}
        assert after == before

    def test_external_equivalence_uses_current_evidence(self, tmp_path):
        """公开矩阵无运行摘要时，等价门禁直接比较本次四平台证据。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root)

        result = run_gate(
            str(bb_root),
            candidate_digest=FAKE_CANDIDATE_DIGEST,
            extra_args=["--check-equivalence"],
        )
        output = parse_gate_output(result)
        assert output["verdict"] == "PASS", result.stderr

        detail = json.loads([r for r in output["results"]
                             if r["id"] == "blackbox-receipt-v2"][0]["detail"])
        for platform in detail["platforms"]:
            layer = platform["traceability"]["9_externalEquivalence"]
            assert layer["ok"] is True
            assert all(check["ok"] for check in layer["checks"].values())

    def test_version_only_platform_projection_can_inherit_bound_evidence(self, tmp_path):
        """三平台只迁移固定清单 version 时可继承；Codex 仍须目标候选新运行。"""
        source_digest = "a" * 64
        target_digest = "b" * 64
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        for index, platform in enumerate(PLATFORMS, start=1):
            build_platform_attempt(
                bb_root, platform, index,
                candidate_digest=target_digest if platform == "codex" else source_digest,
            )
        inherited_platforms = ["claude-code", "kimi-code", "workbuddy"]
        source_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_SOURCE_VERSION, source_digest, inherited_platforms,
        )
        target_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_TARGET_VERSION, target_digest, inherited_platforms,
        )
        inheritance = create_inheritance_manifest(
            tmp_path / "inheritance.json", source_root, target_root,
            source_digest, target_digest, bb_root,
        )

        result = run_gate(
            str(bb_root), candidate_digest=target_digest,
            extra_args=["--inheritance-manifest", str(inheritance), "--check-equivalence"],
        )
        output = parse_gate_output(result)
        assert output["verdict"] == "PASS", result.stderr
        detail = json.loads([entry for entry in output["results"]
                             if entry["id"] == "blackbox-receipt-v2"][0]["detail"])
        bindings = {
            platform["platform"]: platform["traceability"]["1_candidateDigest"]["binding"]
            for platform in detail["platforms"]
        }
        assert bindings["codex"] == "FRESH_CANDIDATE_EXECUTION"
        for platform in inherited_platforms:
            assert bindings[platform] == "INHERITED_VERSION_ONLY_PLATFORM_PROJECTION"

    def test_semantic_blocked_attempt_can_pass_execution_contract(self, tmp_path):
        """五职责与凭据完整但目标语义 BLOCKED：执行门禁通过，语义状态原样报告。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        infos = build_all_four_platforms(
            bb_root,
            platform_overrides={
                "claude-code": {
                    "semantic_status": "BLOCKED",
                    "conclusion_ceiling": "BLOCKED",
                }
            },
        )
        assert infos["claude-code"]["finalization_returncode"] != 0
        cc_dir = bb_root / "audit-output-claude-code-1"
        assert not (cc_dir / "finalization.json").exists()
        machine = json.loads((cc_dir / "machine-report.json").read_text(encoding="utf-8"))
        assert machine["reason"] == "SEMANTIC_FAILURE"

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "PASS", result.stderr
        detail = json.loads([r for r in output["results"]
                             if r["id"] == "blackbox-receipt-v2"][0]["detail"])
        claude = [p for p in detail["platforms"] if p["platform"] == "claude-code"][0]
        assert claude["executionContractStatus"] == "PASS"
        assert claude["targetSemanticStatus"] == "BLOCKED"


# ─── 负向测试 ─────────────────────────────────────────────────────────

class TestGateBlackboxNegative:
    """负向：每项断言精确失败码。"""

    def test_platform_stream_digest_tamper_is_rejected(self, tmp_path):
        """单一直接平台流在汇总后漂移 → PLATFORM_STREAM_DIGEST_MISMATCH。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root)
        stream_path = bb_root / "audit-output-claude-code-1" / "stream.jsonl"
        with stream_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "system", "note": "tampered"}) + "\n")

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)

        assert output["verdict"] == "FAIL"
        bb_check = [r for r in output["results"]
                    if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        claude = [p for p in detail["platforms"]
                  if p["platform"] == "claude-code"][0]
        codes = [failure if isinstance(failure, str) else failure["code"]
                 for failure in claude["failures"]]
        assert "PLATFORM_STREAM_DIGEST_MISMATCH" in codes, codes

    def test_inheritance_rejects_changed_target_projection(self, tmp_path):
        """目标候选平台安装树只改一个字节也不得继承旧运行证据。"""
        source_digest = "a" * 64
        target_digest = "b" * 64
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        for index, platform in enumerate(PLATFORMS, start=1):
            build_platform_attempt(
                bb_root, platform, index,
                candidate_digest=target_digest if platform == "codex" else source_digest,
            )
        inherited_platforms = ["claude-code", "kimi-code", "workbuddy"]
        source_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_SOURCE_VERSION, source_digest, inherited_platforms,
        )
        target_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_TARGET_VERSION, target_digest, inherited_platforms,
        )
        target_skill = target_root / "platforms" / "kimi-code" / "skill" / "SKILL.md"
        target_skill.write_text("tampered but rebound in target manifest\n", encoding="utf-8")
        refresh_candidate_manifest(target_root, INHERITANCE_TARGET_VERSION, target_digest)
        inheritance = create_inheritance_manifest(
            tmp_path / "inheritance.json", source_root, target_root,
            source_digest, target_digest, bb_root,
        )

        result = run_gate(
            str(bb_root), candidate_digest=target_digest,
            extra_args=["--inheritance-manifest", str(inheritance)],
        )
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"
        detail = json.loads([entry for entry in output["results"]
                             if entry["id"] == "blackbox-receipt-v2"][0]["detail"])
        kimi = [platform for platform in detail["platforms"]
                if platform["platform"] == "kimi-code"][0]
        codes = [failure if isinstance(failure, str) else failure["code"]
                 for failure in kimi["failures"]]
        assert "INHERITED_PROJECTION_BINDING_INVALID" in codes

    @pytest.mark.parametrize("mutation", ["other-field", "whitespace"])
    def test_inheritance_rejects_non_version_change_in_allowlisted_manifest(
            self, tmp_path, mutation):
        """固定清单也只允许 version 值变化；其他字段或排版变化必须拒绝。"""
        source_digest = "a" * 64
        target_digest = "b" * 64
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        for index, platform in enumerate(PLATFORMS, start=1):
            build_platform_attempt(
                bb_root, platform, index,
                candidate_digest=target_digest if platform == "codex" else source_digest,
            )
        inherited_platforms = ["claude-code", "kimi-code", "workbuddy"]
        source_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_SOURCE_VERSION, source_digest, inherited_platforms,
        )
        target_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_TARGET_VERSION, target_digest, inherited_platforms,
        )
        manifest = target_root / "platforms" / "claude-code" / VERSION_ONLY_PATHS["claude-code"][0]
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if mutation == "other-field":
            data["name"] = "tampered-name"
            manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            manifest.write_text(json.dumps(data, separators=(",", ":")) + "\n",
                                encoding="utf-8")
        refresh_candidate_manifest(target_root, INHERITANCE_TARGET_VERSION, target_digest)
        inheritance = create_inheritance_manifest(
            tmp_path / "inheritance.json", source_root, target_root,
            source_digest, target_digest, bb_root,
        )

        output = parse_gate_output(run_gate(
            str(bb_root), candidate_digest=target_digest,
            extra_args=["--inheritance-manifest", str(inheritance)],
        ))
        assert output["verdict"] == "FAIL"
        detail = json.loads([entry for entry in output["results"]
                             if entry["id"] == "blackbox-receipt-v2"][0]["detail"])
        claude = [platform for platform in detail["platforms"]
                  if platform["platform"] == "claude-code"][0]
        codes = [failure if isinstance(failure, str) else failure["code"]
                 for failure in claude["failures"]]
        assert "INHERITED_PROJECTION_BINDING_INVALID" in codes

    def test_inheritance_rejects_manifest_supplied_allowlist_expansion(self, tmp_path):
        """继承清单不能自行扩大固定允许路径。"""
        source_digest = "a" * 64
        target_digest = "b" * 64
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        for index, platform in enumerate(PLATFORMS, start=1):
            build_platform_attempt(
                bb_root, platform, index,
                candidate_digest=target_digest if platform == "codex" else source_digest,
            )
        inherited_platforms = ["claude-code", "kimi-code", "workbuddy"]
        source_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_SOURCE_VERSION, source_digest, inherited_platforms,
        )
        target_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_TARGET_VERSION, target_digest, inherited_platforms,
        )
        inheritance = create_inheritance_manifest(
            tmp_path / "inheritance.json", source_root, target_root,
            source_digest, target_digest, bb_root,
        )
        data = json.loads(inheritance.read_text(encoding="utf-8"))
        data["bindings"][0]["allowedVersionOnlyPaths"].append("skill/SKILL.md")
        inheritance.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        output = parse_gate_output(run_gate(
            str(bb_root), candidate_digest=target_digest,
            extra_args=["--inheritance-manifest", str(inheritance)],
        ))
        assert output["verdict"] == "FAIL"
        detail = json.loads([entry for entry in output["results"]
                             if entry["id"] == "blackbox-receipt-v2"][0]["detail"])
        assert any(failure["code"] == "INHERITANCE_MANIFEST_INVALID"
                   for failure in detail["overallFailures"])

    @pytest.mark.parametrize(
        ("field", "wrong_version"),
        [
            ("sourceCandidateVersion", "1.0.0-candidate.11"),
            ("targetCandidateVersion", "1.0.0-candidate.14"),
        ],
    )
    def test_inheritance_rejects_wrong_candidate15_version_coordinates(
            self, tmp_path, field, wrong_version):
        """继承坐标只能是 candidate.10 → candidate.15，错误坐标必须失败关闭。"""
        source_digest = "a" * 64
        target_digest = "b" * 64
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        for index, platform in enumerate(PLATFORMS, start=1):
            build_platform_attempt(
                bb_root, platform, index,
                candidate_digest=target_digest if platform == "codex" else source_digest,
            )
        inherited_platforms = ["claude-code", "kimi-code", "workbuddy"]
        source_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_SOURCE_VERSION, source_digest, inherited_platforms,
        )
        target_root = create_candidate_projection_root(
            tmp_path, INHERITANCE_TARGET_VERSION, target_digest, inherited_platforms,
        )
        inheritance = create_inheritance_manifest(
            tmp_path / "inheritance.json", source_root, target_root,
            source_digest, target_digest, bb_root,
        )
        data = json.loads(inheritance.read_text(encoding="utf-8"))
        data["bindings"][0][field] = wrong_version
        inheritance.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        output = parse_gate_output(run_gate(
            str(bb_root), candidate_digest=target_digest,
            extra_args=["--inheritance-manifest", str(inheritance)],
        ))
        assert output["verdict"] == "FAIL"
        detail = json.loads([entry for entry in output["results"]
                             if entry["id"] == "blackbox-receipt-v2"][0]["detail"])
        assert any(failure["code"] == "INHERITANCE_MANIFEST_INVALID"
                   for failure in detail["overallFailures"])

    def test_external_equivalence_allows_independent_semantic_judgment(self, tmp_path):
        """合同与输入相同但审阅结论不同，不应被误判为平台执行不等价。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        infos = build_all_four_platforms(
            bb_root,
            platform_overrides={
                "kimi-code": {
                    "semantic_status": "NEEDS_REVISION",
                    "conclusion_ceiling": "NEEDS_REVISION",
                },
            },
        )
        assert all(info["finalization_returncode"] == 0 for info in infos.values())

        without_equivalence = run_gate(
            str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        assert parse_gate_output(without_equivalence)["verdict"] == "PASS"

        result = run_gate(
            str(bb_root),
            candidate_digest=FAKE_CANDIDATE_DIGEST,
            extra_args=["--check-equivalence"],
        )
        output = parse_gate_output(result)
        assert output["verdict"] == "PASS"
        detail = json.loads([r for r in output["results"]
                             if r["id"] == "blackbox-receipt-v2"][0]["detail"])
        assert detail["overallFailures"] == []
        assert all(
            platform["traceability"]["9_externalEquivalence"]["checks"]
            ["status_matrices_complete"]["ok"]
            for platform in detail["platforms"]
        )

    def test_missing_platform(self, tmp_path):
        """缺一平台 → MISSING_PLATFORM。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        # 只构建三个平台，跳过 workbuddy
        for i, platform in enumerate(["claude-code", "codex", "kimi-code"]):
            build_platform_attempt(bb_root, platform, i + 1)

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        # 检查 blackbox-receipt-v2 的 detail 包含 MISSING_PLATFORM
        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        assert bb_check["status"] == "FAIL"
        detail = json.loads(bb_check["detail"])
        overall_failures = detail.get("overallFailures", [])
        codes = [f["code"] for f in overall_failures]
        assert "MISSING_PLATFORM" in codes, f"Expected MISSING_PLATFORM, got {codes}"

    def test_duplicate_successful_attempt(self, tmp_path):
        """同平台两个 COMPLETE → DUPLICATE_SUCCESSFUL_ATTEMPT。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        # 为 claude-code 构建两个成功尝试
        build_platform_attempt(bb_root, "claude-code", 1)
        build_platform_attempt(bb_root, "claude-code", 2)
        # 其他平台各一个
        for i, platform in enumerate(["codex", "kimi-code", "workbuddy"]):
            build_platform_attempt(bb_root, platform, i + 1)

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        cc = [p for p in platforms if p["platform"] == "claude-code"][0]
        assert cc["verdict"] == "FAIL"
        failure_codes = cc.get("failures", [])
        assert "DUPLICATE_SUCCESSFUL_ATTEMPT" in failure_codes, \
            f"Expected DUPLICATE_SUCCESSFUL_ATTEMPT, got {failure_codes}"

    def test_stale_candidate_digest(self, tmp_path):
        """候选摘要不一致 → STALE_CANDIDATE_DIGEST。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root, candidate_digest=FAKE_CANDIDATE_DIGEST)

        # 使用不同的候选摘要运行 gate
        wrong_digest = "b" * 64
        result = run_gate(str(bb_root), candidate_digest=wrong_digest)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        # 至少一个平台有 STALE_CANDIDATE_DIGEST
        all_failure_codes = []
        for p in platforms:
            if p.get("failures"):
                all_failure_codes.extend(p["failures"])
        assert "STALE_CANDIDATE_DIGEST" in all_failure_codes, \
            f"Expected STALE_CANDIDATE_DIGEST, got {all_failure_codes}"

    def test_raw_record_digest_mismatch(self, tmp_path):
        """raw 记录篡改一字节 → RAW_RECORD_DIGEST_MISMATCH。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root)

        # 篡改 claude-code 的某个原始流文件
        cc_dir = bb_root / "audit-output-claude-code-1"
        raw_dir = cc_dir / "work" / "raw"
        # 找到第一个 jsonl 文件并修改
        jsonl_files = sorted(raw_dir.glob("*.jsonl"))
        assert len(jsonl_files) > 0
        target = jsonl_files[0]
        original = target.read_text(encoding="utf-8")
        target.write_text(original + '{"tampered": true}\n', encoding="utf-8")

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        cc = [p for p in platforms if p["platform"] == "claude-code"][0]
        failure_codes = cc.get("failures", [])
        assert "RAW_RECORD_DIGEST_MISMATCH" in failure_codes, \
            f"Expected RAW_RECORD_DIGEST_MISMATCH, got {failure_codes}"

    def test_raw_stream_missing_dispatch(self, tmp_path):
        """原始流缺某角色派发 → RAW_STREAM_MISSING_DISPATCH。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()

        # 为 claude-code 使用缺少派发的流
        def missing_dispatch_fn(role, work_raw_dir):
            return create_raw_stream_missing_dispatch(role, work_raw_dir)

        build_platform_attempt(bb_root, "claude-code", 1,
                               custom_stream_fn=missing_dispatch_fn)
        for i, platform in enumerate(["codex", "kimi-code", "workbuddy"]):
            build_platform_attempt(bb_root, platform, i + 1)

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        cc = [p for p in platforms if p["platform"] == "claude-code"][0]
        failure_codes = cc.get("failures", [])
        assert "RAW_STREAM_MISSING_DISPATCH" in failure_codes, \
            f"Expected RAW_STREAM_MISSING_DISPATCH, got {failure_codes}"

    def test_claude_foreground_must_be_explicit(self, tmp_path):
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_platform_attempt(bb_root, "claude-code", 1,
                               custom_stream_fn=create_raw_stream_missing_foreground)
        for i, platform in enumerate(["codex", "kimi-code", "workbuddy"], start=2):
            build_platform_attempt(bb_root, platform, i)

        output = parse_gate_output(run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST))
        assert output["verdict"] == "FAIL"
        detail = json.loads([r for r in output["results"]
                             if r["id"] == "blackbox-receipt-v2"][0]["detail"])
        cc = [p for p in detail["platforms"] if p["platform"] == "claude-code"][0]
        assert "RAW_STREAM_FOREGROUND_NOT_EXPLICIT" in cc.get("failures", [])

    def test_workbuddy_does_not_require_claude_foreground_field(self, tmp_path):
        """WorkBuddy 原生流无该字段时，真实派发仍可由其余绑定证明。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_platform_attempt(bb_root, "claude-code", 1)
        build_platform_attempt(bb_root, "codex", 2)
        build_platform_attempt(bb_root, "kimi-code", 3)
        build_platform_attempt(bb_root, "workbuddy", 4,
                               custom_stream_fn=create_raw_stream_missing_foreground)

        output = parse_gate_output(run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST))
        assert output["verdict"] == "PASS"

    def test_codex_accepts_native_response_item_payload(self, tmp_path):
        """Codex 0.145+ rollout 的 payload function_call 必须能被独立解析。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_platform_attempt(bb_root, "claude-code", 1)
        build_platform_attempt(bb_root, "codex", 2,
                               custom_stream_fn=create_raw_stream_codex_response_item)
        build_platform_attempt(bb_root, "kimi-code", 3)
        build_platform_attempt(bb_root, "workbuddy", 4)

        output = parse_gate_output(run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST))
        assert output["verdict"] == "PASS"

    def test_codex_rejects_unsafe_semantic_role_as_task_name(self, tmp_path):
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_platform_attempt(bb_root, "claude-code", 1)
        build_platform_attempt(bb_root, "codex", 2,
                               custom_stream_fn=create_raw_stream_unsafe_codex_task_name)
        build_platform_attempt(bb_root, "kimi-code", 3)
        build_platform_attempt(bb_root, "workbuddy", 4)

        output = parse_gate_output(run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST))
        assert output["verdict"] == "FAIL"
        detail = json.loads([r for r in output["results"]
                             if r["id"] == "blackbox-receipt-v2"][0]["detail"])
        codex = [p for p in detail["platforms"] if p["platform"] == "codex"][0]
        assert "RAW_STREAM_MISSING_DISPATCH" in codex.get("failures", [])

    def test_kimi_wrong_case(self, tmp_path):
        """kimi 流中出现大写 Plan → RAW_STREAM_WRONG_CASE。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()

        # 为 kimi-code 使用大写 Plan 的流
        def wrong_case_fn(role, work_raw_dir):
            return create_raw_stream_wrong_case_kimi(role, work_raw_dir)

        for i, platform in enumerate(["claude-code", "codex"]):
            build_platform_attempt(bb_root, platform, i + 1)
        build_platform_attempt(bb_root, "kimi-code", 3,
                               custom_stream_fn=wrong_case_fn)
        build_platform_attempt(bb_root, "workbuddy", 4)

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        kimi = [p for p in platforms if p["platform"] == "kimi-code"][0]
        failure_codes = kimi.get("failures", [])
        assert "RAW_STREAM_WRONG_CASE" in failure_codes, \
            f"Expected RAW_STREAM_WRONG_CASE, got {failure_codes}"

    def test_semantic_failure_not_propagated(self, tmp_path):
        """成果 semantic_status=BLOCKED 外壳 COMPLETED → SEMANTIC_FAILURE_NOT_PROPAGATED。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()

        # 构建 claude-code 但有一个角色的 semantic_status=BLOCKED
        # 注意：引擎 finalize-run 会检测到语义失败并写 machine-report.json
        # 不写 finalization.json，所以 gate 不会认为这是一个成功尝试
        # 但是如果我们手工添加 finalization.json（模拟绕过引擎的情况），
        # gate 的语义门禁应该捕获
        #
        # 实际上，引擎的 finalize-run 已经会阻止这种情况。
        # 这个测试验证：当 finalize 被绕过（手工添加 finalization.json）时，
        # gate 的独立语义门禁仍然捕获。

        # 构建正常尝试（semantic_status=PASS），然后手工修改成果文件
        build_all_four_platforms(bb_root)

        # 修改 claude-code 的一个成果文件的 semantic_status
        cc_dir = bb_root / "audit-output-claude-code-1"
        work_dir = cc_dir / "work"
        # 找到 scope-routing 的成果文件
        artifact_path = work_dir / "scope-routing-artifact.json"
        if artifact_path.is_file():
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["semantic_status"] = "BLOCKED"
            # 重新计算 artifact_sha256
            unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
            artifact["artifact_sha256"] = sha256_bytes(canonical_json(unsigned))
            artifact_path.write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        cc = [p for p in platforms if p["platform"] == "claude-code"][0]
        failure_codes = cc.get("failures", [])
        # 可能触发 SEMANTIC_FAILURE_NOT_PROPAGATED 或引擎复验失败
        has_semantic_or_engine_fail = (
            "SEMANTIC_FAILURE_NOT_PROPAGATED" in failure_codes or
            "ENGINE_REVALIDATION_FAILED" in failure_codes or
            "ARTIFACT_DIGEST_MISMATCH" in failure_codes
        )
        assert has_semantic_or_engine_fail, \
            f"Expected semantic/engine failure, got {failure_codes}"

    def test_missing_receipt_engine_fails(self, tmp_path):
        """缺回执（结果无 native_receipt）→ 引擎复验失败。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root)

        # 移除 claude-code 的一个结果文件中的 native_receipt
        cc_dir = bb_root / "audit-output-claude-code-1"
        results_dir = cc_dir / "agent-results"
        result_file = results_dir / "scope-routing.json"
        if result_file.is_file():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            result["native_receipt"] = None
            # 重新计算 result_sha256
            body = {k: v for k, v in result.items() if k != "result_sha256"}
            result["result_sha256"] = sha256_bytes(canonical_json(body))
            result_file.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        cc = [p for p in platforms if p["platform"] == "claude-code"][0]
        failure_codes = cc.get("failures", [])
        # 引擎复验会因 Schema 校验失败而拒绝
        has_failure = any(code in failure_codes for code in [
            "ENGINE_REVALIDATION_FAILED", "WRONG_RECEIPT_KIND",
            "MISSING_NATIVE_RECEIPT", "RESULT_DIGEST_MISMATCH"
        ])
        assert has_failure, f"Expected engine/receipt failure, got {failure_codes}"

    def test_no_candidate_digest_fails(self, tmp_path):
        """full 模式不传 --candidate-digest → blackbox FAIL。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_all_four_platforms(bb_root)

        # 不传 --candidate-digest
        result = run_gate(str(bb_root), candidate_digest=None)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

        bb_check = [r for r in output["results"] if r["id"] == "blackbox-receipt-v2"][0]
        detail = json.loads(bb_check["detail"])
        platforms = detail.get("platforms", [])
        all_codes = []
        for p in platforms:
            if p.get("failures"):
                all_codes.extend(p["failures"])
        assert "MISSING_CANDIDATE_DIGEST" in all_codes, \
            f"Expected MISSING_CANDIDATE_DIGEST, got {all_codes}"


# ─── 回归测试 ─────────────────────────────────────────────────────────

class TestGateBlackboxRegression:
    """回归：旧判据行为不再存在。"""

    def test_old_passcount_logic_removed(self):
        """gate.mjs 不含 passCount >= 1（或等效）。"""
        gate_code = GATE.read_text(encoding="utf-8")
        assert "passCount >= 1" not in gate_code, \
            "旧 passCount >= 1 判据仍存在于 gate.mjs"
        assert "passCount" not in gate_code, \
            "passCount 变量名仍存在于 gate.mjs（应已完全移除）"

    def test_single_platform_fails(self, tmp_path):
        """单平台 COMPLETE 其余缺失时 verdict=FAIL。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()
        build_platform_attempt(bb_root, "claude-code", 1)

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL", \
            "单平台应 FAIL（精确四平台集合要求）"

    def test_empty_evidence_root_fails(self, tmp_path):
        """空证据根 → FAIL。"""
        bb_root = tmp_path / "black-box"
        bb_root.mkdir()

        result = run_gate(str(bb_root), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

    def test_nonexistent_evidence_root_fails(self, tmp_path):
        """不存在的证据根 → FAIL。"""
        result = run_gate(str(tmp_path / "nonexistent"), candidate_digest=FAKE_CANDIDATE_DIGEST)
        output = parse_gate_output(result)
        assert output["verdict"] == "FAIL"

"""统一编排协议 v2.1 引擎测试：三层对象验证、依赖序强制、语义状态单调传播与失败关闭。

R1 重建编排结果真实性合同：
- 13 个负向测试（必须先红后绿，断言明确失败码和无成功终态文件）；
- 正向测试（static/runtime/combined 三模式完整链路 + 自包含回归）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
ENGINE = SCRIPTS_DIR / "orchestration_engine.py"

ROLES_STATIC = ["scope-routing", "static-audit", "evaluation-integrity",
                "adversarial-challenge", "result-synthesis"]
ROLES_RUNTIME = ["scope-routing", "runtime-evidence", "evaluation-integrity",
                 "adversarial-challenge", "result-synthesis"]
ROLES_COMBINED = ["scope-routing", "static-audit", "runtime-evidence",
                  "evaluation-integrity", "adversarial-challenge", "result-synthesis"]

PLATFORM = "claude-code"
RECEIPT_KIND = "claude-trace"

ROLE_NATIVE_TYPES = {
    "scope-routing": "Plan",
    "static-audit": "Explore",
    "runtime-evidence": "Explore",
    "evaluation-integrity": "general-purpose",
    "adversarial-challenge": "general-purpose",
    "result-synthesis": "general-purpose",
}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def compute_artifact_sha256(artifact: dict) -> str:
    unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    return sha256_bytes(canonical(unsigned))


class EngineHarness:
    """测试治具：创建最小目录结构，提供 prepare/write/validate/finalize 封装。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="orch-v21-test-"))
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        all_roles = set(ROLES_STATIC + ROLES_RUNTIME + ROLES_COMBINED)
        for role in all_roles:
            (self.prompts / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")
        self.target = self.root / "target"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("---\nname: sample\n---\n# sample\n", encoding="utf-8")
        self.output = self.root / "run"

    def run_engine(self, *args: str):
        return subprocess.run([sys.executable, str(ENGINE), *args],
                              capture_output=True, text=True)

    def prepare(self, task_id="AUDIT-V21-TEST-001", mode="static", platform=PLATFORM):
        return self.run_engine(
            "prepare-run", "--task-id", task_id, "--platform", platform, "--mode", mode,
            "--target", str(self.target), "--evidence-type", "text",
            "--output-root", str(self.output), "--prompts-root", str(self.prompts))

    def package_path(self) -> Path:
        return self.output / "task-package.json"

    def results_dir(self) -> Path:
        return self.output / "agent-results"

    def work_dir(self) -> Path:
        return self.output / "work"

    def validate(self):
        return self.run_engine("validate-result-set", "--task-package", str(self.package_path()),
                               "--results-dir", str(self.results_dir()))

    def validate_execution(self):
        return self.run_engine("validate-execution-set", "--task-package", str(self.package_path()),
                               "--results-dir", str(self.results_dir()))

    def finalize(self):
        return self.run_engine("finalize-run", "--task-package", str(self.package_path()),
                               "--results-dir", str(self.results_dir()),
                               "--output-root", str(self.output))

    def create_output_file(self, role: str, content: str = "# test output") -> dict:
        """在 work/ 下创建真实输出文件，返回 {path, sha256}。"""
        fpath = self.work_dir() / f"{role}-output.md"
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        return {"path": str(fpath.resolve()), "sha256": sha256_file(fpath)}

    def create_raw_record(self, role: str, content: str = "raw-trace-data") -> dict:
        """在 work/ 下创建原始记录文件，返回 {path, sha256}。"""
        fpath = self.work_dir() / f"{role}-raw-record.json"
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        return {"path": str(fpath.resolve()), "sha256": sha256_file(fpath)}

    def create_valid_receipt(self, role: str, task_id="AUDIT-V21-TEST-001",
                              platform=PLATFORM, native_agent_type=None) -> str:
        """创建合法回执文件，返回路径字符串。"""
        raw = self.create_raw_record(role)
        if native_agent_type is None:
            native_agent_type = ROLE_NATIVE_TYPES.get(role, "general-purpose")
        receipt = {
            "platform": platform,
            "task_id": task_id,
            "role": role,
            "kind": RECEIPT_KIND,
            "native_agent_type": native_agent_type,
            "invocation_id": f"inv-{role}-001",
            "raw_record": raw,
            "completion": {"kind": "exit_status", "value": 0}
        }
        path = self.work_dir() / f"{role}-receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def create_valid_artifact(self, role: str, task_id="AUDIT-V21-TEST-001",
                               platform=PLATFORM, semantic_status="PASS_WITHIN_FROZEN_SCOPE",
                               conclusion_ceiling="PASS_WITHIN_FROZEN_SCOPE",
                               findings=None) -> str:
        """创建合法职责成果文件，返回路径字符串。"""
        if findings is None:
            findings = [{"statement": "Test finding", "evidence_refs": []}]
        artifact = {
            "schema_version": "1.0",
            "task_id": task_id,
            "platform": platform,
            "role": role,
            "semantic_status": semantic_status,
            "conclusion_ceiling": conclusion_ceiling,
            "findings": findings,
            "artifact_sha256": ""
        }
        artifact["artifact_sha256"] = compute_artifact_sha256(artifact)
        path = self.work_dir() / f"{role}-artifact.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def write_result_via_engine(self, role: str, status="COMPLETED", attempt=1,
                                 receipt_file=None, artifact_file=None,
                                 outputs=None, error=None):
        """通过引擎 write-result 命令登记结果。"""
        if outputs is None and status == "COMPLETED":
            out = self.create_output_file(role)
            outputs = [out]

        outputs_file = None
        if outputs:
            ofpath = self.work_dir() / f"{role}-outputs-list.json"
            ofpath.parent.mkdir(parents=True, exist_ok=True)
            ofpath.write_text(json.dumps(outputs, ensure_ascii=False), encoding="utf-8")
            outputs_file = str(ofpath)

        cmd = [sys.executable, str(ENGINE), "write-result",
               "--task-package", str(self.package_path()),
               "--role", role, "--status", status, "--attempt", str(attempt)]
        if receipt_file:
            cmd += ["--receipt-file", receipt_file]
        if artifact_file:
            cmd += ["--artifact-file", artifact_file]
        if outputs_file:
            cmd += ["--outputs-file", outputs_file]
        if error:
            cmd += ["--error", error]
        return subprocess.run(cmd, capture_output=True, text=True)

    def dispatch_role(self, role: str, semantic_status="PASS_WITHIN_FROZEN_SCOPE",
                       conclusion_ceiling="PASS_WITHIN_FROZEN_SCOPE",
                       task_id="AUDIT-V21-TEST-001", platform=PLATFORM):
        """创建全部合法文件并通过引擎登记一个角色。"""
        receipt = self.create_valid_receipt(role, task_id=task_id, platform=platform)
        artifact = self.create_valid_artifact(role, task_id=task_id, platform=platform,
                                              semantic_status=semantic_status,
                                              conclusion_ceiling=conclusion_ceiling)
        return self.write_result_via_engine(role, receipt_file=receipt, artifact_file=artifact)

    def dispatch_all_static(self, semantic_status="PASS_WITHIN_FROZEN_SCOPE",
                             conclusion_ceiling="PASS_WITHIN_FROZEN_SCOPE"):
        for role in ROLES_STATIC:
            out = self.dispatch_role(role, semantic_status=semantic_status,
                                      conclusion_ceiling=conclusion_ceiling)
            assert out.returncode == 0, f"dispatch {role} failed: {out.stdout}"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class NegativeTests(unittest.TestCase):
    """13 个负向测试：断言退出码非零、明确失败码、无成功终态文件。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def _assert_no_success_artifacts(self):
        """断言无 finalization.json、无成功式 audit-report.md。"""
        self.assertFalse((self.h.output / "finalization.json").exists(),
                         "finalization.json 不应存在")
        report = self.h.output / "audit-report.md"
        if report.exists():
            text = report.read_text(encoding="utf-8")
            self.assertNotIn("全部 COMPLETED", text, "成功式 audit-report.md 不应存在")

    # ─── 1. zero_dispatch_cannot_finalize ───
    def test_zero_dispatch_cannot_finalize(self):
        """不派发任何职责直接 finalize，必须 BLOCKED。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        out = self.h.finalize()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("BLOCKED", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 2. missing_native_receipt_rejected ───
    def test_missing_native_receipt_rejected(self):
        """COMPLETED 结果缺少回执文件，必须 REJECTED MISSING_RECEIPT。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        artifact = self.h.create_valid_artifact("scope-routing")
        out = self.h.write_result_via_engine("scope-routing", artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("MISSING_RECEIPT", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 3. empty_outputs_rejected ───
    def test_empty_outputs_rejected(self):
        """COMPLETED 结果空输出，必须 REJECTED EMPTY_OUTPUTS。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        receipt = self.h.create_valid_receipt("scope-routing")
        artifact = self.h.create_valid_artifact("scope-routing")
        # 传空 outputs
        out = self.h.write_result_via_engine("scope-routing", receipt_file=receipt,
                                              artifact_file=artifact, outputs=[])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("EMPTY_OUTPUTS", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 4. nonexistent_output_rejected ───
    def test_nonexistent_output_rejected(self):
        """outputs 引用不存在的文件，必须 REJECTED OUTPUT_NOT_FOUND。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        receipt = self.h.create_valid_receipt("scope-routing")
        artifact = self.h.create_valid_artifact("scope-routing")
        fake_outputs = [{"path": str(self.h.work_dir() / "nonexistent.md"),
                          "sha256": "0" * 64}]
        out = self.h.write_result_via_engine("scope-routing", receipt_file=receipt,
                                              artifact_file=artifact, outputs=fake_outputs)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("OUTPUT_NOT_FOUND", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 5. wrong_output_digest_rejected ───
    def test_wrong_output_digest_rejected(self):
        """outputs sha256 与文件实际摘要不匹配，必须 REJECTED WRONG_OUTPUT_DIGEST。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        receipt = self.h.create_valid_receipt("scope-routing")
        artifact = self.h.create_valid_artifact("scope-routing")
        out_file = self.h.create_output_file("scope-routing")
        bad_outputs = [{"path": out_file["path"], "sha256": "f" * 64}]
        out = self.h.write_result_via_engine("scope-routing", receipt_file=receipt,
                                              artifact_file=artifact, outputs=bad_outputs)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("WRONG_OUTPUT_DIGEST", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 6. duplicate_output_rejected ───
    def test_duplicate_output_rejected(self):
        """outputs 含重复路径，必须 REJECTED DUPLICATE_OUTPUT_PATH。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        receipt = self.h.create_valid_receipt("scope-routing")
        artifact = self.h.create_valid_artifact("scope-routing")
        out_file = self.h.create_output_file("scope-routing")
        dup_outputs = [out_file, dict(out_file)]  # 同一路径两次
        out = self.h.write_result_via_engine("scope-routing", receipt_file=receipt,
                                              artifact_file=artifact, outputs=dup_outputs)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("DUPLICATE_OUTPUT_PATH", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 7. wrong_platform_receipt_rejected ───
    def test_wrong_platform_receipt_rejected(self):
        """回执 platform 与任务包不一致，必须 REJECTED WRONG_PLATFORM_RECEIPT。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        receipt = self.h.create_valid_receipt("scope-routing", platform="kimi-code")
        artifact = self.h.create_valid_artifact("scope-routing")
        out = self.h.write_result_via_engine("scope-routing", receipt_file=receipt,
                                              artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("WRONG_PLATFORM_RECEIPT", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 8. wrong_native_agent_type_rejected ───
    def test_wrong_native_agent_type_rejected(self):
        """回执 native_agent_type 与映射表不一致（大小写敏感），必须 REJECTED。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # claude-code scope-routing 应为 "Plan"，传 "plan"（小写，错误）
        receipt = self.h.create_valid_receipt("scope-routing", native_agent_type="plan")
        artifact = self.h.create_valid_artifact("scope-routing")
        out = self.h.write_result_via_engine("scope-routing", receipt_file=receipt,
                                              artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("WRONG_NATIVE_AGENT_TYPE", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 9. main_context_fabricated_wrapper_rejected ───
    def test_main_context_fabricated_wrapper_rejected(self):
        """主上下文不派发、直接在 agent-results/ 手工创建结果外壳文件，
        validate-result-set 必须失败（receipt 引用文件不存在或摘要不一致）。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 手工伪造一份看似合法的结果文件
        self.h.results_dir().mkdir(parents=True, exist_ok=True)
        fake_body = {
            "task_id": "AUDIT-V21-TEST-001", "platform": PLATFORM,
            "role": "scope-routing", "status": "COMPLETED", "attempt": 1,
            "native_receipt": {
                "platform": PLATFORM, "task_id": "AUDIT-V21-TEST-001",
                "role": "scope-routing", "kind": RECEIPT_KIND,
                "native_agent_type": "Plan", "invocation_id": "fake-inv",
                "raw_record": {"path": "/nonexistent/raw.json", "sha256": "0" * 64},
                "completion": {"kind": "exit_status", "value": 0}
            },
            "outputs": [{"path": "/nonexistent/output.md", "sha256": "0" * 64}],
            "artifact": {"path": "/nonexistent/artifact.json", "sha256": "0" * 64},
        }
        fake_result = {
            "schema_version": "2.1",
            **fake_body,
            "result_sha256": sha256_bytes(canonical(fake_body))
        }
        (self.h.results_dir() / "scope-routing.json").write_text(
            json.dumps(fake_result, ensure_ascii=False), encoding="utf-8")

        # 派发其余四个角色（正常）
        for role in ROLES_STATIC[1:]:
            self.h.dispatch_role(role)

        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        data = json.loads(out.stdout)
        codes = [f.get("code") for f in data.get("failures", [])]
        # 伪造的结果引用的文件不存在，会触发 RAW_RECORD_NOT_FOUND 或 OUTPUT_NOT_FOUND
        self.assertTrue(any(c in codes for c in
                            ["RAW_RECORD_NOT_FOUND", "OUTPUT_NOT_FOUND",
                             "ARTIFACT_NOT_FOUND", "RAW_RECORD_PATH_NOT_ALLOWED",
                             "OUTPUT_PATH_NOT_ALLOWED", "ARTIFACT_PATH_NOT_ALLOWED"]),
                        f"Expected path-related failure, got codes: {codes}")
        self._assert_no_success_artifacts()

    # ─── 10. inner_blocked_cannot_be_outer_completed ───
    def test_inner_blocked_cannot_be_outer_completed(self):
        """内层成果 semantic_status=BLOCKED，外层仍 COMPLETED；
        validate-result-set 必须报 INNER_SEMANTIC_FAILURE。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 第一个角色用 BLOCKED
        self.h.dispatch_role("scope-routing", semantic_status="BLOCKED",
                              conclusion_ceiling="BLOCKED")
        for role in ROLES_STATIC[1:]:
            self.h.dispatch_role(role)

        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        data = json.loads(out.stdout)
        codes = [f.get("code") for f in data.get("failures", [])]
        self.assertIn("INNER_SEMANTIC_FAILURE", codes)
        self._assert_no_success_artifacts()

        execution = self.h.validate_execution()
        self.assertEqual(execution.returncode, 0, execution.stdout)
        execution_data = json.loads(execution.stdout)
        self.assertEqual(execution_data["status"], "COMPLETE")
        self.assertEqual(execution_data["semantic_failures"][0]["semantic_status"], "BLOCKED")

    # ─── 11. inner_incomplete_cannot_be_finalized ───
    def test_inner_incomplete_cannot_be_finalized(self):
        """内层成果 semantic_status=INCOMPLETE，finalize 必须 BLOCKED 非零。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        self.h.dispatch_role("scope-routing", semantic_status="INCOMPLETE",
                              conclusion_ceiling="INCOMPLETE")
        for role in ROLES_STATIC[1:]:
            self.h.dispatch_role(role)

        out = self.h.finalize()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("BLOCKED", out.stdout)
        self._assert_no_success_artifacts()
        # 应该有 machine-report.json
        self.assertTrue((self.h.output / "machine-report.json").exists(),
                        "machine-report.json 应该存在")
        report = json.loads((self.h.output / "machine-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["reason"], "SEMANTIC_FAILURE")

    def test_inner_reject_complete_execution_finalizes_to_machine_report(self):
        """REJECT 不破坏结构完整性；finalize 必须生成负语义机器终态。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        self.h.dispatch_role("scope-routing", semantic_status="REJECT",
                              conclusion_ceiling="REJECT")
        for role in ROLES_STATIC[1:]:
            self.h.dispatch_role(role)

        execution = self.h.validate_execution()
        self.assertEqual(execution.returncode, 0, execution.stdout)
        self.assertEqual(json.loads(execution.stdout)["status"], "COMPLETE")

        out = self.h.finalize()
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse((self.h.output / "audit-report.md").exists())
        report_path = self.h.output / "machine-report.json"
        self.assertTrue(report_path.exists(), "REJECT 必须生成 machine-report.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["reason"], "SEMANTIC_FAILURE")
        self.assertEqual(report["worst_semantic_status"], "REJECT")

    # ─── 12. result_synthesis_before_dependencies_rejected ───
    def test_result_synthesis_before_dependencies_rejected(self):
        """result-synthesis 在 adversarial-challenge 之前登记，
        必须 DEPENDENCY_NOT_SATISFIED。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 只派发 scope-routing（result-synthesis 的最远前置）
        self.h.dispatch_role("scope-routing")
        # 尝试直接派发 result-synthesis，跳过中间依赖
        receipt = self.h.create_valid_receipt("result-synthesis")
        artifact = self.h.create_valid_artifact("result-synthesis")
        out = self.h.write_result_via_engine("result-synthesis",
                                              receipt_file=receipt, artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("DEPENDENCY_NOT_SATISFIED", out.stdout)
        self._assert_no_success_artifacts()

    # ─── 13. missing_duplicate_extra_role_rejected ───
    def test_missing_duplicate_extra_role_rejected(self):
        """缺失角色 + 额外角色：validate-result-set 必须报告 MISSING_OUTPUT 和 EXTRA_OUTPUT。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 只派发 3/5 个角色（缺失 2 个）
        for role in ROLES_STATIC[:3]:
            self.h.dispatch_role(role)
        # 额外创建一个不属于 expected_roles 的文件
        self.h.results_dir().mkdir(parents=True, exist_ok=True)
        (self.h.results_dir() / "extra-role.json").write_text("{}", encoding="utf-8")

        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        data = json.loads(out.stdout)
        codes = [f.get("code") for f in data.get("failures", [])]
        self.assertIn("MISSING_OUTPUT", codes)
        self.assertIn("EXTRA_OUTPUT", codes)
        self._assert_no_success_artifacts()


class PositiveTests(unittest.TestCase):
    """正向测试：完整链路在三种模式下成功。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_static_full_chain(self):
        """static 模式 5 角色完整链路：prepare → dispatch → validate → finalize。"""
        self.assertEqual(self.h.prepare(mode="static").returncode, 0)
        self.h.dispatch_all_static()
        validated = self.h.validate()
        self.assertEqual(validated.returncode, 0, validated.stdout)
        data = json.loads(validated.stdout)
        self.assertEqual(data["status"], "COMPLETE")
        self.assertIn("semantic_summary", data)

        finalized = self.h.finalize()
        self.assertEqual(finalized.returncode, 0, finalized.stdout)
        fin_data = json.loads(finalized.stdout)
        self.assertEqual(fin_data["status"], "FINALIZED")
        self.assertIn("run_verdict", fin_data)
        self.assertEqual(fin_data["run_verdict"], "PASS_WITHIN_FROZEN_SCOPE")
        self.assertTrue((self.h.output / "audit-report.md").exists())
        self.assertTrue((self.h.output / "finalization.json").exists())

    def test_runtime_full_chain(self):
        """runtime 模式 5 角色完整链路。"""
        self.assertEqual(self.h.prepare(mode="runtime").returncode, 0)
        for role in ROLES_RUNTIME:
            self.h.dispatch_role(role)
        validated = self.h.validate()
        self.assertEqual(validated.returncode, 0, validated.stdout)
        finalized = self.h.finalize()
        self.assertEqual(finalized.returncode, 0, finalized.stdout)
        fin_data = json.loads(finalized.stdout)
        self.assertEqual(fin_data["status"], "FINALIZED")
        self.assertEqual(fin_data["run_verdict"], "PASS_WITHIN_FROZEN_SCOPE")

    def test_combined_full_chain(self):
        """combined 模式 6 角色完整链路。"""
        self.assertEqual(self.h.prepare(mode="combined").returncode, 0)
        for role in ROLES_COMBINED:
            self.h.dispatch_role(role)
        validated = self.h.validate()
        self.assertEqual(validated.returncode, 0, validated.stdout)
        finalized = self.h.finalize()
        self.assertEqual(finalized.returncode, 0, finalized.stdout)
        fin_data = json.loads(finalized.stdout)
        self.assertEqual(fin_data["status"], "FINALIZED")
        self.assertEqual(fin_data["run_verdict"], "PASS_WITHIN_FROZEN_SCOPE")

    def test_self_contained_bundled_references(self):
        """自包含回归：打包态 references/ 优先于 spec/（扩展覆盖三份 Schema）。"""
        core = SCRIPTS_DIR.parent
        relocated = self.h.root / "installed-skill"
        shutil.copytree(core, relocated)

        # 确保三份 schema 和映射表都在 references/ 中
        ref_dir = relocated / "references"
        spec_orch = Path(__file__).resolve().parents[4] / "spec" / "orchestration"
        for name in ("platform-adapter-mapping.json", "result.schema.json",
                     "role-artifact.schema.json", "task-package.schema.json"):
            src = spec_orch / name
            if src.is_file():
                (ref_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        out = subprocess.run([sys.executable, str(relocated / "scripts" / "orchestration_engine.py"),
                              "prepare-run", "--task-id", "AUDIT-SELFCONTAINED-001",
                              "--platform", "claude-code", "--mode", "static",
                              "--target", str(self.h.target), "--evidence-type", "text",
                              "--output-root", str(self.h.root / "relocated-run"),
                              "--prompts-root", str(self.h.prompts)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("READY_FOR_ISOLATED_TASKS", out.stdout)

    def test_needs_revision_verdict(self):
        """所有角色 NEEDS_REVISION 时 finalize 成功且 run_verdict=NEEDS_REVISION。"""
        self.assertEqual(self.h.prepare(mode="static").returncode, 0)
        for role in ROLES_STATIC:
            self.h.dispatch_role(role, semantic_status="NEEDS_REVISION",
                                  conclusion_ceiling="NEEDS_REVISION")
        finalized = self.h.finalize()
        self.assertEqual(finalized.returncode, 0, finalized.stdout)
        fin_data = json.loads(finalized.stdout)
        self.assertEqual(fin_data["run_verdict"], "NEEDS_REVISION")

    def test_mixed_pass_and_needs_revision_keeps_strictest_ceiling(self):
        """PASS 与 NEEDS_REVISION 混合时不得被较宽松上限覆盖。"""
        self.assertEqual(self.h.prepare(mode="static").returncode, 0)
        self.h.dispatch_role("scope-routing")
        for role in ROLES_STATIC[1:]:
            self.h.dispatch_role(role, semantic_status="NEEDS_REVISION",
                                 conclusion_ceiling="NEEDS_REVISION")
        finalized = self.h.finalize()
        self.assertEqual(finalized.returncode, 0, finalized.stdout)
        fin_data = json.loads(finalized.stdout)
        self.assertEqual(fin_data["run_verdict"], "NEEDS_REVISION")

    def test_finalize_rejects_when_any_role_blocked(self):
        """任一角色 BLOCKED 时 finalize 失败关闭。"""
        self.assertEqual(self.h.prepare(mode="static").returncode, 0)
        self.h.dispatch_all_static()
        # 最后一个角色用 BLOCKED
        # 先删除已有的 result-synthesis 结果
        rs_path = self.h.results_dir() / "result-synthesis.json"
        rs_path.unlink(missing_ok=True)
        self.h.dispatch_role("result-synthesis", semantic_status="BLOCKED",
                              conclusion_ceiling="BLOCKED")
        out = self.h.finalize()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("BLOCKED", out.stdout)
        self.assertFalse((self.h.output / "finalization.json").exists())

    def test_write_result_retry_after_failure(self):
        """FAILED 后可以递增 attempt 重试。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 第一次 FAILED
        failed = self.h.write_result_via_engine("scope-routing", status="FAILED", error="test fail")
        self.assertEqual(failed.returncode, 0, failed.stdout)
        # 同 attempt 重试被拒
        same = self.h.write_result_via_engine("scope-routing", attempt=1)
        # 注意：FAILED 不要求 receipt/artifact/outputs，所以同 attempt 但没传这些也行
        # 但 DUPLICATE_OUTPUT 守卫会检查 attempt <= prior
        # 第二次 attempt=2 用完整文件
        receipt = self.h.create_valid_receipt("scope-routing")
        artifact = self.h.create_valid_artifact("scope-routing")
        retry = self.h.write_result_via_engine("scope-routing", attempt=2,
                                                receipt_file=receipt, artifact_file=artifact)
        self.assertEqual(retry.returncode, 0, retry.stdout)


class LegacyBehaviorTests(unittest.TestCase):
    """改写旧测试：v2.0 旧测试编码了弱行为（空 outputs 可 COMPLETED），
    现在必须断言新的强行为。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_old_write_result_without_receipt_or_artifact_rejected(self):
        """旧测试 write_result_via_engine 不传 receipt/artifact 时，
        COMPLETED 必须被拒绝（MISSING_RECEIPT）。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 旧调用方式：只传 outputs，不传 receipt/artifact
        out_file = self.h.create_output_file("scope-routing")
        outputs_file = self.h.work_dir() / "sr-outputs.json"
        outputs_file.write_text(json.dumps([out_file], ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, str(ENGINE), "write-result",
               "--task-package", str(self.h.package_path()),
               "--role", "scope-routing", "--status", "COMPLETED",
               "--outputs-file", str(outputs_file)]
        out = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("MISSING_RECEIPT", out.stdout)

    def test_old_empty_outputs_no_longer_completes(self):
        """旧测试允许 --outputs-json '[]' 直接 COMPLETED，现在必须 EMPTY_OUTPUTS。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        receipt = self.h.create_valid_receipt("scope-routing")
        artifact = self.h.create_valid_artifact("scope-routing")
        cmd = [sys.executable, str(ENGINE), "write-result",
               "--task-package", str(self.h.package_path()),
               "--role", "scope-routing", "--status", "COMPLETED",
               "--receipt-file", receipt, "--artifact-file", artifact,
               "--outputs-json", "[]"]
        out = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("EMPTY_OUTPUTS", out.stdout)

    def test_old_dispatch_all_raw_fails_without_receipts(self):
        """旧测试 write_result_raw 直接写入结果文件（绕过引擎），
        validate-result-set 在 v2.1 下必须失败（Schema 缺少必填字段）。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        self.h.results_dir().mkdir(parents=True, exist_ok=True)
        for role in ROLES_STATIC:
            body = {"task_id": "AUDIT-V21-TEST-001", "platform": PLATFORM, "role": role,
                    "status": "COMPLETED", "attempt": 1,
                    "outputs": [{"path": "out.json", "sha256": "0" * 64}]}
            digest = hashlib.sha256(canonical(body)).hexdigest()
            payload = {"schema_version": "2.0", **body, "result_sha256": digest}
            (self.h.results_dir() / f"{role}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        data = json.loads(out.stdout)
        # v2.1 Schema 要求 native_receipt 必填，旧格式缺少
        codes = [f.get("code") for f in data.get("failures", [])]
        self.assertTrue(any("SCHEMA_INVALID" in str(c) for c in codes),
                        f"Expected SCHEMA_INVALID, got: {codes}")


class PrepareRunTests(unittest.TestCase):
    """prepare-run 边界测试。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_prepare_rejects_existing_output_root(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        second = self.h.prepare()
        self.assertEqual(second.returncode, 1)
        self.assertIn("OUTPUT_ROOT_EXISTS", second.stdout)

    def test_prepare_rejects_invalid_task_id(self):
        out = self.h.prepare(task_id="audit-lowercase-1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("INVALID_TASK_ID", out.stdout)

    def test_prepare_rejects_missing_prompt(self):
        (self.h.prompts / "adversarial-challenge.md").unlink()
        out = self.h.prepare()
        self.assertEqual(out.returncode, 1)
        self.assertIn("PROMPT_MISSING", out.stdout)

    def test_prepare_creates_work_directory(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        self.assertTrue(self.h.work_dir().is_dir())

    def test_prepare_creates_real_content_addressed_evidence_index(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        index = json.loads((self.h.output / "evidence-index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema_version"], "1.0")
        self.assertEqual(index["file_count"], 1)
        self.assertEqual(index["chunk_count"], 1)
        self.assertEqual(index["files"][0]["path"], "SKILL.md")
        self.assertEqual(index["files"][0]["sha256"], sha256_file(self.h.target / "SKILL.md"))
        self.assertEqual(index["chunks"][0]["id"], "CHUNK-000000")
        unsigned = {k: v for k, v in index.items() if k != "index_sha256"}
        self.assertEqual(index["index_sha256"], sha256_bytes(canonical(unsigned)))

    def test_target_tree_and_evidence_index_use_distinct_digest_fields(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        package = json.loads(self.h.package_path().read_text(encoding="utf-8"))
        source = json.loads((self.h.output / "source-manifest.json").read_text(encoding="utf-8"))
        index = json.loads((self.h.output / "evidence-index.json").read_text(encoding="utf-8"))

        self.assertEqual(package["schema_version"], "2.1")
        self.assertEqual(package["target"]["tree_algorithm"], "tree-sha256-v1")
        self.assertIn("tree_sha256", package["target"])
        self.assertNotIn("file_set_sha256", package["target"])
        self.assertEqual(source["tree_algorithm"], "tree-sha256-v1")
        self.assertEqual(source["tree_sha256"], package["target"]["tree_sha256"])
        self.assertNotIn("file_set_sha256", source)
        self.assertIn("file_set_sha256", index)
        self.assertNotIn("tree_sha256", index)

    def test_target_tree_drift_is_rejected_before_result_write(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        (self.h.target / "SKILL.md").write_text("changed after freeze\n", encoding="utf-8")
        out = self.h.dispatch_role("scope-routing")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("FROZEN_SUBJECT_TREE_DRIFT", out.stdout)

    def test_evidence_index_drift_is_rejected_independently(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        index_path = self.h.output / "evidence-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["file_set_sha256"] = "0" * 64
        index_path.write_text(json.dumps(index), encoding="utf-8")
        out = self.h.dispatch_role("scope-routing")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("EVIDENCE_INDEX_BINDING_DIGEST_MISMATCH", out.stdout)

    def test_write_result_accepts_digest_bound_reference_to_frozen_subject(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        role = "scope-routing"
        target_file = self.h.target / "SKILL.md"
        findings = [{
            "statement": "Bound target evidence",
            "evidence_refs": [{"path": str(target_file.resolve()), "sha256": sha256_file(target_file)}],
        }]
        receipt = self.h.create_valid_receipt(role)
        artifact = self.h.create_valid_artifact(role, findings=findings)
        out = self.h.write_result_via_engine(role, receipt_file=receipt, artifact_file=artifact)
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_write_result_accepts_exact_relative_evidence_index_path(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        role = "scope-routing"
        target_file = self.h.target / "SKILL.md"
        findings = [{
            "statement": "Relative evidence-index member",
            "evidence_refs": [{"path": "SKILL.md", "sha256": sha256_file(target_file)}],
        }]
        receipt = self.h.create_valid_receipt(role)
        artifact = self.h.create_valid_artifact(role, findings=findings)
        out = self.h.write_result_via_engine(role, receipt_file=receipt, artifact_file=artifact)
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_write_result_rejects_relative_evidence_path_traversal(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        role = "scope-routing"
        findings = [{
            "statement": "Traversal must fail",
            "evidence_refs": [{"path": "../outside.md", "sha256": "0" * 64}],
        }]
        receipt = self.h.create_valid_receipt(role)
        artifact = self.h.create_valid_artifact(role, findings=findings)
        out = self.h.write_result_via_engine(role, receipt_file=receipt, artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ARTIFACT_EVIDENCE_PATH_NOT_ALLOWED", out.stdout)

    def test_write_result_rejects_unindexed_relative_evidence_path(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        role = "scope-routing"
        findings = [{
            "statement": "Unindexed path must fail",
            "evidence_refs": [{"path": "unindexed.md", "sha256": "0" * 64}],
        }]
        receipt = self.h.create_valid_receipt(role)
        artifact = self.h.create_valid_artifact(role, findings=findings)
        out = self.h.write_result_via_engine(role, receipt_file=receipt, artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ARTIFACT_EVIDENCE_PATH_NOT_INDEXED", out.stdout)

    def test_write_result_rejects_relative_evidence_digest_mismatch(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        role = "scope-routing"
        findings = [{
            "statement": "Wrong digest must fail",
            "evidence_refs": [{"path": "SKILL.md", "sha256": "0" * 64}],
        }]
        receipt = self.h.create_valid_receipt(role)
        artifact = self.h.create_valid_artifact(role, findings=findings)
        out = self.h.write_result_via_engine(role, receipt_file=receipt, artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ARTIFACT_EVIDENCE_DIGEST_MISMATCH", out.stdout)

    def test_task_package_digest_drift_detected(self):
        self.assertEqual(self.h.prepare().returncode, 0)
        self.h.dispatch_all_static()
        package = json.loads(self.h.package_path().read_text(encoding="utf-8"))
        package["mode"] = "combined"  # 篡改
        self.h.package_path().write_text(json.dumps(package), encoding="utf-8")
        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("TASK_PACKAGE_DIGEST_DRIFT", out.stdout)


class SchemaVersionCoverageTests(unittest.TestCase):
    """必修 1：result_sha256 覆盖 schema_version。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_schema_version_tampering_detected(self):
        """篡改已登记结果的 schema_version（同时按旧 body 定义重算 result_sha256 使其"自洽"），
        validate 必须失败（摘要不匹配或 Schema const 失败）。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        self.h.dispatch_all_static()

        # 篡改 scope-routing 结果的 schema_version 并按旧 body（不含 schema_version）重算
        result_path = self.h.results_dir() / "scope-routing.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["schema_version"] = "2.0"  # 改为旧版本
        # 按旧 body 定义（不含 schema_version）重算 result_sha256 使其"自洽"
        old_body = {k: result.get(k) for k in
                    ("task_id", "platform", "role", "status", "attempt",
                     "native_receipt", "outputs", "artifact")}
        if "error" in result:
            old_body["error"] = result["error"]
        result["result_sha256"] = sha256_bytes(canonical(old_body))
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        data = json.loads(out.stdout)
        codes = [f.get("code") for f in data.get("failures", [])]
        # 引擎按新 body 定义（含 schema_version）重算摘要，篡改后必然不匹配
        self.assertTrue(any(c in ("RECEIPT_MISMATCH", "SCHEMA_INVALID") for c in codes),
                        f"Expected RECEIPT_MISMATCH or SCHEMA_INVALID, got: {codes}")
        self.assertFalse((self.h.output / "finalization.json").exists())


class ReceiptRoleMismatchTests(unittest.TestCase):
    """必修 3：回执 role 校验缺陷修复验证。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_receipt_role_mismatch_rejected(self):
        """receipt 文件 role=static-audit，write-result --role scope-routing →
        REJECTED RECEIPT_ROLE_MISMATCH，且 agent-results/ 不产生成功结果文件。"""
        self.assertEqual(self.h.prepare().returncode, 0)
        # 创建 role=static-audit 的回执
        receipt_path = self.h.create_valid_receipt("static-audit")
        artifact = self.h.create_valid_artifact("scope-routing")
        # 用 scope-routing 角色登记，但回执里 role=static-audit
        out = self.h.write_result_via_engine("scope-routing",
                                              receipt_file=receipt_path,
                                              artifact_file=artifact)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("RECEIPT_ROLE_MISMATCH", out.stdout)
        # agent-results/ 不应有 scope-routing.json
        self.assertFalse((self.h.results_dir() / "scope-routing.json").exists(),
                         "agent-results/scope-routing.json 不应产生")


class AdversarialBypassTests(unittest.TestCase):
    """必修 2 对抗路径：绕过 write-result 直接放入 agent-results/。"""

    def setUp(self) -> None:
        self.h = EngineHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_adversarial_bypass_empty_outputs_detected(self):
        """手工构造一份 agent-results 结果文件：status=COMPLETED、outputs=[]、
        但 result_sha256 按当前 body 定义（含 schema_version）正确重算。
        validate-result-set 必须失败（Schema allOf 或引擎显式检查拦截）。"""
        self.assertEqual(self.h.prepare().returncode, 0)

        # 派发其余 4 个角色正常
        for role in ROLES_STATIC[1:]:
            self.h.dispatch_role(role)

        # 手工构造绕过 write-result 的 scope-routing 结果
        self.h.results_dir().mkdir(parents=True, exist_ok=True)
        # 需要一个合法的 receipt（指向真实存在的 raw_record）
        raw = self.h.create_raw_record("scope-routing-bypass")
        receipt = {
            "platform": PLATFORM, "task_id": "AUDIT-V21-TEST-001",
            "role": "scope-routing", "kind": RECEIPT_KIND,
            "native_agent_type": "Plan", "invocation_id": "bypass-inv",
            "raw_record": raw,
            "completion": {"kind": "exit_status", "value": 0}
        }
        # 需要一个合法的 artifact
        art_path = self.h.create_valid_artifact("scope-routing")
        artifact_binding = {"path": art_path, "sha256": sha256_file(Path(art_path))}

        body = {
            "schema_version": "2.1",
            "task_id": "AUDIT-V21-TEST-001",
            "platform": PLATFORM,
            "role": "scope-routing",
            "status": "COMPLETED",
            "attempt": 1,
            "native_receipt": receipt,
            "outputs": [],  # 空输出！
            "artifact": artifact_binding,
        }
        result = {**body, "result_sha256": sha256_bytes(canonical(body))}
        (self.h.results_dir() / "scope-routing.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")

        out = self.h.validate()
        self.assertNotEqual(out.returncode, 0)
        data = json.loads(out.stdout)
        codes = [f.get("code") for f in data.get("failures", [])]
        # Schema allOf if/then 或引擎检查必须拦截空 outputs
        self.assertTrue(any(c in ("SCHEMA_INVALID", "RECEIPT_MISMATCH") for c in codes),
                        f"Expected SCHEMA_INVALID or RECEIPT_MISMATCH for empty outputs bypass, got: {codes}")
        self.assertFalse((self.h.output / "finalization.json").exists())


if __name__ == "__main__":
    unittest.main()

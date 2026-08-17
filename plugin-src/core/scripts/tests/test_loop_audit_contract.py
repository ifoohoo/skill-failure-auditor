from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parent / "loop_audit_contract.py"
CORE = TEST_DIR.parent.parent
REGISTRY = CORE / "references" / "failure-modes.jsonl"
PACKAGE = CORE.parent.parent
# loop-agent 领域源为开发机外部依赖：走环境变量注入（候选泄漏扫描对
# abs-user-path 模式 fail-closed，禁止在源码中硬编码机器路径）。
LOOP_PROVIDER_ROOT = Path(os.environ.get("LOOP_PROVIDER_ROOT", ""))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def source_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class LoopAuditContractTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sfa-loop-contract-"))
        self.target = self.root / "target"
        self.target.mkdir()
        (self.target / "input.txt").write_text("frozen target", encoding="utf-8")
        first = json.loads(REGISTRY.read_text(encoding="utf-8").splitlines()[0])
        self.input = {
            "schema_version": "1.0",
            "audit_task_id": "AUDIT-LOOP-001",
            "delivery_cycle_id": "DC-AUDIT-001",
            "mode": "combined",
            "evidence_type": "result",
            "target": {"path": str(self.target), "tree_algorithm": "foundation-resource-closure-v1",
                       "tree_sha256": self.foundation_target_digest()},
            "selected_rules": [{"id": first["id"], "revision": first["revision"],
                                "source_sha256": source_digest(first)}],
            "role_payloads": {role: {"instruction": role} for role in (
                "scope-routing", "static-audit", "runtime-evidence", "evaluation-integrity",
                "adversarial-challenge", "result-synthesis")},
            "loop_provider_root": str(LOOP_PROVIDER_ROOT),
            "loop_policy": {
                "task_phases": ["plan", "execute", "review-1", "repair-1", "review-2"],
                "planning_depth": 3,
                "agent_nesting_depth": 3,
                "max_concurrency": 4,
                "max_total_agents": 100,
                "max_repair_cycles": 1,
                "max_gate_repair_cycles": 1,
            },
        }
        self.input_path = self.root / "input.json"
        self.input_path.write_text(json.dumps(self.input), encoding="utf-8")

    def foundation_target_digest(self):
        environment = dict(os.environ)
        environment["SFA_FOUNDATION_NODE"] = "/opt/homebrew/Cellar/node@22/22.23.2/bin/node"
        script = (
            "from foundation_client import foundation_resource_closure; "
            "import json,sys; "
            "print(json.dumps(foundation_resource_closure(sys.argv[1], "
            "[{'path':'input.txt','role':'input'}])))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.target)],
            cwd=TEST_DIR.parent,
            env={**environment, "PYTHONPATH": str(TEST_DIR.parent)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)["digest"]

    def run_cli(self, *args):
        environment = dict(os.environ)
        environment["SFA_FOUNDATION_NODE"] = "/opt/homebrew/Cellar/node@22/22.23.2/bin/node"
        return subprocess.run([sys.executable, str(SCRIPT), *args], text=True,
                              capture_output=True, env=environment)

    def build_valid_results(self, compiled):
        manifest = json.loads((compiled / "compilation-manifest.json").read_text())
        results_root = self.root / f"results-{len(list(self.root.glob('results-*')))}"
        artifacts = results_root / "artifacts"
        artifacts.mkdir(parents=True)
        evidence = results_root / "evidence.txt"
        evidence.write_text("frozen evidence", encoding="utf-8")
        evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
        selected = manifest["selected_rules"][0]
        for ordinal, role in enumerate(manifest["active_roles"], start=1):
            rule_results = []
            if role in {"static-audit", "runtime-evidence"}:
                rule_results = [{
                    "id": selected["id"], "revision": selected["revision"],
                    "severity": selected["severity"], "status": "NOT_HIT",
                    "reason": "frozen evidence does not show the failure mode",
                    "evidence_refs": [{"path": "evidence.txt", "sha256": evidence_sha}],
                }]
            artifact = {
                "schema_version": "1.0", "task_id": manifest["audit_task_id"],
                "platform": "codex", "role": role,
                "semantic_status": "PASS_WITHIN_FROZEN_SCOPE",
                "conclusion_ceiling": "PASS_WITHIN_FROZEN_SCOPE",
                "rule_results": rule_results, "findings": [],
            }
            artifact["artifact_sha256"] = source_digest(artifact)
            artifact_rel = f"artifacts/{role}.json"
            (results_root / artifact_rel).write_text(json.dumps(artifact), encoding="utf-8")
            loop_result = {
                "schema_version": "2.0",
                "delivery_task_id": f"{manifest['audit_task_id']}.{ordinal:02d}.{role}",
                "process_id": f"PROC-L3-{ordinal:02d}", "execution_status": "SUCCEEDED",
                "changed_files": [], "verification_record_paths": [],
                "evidence_paths": [artifact_rel], "findings": [],
            }
            (results_root / f"{role}.delivery-task-result.json").write_text(
                json.dumps(loop_result), encoding="utf-8")
        return manifest, results_root

    def test_compile_emits_loop_source_without_target_write(self):
        output = self.root / "compiled"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source = json.loads((output / "workflow-source-input.json").read_text())
        self.assertEqual(source["schema_version"], "3.0")
        self.assertEqual(len(source["workflow_tasks"]), 6)
        self.assertTrue(all(task["write_set"] == [] for task in source["workflow_tasks"]))
        self.assertEqual(source["workflow_tasks"][-1]["depends_on"][0]["node_id"],
                         "AUDIT-LOOP-001.05.adversarial-challenge")
        self.assertEqual(source["workflow_tasks"][0]["task_phases"],
                         self.input["loop_policy"]["task_phases"])
        self.assertEqual(source["max_repair_cycles"], 1)
        self.assertEqual((self.target / "input.txt").read_text(), "frozen target")

    def test_compile_rejects_rule_revision_drift_without_output(self):
        self.input["selected_rules"][0]["revision"] += 1
        self.input_path.write_text(json.dumps(self.input), encoding="utf-8")
        output = self.root / "rejected"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rule revision drift", result.stdout)
        self.assertFalse(output.exists())

    def test_validate_binds_six_loop_results_to_sfa_artifacts(self):
        compiled = self.root / "compiled"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(compiled))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest, results_root = self.build_valid_results(compiled)
        output = self.root / "domain-report.json"
        validated = self.run_cli(
            "validate-loop-audit", "--compilation-manifest",
            str(compiled / "compilation-manifest.json"), "--results-root", str(results_root),
            "--output", str(output))
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(len(report["roles"]), 6)
        self.assertFalse(report["loop_acceptance_written"])

    def test_validate_rejects_empty_process_id_before_report(self):
        compiled = self.root / "compiled-empty-process"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(compiled))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest, results_root = self.build_valid_results(compiled)
        role = manifest["active_roles"][0]
        path = results_root / f"{role}.delivery-task-result.json"
        document = json.loads(path.read_text())
        document["process_id"] = ""
        path.write_text(json.dumps(document), encoding="utf-8")
        output = self.root / "must-not-exist.json"
        validated = self.run_cli(
            "validate-loop-audit", "--compilation-manifest",
            str(compiled / "compilation-manifest.json"), "--results-root", str(results_root),
            "--output", str(output))
        self.assertNotEqual(validated.returncode, 0)
        self.assertIn("process_id", validated.stdout)
        self.assertFalse(output.exists())

    def test_compile_requires_explicit_loop_policy(self):
        del self.input["loop_policy"]
        self.input_path.write_text(json.dumps(self.input), encoding="utf-8")
        output = self.root / "missing-policy"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_provider_schema_drift_is_rejected_before_compile(self):
        provider = self.root / "loop-provider"
        shutil.copytree(LOOP_PROVIDER_ROOT / "references", provider / "references")
        schema = provider / "references/schemas/delivery-task-result.schema.json"
        document = json.loads(schema.read_text())
        document["$id"] = "urn:attacker:replacement"
        schema.write_text(json.dumps(document), encoding="utf-8")
        self.input["loop_provider_root"] = str(provider)
        self.input_path.write_text(json.dumps(self.input), encoding="utf-8")
        output = self.root / "provider-drift"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Schema bytes or $id drift", result.stdout)
        self.assertFalse(output.exists())

    def test_adjacent_bundle_missing_or_tampered_fails_closed(self):
        sys.path.insert(0, str(TEST_DIR.parent))
        try:
            import foundation_client
            from common import ContractError
            schema_id = "urn:loop-agent:schema:delivery-task-result"
            original = foundation_client.MECHANISMS_CLI
            try:
                missing = self.root / "missing-mechanisms-cli.mjs"
                foundation_client.MECHANISMS_CLI = missing
                with self.assertRaises(ContractError):
                    foundation_client.require_production_validate_by_schema_id(
                        {}, schema_id,
                        expected_sha256="377364d5d899184bc290681b7d8037712677b2a47eb511b506daf8562a84a899",
                    )
                tampered = self.root / "tampered-mechanisms-cli.mjs"
                tampered.write_text("throw new Error('tampered bundle');\n", encoding="utf-8")
                foundation_client.MECHANISMS_CLI = tampered
                with self.assertRaises(ContractError):
                    foundation_client.require_production_validate_by_schema_id(
                        {}, schema_id,
                        expected_sha256="377364d5d899184bc290681b7d8037712677b2a47eb511b506daf8562a84a899",
                    )
            finally:
                foundation_client.MECHANISMS_CLI = original
        finally:
            sys.path.pop(0)

    def test_compile_has_no_private_orchestration_engine_dependency(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("orchestration_engine", source)
        self.assertNotIn("_subject_tree_sha256", source)
        output = self.root / "compiled-without-private-module"
        result = self.run_cli("compile-loop-audit", "--input", str(self.input_path),
                              "--output-dir", str(output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_platform_policy_requires_loop_without_automatic_fallback(self):
        mapping = json.loads((PACKAGE / "spec/orchestration/platform-adapter-mapping.json").read_text())
        policy = mapping["loopOuterContract"]["platformPolicy"]
        self.assertEqual(policy["claude-code"]["status"], "required")
        self.assertEqual(policy["codex"]["status"], "required")
        self.assertEqual(policy["kimi-code"]["status"], "legacy-compatibility")
        self.assertEqual(policy["workbuddy"]["status"], "legacy-compatibility")
        self.assertTrue(all(not item["automaticLegacyFallback"] for item in policy.values()))


if __name__ == "__main__":
    unittest.main()

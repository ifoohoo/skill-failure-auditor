"""四平台公开静态合同测试：能力对齐、单一映射源、无候选运行事实。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = PACKAGE_ROOT / "plugin-src" / "platforms"
MAPPING = json.loads(
    (PACKAGE_ROOT / "spec" / "orchestration" / "platform-adapter-mapping.json").read_text(
        encoding="utf-8"
    )
)
SUPPORT = json.loads(
    (PACKAGE_ROOT / "spec" / "platforms" / "support-matrix.json").read_text(
        encoding="utf-8"
    )
)
EQUIV = json.loads(
    (PACKAGE_ROOT / "spec" / "platforms" / "equivalence-matrix.json").read_text(
        encoding="utf-8"
    )
)
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]
GENERATOR = (
    PACKAGE_ROOT / "scripts" / "build" / "generate_kimi_compat_manifest.py"
)
EQUIVALENCE_CHECKS = [
    "role_sets_identical",
    "all_roles_completed_on_all_platforms",
    "schema_version_identical",
    "mode_identical",
    "acceptance_criteria_identical",
    "all_finalized",
    "status_matrices_complete",
]
DYNAMIC_KEYS = {
    "appVersionProbed",
    "blockedRule",
    "candidateDigest",
    "candidateDigestNote",
    "candidateRunEvidence",
    "candidateVersion",
    "currentAuthority",
    "currentCandidate",
    "embeddedCliVersionProbed",
    "formalClaim",
    "fourPlatformClaimAllowed",
    "fourPlatformVerifiedClaimAllowed",
    "freezeAt",
    "freezeEvidence",
    "frozen",
    "generatedAt",
    "historicalEvidence",
    "loginStatus",
    "overallStatus",
    "platformDigests",
    "platformDigestsNote",
    "platformsBlocked",
    "platformsPending",
    "platformsVerified",
    "probeDate",
    "probeEvidence",
    "probePlan",
    "probedClientVersion",
    "probedVersion",
    "r2EvidenceStatus",
    "runtimeStatus",
    "stableClaimAllowed",
    "toolSchemaProbed",
    "verdict",
}
CANDIDATE_FACT_PATTERN = re.compile(
    r"(?:candidate\.\d+|blocked_candidate_|pending_r\d+)",
    re.IGNORECASE,
)
EXPECTED_AUTHOR = {"name": "广州市风荷科技有限公司"}
EXPECTED_LICENSE = "Apache-2.0"
PLUGIN_MANIFESTS = {
    "claude-code": PLATFORMS / "claude-code" / ".claude-plugin" / "plugin.json",
    "codex": PLATFORMS / "codex" / ".codex-plugin" / "plugin.json",
    "kimi-code": PLATFORMS / "kimi-code" / "kimi.plugin.json",
    "workbuddy": PLATFORMS / "workbuddy" / ".codebuddy-plugin" / "plugin.json",
}


def manifest(platform_id: str) -> dict[str, Any]:
    return json.loads(
        (PLATFORMS / platform_id / "platform-manifest.json").read_text(
            encoding="utf-8"
        )
    )


def walk_json(value: Any, path: str = "$") -> list[tuple[str, str, Any]]:
    entries: list[tuple[str, str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            entries.append((child_path, key, child))
            entries.extend(walk_json(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            entries.extend(walk_json(child, f"{path}[{index}]"))
    return entries


class CrossPlatformContractTests(unittest.TestCase):
    def test_package_and_platform_identity_metadata_are_aligned(self) -> None:
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["author"], EXPECTED_AUTHOR)
        self.assertEqual(package["license"], EXPECTED_LICENSE)
        for platform_id, manifest_path in PLUGIN_MANIFESTS.items():
            plugin = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(plugin.get("author"), EXPECTED_AUTHOR, platform_id)
            self.assertEqual(plugin.get("license"), EXPECTED_LICENSE, platform_id)

    def test_all_four_platforms_present_and_name_the_product(self) -> None:
        for platform_id in PLATFORM_IDS:
            root = PLATFORMS / platform_id
            self.assertTrue(root.is_dir(), platform_id)
            self.assertEqual(manifest(platform_id)["product"], "skill-failure-auditor")
            self.assertEqual(manifest(platform_id)["platformId"], platform_id)

    def test_platform_sets_are_consistent(self) -> None:
        self.assertEqual(
            [platform["platformId"] for platform in SUPPORT["platforms"]],
            PLATFORM_IDS,
        )
        self.assertEqual(SUPPORT["platformSet"], PLATFORM_IDS)
        self.assertEqual(EQUIV["platformSet"], PLATFORM_IDS)
        self.assertEqual(list(MAPPING["platforms"]), PLATFORM_IDS)

    def test_role_mappings_align_with_single_specification(self) -> None:
        claude = manifest("claude-code")
        claude_unique_types = list(
            dict.fromkeys(
                MAPPING["platforms"]["claude-code"]["roleToNativeAgentType"].values()
            )
        )
        self.assertEqual(claude["delegation"]["builtinAgents"], claude_unique_types)

        kimi = manifest("kimi-code")
        self.assertEqual(
            kimi["delegation"]["roleMapping"],
            MAPPING["platforms"]["kimi-code"]["roleToNativeAgentType"],
        )
        for role, native in kimi["delegation"]["roleMapping"].items():
            self.assertEqual(native, native.lower(), f"Kimi 角色映射必须小写：{role}")

        workbuddy = manifest("workbuddy")
        self.assertEqual(
            workbuddy["delegation"]["roleToNativeAgentType"],
            MAPPING["platforms"]["workbuddy"]["roleToNativeAgentType"],
        )

    def test_equivalence_contract_keeps_only_required_checks(self) -> None:
        checks = EQUIV["equivalenceChecks"]
        self.assertEqual([item["check"] for item in checks], EQUIVALENCE_CHECKS)
        for item in checks:
            self.assertIs(item["required"], True, item["check"])
            self.assertTrue(item["evidenceRequirement"], item["check"])
            self.assertNotIn("status", item)
        policy = EQUIV["adjudicationPolicy"]
        self.assertIs(policy["sameCandidateRequired"], True)
        self.assertIs(policy["allChecksRequired"], True)
        self.assertIs(policy["publicPackageSelfClaimForbidden"], True)
        self.assertIs(policy["externalSemanticReviewRequired"], True)
        self.assertIs(policy["historyMigrationForbidden"], True)

    def test_public_static_sources_contain_no_candidate_run_facts(self) -> None:
        sources: dict[str, Any] = {
            "support-matrix": SUPPORT,
            "equivalence-matrix": EQUIV,
            "platform-adapter-mapping": MAPPING,
        }
        sources.update(
            {
                f"{platform_id}-manifest": manifest(platform_id)
                for platform_id in PLATFORM_IDS
            }
        )
        for source_name, source in sources.items():
            for path, key, value in walk_json(source):
                self.assertNotIn(
                    key,
                    DYNAMIC_KEYS,
                    f"{source_name}:{path} 不得保存候选运行事实或重复冻结状态",
                )
                if isinstance(value, str):
                    self.assertIsNone(
                        CANDIDATE_FACT_PATTERN.search(value),
                        f"{source_name}:{path} 不得引用某次候选运行状态",
                    )

    def test_support_contract_preserves_platform_requirements(self) -> None:
        by_id = {
            platform["platformId"]: platform["toolAvailability"]
            for platform in SUPPORT["platforms"]
        }
        self.assertEqual(by_id["claude-code"]["minClientVersion"], "2.1.218")
        self.assertEqual(by_id["kimi-code"]["minClientVersion"], "0.31.0")
        self.assertEqual(
            by_id["codex"]["delegation"]["requiredTools"],
            ["collaboration.spawn_agent", "collaboration.wait_agent"],
        )
        workbuddy = by_id["workbuddy"]
        self.assertIn("应用壳版本", workbuddy["versionDualReporting"])
        self.assertIn("内嵌 CLI 版本", workbuddy["versionDualReporting"])
        self.assertIn("隔离", workbuddy["isolationPolicy"])

    def test_codex_does_not_mimic_claude_params(self) -> None:
        codex_root = PLATFORMS / "codex"
        for path in codex_root.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("subagent_type", content, str(path))
        codex = manifest("codex")
        self.assertEqual(
            codex["delegation"]["requiredTools"],
            ["collaboration.spawn_agent", "collaboration.wait_agent"],
        )
        self.assertTrue(codex["delegation"]["mustNotMimicClaudeParams"])

    def test_codex_task_names_are_safe_unique_and_reversible(self) -> None:
        mapping = MAPPING["platforms"]["codex"]["roleToTaskName"]
        self.assertEqual(set(mapping), set(MAPPING["canonicalRoles"]))
        self.assertEqual(len(set(mapping.values())), len(mapping))
        for role, task_name in mapping.items():
            self.assertRegex(task_name, r"^[a-z0-9_]+$")
            self.assertEqual(task_name.replace("_", "-"), role)

    def test_codex_terminal_flow_finalizes_structurally_complete_negative_results(self) -> None:
        codex_root = PLATFORMS / "codex"
        entry = (codex_root / "SKILL.md").read_text(encoding="utf-8")
        orchestration = (codex_root / "references" / "codex-orchestration.md").read_text(
            encoding="utf-8"
        )
        for source in (entry, orchestration):
            self.assertIn("validate-execution-set", source)
            self.assertNotIn("validate-result-set", source)
            self.assertIn("finalize-run", source)
            self.assertIn("machine-report.json", source)
            self.assertIn("SEMANTIC_FAILURE", source)
            self.assertLess(source.index("validate-execution-set"), source.index("finalize-run"))

    def test_codex_prepare_adapter_creates_raw_rollout_directory_before_ready(self) -> None:
        codex_root = PLATFORMS / "codex"
        entry = (codex_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('scripts/codex_prepare_run.py"', entry)
        adapter = codex_root / "scripts" / "codex_prepare_run.py"
        self.assertTrue(adapter.is_file())

        with tempfile.TemporaryDirectory(prefix="codex-prepare-contract-") as tmp:
            root = Path(tmp)
            target = root / "target"
            prompts = root / "prompts"
            output = root / "run"
            target.mkdir()
            prompts.mkdir()
            (target / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
            for role in MAPPING["modeRoleSets"]["static"]:
                (prompts / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(adapter),
                    "--task-id", "AUDIT-CODEX-PREPARE-TEST",
                    "--platform", "codex",
                    "--mode", "static",
                    "--target", str(target),
                    "--evidence-type", "skill",
                    "--output-root", str(output),
                    "--prompts-root", str(prompts),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["status"], "READY_FOR_ISOLATED_TASKS")
            raw = output / "work" / "raw"
            self.assertTrue(raw.is_dir())
            self.assertFalse(raw.is_symlink())

    def test_workbuddy_keeps_dual_version_and_isolation_contracts(self) -> None:
        workbuddy = manifest("workbuddy")
        self.assertIn("应用壳版本", workbuddy["dualVersionReporting"])
        self.assertIn("内嵌 CLI 版本", workbuddy["dualVersionReporting"])
        self.assertIn("隔离", workbuddy["isolationPolicy"])
        self.assertEqual(
            workbuddy["delegation"]["roleToNativeAgentType"],
            MAPPING["platforms"]["workbuddy"]["roleToNativeAgentType"],
        )

    def test_kimi_authoritative_and_projection_are_equal(self) -> None:
        check = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            cwd=PACKAGE_ROOT,
        )
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertIn("EQUAL", check.stdout)
        authoritative = json.loads(
            (PLATFORMS / "kimi-code" / "kimi.plugin.json").read_text(
                encoding="utf-8"
            )
        )
        projection = json.loads(
            (PLATFORMS / "kimi-code" / ".kimi-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(authoritative, projection)

    def test_kimi_projection_drift_is_detected(self) -> None:
        projection_path = PLATFORMS / "kimi-code" / ".kimi-plugin" / "plugin.json"
        original = projection_path.read_text(encoding="utf-8")
        try:
            drifted = json.loads(original)
            drifted["version"] = "9.9.9-drifted"
            projection_path.write_text(
                json.dumps(drifted, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            check = subprocess.run(
                [sys.executable, str(GENERATOR), "--check"],
                capture_output=True,
                text=True,
                cwd=PACKAGE_ROOT,
            )
            self.assertEqual(check.returncode, 1)
            self.assertIn("DRIFT", check.stdout)
        finally:
            projection_path.write_text(original, encoding="utf-8")

    def test_no_registry_or_schema_copies_in_any_projection(self) -> None:
        for platform_id in PLATFORM_IDS:
            root = PLATFORMS / platform_id
            self.assertFalse(list(root.glob("**/failure-modes.jsonl")), platform_id)
            self.assertFalse(list(root.glob("**/*.schema.json")), platform_id)
            self.assertFalse((root / "agents").exists(), platform_id)

    def test_projection_status_is_static_build_metadata(self) -> None:
        for platform_id in PLATFORM_IDS:
            self.assertEqual(
                manifest(platform_id)["projectionStatus"],
                "projection_complete",
                platform_id,
            )


if __name__ == "__main__":
    unittest.main()

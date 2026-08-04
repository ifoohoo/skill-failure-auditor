"""Claude Code 投影静态合同测试：清单、frontmatter、角色大小写、核心不可覆盖。"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_ROOT = PACKAGE_ROOT / "plugin-src" / "platforms" / "claude-code"
CORE_PROMPTS = PACKAGE_ROOT / "plugin-src" / "core" / "prompts"
SPEC_MAPPING = PACKAGE_ROOT / "spec" / "orchestration" / "platform-adapter-mapping.json"
SUPPORT_MATRIX = PACKAGE_ROOT / "spec" / "platforms" / "support-matrix.json"

CANONICAL_ROLES = {"scope-routing", "static-audit", "runtime-evidence",
                   "evaluation-integrity", "adversarial-challenge", "result-synthesis"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ClaudeProjectionTests(unittest.TestCase):
    def test_plugin_manifest_names_the_product(self) -> None:
        manifest = json.loads((PLATFORM_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "skill-failure-auditor")
        self.assertIn("version", manifest)

    def test_skill_frontmatter_uses_builtin_agent_and_engine(self) -> None:
        text = (PLATFORM_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        _, frontmatter, _ = text.split("---", 2)
        self.assertIn("context: fork", frontmatter)
        self.assertIn("agent: general-purpose", frontmatter)
        self.assertIn("background: false", frontmatter)
        self.assertIn("allowed-tools: Agent, Read, Write, Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/orchestration_engine.py *)", frontmatter)
        self.assertIn("2.1.218", text)
        self.assertIn("禁止创建或依赖自定义 Agent", text)

    def test_prompt_manifest_roles_digests_and_agent_types(self) -> None:
        manifest = json.loads((PLATFORM_ROOT / "claude-prompt-manifest.json").read_text(encoding="utf-8"))
        mapping = json.loads(SPEC_MAPPING.read_text(encoding="utf-8"))["platforms"]["claude-code"]
        expected_types = mapping["roleToNativeAgentType"]
        prompts = {p["role"]: p for p in manifest["prompts"]}
        self.assertEqual(set(prompts), CANONICAL_ROLES)
        self.assertEqual(manifest["allowed_builtin_agents"], ["Plan", "Explore", "general-purpose"])
        for role, item in prompts.items():
            # R1: 提示词从 core/prompts/ 取源（共享权威库）
            rel_path = item["path"]
            if rel_path.startswith("prompts/"):
                path = CORE_PROMPTS / rel_path[len("prompts/"):]
            else:
                path = PLATFORM_ROOT / rel_path
            self.assertTrue(path.is_file(), f"prompt not found: {rel_path} (looked at {path})")
            self.assertEqual(sha256(path), item["sha256"], f"digest drift: {role}")
            self.assertEqual(item["agent_type"], expected_types[role], f"agent type drift: {role}")

    def test_no_core_artifacts_are_overridden_or_copied(self) -> None:
        forbidden = ["failure-modes.jsonl", "registry_tool.py", "orchestration_engine.py",
                     "evaluation_tool.py", "evidence_tool.py", "attempt_tool.py"]
        for name in forbidden:
            self.assertFalse((PLATFORM_ROOT / "scripts" / name).exists(), name)
            self.assertFalse((PLATFORM_ROOT / "references" / name).exists(), name)
        self.assertFalse(list(PLATFORM_ROOT.glob("**/*.schema.json")), "投影不得携带 Schema 副本")

    def test_no_custom_agents_or_foreign_manifests(self) -> None:
        self.assertFalse((PLATFORM_ROOT / "agents").exists())
        for foreign in (".codex-plugin", ".codebuddy-plugin", ".kimi-plugin"):
            self.assertFalse((PLATFORM_ROOT / foreign).exists(), foreign)
        self.assertFalse((PLATFORM_ROOT / "kimi.plugin.json").exists())

    def test_no_legacy_role_names_anywhere(self) -> None:
        for path in PLATFORM_ROOT.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("scope-and-routing", content, str(path))
                self.assertNotIn("runtime-evidence-audit", content, str(path))
                self.assertNotIn("claude_orchestration_tool", content, str(path))

    def test_support_matrix_alignment(self) -> None:
        matrix = json.loads(SUPPORT_MATRIX.read_text(encoding="utf-8"))
        claude = next(p for p in matrix["platforms"] if p["platformId"] == "claude-code")
        manifest = json.loads((PLATFORM_ROOT / "platform-manifest.json").read_text(encoding="utf-8"))
        # R7: support-matrix 三层结构，delegation 在 toolAvailability 层
        self.assertEqual(claude["toolAvailability"]["delegation"]["nativeRoles"], manifest["delegation"]["builtinAgents"])
        self.assertEqual(claude["toolAvailability"]["manifestPath"], manifest["pluginManifest"])
        self.assertEqual(claude["toolAvailability"]["minClientVersion"], manifest["minClientVersion"])


if __name__ == "__main__":
    unittest.main()

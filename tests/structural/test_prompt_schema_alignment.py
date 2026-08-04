"""职责提示词的输出字段必须与 role-artifact Schema 精确对齐。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = PACKAGE_ROOT / "plugin-src" / "core" / "prompts"
SCHEMA = json.loads(
    (PACKAGE_ROOT / "spec" / "orchestration" / "role-artifact.schema.json")
    .read_text(encoding="utf-8")
)


class PromptSchemaAlignmentTests(unittest.TestCase):
    def test_output_sections_name_only_schema_defined_top_level_fields(self) -> None:
        allowed = set(SCHEMA["properties"])
        forbidden = {
            "agent_type",
            "status",
            "coverage_claims",
            "unchecked",
            "errors",
            "novel_hypotheses",
        }
        self.assertTrue(forbidden.isdisjoint(allowed))

        prompt_paths = sorted(PROMPTS.glob("*.md"))
        self.assertEqual(len(prompt_paths), 6)
        for prompt_path in prompt_paths:
            output = prompt_path.read_text(encoding="utf-8").split("## 输出", 1)[1]
            for field in allowed:
                self.assertIn(f"`{field}`", output, f"{prompt_path.name}: missing {field}")
            for field in forbidden:
                self.assertNotIn(f"`{field}`", output, f"{prompt_path.name}: undefined {field}")

if __name__ == "__main__":
    unittest.main()

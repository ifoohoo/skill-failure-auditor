"""职责提示词的输出字段必须与 role-artifact Schema 精确对齐。"""
from __future__ import annotations

import json
import re
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
            declarations = re.findall(
                r"顶层字段只能且必须是(?P<fields>.*?)[；;]",
                output,
                flags=re.DOTALL,
            )
            self.assertEqual(
                len(declarations), 1,
                f"{prompt_path.name}: expected exactly one top-level declaration",
            )
            declared = set(re.findall(r"`([^`]+)`", declarations[0]))
            self.assertEqual(declared, allowed, f"{prompt_path.name}: top-level field drift")
            self.assertTrue(
                forbidden.isdisjoint(declared),
                f"{prompt_path.name}: undefined top-level field",
            )

if __name__ == "__main__":
    unittest.main()

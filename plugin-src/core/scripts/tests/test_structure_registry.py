from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
CANDIDATE_DIR = SCRIPTS_DIR.parent
REFERENCES_DIR = CANDIDATE_DIR / "references"
REGISTRY_PATH = REFERENCES_DIR / "failure-modes.jsonl"
SCHEMA_PATH = REFERENCES_DIR / "failure-mode.schema.json"

sys.path.insert(0, str(SCRIPTS_DIR))

from common import ContractError, canonical_json_bytes, load_jsonl  # noqa: E402
from registry_tool import (  # noqa: E402
    CORE_REDLINES,
    build_selection,
    validate_registry,
)


LEGACY_SEMANTICS = {
    "FM-01": ("自己宣布完成就算通过", "允许执行者自己宣布完成、通过或验收，没有外部裁判环节。"),
    "FM-02": ("只看表面痕迹、不看实际结果", "验收只看文件存在、字段出现或接口被调用等表面痕迹，不看结果内容本身对不对。"),
    "FM-03": ("检查写得等于没查", "验收测试使用len(x)>0、is not None、assert True等怎么都过的写法，而不是比对精确值或性质。"),
    "FM-04": ("拿数量冒充质量", "验收强调跑了N个用例或做了多少个变异，却不验证用例覆盖面和断言精度。"),
    "FM-05": ("自己审自己", "同一个角色或同一会话既写实现又写验收结论，没有独立审阅者。"),
    "FM-06": ("审查者的任务被设成“确认没问题”", "审查者收到的指令是确认实现是否正确，而不是主动寻找能够推翻实现的证据。"),
    "FM-07": ("没人对最终结果负责", "多角色链路没有明确谁最终把关，每个角色都假设其他角色会兜底。"),
    "FM-08": ("关键要求只在一个地方提了一次", "关键禁止或必须事项只在文档中间出现一次，开头、结尾和机器检查都没有强化。"),
    "FM-09": ("上下文越堆越多、从不整理", "长任务或多轮流程没有总结、压缩、清除走不通路径或结构化交接的机制。"),
    "FM-10": ("关键结论没有人复核", "关键中间结论一旦得出，后续没有重新质疑或出错即停的检查点。"),
    "FM-11": ("只说不许做、没说该怎么做", "大量禁止、不要、不可以，却缺少可执行的正确做法。"),
    "FM-12": ("规则不分轻重缓急", "大量规则并列罗列，没有区分违反即失败的硬约束和一般偏好。"),
    "FM-13": ("拿几个例子当完整规则", "验收只给具体案例，没有说明需要保持的抽象性质。"),
    "FM-14": ("被例子的表面形式带偏", "少量同质示例被当作隐性边界，未声明示例与抽象规则的关系。"),
    "FM-15": ("把外部内容当指令执行", "工具返回值、文件内容或日志被当作可信指令直接照做，没有标记为数据。"),
    "FM-16": ("角色权限没有边界", "角色被赋予无需请示的自主判断权，却没有不确定时停下来上报的边界。"),
    "FM-17": ("任务靠口头转述层层传递", "任务依赖多次自然语言转述或摘要，而不是结构化的任务文件。"),
    "FM-18": ("没有故意挑错的验证环节", "没有故意注入已知缺陷来验证验收流程能否抓出来。"),
}


def write_jsonl(path: Path, records: list[dict]) -> None:
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )
    path.write_text(payload, encoding="utf-8")


def estimated_tokens(text: str) -> int:
    """Deterministic conservative estimate without a tokenizer dependency."""
    pieces = re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", text)
    total = 0
    for piece in pieces:
        if re.fullmatch(r"[A-Za-z0-9_]+", piece):
            total += max(1, math.ceil(len(piece) / 4))
        else:
            total += 1
    return total


class SkillStructureTests(unittest.TestCase):
    def test_skill_structure_and_routing_contract(self) -> None:
        skill_path = CANDIDATE_DIR / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        _, frontmatter, body = text.split("---", 2)
        keys = {
            line.split(":", 1)[0].strip()
            for line in frontmatter.splitlines()
            if line.strip() and ":" in line
        }
        self.assertEqual(keys, {"name", "description"})
        self.assertIn("name: skill-failure-auditor", frontmatter)

        for mode in ("static", "runtime", "combined"):
            self.assertIn(f"`{mode}`", body)
        for channel in ("对照清单逐条查", "主动找清单外的问题"):
            self.assertIn(channel, body)
        self.assertIn("SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW", body)
        self.assertIn("只有外部终审方可以给出接受决定", body)
        self.assertIn("--mode", body)
        self.assertIn("--target-type", body)

        linked = re.findall(r"\[[^\]]+\]\(([^)]+)\)", body)
        self.assertGreaterEqual(len(linked), 6)
        for relative in linked:
            self.assertTrue((CANDIDATE_DIR / relative).is_file(), relative)

        self.assertLessEqual(
            estimated_tokens(text),
            2000,
            "SKILL.md exceeds the frozen 2000-token routing-entry budget",
        )

class RegistryContractTests(unittest.TestCase):
    def test_schema_validation_uniqueness_dependencies_and_conflicts(self) -> None:
        validation = validate_registry(REGISTRY_PATH, SCHEMA_PATH)
        self.assertEqual(validation["status"], "VALID")
        self.assertEqual(validation["entry_count"], 28)
        self.assertEqual(validation["builtin_count"], 28)
        self.assertEqual(set(validation["core_redlines"]), CORE_REDLINES)
        self.assertEqual(
            validation["mutation_count"],
            sum(len(entry["mutation_operators"]) for entry in validation["entries"]),
        )

        entries = validation["entries"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            duplicate_path = root / "duplicate.jsonl"
            write_jsonl(duplicate_path, entries + [copy.deepcopy(entries[0])])
            with self.assertRaisesRegex(ContractError, "duplicate rule id"):
                validate_registry(duplicate_path, SCHEMA_PATH)

            unknown_dependency = copy.deepcopy(entries)
            unknown_dependency[3]["depends_on"] = ["FM-99"]
            dependency_path = root / "unknown-dependency.jsonl"
            write_jsonl(dependency_path, unknown_dependency)
            with self.assertRaises(ContractError):
                validate_registry(dependency_path, SCHEMA_PATH)

            asymmetric = copy.deepcopy(entries)
            asymmetric[3]["conflicts_with"] = ["FM-05"]
            conflict_path = root / "asymmetric-conflict.jsonl"
            write_jsonl(conflict_path, asymmetric)
            with self.assertRaisesRegex(ContractError, "asymmetric conflict"):
                validate_registry(conflict_path, SCHEMA_PATH)

            downgraded = copy.deepcopy(entries)
            downgraded[0]["severity"] = "low"
            downgraded_path = root / "downgraded-builtin.jsonl"
            write_jsonl(downgraded_path, downgraded)
            with self.assertRaisesRegex(ContractError, "frozen registry lock"):
                validate_registry(downgraded_path, SCHEMA_PATH)

    def test_legacy_semantics_are_frozen_and_new_ids_are_contiguous(self) -> None:
        entries = load_jsonl(REGISTRY_PATH)
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(
            {f"FM-{index:02d}" for index in range(1, 19)},
            set(LEGACY_SEMANTICS),
        )
        for identifier, expected in LEGACY_SEMANTICS.items():
            self.assertIn(identifier, by_id)
            self.assertEqual((by_id[identifier]["name_zh"], by_id[identifier]["legacy_definition"]), expected)
        self.assertEqual(
            [f"FM-{index:02d}" for index in range(1, 29)],
            sorted(by_id),
        )

    def test_skill_routing_preserves_all_builtins_and_dependency_closure(self) -> None:
        validation = validate_registry(REGISTRY_PATH, SCHEMA_PATH)
        builtin_ids = {f"FM-{index:02d}" for index in range(1, 29)}
        for mode, evidence_type in (("static", "text"), ("combined", "progress")):
            selection = build_selection(
                validation,
                mode,
                "skill",
                {evidence_type},
                28,
            )["selection"]
            selected_ids = {
                item["id"]
                for item in selection["selection_context"]["selected_rules"]
            }
            with self.subTest(mode=mode, evidence_type=evidence_type):
                self.assertEqual(selected_ids, builtin_ids)
                self.assertTrue(
                    {"FM-04", "FM-07", "FM-16", "FM-17", "FM-19", "FM-20", "FM-23", "FM-24"}.issubset(
                        selected_ids
                    )
                )
                self.assertIn("FM-21", selected_ids)
                self.assertEqual(selection["status"], "SELECTED")

        with self.assertRaisesRegex(ContractError, "mandatory closed rule count"):
            build_selection(validation, "static", "skill", {"text"}, 27)

    def test_dual_run_registry_selection_and_ledgers_are_identical(self) -> None:
        first_validation = validate_registry(REGISTRY_PATH, SCHEMA_PATH)
        second_validation = validate_registry(REGISTRY_PATH, SCHEMA_PATH)
        self.assertEqual(first_validation, second_validation)

        arguments = ("combined", "skill", {"text", "test"}, 28)
        first_bundle = build_selection(first_validation, *arguments)
        second_bundle = build_selection(second_validation, *arguments)
        self.assertEqual(first_bundle, second_bundle)
        first_selection = first_bundle["selection"]
        second_selection = second_bundle["selection"]
        self.assertEqual(
            first_selection["selection_sha256"],
            second_selection["selection_sha256"],
        )
        self.assertEqual(
            first_selection["coverage_ledger_sha256"],
            second_selection["coverage_ledger_sha256"],
        )
        self.assertEqual(
            first_selection["registry_entry_count"],
            len(first_bundle["coverage"]["entries"]),
        )
        selected_ids = {
            item["id"] for item in first_selection["selection_context"]["selected_rules"]
        }
        self.assertTrue(CORE_REDLINES.issubset(selected_ids))

    def test_synthetic_200_rule_capacity_keeps_entry_and_selection_bounded(self) -> None:
        original_skill_size = (CANDIDATE_DIR / "SKILL.md").stat().st_size
        validation = validate_registry(REGISTRY_PATH, SCHEMA_PATH)
        entries = copy.deepcopy(validation["entries"])
        template = copy.deepcopy(entries[10])
        for index in range(1, 201):
            synthetic = copy.deepcopy(template)
            synthetic["id"] = f"SYN-{index:04d}"
            synthetic["name_zh"] = f"合成规则{index:04d}"
            synthetic["legacy_definition"] = f"容量测试合成规则定义{index:04d}。"
            synthetic["priority"] = 1
            synthetic["core_redline"] = False
            synthetic["depends_on"] = []
            synthetic["conflicts_with"] = []
            synthetic["mutation_operators"] = [
                {
                    "id": f"MUT-SYN-{index:04d}-01",
                    "description": f"容量测试变异{index:04d}",
                    "expected_detection": "should_detect",
                }
            ]
            entries.append(synthetic)

        with tempfile.TemporaryDirectory() as temporary:
            expanded_path = Path(temporary) / "expanded.jsonl"
            write_jsonl(expanded_path, entries)
            expanded = validate_registry(expanded_path, SCHEMA_PATH)
            bundle = build_selection(expanded, "combined", "skill", {"text"}, 28)
            selection = bundle["selection"]

        self.assertEqual(expanded["entry_count"], 228)
        self.assertEqual(selection["registry_entry_count"], 228)
        self.assertEqual(selection["selected_count"], 28)
        self.assertEqual(selection["status"], "INCOMPLETE_LOW_CONFIDENCE")
        self.assertGreater(selection["truncated_count"], 0)
        self.assertEqual(len(bundle["coverage"]["entries"]), 228)
        self.assertLess(
            len(canonical_json_bytes(selection["selection_context"])),
            32 * 1024,
        )
        selected_ids = {
            item["id"] for item in selection["selection_context"]["selected_rules"]
        }
        self.assertTrue(CORE_REDLINES.issubset(selected_ids))
        self.assertEqual((CANDIDATE_DIR / "SKILL.md").stat().st_size, original_skill_size)

    def test_synthetic_200_high_priority_rules_cannot_displace_core_redlines(self) -> None:
        validation = validate_registry(REGISTRY_PATH, SCHEMA_PATH)
        entries = copy.deepcopy(validation["entries"])
        template = copy.deepcopy(entries[10])
        for index in range(1, 201):
            synthetic = copy.deepcopy(template)
            synthetic["id"] = f"SYN-{index:04d}"
            synthetic["name_zh"] = f"高优先合成规则{index:04d}"
            synthetic["legacy_definition"] = f"不得挤出核心红线的合成规则{index:04d}。"
            synthetic["priority"] = 100
            synthetic["core_redline"] = False
            synthetic["depends_on"] = []
            synthetic["conflicts_with"] = []
            synthetic["mutation_operators"] = [
                {
                    "id": f"MUT-SYN-{index:04d}-01",
                    "description": f"高优先容量变异{index:04d}",
                    "expected_detection": "should_detect",
                }
            ]
            entries.append(synthetic)

        with tempfile.TemporaryDirectory() as temporary:
            expanded_path = Path(temporary) / "high-priority-expanded.jsonl"
            write_jsonl(expanded_path, entries)
            expanded = validate_registry(expanded_path, SCHEMA_PATH)
            bundle = build_selection(expanded, "combined", "skill", {"text"}, 28)
            selection = bundle["selection"]

        selected_ids = {
            item["id"] for item in selection["selection_context"]["selected_rules"]
        }
        self.assertEqual(selection["selected_count"], 28)
        self.assertTrue(
            CORE_REDLINES.issubset(selected_ids),
            f"high-priority synthetic rules displaced core redlines: {sorted(CORE_REDLINES - selected_ids)}",
        )


if __name__ == "__main__":
    unittest.main()

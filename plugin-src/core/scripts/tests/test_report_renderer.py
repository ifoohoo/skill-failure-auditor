from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
CANDIDATE_DIR = SCRIPTS_DIR.parent
REGISTRY_PATH = CANDIDATE_DIR / "references" / "failure-modes.jsonl"

sys.path.insert(0, str(SCRIPTS_DIR))

from report_renderer import load_registry_names, render  # noqa: E402


OLD_JARGON = (
    "失败关闭",
    "密封",
    "判定标准",
    "硬门禁",
    "物化",
    "覆盖账本",
    "摘要漂移",
    "续接包",
    "排他",
    "一票否决",
    "机器登记表",
    "外部最终裁决器",
)


def sample_result() -> dict:
    digest = "a" * 64
    return {
        "schema_version": "1.0",
        "audit_id": "AUDIT-TEST-001",
        "mode": "combined",
        "subject": {"path": "/tmp/target", "file_set_sha256": digest},
        "evidence_index": {"path": "/tmp/index.json", "sha256": digest},
        "coverage_records": {"path": "/tmp/records.jsonl", "sha256": digest},
        "coverage_ledger": {"path": "/tmp/ledger.json", "sha256": digest},
        "registry_sha256": digest,
        "selection_sha256": digest,
        "coverage_status": "COMPLETE",
        "known_rule_results": [
            {
                "id": "FM-05",
                "revision": 1,
                "severity": "critical",
                "status": "HIT",
                "evidence_refs": [{"chunk_id": "CHUNK-000001", "chunk_sha256": digest}],
                "reason": "同一个人既写实现又写验收结论。",
            },
            {
                "id": "FM-01",
                "revision": 1,
                "severity": "critical",
                "status": "NOT_HIT",
                "evidence_refs": [{"chunk_id": "CHUNK-000002", "chunk_sha256": digest}],
                "reason": "存在外部裁决记录。",
            },
            {
                "id": "FM-26",
                "revision": 1,
                "severity": "critical",
                "status": "UNCHECKED",
                "evidence_refs": [],
                "reason": "缺少分片材料。",
            },
        ],
        "novel_hypotheses": [
            {
                "id": "HYP-01",
                "hypothesis": "验证器可能被实现者修改",
                "observable_signal": "验证器与实现同目录",
                "falsifier": "验证器来源是外部只读路径",
                "next_probe": "检查验证器写入权限",
            }
        ],
        "executable_acceptance": {
            "status": "ABSENT",
            "evidence_refs": [],
            "reason": "没有可执行验收工件。",
        },
        "hard_gate_failures": ["HIGH_SEVERITY_UNCHECKED"],
        "conclusion": "REJECT",
        "self_audit": False,
        "status": "AUDIT_SUBMITTED_FOR_REVIEW",
    }


class ReportRendererTests(unittest.TestCase):
    def test_render_produces_human_readable_report(self) -> None:
        names = load_registry_names(REGISTRY_PATH)
        report = render(sample_result(), names)

        for section in ("## 总结", "## 结论", "## 逐条发现", "## 清单外的新问题", "## 未完成事项", "## 附录：机器数据"):
            self.assertIn(section, report)
        self.assertIn("FM-05（自己审自己）", report)
        self.assertIn("FM-01（自己宣布完成就算通过）", report)
        self.assertIn("命中红线组合或存在结构性缺陷", report)
        self.assertIn("同一个人既写实现又写验收结论", report)
        for jargon in OLD_JARGON:
            self.assertNotIn(jargon, report)

    def test_cli_writes_report_exclusively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "result.json"
            output_path = root / "report.md"
            import json

            result_path.write_text(
                json.dumps(sample_result(), ensure_ascii=False), encoding="utf-8"
            )
            command = [
                sys.executable,
                str(SCRIPTS_DIR / "report_renderer.py"),
                "--input",
                str(result_path),
                "--output",
                str(output_path),
            ]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            text = output_path.read_text(encoding="utf-8")
            self.assertIn("## 总结", text)
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)


if __name__ == "__main__":
    unittest.main()

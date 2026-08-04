from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
CANDIDATE_DIR = SCRIPTS_DIR.parent
REFERENCES_DIR = CANDIDATE_DIR / "references"
sys.path.insert(0, str(SCRIPTS_DIR))

from common import ContractError, canonical_json_bytes, load_jsonl  # noqa: E402
from evidence_tool import (  # noqa: E402
    build_coverage,
    build_index,
    search_index,
    verify_index,
)
from registry_tool import build_selection, validate_registry  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def audited_records(index: dict, rule_ids: list[str] | None = None) -> list[dict]:
    covered_rules = rule_ids or ["FM-26"]
    return [
        {
            "chunk_id": chunk["id"],
            "chunk_sha256": chunk["sha256"],
            "status": "AUDITED",
            "rule_ids": covered_rules,
            "finding_count": 0,
        }
        for chunk in index["chunks"]
    ]


def selection_fixture(root: Path) -> tuple[Path, Path, list[str]]:
    registry_path = REFERENCES_DIR / "failure-modes.jsonl"
    validation = validate_registry(
        registry_path,
        REFERENCES_DIR / "failure-mode.schema.json",
    )
    selection = build_selection(
        validation,
        "combined",
        "skill",
        {"text"},
        28,
    )["selection"]
    selection_path = root / "selection.json"
    write_json(selection_path, selection)
    selected_ids = [
        item["id"] for item in selection["selection_context"]["selected_rules"]
    ]
    return selection_path, registry_path, selected_ids


def run_coverage(
    source: Path,
    index: Path,
    selection: Path,
    records: Path,
    output: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "evidence_tool.py"),
            "coverage",
            "--input",
            str(source),
            "--index",
            str(index),
            "--selection",
            str(selection),
            "--records",
            str(records),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


class EvidenceIntegrityTests(unittest.TestCase):
    def test_empty_evidence_and_empty_coverage_cannot_vacuously_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty_file = root / "empty.bin"
            empty_file.write_bytes(b"")
            empty_directory = root / "empty-directory"
            empty_directory.mkdir()
            for path in (empty_file, empty_directory):
                with self.subTest(path=path), self.assertRaisesRegex(
                    ContractError,
                    "at least one regular file|non-empty chunk",
                ):
                    build_index(path, 1024)

            source = root / "source.bin"
            source.write_bytes(b"A" * 2048)
            index = build_index(source, 1024)
            index_path = root / "index.json"
            write_json(index_path, index)
            selection_path, registry_path, _ = selection_fixture(root)
            records_path = root / "empty-records.jsonl"
            records_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "at least one audited"):
                build_coverage(
                    source,
                    index_path,
                    selection_path,
                    registry_path,
                    records_path,
                )

    def test_synthetic_10mb_first_middle_and_last_markers_are_located(self) -> None:
        markers = {
            "first": (4096, b"DEFECT-FIRST-5E4A"),
            "middle": (5 * 1024 * 1024 + 17, b"DEFECT-MIDDLE-91C2"),
            "last": (10 * 1024 * 1024 - 128, b"DEFECT-LAST-A0D7"),
        }
        payload = bytearray(b"x" * (10 * 1024 * 1024 + 257))
        for offset, marker in markers.values():
            payload[offset : offset + len(marker)] = marker

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "large-evidence.bin"
            index_path = root / "index.json"
            evidence.write_bytes(payload)
            first_index = build_index(evidence, 64 * 1024)
            second_index = build_index(evidence, 64 * 1024)
            self.assertEqual(first_index, second_index)
            write_json(index_path, first_index)
            self.assertEqual(verify_index(evidence, index_path), first_index)

            for label, (expected_offset, marker) in markers.items():
                result = search_index(evidence, index_path, marker)
                self.assertEqual(result["match_count"], 1, label)
                match = result["matches"][0]
                self.assertEqual(match["start"], expected_offset, label)
                self.assertEqual(match["end"], expected_offset + len(marker), label)
                self.assertGreaterEqual(len(match["chunk_ids"]), 1, label)

            selection_path, registry_path, selected_ids = selection_fixture(root)
            records_path = root / "coverage.jsonl"
            write_jsonl(records_path, audited_records(first_index, selected_ids))
            first_ledger, first_exit = build_coverage(
                evidence,
                index_path,
                selection_path,
                registry_path,
                records_path,
            )
            second_ledger, second_exit = build_coverage(
                evidence,
                index_path,
                selection_path,
                registry_path,
                records_path,
            )
            self.assertEqual((first_ledger, first_exit), (second_ledger, second_exit))
            self.assertEqual(first_exit, 0)
            self.assertEqual(first_ledger["status"], "COMPLETE")
            self.assertTrue(first_ledger["conservation_holds"])

    def test_strict_jsonl_rejects_blank_malformed_and_duplicate_key_lines(self) -> None:
        invalid_payloads = {
            "blank": '{"ok":1}\n\n{"ok":2}\n',
            "malformed": '{"ok":1}\n{"broken":}\n',
            "duplicate-key": '{"same":1,"same":2}\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in invalid_payloads.items():
                path = root / f"{name}.jsonl"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ContractError):
                    load_jsonl(path)

    def test_missing_duplicate_and_digest_mismatch_coverage_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence.bin"
            index_path = root / "index.json"
            evidence.write_bytes(b"A" * 4096)
            index = build_index(evidence, 1024)
            write_json(index_path, index)
            selection_path, registry_path, selected_ids = selection_fixture(root)
            complete = audited_records(index, selected_ids)

            cases = {
                "missing": complete[:-1],
                "duplicate": complete + [copy.deepcopy(complete[0])],
                "digest-mismatch": [
                    (
                        {**record, "chunk_sha256": "0" * 64}
                        if position == 1
                        else record
                    )
                    for position, record in enumerate(complete)
                ],
            }
            expected_status = {
                "missing": "INCOMPLETE",
                "duplicate": "INVALID",
                "digest-mismatch": "INVALID",
            }
            for name, records in cases.items():
                records_path = root / f"{name}.jsonl"
                write_jsonl(records_path, records)
                ledger, exit_code = build_coverage(
                    evidence,
                    index_path,
                    selection_path,
                    registry_path,
                    records_path,
                )
                with self.subTest(name=name):
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(ledger["status"], expected_status[name])
                    self.assertFalse(ledger["conservation_holds"])

    def test_tampered_source_or_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence.bin"
            index_path = root / "index.json"
            evidence.write_bytes(b"A" * 4096)
            index = build_index(evidence, 1024)
            write_json(index_path, index)

            evidence.write_bytes(b"A" * 2048 + b"B" + b"A" * 2047)
            with self.assertRaisesRegex(ContractError, "drifted"):
                verify_index(evidence, index_path)

            evidence.write_bytes(b"A" * 4096)
            tampered = copy.deepcopy(index)
            tampered["chunks"][1]["sha256"] = "0" * 64
            write_json(root / "tampered-index.json", tampered)
            with self.assertRaisesRegex(ContractError, "self digest mismatch"):
                verify_index(evidence, root / "tampered-index.json")

    def test_coverage_binds_verified_index_and_selected_nonempty_rule_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            source.write_bytes(b"A" * 4096)
            index = build_index(source, 1024)
            index_path = root / "index.json"
            write_json(index_path, index)

            selection_path, _, selected_ids = selection_fixture(root)

            valid_records = audited_records(index, selected_ids)
            valid_path = root / "valid.jsonl"
            valid_output = root / "valid-ledger.json"
            write_jsonl(valid_path, valid_records)
            valid_run = run_coverage(
                source,
                index_path,
                selection_path,
                valid_path,
                valid_output,
            )
            self.assertEqual(valid_run.returncode, 0, valid_run.stderr)
            valid_ledger = json.loads(valid_output.read_text(encoding="utf-8"))
            self.assertEqual(valid_ledger["status"], "COMPLETE")
            self.assertTrue(valid_ledger["conservation_holds"])

            tampered_index = copy.deepcopy(index)
            tampered_index["chunks"][0]["sha256"] = "0" * 64
            tampered_index_path = root / "tampered-index-for-coverage.json"
            write_json(tampered_index_path, tampered_index)

            invalid_cases = {
                "tampered-index": (
                    tampered_index_path,
                    valid_records,
                ),
                "empty-rule-ids": (
                    index_path,
                    [{**record, "rule_ids": []} for record in valid_records],
                ),
                "unselected-rule-id": (
                    index_path,
                    [{**record, "rule_ids": ["FM-99"]} for record in valid_records],
                ),
            }
            for name, (case_index, records) in invalid_cases.items():
                records_path = root / f"{name}.jsonl"
                output_path = root / f"{name}-ledger.json"
                write_jsonl(records_path, records)
                run = run_coverage(
                    source,
                    case_index,
                    selection_path,
                    records_path,
                    output_path,
                )
                with self.subTest(name=name):
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()

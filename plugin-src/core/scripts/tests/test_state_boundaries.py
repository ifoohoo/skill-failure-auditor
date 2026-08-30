from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent
CANDIDATE_DIR = SCRIPTS_DIR.parent
REFERENCES_DIR = CANDIDATE_DIR / "references"
sys.path.insert(0, str(SCRIPTS_DIR))

import evaluation_tool  # noqa: E402
from attempt_tool import (  # noqa: E402
    OUTCOMES,
    create_attempt,
    record_artifact,
    seal_attempt,
    unsigned_digest,
    verify_attempt,
)
from common import ContractError  # noqa: E402
from evidence_tool import build_coverage, build_index  # noqa: E402
from evaluation_tool import (  # noqa: E402
    auditor_core_identity,
    build_reuse_receipt,
    continuation_create,
    continuation_digest,
    continuation_verify,
    derive_self_audit,
    grade_cases,
    reuse_check,
    source_manifest_digest,
    validate_audit_result,
)
from registry_tool import build_selection, validate_registry  # noqa: E402
from report_renderer import load_registry_names, render  # noqa: E402


def sha256_file(path: Path) -> str:
    """Local value-equivalent helper; the production raw-byte digest now lives in Foundation."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def valid_audit_result(
    subject: Path,
    index_path: Path,
    records_path: Path,
    ledger_path: Path,
    index: dict,
) -> dict:
    self_audit = derive_self_audit(subject)
    evidence_ref = {
        "chunk_id": index["chunks"][0]["id"],
        "chunk_sha256": index["chunks"][0]["sha256"],
    }
    return {
        "schema_version": "1.0",
        "audit_id": "AUDIT-01",
        "mode": "combined",
        "subject": {
            "path": str(subject),
            "file_set_sha256": index["file_set_sha256"],
        },
        "evidence_index": {
            "path": str(index_path),
            "sha256": sha256_file(index_path),
        },
        "coverage_records": {
            "path": str(records_path),
            "sha256": sha256_file(records_path),
        },
        "coverage_ledger": {
            "path": str(ledger_path),
            "sha256": sha256_file(ledger_path),
        },
        "registry_sha256": "a" * 64,
        "selection_sha256": "b" * 64,
        "coverage_status": "COMPLETE",
        "known_rule_results": [
            {
                "id": "FM-01",
                "revision": 1,
                "severity": "critical",
                "status": "NOT_HIT",
                "evidence_refs": [evidence_ref],
                "reason": "冻结证据未见该失效信号",
            }
        ],
        "novel_hypotheses": [
            {
                "id": "HYP-01",
                "hypothesis": "外部状态可能与本地证据漂移",
                "observable_signal": "摘要不同",
                "falsifier": "外部重算一致",
                "next_probe": "请求外部重算",
            }
        ],
        "executable_acceptance": {
            "status": "UNCHECKED",
            "evidence_refs": [],
            "reason": "当前公开测试不提供外部可执行验收",
        },
        "hard_gate_failures": [
            "NO_EXTERNAL_FINALIZER",
            "EXECUTABLE_ACCEPTANCE_UNCHECKED",
        ],
        "conclusion": "BLOCKED",
        "self_audit": self_audit,
        "status": (
            "SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW"
            if self_audit
            else "AUDIT_SUBMITTED_FOR_REVIEW"
        ),
    }


def run_result_validation(
    result: Path,
    selection: Path,
    registry: Path,
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPTS_DIR / "evaluation_tool.py"),
        "validate-result",
        "--input",
        str(result),
        "--selection",
        str(selection),
        "--registry",
        str(registry),
    ]
    if output is not None:
        command.extend(["--output", str(output)])
    return subprocess.run(command, check=False, capture_output=True, text=True)


def bound_audit_fixture(root: Path, subject: Path) -> tuple[dict, Path, Path]:
    registry_path = REFERENCES_DIR / "failure-modes.jsonl"
    registry = validate_registry(
        registry_path,
        REFERENCES_DIR / "failure-mode.schema.json",
    )
    selection = build_selection(
        registry,
        "combined",
        "skill",
        {"text"},
        28,
    )["selection"]
    selection_path = root / "selection.json"
    write_json(selection_path, selection)
    index_path = root / "audit-index.json"
    records_path = root / "audit-coverage-records.jsonl"
    ledger_path = root / "audit-coverage-ledger.json"
    index = build_index(subject, 1024)
    write_json(index_path, index)
    selected_ids = [
        item["id"] for item in selection["selection_context"]["selected_rules"]
    ]
    records = [
        {
            "chunk_id": chunk["id"],
            "chunk_sha256": chunk["sha256"],
            "status": "AUDITED",
            "rule_ids": selected_ids,
            "finding_count": 0,
        }
        for chunk in index["chunks"]
    ]
    write_jsonl(records_path, records)
    ledger, exit_code = build_coverage(
        subject,
        index_path,
        selection_path,
        registry_path,
        records_path,
    )
    if exit_code != 0:
        raise AssertionError(f"fixture coverage must be complete: {ledger}")
    write_json(ledger_path, ledger)
    result = valid_audit_result(
        subject,
        index_path,
        records_path,
        ledger_path,
        index,
    )
    result["mode"] = selection["selection_context"]["mode"]
    result["registry_sha256"] = registry["registry_sha256"]
    result["selection_sha256"] = selection["selection_sha256"]
    evidence_ref = {
        "chunk_id": index["chunks"][0]["id"],
        "chunk_sha256": index["chunks"][0]["sha256"],
    }
    result["known_rule_results"] = [
        {
            "id": item["id"],
            "revision": item["revision"],
            "severity": item["severity"],
            "status": "NOT_HIT",
            "evidence_refs": [evidence_ref],
            "reason": "冻结证据未见该失效信号",
        }
        for item in selection["selection_context"]["selected_rules"]
    ]
    return result, selection_path, registry_path


def eligible_reuse_fixture(
    root: Path,
    *,
    record_receipt: bool = True,
    duplicate_receipt: bool = False,
) -> dict[str, object]:
    subject = root / "subject.txt"
    subject.write_text("stable audited subject\n", encoding="utf-8")
    result, selection_path, registry_path = bound_audit_fixture(root, subject)
    evidence_ref = copy.deepcopy(result["known_rule_results"][0]["evidence_refs"])
    result["executable_acceptance"] = {
        "status": "VERIFIED",
        "evidence_refs": evidence_ref,
        "reason": "冻结验收工件已验证",
    }
    result["hard_gate_failures"] = []
    result["conclusion"] = "PASS_WITHIN_FROZEN_SCOPE"
    result["status"] = "AUDIT_SUBMITTED_FOR_REVIEW"
    result_path = root / "audit-result.json"
    write_json(result_path, result)
    report_path = root / "report.md"
    report_path.write_text(
        render(result, load_registry_names(registry_path)),
        encoding="utf-8",
    )
    criteria_path = root / "criteria.json"
    write_json(
        criteria_path,
        {
            "goal": "audit the frozen subject",
            "acceptance": "all selected rules checked",
            "permissions": "read-only audit",
            "write_scope": "evidence only",
        },
    )
    created = create_attempt(
        argparse.Namespace(
            root=root / "attempts",
            attempt_id="ATTEMPT-REUSE-01",
            candidate_sha256=result["subject"]["file_set_sha256"],
            criteria_commitment_sha256=sha256_file(criteria_path),
            write_path=["evidence/attempts/ATTEMPT-REUSE-01"],
            created_by="reuse-test",
            created_at="2026-08-28T00:00:00Z",
        )
    )
    attempt = Path(created["attempt"])
    for kind, artifact, timestamp in (
        ("audit_result", result_path, "2026-08-28T00:01:00Z"),
        ("audit_report", report_path, "2026-08-28T00:02:00Z"),
    ):
        record_artifact(
            argparse.Namespace(
                attempt=attempt,
                kind=kind,
                artifact=artifact,
                recorded_at=timestamp,
            )
        )
    receipt_path = root / "audit-reuse-receipt.json"
    receipt = build_reuse_receipt(
        result_path,
        report_path,
        selection_path,
        attempt,
    )
    write_json(receipt_path, receipt)
    if record_receipt:
        record_artifact(
            argparse.Namespace(
                attempt=attempt,
                kind="audit_reuse_receipt",
                artifact=receipt_path,
                recorded_at="2026-08-28T00:03:00Z",
            )
        )
        if duplicate_receipt:
            record_artifact(
                argparse.Namespace(
                    attempt=attempt,
                    kind="audit_reuse_receipt",
                    artifact=receipt_path,
                    recorded_at="2026-08-28T00:03:30Z",
                )
            )
    sealed = seal_attempt(
        argparse.Namespace(
            attempt=attempt,
            outcome="CANDIDATE_SUBMITTED",
            reason_code="AUDIT_SUBMITTED",
            sealed_at="2026-08-28T00:04:00Z",
        )
    )
    return {
        "subject": subject,
        "result": result_path,
        "report": report_path,
        "selection": selection_path,
        "criteria": criteria_path,
        "attempt": attempt,
        "receipt": receipt_path,
        "seal_file_sha256": sealed["seal_file_sha256"],
    }


class AttemptBoundaryTests(unittest.TestCase):
    def test_attempt_cannot_be_recreated_resealed_or_written_after_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.txt"
            artifact.write_text("frozen evidence\n", encoding="utf-8")
            create_args = argparse.Namespace(
                root=root / "attempts",
                attempt_id="ATTEMPT-01",
                candidate_sha256="a" * 64,
                criteria_commitment_sha256="b" * 64,
                write_path=["evidence/attempts/ATTEMPT-01"],
                created_by="public-test",
                created_at="2026-07-28T00:00:00Z",
            )
            created = create_attempt(create_args)
            attempt = Path(created["attempt"])

            record_args = argparse.Namespace(
                attempt=attempt,
                kind="gate_result",
                artifact=artifact,
                recorded_at="2026-07-28T00:01:00Z",
            )
            record_artifact(record_args)
            seal_args = argparse.Namespace(
                attempt=attempt,
                outcome="FAILED",
                reason_code="PUBLIC_GATE_FAILED",
                sealed_at="2026-07-28T00:02:00Z",
            )
            sealed = seal_attempt(seal_args)
            verification = verify_attempt(attempt)
            self.assertEqual(
                verification["status"],
                "SEALED_PENDING_EXTERNAL_BINDING",
            )
            self.assertEqual(verification["seal_binding_status"], "UNBOUND")
            self.assertFalse(verification["formal_acceptance_eligible"])
            bound = verify_attempt(attempt, sealed["seal_file_sha256"])
            self.assertEqual(bound["status"], "VERIFIED_SEALED_BOUND")
            self.assertEqual(bound["seal_binding_status"], "EXTERNAL_DIGEST_MATCH")
            self.assertEqual(verification["outcome"], "FAILED")

            with self.assertRaisesRegex(ContractError, "already exists"):
                create_attempt(create_args)
            with self.assertRaisesRegex(ContractError, "already sealed"):
                seal_attempt(seal_args)
            with self.assertRaisesRegex(ContractError, "already sealed"):
                record_artifact(record_args)

    def test_attempt_outcomes_have_no_acceptance_terminal(self) -> None:
        self.assertNotIn("PASS", OUTCOMES)
        self.assertNotIn("ACCEPTED", OUTCOMES)
        self.assertNotIn("CANDIDATE_ACCEPT", OUTCOMES)

    def test_sealed_attempt_rejects_self_consistent_manifest_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.txt"
            artifact.write_text("sealed evidence\n", encoding="utf-8")
            created = create_attempt(
                argparse.Namespace(
                    root=root / "attempts",
                    attempt_id="ATTEMPT-02",
                    candidate_sha256="a" * 64,
                    criteria_commitment_sha256="b" * 64,
                    write_path=["evidence/attempts/ATTEMPT-02"],
                    created_by="public-test",
                    created_at="2026-07-28T00:00:00Z",
                )
            )
            attempt = Path(created["attempt"])
            record_artifact(
                argparse.Namespace(
                    attempt=attempt,
                    kind="gate_result",
                    artifact=artifact,
                    recorded_at="2026-07-28T00:01:00Z",
                )
            )
            seal_attempt(
                argparse.Namespace(
                    attempt=attempt,
                    outcome="FAILED",
                    reason_code="PUBLIC_GATE_FAILED",
                    sealed_at="2026-07-28T00:02:00Z",
                )
            )

            manifest_path = attempt / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["created_by"] = "manifest-replacement-attacker"
            manifest["manifest_sha256"] = unsigned_digest(manifest, "manifest_sha256")
            write_json(manifest_path, manifest)

            with self.assertRaises(ContractError):
                verify_attempt(attempt)

    def test_rehashed_seal_rewrite_fails_external_frozen_file_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.txt"
            artifact.write_text("sealed evidence\n", encoding="utf-8")
            created = create_attempt(
                argparse.Namespace(
                    root=root / "attempts",
                    attempt_id="ATTEMPT-03",
                    candidate_sha256="a" * 64,
                    criteria_commitment_sha256="b" * 64,
                    write_path=["evidence/attempts/ATTEMPT-03"],
                    created_by="public-test",
                    created_at="2026-07-28T00:00:00Z",
                )
            )
            attempt = Path(created["attempt"])
            record_artifact(
                argparse.Namespace(
                    attempt=attempt,
                    kind="gate_result",
                    artifact=artifact,
                    recorded_at="2026-07-28T00:01:00Z",
                )
            )
            sealed = seal_attempt(
                argparse.Namespace(
                    attempt=attempt,
                    outcome="FAILED",
                    reason_code="PUBLIC_GATE_FAILED",
                    sealed_at="2026-07-28T00:02:00Z",
                )
            )
            seal_path = attempt / "seal.json"
            seal_path.chmod(0o600)
            rewritten = json.loads(seal_path.read_text(encoding="utf-8"))
            rewritten["outcome"] = "CANDIDATE_SUBMITTED"
            rewritten["reason_code"] = "PUBLIC_GATE_PASSED"
            rewritten["seal_sha256"] = unsigned_digest(rewritten, "seal_sha256")
            write_json(seal_path, rewritten)

            with self.assertRaisesRegex(ContractError, "external frozen digest"):
                verify_attempt(attempt, sealed["seal_file_sha256"])


class ContinuationBoundaryTests(unittest.TestCase):
    def test_continuation_rejects_package_and_bound_source_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("frozen source\n", encoding="utf-8")
            second_source = root / "source-2.txt"
            second_source.write_text("second frozen source\n", encoding="utf-8")
            template_path = root / "template.json"
            package_path = root / "package.json"
            source_manifest_path = root / "source-manifest.json"
            registry_path = REFERENCES_DIR / "failure-modes.jsonl"
            registry = validate_registry(
                registry_path,
                REFERENCES_DIR / "failure-mode.schema.json",
            )
            selection = build_selection(
                registry,
                "combined",
                "skill",
                {"text"},
                28,
            )["selection"]
            selection_path = root / "selection.json"
            write_json(selection_path, selection)
            source_bindings = sorted(
                [
                    {"path": str(source), "sha256": sha256_file(source)},
                    {"path": str(second_source), "sha256": sha256_file(second_source)},
                ],
                key=lambda item: item["path"].encode("utf-8"),
            )
            source_manifest = {
                "schema_version": "1.0",
                "manifest_id": "SOURCE-CONT-01",
                "task_id": "skill-failure-auditor-productization-test",
                "bindings": source_bindings,
            }
            source_manifest["manifest_digest"] = source_manifest_digest(source_manifest)
            write_json(source_manifest_path, source_manifest)
            template = {
                "schema_version": "1.0",
                "package_id": "CONT-01",
                "task_id": "skill-failure-auditor-productization-test",
                "current_node": "public-evaluation",
                "registry_sha256": selection["registry_sha256"],
                "selection_sha256": selection["selection_sha256"],
                "source_manifest_sha256": sha256_file(source_manifest_path),
                "source_bindings": source_bindings,
                "selected_rules": [
                    {
                        "id": item["id"],
                        "revision": item["revision"],
                        "source_sha256": item["source_sha256"],
                    }
                    for item in selection["selection_context"]["selected_rules"]
                ],
                "accepted_artifacts": [],
                "remaining_tasks": ["external semantic review"],
                "disproved_paths": ["self-review as independent acceptance"],
                "open_questions": ["external finalizer availability"],
                "next_action": "submit sealed evidence to an external reviewer",
                "context_state": "HANDOFF_REQUIRED",
            }
            write_json(template_path, template)
            continuation_create(
                template_path,
                package_path,
                selection_path,
                registry_path,
                source_manifest_path,
            )
            self.assertEqual(
                continuation_verify(
                    package_path,
                    selection_path,
                    registry_path,
                    source_manifest_path,
                )["status"],
                "VERIFIED",
            )

            package = json.loads(package_path.read_text(encoding="utf-8"))
            binding_mutations: dict[str, dict] = {}

            wrong_id = copy.deepcopy(package)
            wrong_id["selected_rules"][0]["id"] = "SYN-9999"
            binding_mutations["rule-id"] = wrong_id

            wrong_revision = copy.deepcopy(package)
            wrong_revision["selected_rules"][0]["revision"] += 1
            binding_mutations["revision"] = wrong_revision

            wrong_source_digest = copy.deepcopy(package)
            wrong_source_digest["selected_rules"][0]["source_sha256"] = "0" * 64
            binding_mutations["source-digest"] = wrong_source_digest

            missing_core = copy.deepcopy(package)
            missing_core["selected_rules"] = [
                item for item in missing_core["selected_rules"] if item["id"] != "FM-01"
            ]
            binding_mutations["missing-core-redline"] = missing_core

            missing_source = copy.deepcopy(package)
            missing_source["source_bindings"] = missing_source["source_bindings"][:-1]
            binding_mutations["missing-source-binding"] = missing_source

            for name, mutated in binding_mutations.items():
                mutated["package_digest"] = continuation_digest(mutated)
                mutated_path = root / f"rehashed-{name}.json"
                write_json(mutated_path, mutated)
                expected_error = (
                    "source bindings do not exactly match"
                    if name == "missing-source-binding"
                    else "selected rules do not exactly match selection"
                )
                with self.subTest(name=name), self.assertRaisesRegex(
                    ContractError,
                    expected_error,
                ):
                    continuation_verify(
                        mutated_path,
                        selection_path,
                        registry_path,
                        source_manifest_path,
                    )

            package["next_action"] = "silently accept candidate"
            tampered_package = root / "tampered-package.json"
            write_json(tampered_package, package)
            with self.assertRaisesRegex(ContractError, "self digest mismatch"):
                continuation_verify(
                    tampered_package,
                    selection_path,
                    registry_path,
                    source_manifest_path,
                )

            source.write_text("tampered source\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "digest mismatch"):
                continuation_verify(
                    package_path,
                    selection_path,
                    registry_path,
                    source_manifest_path,
                )


class EvaluationBoundaryTests(unittest.TestCase):
    def test_result_validation_reuses_registry_validation_for_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = root / "subject.txt"
            subject.write_text("candidate\n", encoding="utf-8")
            result, selection_path, registry_path = bound_audit_fixture(root, subject)
            result_path = root / "result.json"
            write_json(result_path, result)

            import registry_tool

            with patch.object(
                registry_tool,
                "validate_registry",
                wraps=registry_tool.validate_registry,
            ) as validate_registry_spy:
                validation = validate_audit_result(
                    result_path,
                    selection_path,
                    registry_path,
                )

            self.assertEqual(validation["status"], "VALID")
            self.assertEqual(validate_registry_spy.call_count, 1)

    def test_self_audit_identity_covers_descendants_aliases_and_counterparts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(derive_self_audit(CANDIDATE_DIR))
            self.assertTrue(derive_self_audit(CANDIDATE_DIR / "SKILL.md"))
            self.assertTrue(
                derive_self_audit(CANDIDATE_DIR / "references" / "report-contract.md")
            )

            alias = root / "candidate-alias"
            alias.symlink_to(CANDIDATE_DIR, target_is_directory=True)
            self.assertTrue(derive_self_audit(alias / "SKILL.md"))

            counterpart = root / "counterpart"
            for relative in (
                "SKILL.md",
                "scripts/evaluation_tool.py",
                "references/builtin-registry-lock.json",
            ):
                source = CANDIDATE_DIR / relative
                destination = counterpart / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            self.assertTrue(derive_self_audit(counterpart / "SKILL.md"))

            different = root / "different-skill"
            shutil.copytree(counterpart, different)
            different_lock = different / "references" / "builtin-registry-lock.json"
            different_lock.unlink()
            different_lock.write_text(
                "{}\n",
                encoding="utf-8",
            )
            self.assertFalse(derive_self_audit(different / "SKILL.md"))
            external = root / "external.txt"
            external.write_text("external subject\n", encoding="utf-8")
            self.assertFalse(derive_self_audit(external))

            result, selection_path, registry_path = bound_audit_fixture(
                root,
                CANDIDATE_DIR / "SKILL.md",
            )
            result_path = root / "child-self-audit.json"
            write_json(result_path, result)
            validation = validate_audit_result(
                result_path,
                selection_path,
                registry_path,
            )
            self.assertTrue(validation["derived_self_audit"])
            self.assertEqual(validation["conclusion"], "BLOCKED")

            forged_external_pass = copy.deepcopy(result)
            forged_external_pass["self_audit"] = False
            forged_external_pass["status"] = "AUDIT_SUBMITTED_FOR_REVIEW"
            forged_external_pass["hard_gate_failures"] = []
            forged_external_pass["executable_acceptance"] = {
                "status": "VERIFIED",
                "evidence_refs": copy.deepcopy(
                    result["known_rule_results"][0]["evidence_refs"]
                ),
                "reason": "finalizer child-path regression",
            }
            forged_external_pass["conclusion"] = "PASS_WITHIN_FROZEN_SCOPE"
            forged_path = root / "forged-external-pass.json"
            write_json(forged_path, forged_external_pass)
            with self.assertRaisesRegex(
                ContractError,
                "self_audit does not match",
            ):
                validate_audit_result(
                    forged_path,
                    selection_path,
                    registry_path,
                )

    def test_self_audit_is_valid_only_as_non_accepting_external_review_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = CANDIDATE_DIR
            result_path = root / "result.json"
            result, selection_path, registry_path = bound_audit_fixture(root, subject)
            write_json(result_path, result)
            validation = validate_audit_result(
                result_path,
                selection_path,
                registry_path,
            )
            self.assertEqual(validation["status"], "VALID")
            self.assertEqual(validation["conclusion"], "BLOCKED")
            self.assertTrue(validation["derived_self_audit"])
            self.assertFalse(validation["acceptance_eligible"])

            accepting = copy.deepcopy(result)
            accepting["hard_gate_failures"] = []
            accepting["executable_acceptance"] = {
                "status": "VERIFIED",
                "evidence_refs": copy.deepcopy(
                    result["known_rule_results"][0]["evidence_refs"]
                ),
                "reason": "仅用于证明自审仍无接受权",
            }
            accepting["conclusion"] = "PASS_WITHIN_FROZEN_SCOPE"
            write_json(root / "accepting-self-audit.json", accepting)
            with self.assertRaisesRegex(ContractError, "self-audit cannot produce PASS"):
                validate_audit_result(
                    root / "accepting-self-audit.json",
                    selection_path,
                    registry_path,
                )

            wrong_status = copy.deepcopy(result)
            wrong_status["status"] = "AUDIT_SUBMITTED_FOR_REVIEW"
            write_json(root / "wrong-status.json", wrong_status)
            with self.assertRaisesRegex(ContractError, "external-review submission status"):
                validate_audit_result(
                    root / "wrong-status.json",
                    selection_path,
                    registry_path,
                )

    def test_unchecked_high_severity_and_abnormal_case_sets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = root / "subject.txt"
            subject.write_text("candidate\n", encoding="utf-8")
            unchecked, selection_path, registry_path = bound_audit_fixture(root, subject)
            high_rule = next(
                item
                for item in unchecked["known_rule_results"]
                if item["severity"] in {"critical", "high"}
            )
            high_rule["status"] = "UNCHECKED"
            high_rule["evidence_refs"] = []
            unchecked["hard_gate_failures"].append("HIGH_SEVERITY_UNCHECKED")
            unchecked["conclusion"] = "NEEDS_REVISION"
            unchecked_path = root / "unchecked.json"
            write_json(unchecked_path, unchecked)
            with self.assertRaisesRegex(ContractError, "fail-closed conclusion"):
                validate_audit_result(
                    unchecked_path,
                    selection_path,
                    registry_path,
                )

            cases_path = root / "cases.jsonl"
            results_path = root / "results.jsonl"
            cases_path.write_text(
                json.dumps(
                    {
                        "case_id": "CASE-01",
                        "required_rule_ids": ["FM-01"],
                        "forbidden_rule_ids": [],
                        "allowed_conclusions": ["BLOCKED"],
                        "required_status": "AUDIT_SUBMITTED_FOR_REVIEW",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            results_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                ContractError,
                "case result set must not be empty|case set mismatch",
            ):
                grade_cases(cases_path, results_path)

    def test_public_grading_never_becomes_formal_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = {
                "case_id": "CASE-01",
                "required_rule_ids": ["FM-01"],
                "forbidden_rule_ids": ["FM-02"],
                "allowed_conclusions": ["BLOCKED"],
                "required_status": "AUDIT_SUBMITTED_FOR_REVIEW",
            }
            result = {
                "case_id": "CASE-01",
                "detected_rule_ids": ["FM-01"],
                "conclusion": "BLOCKED",
                "status": "AUDIT_SUBMITTED_FOR_REVIEW",
            }
            cases_path = root / "cases.jsonl"
            results_path = root / "results.jsonl"
            cases_path.write_text(json.dumps(case, sort_keys=True) + "\n", encoding="utf-8")
            results_path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            grade = grade_cases(cases_path, results_path)
            self.assertTrue(grade["overall_pass"])
            self.assertEqual(grade["status"], "PUBLIC_EVALUATION_PASSED")
            self.assertFalse(grade["acceptance_eligible"])
            self.assertNotIn("CANDIDATE_ACCEPT", json.dumps(grade, sort_keys=True))

    def test_empty_case_and_result_sets_cannot_vacuously_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases_path = root / "empty-cases.jsonl"
            results_path = root / "empty-results.jsonl"
            cases_path.write_text("", encoding="utf-8")
            results_path.write_text("", encoding="utf-8")
            with self.assertRaises(ContractError):
                grade_cases(cases_path, results_path)

    def test_result_must_bind_registry_selection_complete_set_and_severities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = root / "subject.txt"
            subject.write_text("candidate\n", encoding="utf-8")
            result, selection_path, registry_path = bound_audit_fixture(root, subject)
            result_path = root / "bound-result.json"
            output_path = root / "bound-validation.json"
            write_json(result_path, result)
            valid_run = run_result_validation(
                result_path,
                selection_path,
                registry_path,
                output_path,
            )
            self.assertEqual(valid_run.returncode, 0, valid_run.stderr)
            validation_output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertFalse(validation_output["acceptance_eligible"])

            high_identifier = next(
                item["id"]
                for item in result["known_rule_results"]
                if item["severity"] in {"critical", "high"}
            )
            invalid_results = {}

            omitted = copy.deepcopy(result)
            omitted["known_rule_results"] = [
                item for item in omitted["known_rule_results"] if item["id"] != high_identifier
            ]
            invalid_results["omitted-selected-high"] = omitted

            downgraded = copy.deepcopy(result)
            next(
                item for item in downgraded["known_rule_results"] if item["id"] == high_identifier
            )["severity"] = "low"
            invalid_results["forged-severity"] = downgraded

            forged_registry = copy.deepcopy(result)
            forged_registry["registry_sha256"] = "0" * 64
            invalid_results["forged-registry-digest"] = forged_registry

            forged_selection = copy.deepcopy(result)
            forged_selection["selection_sha256"] = "0" * 64
            invalid_results["forged-selection-digest"] = forged_selection

            for name, invalid in invalid_results.items():
                invalid_path = root / f"{name}.json"
                invalid_output = root / f"{name}-validation.json"
                write_json(invalid_path, invalid)
                run = run_result_validation(
                    invalid_path,
                    selection_path,
                    registry_path,
                    invalid_output,
                )
                with self.subTest(name=name):
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertFalse(invalid_output.exists())

    def test_fabricated_evidence_coverage_or_external_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = root / "subject.txt"
            subject.write_text("candidate\n", encoding="utf-8")
            result, selection_path, registry_path = bound_audit_fixture(root, subject)

            missing_chunk = copy.deepcopy(result)
            missing_chunk["known_rule_results"][0]["evidence_refs"] = [
                {"chunk_id": "CHUNK-999999", "chunk_sha256": "0" * 64}
            ]
            missing_chunk_path = root / "missing-chunk.json"
            write_json(missing_chunk_path, missing_chunk)
            with self.assertRaisesRegex(ContractError, "verified chunk"):
                validate_audit_result(
                    missing_chunk_path,
                    selection_path,
                    registry_path,
                )

            forged_coverage = copy.deepcopy(result)
            forged_coverage["coverage_status"] = "INVALID"
            forged_coverage_path = root / "forged-coverage.json"
            write_json(forged_coverage_path, forged_coverage)
            with self.assertRaisesRegex(ContractError, "recomputed coverage"):
                validate_audit_result(
                    forged_coverage_path,
                    selection_path,
                    registry_path,
                )

            external_identity = copy.deepcopy(result)
            external_identity["self_audit"] = False
            external_identity["status"] = "EXTERNALLY_REVIEWED"
            external_identity_path = root / "self-declared-external.json"
            write_json(external_identity_path, external_identity)
            with self.assertRaises(ContractError):
                validate_audit_result(
                    external_identity_path,
                    selection_path,
                    registry_path,
                )

            subject.write_text("drifted candidate\n", encoding="utf-8")
            drifted_path = root / "drifted-subject.json"
            write_json(drifted_path, result)
            with self.assertRaisesRegex(ContractError, "drifted"):
                validate_audit_result(
                    drifted_path,
                    selection_path,
                    registry_path,
                )

    def test_legacy_three_one_vote_redlines_are_derived_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = root / "subject.txt"
            subject.write_text("candidate\n", encoding="utf-8")
            base, selection_path, registry_path = bound_audit_fixture(root, subject)
            base["executable_acceptance"] = {
                "status": "VERIFIED",
                "evidence_refs": copy.deepcopy(
                    base["known_rule_results"][0]["evidence_refs"]
                ),
                "reason": "用于隔离测试旧版红线",
            }
            base["hard_gate_failures"] = ["NO_EXTERNAL_FINALIZER"]

            redline_cases = {
                "fm01-fm05": (
                    ["FM-01", "FM-05"],
                    "REDLINE_FM01_FM05",
                ),
                "fm06-without-role-separation": (
                    ["FM-05", "FM-06"],
                    "REDLINE_FM06_WITHOUT_ROLE_SEPARATION",
                ),
            }
            for name, (hit_ids, required_gate) in redline_cases.items():
                result = copy.deepcopy(base)
                result["audit_id"] = f"AUDIT-{name.upper().replace('-', '_')}"
                for item in result["known_rule_results"]:
                    if item["id"] in hit_ids:
                        item["status"] = "HIT"
                result["conclusion"] = "BLOCKED"
                omitted_path = root / f"{name}-omitted-gate.json"
                write_json(omitted_path, result)
                with self.subTest(name=f"{name}-derived"), self.assertRaisesRegex(
                    ContractError,
                    required_gate,
                ):
                    validate_audit_result(
                        omitted_path,
                        selection_path,
                        registry_path,
                    )

                result["hard_gate_failures"].append(required_gate)
                result["conclusion"] = "NEEDS_REVISION"
                weak_conclusion_path = root / f"{name}-weak-conclusion.json"
                write_json(weak_conclusion_path, result)
                with self.subTest(name=f"{name}-conclusion"), self.assertRaises(
                    ContractError
                ):
                    validate_audit_result(
                        weak_conclusion_path,
                        selection_path,
                        registry_path,
                    )

            no_executable = copy.deepcopy(base)
            no_executable["audit_id"] = "AUDIT-NO_EXECUTABLE"
            no_executable["executable_acceptance"] = {
                "status": "ABSENT",
                "evidence_refs": copy.deepcopy(
                    base["known_rule_results"][0]["evidence_refs"]
                ),
                "reason": "目标没有可执行验收工件",
            }
            no_executable["conclusion"] = "BLOCKED"
            missing_gate_path = root / "no-executable-omitted-gate.json"
            write_json(missing_gate_path, no_executable)
            with self.assertRaisesRegex(
                ContractError,
                "NO_EXECUTABLE_ACCEPTANCE_ARTIFACT",
            ):
                validate_audit_result(
                    missing_gate_path,
                    selection_path,
                    registry_path,
                )

            no_executable["hard_gate_failures"].append(
                "NO_EXECUTABLE_ACCEPTANCE_ARTIFACT"
            )
            no_executable["conclusion"] = "NEEDS_REVISION"
            weak_no_executable_path = root / "no-executable-weak-conclusion.json"
            write_json(weak_no_executable_path, no_executable)
            with self.assertRaisesRegex(ContractError, "legacy one-vote redline"):
                validate_audit_result(
                    weak_no_executable_path,
                    selection_path,
                    registry_path,
                )


class ExactReuseBoundaryTests(unittest.TestCase):
    def _check(self, fixture: dict[str, object], **overrides: object) -> dict:
        values = {
            "subject_path": fixture["subject"],
            "mode": "combined",
            "evidence_types": {"text"},
            "criteria_path": fixture["criteria"],
            "prior_result_path": fixture["result"],
            "prior_report_path": fixture["report"],
            "prior_selection_path": fixture["selection"],
            "prior_attempt": fixture["attempt"],
            "prior_reuse_receipt_path": fixture["receipt"],
            "expected_prior_seal_file_sha256": fixture["seal_file_sha256"],
        }
        values.update(overrides)
        return reuse_check(**values)

    def test_identical_inputs_reuse_without_creating_a_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = eligible_reuse_fixture(root)
            before = sorted((root / "attempts").iterdir())
            decision = self._check(fixture)
            after = sorted((root / "attempts").iterdir())
            self.assertEqual(decision["status"], "REUSE_IDENTICAL")
            self.assertEqual(decision["reason_codes"], ["ALL_IDENTITY_CHECKS_MATCH"])
            self.assertEqual(decision["reused_conclusion"], "PASS_WITHIN_FROZEN_SCOPE")
            self.assertFalse(decision["acceptance_eligible"])
            self.assertEqual(before, after)

    def test_subject_drift_between_identity_reads_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary))
            first = build_index(Path(fixture["subject"]), 1024 * 1024)
            second = copy.deepcopy(first)
            second["file_set_sha256"] = "0" * 64
            with patch.object(
                evaluation_tool,
                "build_index",
                side_effect=(first, second),
            ), self.assertRaisesRegex(ContractError, "INPUT_DRIFT: audit subject"):
                self._check(fixture)

    def test_criteria_drift_between_identity_reads_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary))
            original = evaluation_tool.foundation_file_sha256
            criteria = Path(fixture["criteria"])
            reads = 0

            def drift_on_second_criteria_read(path: Path) -> str:
                nonlocal reads
                digest = original(path)
                if Path(path) == criteria:
                    reads += 1
                    if reads == 2:
                        return "0" * 64
                return digest

            with patch.object(
                evaluation_tool,
                "foundation_file_sha256",
                side_effect=drift_on_second_criteria_read,
            ), self.assertRaisesRegex(ContractError, "INPUT_DRIFT: criteria commitment"):
                self._check(fixture)

    def test_auditor_drift_between_identity_reads_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary))
            first = auditor_core_identity()
            second = copy.deepcopy(first)
            second["auditor_core_sha256"] = "0" * 64
            with patch.object(
                evaluation_tool,
                "auditor_core_identity",
                side_effect=(first, second),
            ), self.assertRaisesRegex(ContractError, "INPUT_DRIFT: auditor closure"):
                self._check(fixture)

    def test_every_legal_identity_change_requires_a_full_audit(self) -> None:
        cases = (
            ("mode", {"mode": "runtime"}, {"MODE_CHANGED", "SELECTION_CHANGED"}),
            (
                "evidence-type",
                {"evidence_types": {"runtime-log"}},
                {"EVIDENCE_TYPE_CHANGED", "SELECTION_CHANGED"},
            ),
            (
                "fresh-evidence",
                {"fresh_evidence_required": True},
                {"FRESH_EVIDENCE_REQUIRED"},
            ),
        )
        for name, overrides, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = eligible_reuse_fixture(Path(temporary))
                decision = self._check(fixture, **overrides)
                self.assertEqual(decision["status"], "FULL_AUDIT_REQUIRED")
                self.assertTrue(expected.issubset(set(decision["reason_codes"])))
                self.assertIsNone(decision["reused_conclusion"])

        with self.subTest(name="criteria"), tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary))
            Path(fixture["criteria"]).write_text("changed criteria\n", encoding="utf-8")
            decision = self._check(fixture)
            self.assertIn("CRITERIA_CHANGED", decision["reason_codes"])

        with self.subTest(name="subject-content"), tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary))
            Path(fixture["subject"]).write_text("changed subject\n", encoding="utf-8")
            decision = self._check(fixture)
            self.assertIn("SUBJECT_CONTENT_CHANGED", decision["reason_codes"])

        with self.subTest(name="subject-path"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = eligible_reuse_fixture(root)
            copy_path = root / "subject-copy.txt"
            shutil.copy2(Path(fixture["subject"]), copy_path)
            decision = self._check(fixture, subject_path=copy_path)
            self.assertIn("SUBJECT_IDENTITY_CHANGED", decision["reason_codes"])
            self.assertIn("SUBJECT_CONTENT_CHANGED", decision["reason_codes"])

    def test_each_auditor_closure_area_changes_the_identity_and_blocks_reuse(self) -> None:
        mutations = (
            ("skill", Path("SKILL.md"), "\n<!-- identity mutation -->\n"),
            (
                "reference",
                Path("references/report-contract.md"),
                "\n<!-- identity mutation -->\n",
            ),
            ("script", Path("scripts/common.py"), "\n# identity mutation\n"),
            ("foundation", Path("foundation/foundation-pin.json"), " \n"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = eligible_reuse_fixture(root)
            original_identity = auditor_core_identity()["auditor_core_sha256"]
            for name, relative, suffix in mutations:
                with self.subTest(name=name):
                    mutated_root = root / f"auditor-{name}"
                    shutil.copytree(CANDIDATE_DIR, mutated_root)
                    target = mutated_root / relative
                    target.chmod(0o600)
                    target.write_text(
                        target.read_text(encoding="utf-8") + suffix,
                        encoding="utf-8",
                    )
                    mutated_identity = auditor_core_identity(mutated_root)[
                        "auditor_core_sha256"
                    ]
                    self.assertNotEqual(original_identity, mutated_identity)
                    decision = self._check(fixture, skill_root=mutated_root)
                    self.assertEqual(decision["status"], "FULL_AUDIT_REQUIRED")
                    self.assertIn("AUDITOR_CHANGED", decision["reason_codes"])

    def test_unbound_or_incompletely_recorded_attempt_cannot_reuse(self) -> None:
        with self.subTest(name="unbound"), tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary))
            no_receipt = self._check(
                fixture,
                prior_reuse_receipt_path=None,
            )
            self.assertEqual(no_receipt["status"], "FULL_AUDIT_REQUIRED")
            self.assertIn("NO_PRIOR_REUSE_RECEIPT", no_receipt["reason_codes"])
            decision = self._check(
                fixture,
                expected_prior_seal_file_sha256=None,
            )
            self.assertEqual(decision["status"], "FULL_AUDIT_REQUIRED")
            self.assertIn("PRIOR_ATTEMPT_NOT_BOUND", decision["reason_codes"])

        with self.subTest(name="missing-record"), tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary), record_receipt=False)
            decision = self._check(fixture)
            self.assertEqual(decision["status"], "FULL_AUDIT_REQUIRED")
            self.assertIn("PRIOR_ATTEMPT_RECORD_MISMATCH", decision["reason_codes"])

        with self.subTest(name="duplicate-record"), tempfile.TemporaryDirectory() as temporary:
            fixture = eligible_reuse_fixture(Path(temporary), duplicate_receipt=True)
            with self.assertRaisesRegex(ContractError, "duplicate required records"):
                self._check(fixture)

    def test_valid_registry_addition_changes_registry_selection_and_auditor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = eligible_reuse_fixture(root)
            mutated_root = root / "auditor-registry"
            shutil.copytree(CANDIDATE_DIR, mutated_root)
            registry_path = mutated_root / "references/failure-modes.jsonl"
            registry_path.chmod(0o600)
            synthetic = json.loads(
                registry_path.read_text(encoding="utf-8").splitlines()[0]
            )
            synthetic.update(
                {
                    "id": "SYN-9999",
                    "name_zh": "合成登记表变化",
                    "legacy_definition": "用于证明合法登记表变化会阻止完全同一复用。",
                    "severity": "low",
                    "priority": 1,
                    "modes": ["static"],
                    "applies_when": {
                        "target_types": ["prompt"],
                        "evidence_types": ["text"],
                        "conditions": ["只用于测试登记表身份变化"],
                    },
                    "conflicts_with": [],
                    "depends_on": [],
                    "mutation_operators": [
                        {
                            "id": "MUT-SYN-9999-01",
                            "description": "改变登记表但不替换内置规则",
                            "expected_detection": "must_detect",
                        }
                    ],
                    "core_redline": False,
                }
            )
            with registry_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        synthetic,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            decision = self._check(fixture, skill_root=mutated_root)
            self.assertEqual(decision["status"], "FULL_AUDIT_REQUIRED")
            self.assertTrue(
                {"REGISTRY_CHANGED", "SELECTION_CHANGED", "AUDITOR_CHANGED"}.issubset(
                    set(decision["reason_codes"])
                )
            )

    def test_tampered_prior_artifacts_exit_with_contract_error(self) -> None:
        mutations = ("result", "report", "selection", "receipt", "attempt")
        for name in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = eligible_reuse_fixture(Path(temporary))
                if name == "attempt":
                    path = Path(fixture["attempt"]) / "seal.json"
                else:
                    path = Path(fixture[name])
                path.chmod(0o600)
                if path.suffix == ".json":
                    value = json.loads(path.read_text(encoding="utf-8"))
                    first_key = next(iter(value))
                    value[first_key] = "tampered"
                    write_json(path, value)
                else:
                    path.write_text("tampered report\n", encoding="utf-8")
                with self.assertRaises(ContractError):
                    self._check(fixture)

    def test_incomplete_blocked_and_high_unchecked_results_cannot_get_receipts(self) -> None:
        for conclusion, unchecked_high in (
            ("BLOCKED", False),
            ("INCOMPLETE", True),
        ):
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                subject = root / "subject.txt"
                subject.write_text("candidate\n", encoding="utf-8")
                result, selection_path, registry_path = bound_audit_fixture(root, subject)
                if unchecked_high:
                    high = next(
                        item
                        for item in result["known_rule_results"]
                        if item["severity"] in {"critical", "high"}
                    )
                    high["status"] = "UNCHECKED"
                    high["evidence_refs"] = []
                    result["hard_gate_failures"].append("HIGH_SEVERITY_UNCHECKED")
                result["conclusion"] = conclusion
                result_path = root / "result.json"
                write_json(result_path, result)
                report_path = root / "report.md"
                report_path.write_text(
                    render(result, load_registry_names(registry_path)),
                    encoding="utf-8",
                )
                criteria = root / "criteria.json"
                write_json(criteria, {"goal": "frozen"})
                created = create_attempt(
                    argparse.Namespace(
                        root=root / "attempts",
                        attempt_id="ATTEMPT-INELIGIBLE",
                        candidate_sha256=result["subject"]["file_set_sha256"],
                        criteria_commitment_sha256=sha256_file(criteria),
                        write_path=["evidence/attempts/ATTEMPT-INELIGIBLE"],
                        created_by="reuse-test",
                        created_at="2026-08-28T00:00:00Z",
                    )
                )
                attempt = Path(created["attempt"])
                for kind, artifact in (
                    ("audit_result", result_path),
                    ("audit_report", report_path),
                ):
                    record_artifact(
                        argparse.Namespace(
                            attempt=attempt,
                            kind=kind,
                            artifact=artifact,
                            recorded_at="2026-08-28T00:01:00Z",
                        )
                    )
                with self.assertRaisesRegex(
                    ContractError,
                    "not reusable|unchecked high-severity",
                ):
                    build_reuse_receipt(
                        result_path,
                        report_path,
                        selection_path,
                        attempt,
                    )


if __name__ == "__main__":
    unittest.main()

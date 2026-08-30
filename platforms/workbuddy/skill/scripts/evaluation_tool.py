#!/usr/bin/env python3
"""Validate audit results, grade frozen cases, and manage continuation packages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import (
    ContractError,
    list_regular_files,
    load_json,
    load_jsonl,
    require_sha256,
    write_json_exclusive,
)
from attempt_tool import list_records, verify_attempt, verify_manifest
from evidence_tool import (
    DEFAULT_CHUNK_SIZE,
    _build_coverage_from_verified_inputs,
    build_index,
    validate_index_document,
    verify_index,
)
from foundation_client import (
    foundation_digest_document,
    foundation_file_sha256,
    foundation_resource_closure,
    require_production_validate,
)
from registry_tool import (
    DEFAULT_REGISTRY,
    DEFAULT_SCHEMA,
    build_selection,
    validate_registry,
    validate_selection_artifact,
)
from report_renderer import load_registry_names, render


SCRIPT_DIR = Path(__file__).resolve().parent
AUDIT_SCHEMA = SCRIPT_DIR.parent / "references" / "audit-result.schema.json"
CONTINUATION_SCHEMA = SCRIPT_DIR.parent / "references" / "continuation-package.schema.json"
SOURCE_MANIFEST_SCHEMA = SCRIPT_DIR.parent / "references" / "source-manifest.schema.json"
FAIL_CLOSED_CONCLUSIONS = {"INCOMPLETE", "BLOCKED"}
REUSABLE_CONCLUSIONS = {"PASS_WITHIN_FROZEN_SCOPE", "NEEDS_REVISION", "REJECT"}
REUSE_REASON_ORDER = (
    "NO_PRIOR_REUSE_RECEIPT",
    "FRESH_EVIDENCE_REQUIRED",
    "PRIOR_RESULT_NOT_REUSABLE",
    "PRIOR_ATTEMPT_NOT_BOUND",
    "PRIOR_ATTEMPT_RECORD_MISMATCH",
    "EVIDENCE_TYPE_CHANGED",
    "MODE_CHANGED",
    "SUBJECT_IDENTITY_CHANGED",
    "SUBJECT_CONTENT_CHANGED",
    "CRITERIA_CHANGED",
    "REGISTRY_CHANGED",
    "SELECTION_CHANGED",
    "AUDITOR_CHANGED",
)
SKILL_IDENTITY_ANCHORS = (
    "SKILL.md",
    "scripts/evaluation_tool.py",
    "references/builtin-registry-lock.json",
)


def stable_skill_identity(root: Path) -> tuple[tuple[str, str], ...] | None:
    """Return stable identity anchors for a real skill root, otherwise None."""
    if root.is_symlink() or not root.is_dir():
        return None
    identity: list[tuple[str, str]] = []
    for relative in SKILL_IDENTITY_ANCHORS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            return None
        identity.append((relative, foundation_file_sha256(path)))
    return tuple(identity)


def derive_self_audit(subject_path: Path) -> bool:
    """Fail closed for runtime descendants and byte-identical skill copies."""
    subject = subject_path.resolve(strict=True)
    runtime_root = SCRIPT_DIR.parent.resolve(strict=True)
    if subject == runtime_root or subject.is_relative_to(runtime_root):
        return True

    runtime_identity = stable_skill_identity(runtime_root)
    if runtime_identity is None:
        raise ContractError("runtime skill identity anchors are missing or invalid")
    start = subject if subject.is_dir() else subject.parent
    for candidate_root in (start, *start.parents):
        if stable_skill_identity(candidate_root) == runtime_identity:
            return True
    return False


def validate_selection_document(path: Path) -> dict[str, Any]:
    """Validate a frozen selection without consulting a possibly changed registry."""
    selection = load_json(path)
    expected_keys = {
        "schema_version",
        "status",
        "registry_sha256",
        "schema_sha256",
        "registry_entry_count",
        "selected_count",
        "unselected_count",
        "truncated_count",
        "selection_context",
        "selection_context_sha256",
        "coverage_ledger_sha256",
        "selection_sha256",
    }
    if not isinstance(selection, dict) or set(selection) != expected_keys:
        raise ContractError("selection artifact has an unexpected field set")
    unsigned = dict(selection)
    unsigned.pop("selection_sha256")
    if selection["selection_sha256"] != foundation_digest_document(unsigned):
        raise ContractError("selection artifact self digest mismatch")
    for field in (
        "registry_sha256",
        "schema_sha256",
        "selection_context_sha256",
        "coverage_ledger_sha256",
        "selection_sha256",
    ):
        require_sha256(selection[field], f"selection {field}")
    if selection["schema_version"] != "1.0" or selection["status"] not in {
        "SELECTED",
        "INCOMPLETE_LOW_CONFIDENCE",
    }:
        raise ContractError("selection artifact header is invalid")
    for field in (
        "registry_entry_count",
        "selected_count",
        "unselected_count",
        "truncated_count",
    ):
        value = selection[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ContractError(f"selection {field} is invalid")
    if (
        selection["selected_count"] + selection["unselected_count"]
        != selection["registry_entry_count"]
    ):
        raise ContractError("selection counts do not conserve the registry")
    context = selection["selection_context"]
    if not isinstance(context, dict) or set(context) != {
        "mode",
        "target_type",
        "evidence_types",
        "selected_rules",
    }:
        raise ContractError("selection context has an unexpected field set")
    if context["mode"] not in {"static", "runtime", "combined"}:
        raise ContractError("selection context mode is invalid")
    if not isinstance(context["target_type"], str) or not context["target_type"]:
        raise ContractError("selection context target type is invalid")
    evidence_types = context["evidence_types"]
    if (
        not isinstance(evidence_types, list)
        or not all(isinstance(item, str) and item for item in evidence_types)
        or evidence_types != sorted(set(evidence_types))
    ):
        raise ContractError("selection context evidence types are not canonical")
    rules = context["selected_rules"]
    if not isinstance(rules, list) or not rules:
        raise ContractError("selection context selected rules are empty")
    expected_rule_keys = {
        "id",
        "revision",
        "severity",
        "priority",
        "core_redline",
        "selection_reason",
        "source_sha256",
        "guidance_ref",
    }
    identifiers: list[str] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) != expected_rule_keys:
            raise ContractError(f"selection rule {index} has an unexpected field set")
        if not isinstance(rule["id"], str) or not rule["id"]:
            raise ContractError(f"selection rule {index} has an invalid id")
        require_sha256(rule["source_sha256"], f"selection rule {rule['id']} source")
        identifiers.append(rule["id"])
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("selection context contains duplicate rule ids")
    if selection["selected_count"] != len(rules):
        raise ContractError("selection selected count mismatch")
    if selection["selection_context_sha256"] != foundation_digest_document(context):
        raise ContractError("selection context digest mismatch")
    return selection


def canonical_subject_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ContractError(f"audit subject path must be absolute: {path}")
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise ContractError(f"audit subject must be a real file or directory: {path}")
    return path.resolve(strict=True)


def auditor_core_identity(skill_root: Path = SCRIPT_DIR.parent) -> dict[str, Any]:
    """Digest the runtime instruction, reference, script, and Foundation closure."""
    root = skill_root.resolve(strict=True)
    if skill_root.is_symlink() or not root.is_dir():
        raise ContractError("auditor skill root must be a real directory")
    members: list[Path] = []
    skill_entry = root / "SKILL.md"
    if skill_entry.is_symlink() or not skill_entry.is_file():
        raise ContractError("auditor SKILL.md is missing or invalid")
    members.append(skill_entry)
    for directory_name in ("references", "foundation"):
        directory = root / directory_name
        for path in list_regular_files(directory):
            relative = path.relative_to(root)
            if (
                "__pycache__" in relative.parts
                or "tests" in relative.parts
                or path.suffix == ".pyc"
                or path.name.startswith("test_")
            ):
                continue
            members.append(path)
    scripts = root / "scripts"
    if scripts.is_symlink() or not scripts.is_dir():
        raise ContractError("auditor scripts directory is missing or invalid")
    for path in scripts.iterdir():
        if path.is_symlink():
            raise ContractError(f"symlink is not allowed in auditor closure: {path}")
        if path.is_file() and path.suffix == ".py" and not path.name.startswith("test_"):
            members.append(path)
    unique = {path.relative_to(root).as_posix(): path for path in members}
    if len(unique) != len(members):
        raise ContractError("auditor closure contains duplicate paths")
    relative_paths = sorted(unique, key=lambda item: item.encode("utf-8"))
    closure = foundation_resource_closure(
        str(root),
        [{"path": relative, "role": "input"} for relative in relative_paths],
    )
    closure_records = {
        record["path"]: record
        for record in closure.get("resources", [])
        if isinstance(record, dict)
    }
    if set(closure_records) != set(relative_paths):
        raise ContractError("auditor closure did not return the exact requested member set")
    records = []
    for relative in relative_paths:
        path = unique[relative]
        closure_record = closure_records[relative]
        if closure_record.get("exists") is not True:
            raise ContractError(f"auditor closure member is missing: {relative}")
        require_sha256(closure_record.get("sha256"), f"auditor member {relative}")
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": closure_record["sha256"],
            }
        )
    return {
        "members": records,
        "auditor_core_sha256": foundation_digest_document(records),
    }


def reuse_receipt_digest(receipt: dict[str, Any]) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest", None)
    return foundation_digest_document(unsigned)


def validate_reuse_receipt(path: Path) -> dict[str, Any]:
    receipt = load_json(path)
    expected_keys = {
        "schema_version",
        "status",
        "audit_id",
        "mode",
        "subject",
        "criteria_commitment_sha256",
        "registry_sha256",
        "selection_sha256",
        "auditor_core_sha256",
        "audit_result_sha256",
        "audit_report_sha256",
        "prior_conclusion",
        "prior_result_status",
        "self_audit",
        "attempt_id",
        "attempt_manifest_sha256",
        "receipt_digest",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise ContractError("reuse receipt has an unexpected field set")
    if receipt["schema_version"] != "1.0" or receipt["status"] != "REUSE_ELIGIBLE":
        raise ContractError("reuse receipt header is invalid")
    if receipt["mode"] not in {"static", "runtime", "combined"}:
        raise ContractError("reuse receipt mode is invalid")
    if receipt["prior_conclusion"] not in REUSABLE_CONCLUSIONS:
        raise ContractError("reuse receipt conclusion is not reusable")
    if receipt["prior_result_status"] not in {
        "AUDIT_SUBMITTED_FOR_REVIEW",
        "SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW",
        "EXTERNALLY_REVIEWED",
    }:
        raise ContractError("reuse receipt result status is invalid")
    if not isinstance(receipt["self_audit"], bool):
        raise ContractError("reuse receipt self_audit must be boolean")
    for field in ("audit_id", "attempt_id"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise ContractError(f"reuse receipt {field} is invalid")
    subject = receipt["subject"]
    if not isinstance(subject, dict) or set(subject) != {
        "canonical_path",
        "file_set_sha256",
    }:
        raise ContractError("reuse receipt subject is invalid")
    canonical_path = subject["canonical_path"]
    if not isinstance(canonical_path, str) or not canonical_path:
        raise ContractError("reuse receipt subject canonical_path is invalid")
    canonical = Path(canonical_path)
    if not canonical.is_absolute() or str(canonical) != subject["canonical_path"]:
        raise ContractError("reuse receipt subject path is not canonical absolute syntax")
    digest_fields = (
        "criteria_commitment_sha256",
        "registry_sha256",
        "selection_sha256",
        "auditor_core_sha256",
        "audit_result_sha256",
        "audit_report_sha256",
        "attempt_manifest_sha256",
        "receipt_digest",
    )
    for field in digest_fields:
        require_sha256(receipt[field], f"reuse receipt {field}")
    require_sha256(subject["file_set_sha256"], "reuse receipt subject digest")
    if receipt["receipt_digest"] != reuse_receipt_digest(receipt):
        raise ContractError("reuse receipt self digest mismatch")
    return receipt


def _require_record_binding(
    records: list[dict[str, Any]],
    kind: str,
    expected_sha256: str,
    *,
    required: bool,
) -> bool:
    matches = [record for record in records if record["kind"] == kind]
    if len(matches) > 1:
        raise ContractError(f"attempt contains duplicate required records: {kind}")
    if not matches:
        if required:
            raise ContractError(f"attempt is missing required record: {kind}")
        return False
    if matches[0]["artifact_sha256"] != expected_sha256:
        raise ContractError(f"attempt {kind} record digest mismatch")
    return True


def build_reuse_receipt(
    result_path: Path,
    report_path: Path,
    selection_path: Path,
    attempt: Path,
    *,
    skill_root: Path = SCRIPT_DIR.parent,
) -> dict[str, Any]:
    registry_path = skill_root / "references" / "failure-modes.jsonl"
    validation = validate_audit_result(result_path, selection_path, registry_path)
    result = load_json(result_path)
    selection, _ = validate_selection_artifact(selection_path, registry_path)
    if selection["status"] != "SELECTED":
        raise ContractError("only a SELECTED rule set can produce a reuse receipt")
    if result["coverage_status"] != "COMPLETE":
        raise ContractError("only COMPLETE coverage can produce a reuse receipt")
    if validation["unchecked_high_severity"]:
        raise ContractError("unchecked high-severity rules cannot produce a reuse receipt")
    if result["conclusion"] not in REUSABLE_CONCLUSIONS:
        raise ContractError("audit conclusion is not reusable")
    attempt_verification = verify_attempt(attempt)
    if attempt_verification["status"] != "VERIFIED_OPEN":
        raise ContractError("reuse receipt must be created before the attempt is sealed")
    manifest = verify_manifest(attempt)
    if manifest["candidate_sha256"] != result["subject"]["file_set_sha256"]:
        raise ContractError("attempt candidate digest does not bind the audit subject")
    records = list_records(attempt)
    result_sha256 = foundation_file_sha256(result_path)
    report_sha256 = foundation_file_sha256(report_path)
    _require_record_binding(records, "audit_result", result_sha256, required=True)
    _require_record_binding(records, "audit_report", report_sha256, required=True)
    if report_path.is_symlink() or not report_path.is_file():
        raise ContractError("audit report must be a regular file")
    expected_report = render(result, load_registry_names(registry_path)).encode("utf-8")
    if report_path.read_bytes() != expected_report:
        raise ContractError("audit report does not match deterministic rendering")
    subject = canonical_subject_path(Path(result["subject"]["path"]))
    receipt: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "REUSE_ELIGIBLE",
        "audit_id": result["audit_id"],
        "mode": result["mode"],
        "subject": {
            "canonical_path": str(subject),
            "file_set_sha256": result["subject"]["file_set_sha256"],
        },
        "criteria_commitment_sha256": manifest["criteria_commitment_sha256"],
        "registry_sha256": result["registry_sha256"],
        "selection_sha256": result["selection_sha256"],
        "auditor_core_sha256": auditor_core_identity(skill_root)[
            "auditor_core_sha256"
        ],
        "audit_result_sha256": result_sha256,
        "audit_report_sha256": report_sha256,
        "prior_conclusion": result["conclusion"],
        "prior_result_status": result["status"],
        "self_audit": result["self_audit"],
        "attempt_id": manifest["attempt_id"],
        "attempt_manifest_sha256": manifest["manifest_sha256"],
    }
    receipt["receipt_digest"] = reuse_receipt_digest(receipt)
    return receipt


def reuse_decision_digest(decision: dict[str, Any]) -> str:
    unsigned = dict(decision)
    unsigned.pop("decision_digest", None)
    return foundation_digest_document(unsigned)


def _ordered_reasons(reasons: set[str]) -> list[str]:
    unknown = reasons - set(REUSE_REASON_ORDER)
    if unknown:
        raise ContractError(f"unknown reuse reason codes: {sorted(unknown)}")
    return [reason for reason in REUSE_REASON_ORDER if reason in reasons]


def _build_reuse_decision(
    reasons: set[str],
    prior_audit_id: str,
    prior_result_sha256: str,
    prior_report_sha256: str,
    conclusion: str | None,
) -> dict[str, Any]:
    identical = not reasons
    decision: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "REUSE_IDENTICAL" if identical else "FULL_AUDIT_REQUIRED",
        "reason_codes": (
            ["ALL_IDENTITY_CHECKS_MATCH"] if identical else _ordered_reasons(reasons)
        ),
        "prior_audit_id": prior_audit_id,
        "prior_result_sha256": prior_result_sha256,
        "prior_report_sha256": prior_report_sha256,
        "reused_conclusion": conclusion if identical else None,
        "acceptance_eligible": False,
    }
    decision["decision_digest"] = reuse_decision_digest(decision)
    return decision


def _assert_reuse_inputs_stable(
    subject: Path,
    first_index: dict[str, Any],
    criteria_path: Path,
    first_criteria_sha256: str,
    skill_root: Path,
    first_auditor_sha256: str,
) -> None:
    second_index = build_index(subject, DEFAULT_CHUNK_SIZE)
    second_criteria = foundation_file_sha256(criteria_path)
    second_auditor = auditor_core_identity(skill_root)["auditor_core_sha256"]
    if second_index["file_set_sha256"] != first_index["file_set_sha256"]:
        raise ContractError("INPUT_DRIFT: audit subject changed during reuse check")
    if second_criteria != first_criteria_sha256:
        raise ContractError("INPUT_DRIFT: criteria commitment changed during reuse check")
    if second_auditor != first_auditor_sha256:
        raise ContractError("INPUT_DRIFT: auditor closure changed during reuse check")


def reuse_check(
    subject_path: Path,
    mode: str,
    evidence_types: set[str],
    criteria_path: Path,
    prior_result_path: Path,
    prior_report_path: Path,
    prior_selection_path: Path,
    prior_attempt: Path,
    prior_reuse_receipt_path: Path | None,
    expected_prior_seal_file_sha256: str | None,
    *,
    fresh_evidence_required: bool = False,
    skill_root: Path = SCRIPT_DIR.parent,
) -> dict[str, Any]:
    if expected_prior_seal_file_sha256 is not None:
        require_sha256(
            expected_prior_seal_file_sha256,
            "expected prior seal file",
        )
    subject = canonical_subject_path(subject_path)
    if criteria_path.is_symlink() or not criteria_path.is_file():
        raise ContractError("criteria commitment must be a regular file")
    current_index = build_index(subject, DEFAULT_CHUNK_SIZE)
    criteria_sha256 = foundation_file_sha256(criteria_path)
    auditor_sha256 = auditor_core_identity(skill_root)["auditor_core_sha256"]
    registry_path = skill_root / "references" / "failure-modes.jsonl"
    schema_path = skill_root / "references" / "failure-mode.schema.json"
    current_registry = validate_registry(registry_path, schema_path)
    current_selection = build_selection(
        current_registry,
        mode,
        "skill",
        evidence_types,
        28,
    )["selection"]
    prior_result_sha256 = foundation_file_sha256(prior_result_path)
    prior_report_sha256 = foundation_file_sha256(prior_report_path)
    prior_result = load_json(prior_result_path)
    require_production_validate(prior_result, load_json(AUDIT_SCHEMA))

    reasons: set[str] = set()
    if fresh_evidence_required:
        reasons.add("FRESH_EVIDENCE_REQUIRED")
    if prior_reuse_receipt_path is None:
        reasons.add("NO_PRIOR_REUSE_RECEIPT")
        _assert_reuse_inputs_stable(
            subject,
            current_index,
            criteria_path,
            criteria_sha256,
            skill_root,
            auditor_sha256,
        )
        return _build_reuse_decision(
            reasons,
            prior_result["audit_id"],
            prior_result_sha256,
            prior_report_sha256,
            None,
        )

    receipt = validate_reuse_receipt(prior_reuse_receipt_path)
    if prior_result_sha256 != receipt["audit_result_sha256"]:
        raise ContractError("prior audit result digest does not match reuse receipt")
    if prior_report_sha256 != receipt["audit_report_sha256"]:
        raise ContractError("prior audit report digest does not match reuse receipt")
    prior_selection = validate_selection_document(prior_selection_path)
    if prior_selection["selection_sha256"] != receipt["selection_sha256"]:
        raise ContractError("prior selection digest does not match reuse receipt")
    if prior_selection["registry_sha256"] != receipt["registry_sha256"]:
        raise ContractError("prior selection registry digest does not match reuse receipt")

    prior_subject_path = canonical_subject_path(Path(prior_result["subject"]["path"]))
    if receipt["subject"]["canonical_path"] != str(prior_subject_path):
        raise ContractError("prior result subject identity does not match reuse receipt")
    if (
        prior_result["subject"]["file_set_sha256"]
        != receipt["subject"]["file_set_sha256"]
    ):
        raise ContractError("prior result subject digest does not match reuse receipt")
    for result_field, receipt_field in (
        ("audit_id", "audit_id"),
        ("mode", "mode"),
        ("registry_sha256", "registry_sha256"),
        ("selection_sha256", "selection_sha256"),
        ("conclusion", "prior_conclusion"),
        ("status", "prior_result_status"),
        ("self_audit", "self_audit"),
    ):
        if prior_result[result_field] != receipt[receipt_field]:
            raise ContractError(
                f"prior result {result_field} does not match reuse receipt"
            )

    registry_changed = (
        current_registry["registry_sha256"] != receipt["registry_sha256"]
    )
    subject_content_changed = (
        str(subject) == receipt["subject"]["canonical_path"]
        and current_index["file_set_sha256"]
        != receipt["subject"]["file_set_sha256"]
    )
    validation = validate_audit_result(
        prior_result_path,
        prior_selection_path,
        registry_path,
        allow_subject_drift=subject_content_changed,
        allow_registry_drift=registry_changed,
    )
    if (
        validation["conclusion"] not in REUSABLE_CONCLUSIONS
        or validation["unchecked_high_severity"]
        or prior_selection["status"] != "SELECTED"
    ):
        reasons.add("PRIOR_RESULT_NOT_REUSABLE")

    current_report_bytes = render(
        prior_result,
        load_registry_names(registry_path),
    ).encode("utf-8")
    if (
        auditor_sha256 == receipt["auditor_core_sha256"]
        and prior_report_path.read_bytes() != current_report_bytes
    ):
        raise ContractError("prior report does not match deterministic rendering")

    attempt_verification = verify_attempt(
        prior_attempt,
        expected_prior_seal_file_sha256,
    )
    manifest = verify_manifest(prior_attempt)
    if manifest["attempt_id"] != receipt["attempt_id"]:
        raise ContractError("prior attempt id does not match reuse receipt")
    if manifest["manifest_sha256"] != receipt["attempt_manifest_sha256"]:
        raise ContractError("prior attempt manifest does not match reuse receipt")
    if manifest["candidate_sha256"] != receipt["subject"]["file_set_sha256"]:
        raise ContractError("prior attempt candidate does not match reuse receipt")
    if (
        manifest["criteria_commitment_sha256"]
        != receipt["criteria_commitment_sha256"]
    ):
        raise ContractError("prior attempt criteria does not match reuse receipt")
    if attempt_verification["status"] != "VERIFIED_SEALED_BOUND":
        reasons.add("PRIOR_ATTEMPT_NOT_BOUND")
    expected_outcome = (
        "SELF_AUDIT_SUBMITTED" if receipt["self_audit"] else "CANDIDATE_SUBMITTED"
    )
    if attempt_verification["outcome"] != expected_outcome:
        reasons.add("PRIOR_RESULT_NOT_REUSABLE")
    records = list_records(prior_attempt)
    record_bindings = (
        ("audit_result", receipt["audit_result_sha256"]),
        ("audit_report", receipt["audit_report_sha256"]),
        ("audit_reuse_receipt", foundation_file_sha256(prior_reuse_receipt_path)),
    )
    record_statuses = [
        _require_record_binding(records, kind, digest, required=False)
        for kind, digest in record_bindings
    ]
    if not all(record_statuses):
        reasons.add("PRIOR_ATTEMPT_RECORD_MISMATCH")

    prior_evidence_types = set(
        prior_selection["selection_context"]["evidence_types"]
    )
    if evidence_types != prior_evidence_types:
        reasons.add("EVIDENCE_TYPE_CHANGED")
    if mode != receipt["mode"]:
        reasons.add("MODE_CHANGED")
    if str(subject) != receipt["subject"]["canonical_path"]:
        reasons.add("SUBJECT_IDENTITY_CHANGED")
    if current_index["file_set_sha256"] != receipt["subject"]["file_set_sha256"]:
        reasons.add("SUBJECT_CONTENT_CHANGED")
    if criteria_sha256 != receipt["criteria_commitment_sha256"]:
        reasons.add("CRITERIA_CHANGED")
    if registry_changed:
        reasons.add("REGISTRY_CHANGED")
    if current_selection["selection_sha256"] != receipt["selection_sha256"]:
        reasons.add("SELECTION_CHANGED")
    if auditor_sha256 != receipt["auditor_core_sha256"]:
        reasons.add("AUDITOR_CHANGED")

    _assert_reuse_inputs_stable(
        subject,
        current_index,
        criteria_path,
        criteria_sha256,
        skill_root,
        auditor_sha256,
    )
    return _build_reuse_decision(
        reasons,
        receipt["audit_id"],
        prior_result_sha256,
        prior_report_sha256,
        receipt["prior_conclusion"],
    )


def validate_audit_result(
    path: Path,
    selection_path: Path,
    registry_path: Path = DEFAULT_REGISTRY,
    *,
    allow_subject_drift: bool = False,
    allow_registry_drift: bool = False,
) -> dict[str, Any]:
    result = load_json(path)
    schema = load_json(AUDIT_SCHEMA)
    require_production_validate(result, schema)
    if allow_registry_drift:
        selection = validate_selection_document(selection_path)
    else:
        selection, _ = validate_selection_artifact(selection_path, registry_path)
    context = selection["selection_context"]
    if result["mode"] != context["mode"]:
        raise ContractError("audit mode does not match the frozen selection")
    if result["registry_sha256"] != selection["registry_sha256"]:
        raise ContractError("audit registry digest does not match the frozen selection")
    if result["selection_sha256"] != selection["selection_sha256"]:
        raise ContractError("audit selection digest does not match the frozen selection")

    subject_path = Path(result["subject"]["path"])
    if not subject_path.is_absolute():
        raise ContractError(f"audit subject path must be absolute: {subject_path}")
    if subject_path.is_symlink() or not (subject_path.is_file() or subject_path.is_dir()):
        raise ContractError(f"audit subject must be a real file or directory: {subject_path}")
    derived_self_audit = (
        result["self_audit"] if allow_subject_drift else derive_self_audit(subject_path)
    )
    if result["self_audit"] is not derived_self_audit:
        raise ContractError("self_audit does not match the verified audit subject")
    index_path = verify_binding(result["evidence_index"], "audit evidence index")
    records_path = verify_binding(result["coverage_records"], "audit coverage records")
    ledger_path = verify_binding(result["coverage_ledger"], "audit coverage ledger")
    index = (
        validate_index_document(load_json(index_path))
        if allow_subject_drift
        else verify_index(subject_path, index_path)
    )
    if result["subject"]["file_set_sha256"] != index["file_set_sha256"]:
        raise ContractError("audit subject file-set digest does not match verified evidence index")
    recomputed_ledger, coverage_exit = _build_coverage_from_verified_inputs(
        index,
        selection,
        records_path,
    )
    declared_ledger = load_json(ledger_path)
    if declared_ledger != recomputed_ledger:
        raise ContractError("audit coverage ledger does not match recomputed coverage")
    if result["coverage_status"] != recomputed_ledger["status"]:
        raise ContractError("audit coverage status does not match recomputed coverage")
    if coverage_exit == 0 and recomputed_ledger["status"] != "COMPLETE":
        raise ContractError("audit coverage returned success without COMPLETE status")
    chunk_map = {chunk["id"]: chunk for chunk in index["chunks"]}
    coverage_by_chunk = {
        record["chunk_id"]: record for record in load_jsonl(records_path)
    }

    def verify_evidence_refs(
        refs: list[dict[str, str]],
        label: str,
        expected_rule_id: str | None = None,
    ) -> None:
        for ref in refs:
            chunk = chunk_map.get(ref["chunk_id"])
            if chunk is None or chunk["sha256"] != ref["chunk_sha256"]:
                raise ContractError(f"{label}: evidence reference does not bind a verified chunk")
            coverage_record = coverage_by_chunk.get(ref["chunk_id"])
            if coverage_record is None:
                raise ContractError(f"{label}: evidence chunk has no audited coverage record")
            if (
                expected_rule_id is not None
                and expected_rule_id not in coverage_record["rule_ids"]
            ):
                raise ContractError(
                    f"{label}: evidence chunk was not audited for {expected_rule_id}"
                )

    rule_results = result["known_rule_results"]
    ids = [item["id"] for item in rule_results]
    if len(ids) != len(set(ids)):
        raise ContractError("known rule results contain duplicate ids")
    selected_by_id = {
        item["id"]: item for item in context["selected_rules"]
    }
    if set(ids) != set(selected_by_id):
        missing = sorted(set(selected_by_id) - set(ids))
        extra = sorted(set(ids) - set(selected_by_id))
        raise ContractError(
            f"known rule result set does not match selection; missing={missing}, extra={extra}"
        )
    for item in rule_results:
        selected = selected_by_id[item["id"]]
        if item["revision"] != selected["revision"]:
            raise ContractError(f"{item['id']}: revision does not match selection")
        if item["severity"] != selected["severity"]:
            raise ContractError(f"{item['id']}: severity does not match selection")
        if item["status"] != "UNCHECKED" and not item["evidence_refs"]:
            raise ContractError(
                f"{item['id']}: checked status requires at least one evidence reference"
            )
        verify_evidence_refs(
            item["evidence_refs"],
            f"{item['id']} evidence",
            item["id"],
        )

    unchecked_high = [
        item["id"]
        for item in rule_results
        if item["severity"] in {"critical", "high"} and item["status"] == "UNCHECKED"
    ]
    critical_hits = [
        item["id"]
        for item in rule_results
        if item["severity"] == "critical" and item["status"] == "HIT"
    ]
    high_hits = [
        item["id"]
        for item in rule_results
        if item["severity"] == "high" and item["status"] == "HIT"
    ]
    hit_ids = {item["id"] for item in rule_results if item["status"] == "HIT"}
    required_hard_failures: set[str] = set()
    if unchecked_high:
        required_hard_failures.add("HIGH_SEVERITY_UNCHECKED")
    if result["coverage_status"] != "COMPLETE":
        required_hard_failures.add("COVERAGE_INCOMPLETE")
    if selection["status"] != "SELECTED":
        required_hard_failures.add("LOW_CONFIDENCE_SELECTION")
    if {"FM-01", "FM-05"}.issubset(hit_ids):
        required_hard_failures.add("REDLINE_FM01_FM05")
    if {"FM-05", "FM-06"}.issubset(hit_ids):
        required_hard_failures.add("REDLINE_FM06_WITHOUT_ROLE_SEPARATION")

    executable = result["executable_acceptance"]
    if executable["status"] != "UNCHECKED" and not executable["evidence_refs"]:
        raise ContractError("checked executable-acceptance status requires evidence")
    verify_evidence_refs(
        executable["evidence_refs"],
        "executable acceptance evidence",
    )
    if executable["status"] == "ABSENT":
        required_hard_failures.add("NO_EXECUTABLE_ACCEPTANCE_ARTIFACT")
    elif executable["status"] == "UNCHECKED":
        required_hard_failures.add("EXECUTABLE_ACCEPTANCE_UNCHECKED")
    missing_hard_failures = sorted(
        required_hard_failures - set(result["hard_gate_failures"])
    )
    if missing_hard_failures:
        raise ContractError(
            f"audit result omitted derived hard gate failures: {missing_hard_failures}"
        )

    if unchecked_high and result["conclusion"] not in FAIL_CLOSED_CONCLUSIONS:
        raise ContractError(
            f"unchecked high-severity rules require fail-closed conclusion: {unchecked_high}"
        )
    if result["coverage_status"] != "COMPLETE" and result["conclusion"] not in FAIL_CLOSED_CONCLUSIONS:
        raise ContractError("incomplete coverage requires INCOMPLETE or BLOCKED")
    if selection["status"] != "SELECTED" and result["conclusion"] not in FAIL_CLOSED_CONCLUSIONS:
        raise ContractError("low-confidence selection requires INCOMPLETE or BLOCKED")
    if result["hard_gate_failures"] and result["conclusion"] == "PASS_WITHIN_FROZEN_SCOPE":
        raise ContractError("hard gate failures cannot produce PASS_WITHIN_FROZEN_SCOPE")
    if critical_hits and result["conclusion"] not in {"REJECT", "BLOCKED"}:
        raise ContractError(f"critical hits require REJECT or BLOCKED: {critical_hits}")
    if high_hits and result["conclusion"] == "PASS_WITHIN_FROZEN_SCOPE":
        raise ContractError(f"high-severity hits cannot produce PASS: {high_hits}")
    if (
        {"FM-01", "FM-05"}.issubset(hit_ids)
        or {"FM-05", "FM-06"}.issubset(hit_ids)
        or executable["status"] == "ABSENT"
    ) and result["conclusion"] not in {"REJECT", "BLOCKED"}:
        raise ContractError("legacy one-vote redline requires REJECT or BLOCKED")
    if executable["status"] == "UNCHECKED" and result["conclusion"] not in FAIL_CLOSED_CONCLUSIONS:
        raise ContractError("unchecked executable acceptance requires INCOMPLETE or BLOCKED")
    if result["conclusion"] == "PASS_WITHIN_FROZEN_SCOPE":
        if any(item["status"] in {"HIT", "UNCHECKED"} for item in rule_results):
            raise ContractError("PASS requires every selected rule to be NOT_HIT or NOT_APPLICABLE")
        if result["coverage_status"] != "COMPLETE":
            raise ContractError("PASS requires complete coverage")

    if result["self_audit"]:
        if result["status"] != "SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW":
            raise ContractError("self-audit must use the external-review submission status")
        if result["conclusion"] == "PASS_WITHIN_FROZEN_SCOPE":
            raise ContractError("self-audit cannot produce PASS_WITHIN_FROZEN_SCOPE")
    elif result["status"] == "SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW":
        raise ContractError("non-self audit cannot use self-audit status")

    return {
        "schema_version": "1.0",
        "status": "VALID",
        "audit_id": result["audit_id"],
        "result_sha256": foundation_file_sha256(path),
        "selection_sha256": selection["selection_sha256"],
        "subject_file_set_sha256": index["file_set_sha256"],
        "coverage_ledger_sha256": recomputed_ledger["ledger_sha256"],
        "derived_self_audit": derived_self_audit,
        "known_rule_count": len(rule_results),
        "novel_hypothesis_count": len(result["novel_hypotheses"]),
        "unchecked_high_severity": unchecked_high,
        "critical_hits": critical_hits,
        "high_hits": high_hits,
        "conclusion": result["conclusion"],
        "acceptance_eligible": False,
    }


def validate_case(case: Any, index: int) -> dict[str, Any]:
    required_keys = {
        "case_id",
        "required_rule_ids",
        "forbidden_rule_ids",
        "allowed_conclusions",
        "required_status",
    }
    if not isinstance(case, dict) or set(case) != required_keys:
        raise ContractError(f"case {index} has an unexpected field set")
    if not isinstance(case["case_id"], str) or not case["case_id"]:
        raise ContractError(f"case {index} has invalid case_id")
    for key in ("required_rule_ids", "forbidden_rule_ids", "allowed_conclusions"):
        value = case[key]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ContractError(f"case {case['case_id']} has invalid {key}")
        if len(value) != len(set(value)):
            raise ContractError(f"case {case['case_id']} has duplicate {key}")
    if not case["allowed_conclusions"]:
        raise ContractError(f"case {case['case_id']} has no allowed conclusion")
    allowed_conclusions = {
        "PASS_WITHIN_FROZEN_SCOPE",
        "NEEDS_REVISION",
        "REJECT",
        "INCOMPLETE",
        "BLOCKED",
    }
    if not set(case["allowed_conclusions"]).issubset(allowed_conclusions):
        raise ContractError(f"case {case['case_id']} has an unknown allowed conclusion")
    if set(case["required_rule_ids"]) & set(case["forbidden_rule_ids"]):
        raise ContractError(f"case {case['case_id']} requires and forbids the same rule")
    allowed_statuses = {
        "AUDIT_SUBMITTED_FOR_REVIEW",
        "SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW",
        "EXTERNALLY_REVIEWED",
    }
    if case["required_status"] not in allowed_statuses:
        raise ContractError(f"case {case['case_id']} has invalid required_status")
    return case


def validate_case_result(result: Any, index: int) -> dict[str, Any]:
    required_keys = {"case_id", "detected_rule_ids", "conclusion", "status"}
    if not isinstance(result, dict) or set(result) != required_keys:
        raise ContractError(f"case result {index} has an unexpected field set")
    if not isinstance(result["case_id"], str) or not result["case_id"]:
        raise ContractError(f"case result {index} has invalid case_id")
    detected = result["detected_rule_ids"]
    if not isinstance(detected, list) or not all(isinstance(item, str) and item for item in detected):
        raise ContractError(f"case result {result['case_id']} has invalid detected_rule_ids")
    if len(detected) != len(set(detected)):
        raise ContractError(f"case result {result['case_id']} has duplicate detected_rule_ids")
    if not isinstance(result["conclusion"], str) or not isinstance(result["status"], str):
        raise ContractError(f"case result {result['case_id']} has invalid terminal fields")
    return result


def grade_cases(cases_path: Path, results_path: Path) -> dict[str, Any]:
    cases_raw = load_jsonl(cases_path)
    results_raw = load_jsonl(results_path)
    cases = [validate_case(case, index) for index, case in enumerate(cases_raw)]
    results = [
        validate_case_result(result, index) for index, result in enumerate(results_raw)
    ]
    if not cases:
        raise ContractError("frozen case set must not be empty")
    if not results:
        raise ContractError("case result set must not be empty")
    case_map = {case["case_id"]: case for case in cases}
    result_map = {result["case_id"]: result for result in results}
    if len(case_map) != len(cases):
        raise ContractError("frozen cases contain duplicate case ids")
    if len(result_map) != len(results):
        raise ContractError("results contain duplicate case ids")
    if set(case_map) != set(result_map):
        missing = sorted(set(case_map) - set(result_map))
        extra = sorted(set(result_map) - set(case_map))
        raise ContractError(f"case set mismatch; missing={missing}, extra={extra}")

    grades: list[dict[str, Any]] = []
    for case_id in sorted(case_map):
        case = case_map[case_id]
        result = result_map[case_id]
        detected = set(result["detected_rule_ids"])
        missing_rules = sorted(set(case["required_rule_ids"]) - detected)
        forbidden_rules = sorted(set(case["forbidden_rule_ids"]) & detected)
        conclusion_ok = result["conclusion"] in case["allowed_conclusions"]
        status_ok = result["status"] == case["required_status"]
        passed = not missing_rules and not forbidden_rules and conclusion_ok and status_ok
        grades.append(
            {
                "case_id": case_id,
                "passed": passed,
                "missing_required_rule_ids": missing_rules,
                "detected_forbidden_rule_ids": forbidden_rules,
                "conclusion_ok": conclusion_ok,
                "status_ok": status_ok,
            }
        )
    overall = all(grade["passed"] for grade in grades)
    grade: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "PUBLIC_EVALUATION_PASSED" if overall else "REWORK_REQUIRED",
        "case_count": len(cases),
        "passed_count": sum(item["passed"] for item in grades),
        "failed_count": sum(not item["passed"] for item in grades),
        "overall_pass": overall,
        "acceptance_eligible": False,
        "cases_sha256": foundation_file_sha256(cases_path),
        "results_sha256": foundation_file_sha256(results_path),
        "case_grades": grades,
    }
    grade["grade_sha256"] = foundation_digest_document(grade)
    return grade


def verify_binding(binding: dict[str, Any], label: str) -> Path:
    path = Path(binding["path"])
    if not path.is_absolute():
        raise ContractError(f"{label} path must be absolute: {path}")
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} must bind a regular file: {path}")
    require_sha256(binding["sha256"], f"{label} sha256")
    if foundation_file_sha256(path) != binding["sha256"]:
        raise ContractError(f"{label} digest mismatch: {path}")
    return path


def continuation_digest(package: dict[str, Any]) -> str:
    unsigned = dict(package)
    unsigned.pop("package_digest", None)
    return foundation_digest_document(unsigned)


def source_manifest_digest(manifest: dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_digest", None)
    return foundation_digest_document(unsigned)


def validate_source_manifest(path: Path) -> dict[str, Any]:
    manifest = load_json(path)
    schema = load_json(SOURCE_MANIFEST_SCHEMA)
    require_production_validate(manifest, schema)
    if manifest["manifest_digest"] != source_manifest_digest(manifest):
        raise ContractError("source manifest self digest mismatch")
    bindings = manifest["bindings"]
    paths = [binding["path"] for binding in bindings]
    if len(paths) != len(set(paths)):
        raise ContractError("source manifest contains duplicate paths")
    if paths != sorted(paths, key=lambda item: item.encode("utf-8")):
        raise ContractError("source manifest bindings are not canonically sorted")
    for index, binding in enumerate(bindings):
        verify_binding(binding, f"source manifest binding {index}")
    return manifest


def verify_continuation_selection(
    package: dict[str, Any],
    selection_path: Path,
    registry_path: Path,
) -> dict[str, Any]:
    selection, _ = validate_selection_artifact(selection_path, registry_path)
    expected_rules = [
        {
            "id": item["id"],
            "revision": item["revision"],
            "source_sha256": item["source_sha256"],
        }
        for item in selection["selection_context"]["selected_rules"]
    ]
    if package["registry_sha256"] != selection["registry_sha256"]:
        raise ContractError("continuation registry digest does not match selection")
    if package["selection_sha256"] != selection["selection_sha256"]:
        raise ContractError("continuation selection digest does not match selection")
    if package["selected_rules"] != expected_rules:
        raise ContractError("continuation selected rules do not exactly match selection")
    return selection


def continuation_create(
    template_path: Path,
    output_path: Path,
    selection_path: Path,
    registry_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    template = load_json(template_path)
    schema = load_json(CONTINUATION_SCHEMA)
    required = set(schema["required"])
    if not isinstance(template, dict) or set(template) != required - {"package_digest"}:
        raise ContractError("continuation template field set does not match schema")
    for index, binding in enumerate(template["source_bindings"]):
        verify_binding(binding, f"source binding {index}")
    for index, binding in enumerate(template["accepted_artifacts"]):
        verify_binding(binding, f"accepted artifact {index}")
    source_manifest = validate_source_manifest(source_manifest_path)
    if template["task_id"] != source_manifest["task_id"]:
        raise ContractError("continuation task id does not match source manifest")
    if template["source_manifest_sha256"] != foundation_file_sha256(source_manifest_path):
        raise ContractError("continuation source manifest file digest mismatch")
    if template["source_bindings"] != source_manifest["bindings"]:
        raise ContractError(
            "continuation source bindings do not exactly match the frozen source manifest"
        )
    verify_continuation_selection(template, selection_path, registry_path)
    package = dict(template)
    package["package_digest"] = continuation_digest(package)
    require_production_validate(package, schema)
    write_json_exclusive(output_path, package)
    return package


def continuation_verify(
    path: Path,
    selection_path: Path,
    registry_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    package = load_json(path)
    schema = load_json(CONTINUATION_SCHEMA)
    require_production_validate(package, schema)
    if package["package_digest"] != continuation_digest(package):
        raise ContractError("continuation package self digest mismatch")
    for index, binding in enumerate(package["source_bindings"]):
        verify_binding(binding, f"source binding {index}")
    for index, binding in enumerate(package["accepted_artifacts"]):
        verify_binding(binding, f"accepted artifact {index}")
    source_manifest = validate_source_manifest(source_manifest_path)
    if package["task_id"] != source_manifest["task_id"]:
        raise ContractError("continuation task id does not match source manifest")
    if package["source_manifest_sha256"] != foundation_file_sha256(source_manifest_path):
        raise ContractError("continuation source manifest file digest mismatch")
    if package["source_bindings"] != source_manifest["bindings"]:
        raise ContractError(
            "continuation source bindings do not exactly match the frozen source manifest"
        )
    verify_continuation_selection(package, selection_path, registry_path)
    return {
        "schema_version": "1.0",
        "status": "VERIFIED",
        "package_id": package["package_id"],
        "package_sha256": foundation_file_sha256(path),
        "package_digest": package["package_digest"],
        "source_binding_count": len(package["source_bindings"]),
        "selected_rule_count": len(package["selected_rules"]),
        "next_action": package["next_action"],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subcommands = root.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate-result")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--selection", type=Path, required=True)
    validate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    validate.add_argument("--output", type=Path)

    grade = subcommands.add_parser("grade")
    grade.add_argument("--cases", type=Path, required=True)
    grade.add_argument("--results", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)

    create = subcommands.add_parser("continuation-create")
    create.add_argument("--template", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--selection", type=Path, required=True)
    create.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    create.add_argument("--source-manifest", type=Path, required=True)

    verify = subcommands.add_parser("continuation-verify")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--selection", type=Path, required=True)
    verify.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    verify.add_argument("--source-manifest", type=Path, required=True)
    verify.add_argument("--output", type=Path)

    receipt = subcommands.add_parser("reuse-receipt-create")
    receipt.add_argument("--result", type=Path, required=True)
    receipt.add_argument("--report", type=Path, required=True)
    receipt.add_argument("--selection", type=Path, required=True)
    receipt.add_argument("--attempt", type=Path, required=True)
    receipt.add_argument("--output", type=Path, required=True)

    reuse = subcommands.add_parser("reuse-check")
    reuse.add_argument("--subject", type=Path, required=True)
    reuse.add_argument(
        "--mode",
        choices=("static", "runtime", "combined"),
        required=True,
    )
    reuse.add_argument("--evidence-type", action="append", required=True)
    reuse.add_argument("--criteria-commitment", type=Path, required=True)
    reuse.add_argument("--prior-result", type=Path, required=True)
    reuse.add_argument("--prior-report", type=Path, required=True)
    reuse.add_argument("--prior-selection", type=Path, required=True)
    reuse.add_argument("--prior-attempt", type=Path, required=True)
    reuse.add_argument("--prior-reuse-receipt", type=Path)
    reuse.add_argument("--expected-prior-seal-file-sha256")
    reuse.add_argument("--fresh-evidence-required", action="store_true")
    reuse.add_argument("--output", type=Path)
    return root


def emit(value: dict[str, Any], output: Path | None) -> None:
    if output:
        write_json_exclusive(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def output_inside_subject(subject: Path, output: Path) -> bool:
    canonical_subject = canonical_subject_path(subject)
    canonical_output = output.resolve(strict=False)
    if canonical_subject.is_file():
        return canonical_output == canonical_subject
    try:
        canonical_output.relative_to(canonical_subject)
        return True
    except ValueError:
        return False


def output_inside_directory(directory: Path, output: Path) -> bool:
    canonical_directory = directory.resolve(strict=True)
    canonical_output = output.resolve(strict=False)
    try:
        canonical_output.relative_to(canonical_directory)
        return True
    except ValueError:
        return False


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "validate-result":
            emit(
                validate_audit_result(args.input, args.selection, args.registry),
                args.output,
            )
        elif args.command == "grade":
            write_json_exclusive(args.output, grade_cases(args.cases, args.results))
        elif args.command == "continuation-create":
            continuation_create(
                args.template,
                args.output,
                args.selection,
                args.registry,
                args.source_manifest,
            )
        elif args.command == "continuation-verify":
            emit(
                continuation_verify(
                    args.input,
                    args.selection,
                    args.registry,
                    args.source_manifest,
                ),
                args.output,
            )
        elif args.command == "reuse-receipt-create":
            write_json_exclusive(
                args.output,
                build_reuse_receipt(
                    args.result,
                    args.report,
                    args.selection,
                    args.attempt,
                ),
            )
        elif args.command == "reuse-check":
            if args.output is not None and output_inside_subject(
                args.subject,
                args.output,
            ):
                raise ContractError("reuse decision output must be outside the audit subject")
            if args.output is not None and output_inside_directory(
                args.prior_attempt,
                args.output,
            ):
                raise ContractError("reuse decision output must be outside the prior attempt")
            if args.output is not None and output_inside_directory(
                SCRIPT_DIR.parent,
                args.output,
            ):
                raise ContractError("reuse decision output must be outside the auditor skill root")
            emit(
                reuse_check(
                    args.subject,
                    args.mode,
                    set(args.evidence_type),
                    args.criteria_commitment,
                    args.prior_result,
                    args.prior_report,
                    args.prior_selection,
                    args.prior_attempt,
                    args.prior_reuse_receipt,
                    args.expected_prior_seal_file_sha256,
                    fresh_evidence_required=args.fresh_evidence_required,
                ),
                args.output,
            )
        else:
            raise ContractError(f"unsupported command: {args.command}")
        return 0
    except (ContractError, OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

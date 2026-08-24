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
    load_json,
    load_jsonl,
    require_sha256,
    write_json_exclusive,
)
from evidence_tool import _build_coverage_from_verified_inputs, verify_index
from foundation_client import foundation_digest_document, foundation_file_sha256, require_production_validate
from registry_tool import DEFAULT_REGISTRY, validate_selection_artifact


SCRIPT_DIR = Path(__file__).resolve().parent
AUDIT_SCHEMA = SCRIPT_DIR.parent / "references" / "audit-result.schema.json"
CONTINUATION_SCHEMA = SCRIPT_DIR.parent / "references" / "continuation-package.schema.json"
SOURCE_MANIFEST_SCHEMA = SCRIPT_DIR.parent / "references" / "source-manifest.schema.json"
FAIL_CLOSED_CONCLUSIONS = {"INCOMPLETE", "BLOCKED"}
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


def validate_audit_result(
    path: Path,
    selection_path: Path,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    result = load_json(path)
    schema = load_json(AUDIT_SCHEMA)
    require_production_validate(result, schema)
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
    derived_self_audit = derive_self_audit(subject_path)
    if result["self_audit"] is not derived_self_audit:
        raise ContractError("self_audit does not match the verified audit subject")
    index_path = verify_binding(result["evidence_index"], "audit evidence index")
    records_path = verify_binding(result["coverage_records"], "audit coverage records")
    ledger_path = verify_binding(result["coverage_ledger"], "audit coverage ledger")
    index = verify_index(subject_path, index_path)
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
    return root


def emit(value: dict[str, Any], output: Path | None) -> None:
    if output:
        write_json_exclusive(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


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
        else:
            raise ContractError(f"unsupported command: {args.command}")
        return 0
    except (ContractError, OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

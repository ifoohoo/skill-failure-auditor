#!/usr/bin/env python3
"""Validate and deterministically route the failure-mode registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import (
    ContractError,
    canonical_json_bytes,
    load_json,
    load_jsonl,
    sha256_bytes,
    sha256_file,
    validate_schema,
    write_json_exclusive,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = SCRIPT_DIR.parent / "references" / "failure-modes.jsonl"
DEFAULT_SCHEMA = SCRIPT_DIR.parent / "references" / "failure-mode.schema.json"
DEFAULT_BUILTIN_LOCK = SCRIPT_DIR.parent / "references" / "builtin-registry-lock.json"
EXPECTED_LEGACY = [f"FM-{index:02d}" for index in range(1, 19)]
EXPECTED_BUILTINS = [f"FM-{index:02d}" for index in range(1, 29)]
CORE_REDLINES = {
    "FM-01",
    "FM-02",
    "FM-03",
    "FM-05",
    "FM-06",
    "FM-15",
    "FM-18",
    "FM-22",
    "FM-25",
    "FM-26",
    "FM-27",
}


def source_digest(entry: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(entry))


def validate_builtin_lock(
    by_id: dict[str, dict[str, Any]],
    lock_path: Path = DEFAULT_BUILTIN_LOCK,
) -> dict[str, Any]:
    lock = load_json(lock_path)
    if not isinstance(lock, dict) or set(lock) != {
        "schema_version",
        "builtin_count",
        "entries",
        "builtin_set_sha256",
        "lock_sha256",
    }:
        raise ContractError("built-in registry lock has an unexpected field set")
    unsigned = dict(lock)
    unsigned.pop("lock_sha256")
    if lock["lock_sha256"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ContractError("built-in registry lock self digest mismatch")
    entries = lock["entries"]
    if (
        lock["schema_version"] != "1.0"
        or not isinstance(entries, list)
        or lock["builtin_count"] != len(entries)
        or lock["builtin_count"] != len(EXPECTED_BUILTINS)
        or lock["builtin_set_sha256"] != sha256_bytes(canonical_json_bytes(entries))
    ):
        raise ContractError("built-in registry lock header or set digest is invalid")
    expected_records = []
    for index, identifier in enumerate(EXPECTED_BUILTINS):
        record = entries[index] if index < len(entries) else None
        if (
            not isinstance(record, dict)
            or set(record) != {"id", "revision", "source_sha256"}
            or record["id"] != identifier
            or not isinstance(record["revision"], int)
            or isinstance(record["revision"], bool)
            or record["revision"] < 1
            or not isinstance(record["source_sha256"], str)
            or len(record["source_sha256"]) != 64
        ):
            raise ContractError(f"invalid built-in registry lock record: {identifier}")
        expected_records.append(record)

    actual_builtin_ids = {identifier for identifier in by_id if identifier.startswith("FM-")}
    if actual_builtin_ids != set(EXPECTED_BUILTINS):
        missing = sorted(set(EXPECTED_BUILTINS) - actual_builtin_ids)
        extra = sorted(actual_builtin_ids - set(EXPECTED_BUILTINS))
        raise ContractError(f"built-in rule set mismatch; missing={missing}, extra={extra}")
    for record in expected_records:
        entry = by_id[record["id"]]
        if (
            entry["revision"] != record["revision"]
            or source_digest(entry) != record["source_sha256"]
        ):
            raise ContractError(
                f"{record['id']}: built-in rule differs from the frozen registry lock"
            )
    return lock


def validate_registry(
    registry_path: Path,
    schema_path: Path,
    require_builtins: bool = True,
    builtin_lock_path: Path = DEFAULT_BUILTIN_LOCK,
) -> dict[str, Any]:
    schema = load_json(schema_path)
    entries = load_jsonl(registry_path)
    if not entries:
        raise ContractError("registry must contain at least one entry")

    by_id: dict[str, dict[str, Any]] = {}
    mutation_ids: set[str] = set()
    for index, entry in enumerate(entries):
        validate_schema(entry, schema, schema, path=f"$[{index}]")
        identifier = entry["id"]
        if identifier in by_id:
            raise ContractError(f"duplicate rule id: {identifier}")
        by_id[identifier] = entry
        for mutation in entry["mutation_operators"]:
            mutation_id = mutation["id"]
            if mutation_id in mutation_ids:
                raise ContractError(f"duplicate mutation id: {mutation_id}")
            mutation_ids.add(mutation_id)

    if require_builtins:
        actual_core = {identifier for identifier, entry in by_id.items() if entry["core_redline"]}
        if actual_core != CORE_REDLINES:
            raise ContractError(
                f"core redline set mismatch; expected={sorted(CORE_REDLINES)}, actual={sorted(actual_core)}"
            )
        builtin_lock = None
    else:
        builtin_lock = None

    for identifier, entry in by_id.items():
        for relation in ("depends_on", "conflicts_with"):
            for related in entry[relation]:
                if related == identifier:
                    raise ContractError(f"{identifier}: self reference in {relation}")
                if related not in by_id:
                    raise ContractError(f"{identifier}: unknown {relation} id {related}")
        for conflict in entry["conflicts_with"]:
            if identifier not in by_id[conflict]["conflicts_with"]:
                raise ContractError(f"asymmetric conflict: {identifier} -> {conflict}")

    if require_builtins:
        builtin_lock = validate_builtin_lock(by_id, builtin_lock_path)

    return {
        "schema_version": "1.0",
        "status": "VALID",
        "entry_count": len(entries),
        "legacy_count": sum(identifier in by_id for identifier in EXPECTED_LEGACY),
        "builtin_count": sum(identifier in by_id for identifier in EXPECTED_BUILTINS),
        "mutation_count": len(mutation_ids),
        "registry_sha256": sha256_file(registry_path),
        "schema_sha256": sha256_file(schema_path),
        "builtin_lock_sha256": (
            builtin_lock["lock_sha256"] if builtin_lock is not None else None
        ),
        "core_redlines": sorted(identifier for identifier, entry in by_id.items() if entry["core_redline"]),
        "entries": entries,
    }


def mode_matches(requested: str, offered: list[str]) -> bool:
    if requested == "combined":
        return True
    return requested in offered or "combined" in offered


def route_reason(
    entry: dict[str, Any],
    mode: str,
    target_type: str,
    evidence_types: set[str],
) -> tuple[bool, str]:
    if entry["core_redline"]:
        return True, "core_redline"
    if not mode_matches(mode, entry["modes"]):
        return False, "mode_not_applicable"
    targets = set(entry["applies_when"]["target_types"])
    if "*" not in targets and target_type not in targets:
        return False, "target_type_not_applicable"
    offered_evidence = set(entry["applies_when"]["evidence_types"])
    if evidence_types and "*" not in offered_evidence and offered_evidence.isdisjoint(evidence_types):
        return False, "evidence_type_not_applicable"
    return True, "metadata_match"


def build_selection(
    validation: dict[str, Any],
    mode: str,
    target_type: str,
    evidence_types: set[str],
    max_selected: int,
) -> dict[str, Any]:
    entries = validation["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    route_results: dict[str, tuple[bool, str]] = {}
    for entry in entries:
        selected, reason = route_reason(entry, mode, target_type, evidence_types)
        if target_type == "skill" and entry["id"] in EXPECTED_BUILTINS:
            selected = True
            reason = "builtin_skill_rule"
        route_results[entry["id"]] = (selected, reason)

    def dependency_closure(seed: set[str]) -> set[str]:
        closure = set(seed)
        pending = sorted(seed)
        while pending:
            identifier = pending.pop(0)
            for dependency in by_id[identifier]["depends_on"]:
                if dependency not in closure:
                    closure.add(dependency)
                    pending.append(dependency)
                    pending.sort()
        return closure

    mandatory_ids = {
        entry["id"]
        for entry in entries
        if entry["core_redline"]
        or (target_type == "skill" and entry["id"] in EXPECTED_BUILTINS)
    }
    mandatory_ids = dependency_closure(mandatory_ids)
    if max_selected < len(mandatory_ids):
        raise ContractError(
            f"max-selected {max_selected} is below mandatory closed rule count "
            f"{len(mandatory_ids)}"
        )

    selected_ids = set(mandatory_ids)
    selection_reasons: dict[str, str] = {}
    for identifier in selected_ids:
        entry = by_id[identifier]
        if entry["core_redline"]:
            selection_reasons[identifier] = "core_redline"
        elif target_type == "skill" and identifier in EXPECTED_BUILTINS:
            selection_reasons[identifier] = "builtin_skill_rule"
        else:
            selection_reasons[identifier] = "mandatory_dependency"

    optional_roots = [
        entry
        for entry in entries
        if route_results[entry["id"]][0] and entry["id"] not in selected_ids
    ]
    optional_roots.sort(key=lambda entry: (-entry["priority"], entry["id"]))
    truncated_ids: set[str] = set()
    for entry in optional_roots:
        identifier = entry["id"]
        additions = dependency_closure(selected_ids | {identifier}) - selected_ids
        if len(selected_ids) + len(additions) > max_selected:
            truncated_ids.add(identifier)
            continue
        selected_ids.update(additions)
        selection_reasons[identifier] = route_results[identifier][1]
        for dependency in sorted(additions - {identifier}):
            selection_reasons.setdefault(dependency, f"dependency_of:{identifier}")

    selected_entries = [by_id[identifier] for identifier in selected_ids]
    selected_entries.sort(
        key=lambda entry: (
            not (
                entry["core_redline"]
                or (target_type == "skill" and entry["id"] in EXPECTED_BUILTINS)
            ),
            -entry["priority"],
            entry["id"],
        )
    )
    selected_rules = []
    for entry in selected_entries:
        item = {
            "id": entry["id"],
            "revision": entry["revision"],
            "severity": entry["severity"],
            "priority": entry["priority"],
            "core_redline": entry["core_redline"],
            "selection_reason": selection_reasons[entry["id"]],
            "source_sha256": source_digest(entry),
            "guidance_ref": entry["guidance_ref"],
        }
        selected_rules.append(item)

    ledger: list[dict[str, Any]] = []
    for entry in entries:
        identifier = entry["id"]
        if identifier in selected_ids:
            selected = True
            reason = selection_reasons[identifier]
        else:
            selected = False
            reason = (
                "capacity_limit_low_priority"
                if identifier in truncated_ids
                else route_results[identifier][1]
            )
        ledger.append(
            {
                "id": identifier,
                "revision": entry["revision"],
                "selected": selected,
                "reason": reason,
                "source_sha256": source_digest(entry),
            }
        )

    ledger.sort(key=lambda item: item["id"])
    if not CORE_REDLINES.issubset(selected_ids):
        raise ContractError("selection omitted one or more mandatory core redlines")
    for identifier in selected_ids:
        missing_dependencies = set(by_id[identifier]["depends_on"]) - selected_ids
        if missing_dependencies:
            raise ContractError(
                f"selection dependency closure failed for {identifier}: "
                f"{sorted(missing_dependencies)}"
            )
    if len(ledger) != len(entries):
        raise ContractError("coverage ledger does not conserve registry entry count")
    if len({item["id"] for item in ledger}) != len(entries):
        raise ContractError("coverage ledger contains duplicate rule ids")

    selection_context = {
        "mode": mode,
        "target_type": target_type,
        "evidence_types": sorted(evidence_types),
        "selected_rules": selected_rules,
    }
    context_sha = sha256_bytes(canonical_json_bytes(selection_context))
    status = "INCOMPLETE_LOW_CONFIDENCE" if truncated_ids else "SELECTED"
    coverage: dict[str, Any] = {
        "schema_version": "1.0",
        "registry_sha256": validation["registry_sha256"],
        "registry_entry_count": len(entries),
        "selected_count": len(selected_rules),
        "unselected_count": len(entries) - len(selected_rules),
        "entries": ledger,
    }
    coverage["ledger_sha256"] = sha256_bytes(canonical_json_bytes(coverage))
    result = {
        "schema_version": "1.0",
        "status": status,
        "registry_sha256": validation["registry_sha256"],
        "schema_sha256": validation["schema_sha256"],
        "registry_entry_count": len(entries),
        "selected_count": len(selected_rules),
        "unselected_count": len(entries) - len(selected_rules),
        "truncated_count": len(truncated_ids),
        "selection_context": selection_context,
        "selection_context_sha256": context_sha,
        "coverage_ledger_sha256": coverage["ledger_sha256"],
    }
    result["selection_sha256"] = sha256_bytes(canonical_json_bytes(result))
    return {"selection": result, "coverage": coverage}


def validate_selection_artifact(
    selection_path: Path,
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = load_json(selection_path)
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
    if selection["selection_sha256"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ContractError("selection artifact self digest mismatch")

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
    if (
        not isinstance(context["evidence_types"], list)
        or not all(isinstance(item, str) and item for item in context["evidence_types"])
        or context["evidence_types"] != sorted(set(context["evidence_types"]))
    ):
        raise ContractError("selection context evidence types are not canonical")
    if not isinstance(context["selected_rules"], list) or not context["selected_rules"]:
        raise ContractError("selection context selected rules are empty")
    if selection["selection_context_sha256"] != sha256_bytes(canonical_json_bytes(context)):
        raise ContractError("selection context digest mismatch")

    validation = validate_registry(registry_path, schema_path)
    rebuilt = build_selection(
        validation,
        context["mode"],
        context["target_type"],
        set(context["evidence_types"]),
        selection["selected_count"],
    )
    if rebuilt["selection"] != selection:
        raise ContractError("selection artifact does not match deterministic registry routing")
    return selection, validation


def validate_coverage_artifact(
    coverage_path: Path,
    selection_path: Path,
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    selection, validation = validate_selection_artifact(
        selection_path,
        registry_path,
        schema_path,
    )
    context = selection["selection_context"]
    rebuilt = build_selection(
        validation,
        context["mode"],
        context["target_type"],
        set(context["evidence_types"]),
        selection["selected_count"],
    )
    coverage = load_json(coverage_path)
    if coverage != rebuilt["coverage"]:
        raise ContractError("coverage artifact does not match deterministic registry routing")
    if coverage["ledger_sha256"] != selection["coverage_ledger_sha256"]:
        raise ContractError("selection and coverage artifact digests do not match")
    return coverage


def emit(value: dict[str, Any], output: Path | None) -> None:
    if output:
        write_json_exclusive(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subcommands = root.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate")
    validate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    validate.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    validate.add_argument("--allow-missing-builtins", action="store_true")
    validate.add_argument("--output", type=Path)

    select = subcommands.add_parser("select")
    select.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    select.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    select.add_argument("--mode", choices=["static", "runtime", "combined"], required=True)
    select.add_argument("--target-type", required=True)
    select.add_argument("--evidence-type", action="append", default=[])
    select.add_argument("--max-selected", type=int, default=28)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--coverage-output", type=Path, required=True)

    verify = subcommands.add_parser("verify-selection")
    verify.add_argument("--selection", type=Path, required=True)
    verify.add_argument("--coverage", type=Path, required=True)
    verify.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    verify.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    verify.add_argument("--output", type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "validate":
            result = validate_registry(
                args.registry,
                args.schema,
                require_builtins=not args.allow_missing_builtins,
            )
            public_result = dict(result)
            public_result.pop("entries")
            emit(public_result, args.output)
            return 0
        if args.command == "verify-selection":
            selection, _ = validate_selection_artifact(
                args.selection,
                args.registry,
                args.schema,
            )
            coverage = validate_coverage_artifact(
                args.coverage,
                args.selection,
                args.registry,
                args.schema,
            )
            emit(
                {
                    "schema_version": "1.0",
                    "status": "VERIFIED",
                    "selection_sha256": selection["selection_sha256"],
                    "coverage_ledger_sha256": coverage["ledger_sha256"],
                    "selected_count": selection["selected_count"],
                    "registry_entry_count": selection["registry_entry_count"],
                },
                args.output,
            )
            return 0
        if args.max_selected <= 0:
            raise ContractError("--max-selected must be positive")
        validation = validate_registry(args.registry, args.schema)
        artifacts = build_selection(
            validation,
            args.mode,
            args.target_type,
            set(args.evidence_type),
            args.max_selected,
        )
        emit(artifacts["selection"], args.output)
        emit(artifacts["coverage"], args.coverage_output)
        return 0 if artifacts["selection"]["status"] == "SELECTED" else 2
    except (ContractError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

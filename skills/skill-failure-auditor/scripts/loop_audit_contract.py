#!/usr/bin/env python3
"""Compile and validate the SFA domain boundary carried by Loop Agent.

This module never launches a process, mutates the audited target, or writes a
Loop acceptance decision.  ``compile-loop-audit`` writes only a caller-chosen
new directory containing inputs accepted by Loop Agent's public
``prepare-workflow-source.mjs`` command.  ``validate-loop-audit`` reads Loop
delivery-task-result files plus bound SFA role artifacts and writes one SFA
domain report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from common import ContractError
from foundation_client import (
    foundation_digest_document,
    foundation_resource_closure,
    foundation_schema_provenance,
    require_production_validate,
    require_production_validate_by_schema_id,
)
from registry_tool import validate_registry


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
REGISTRY = CORE_ROOT / "references" / "failure-modes.jsonl"
REGISTRY_SCHEMA = CORE_ROOT / "references" / "failure-mode.schema.json"
ROLE_ARTIFACT_SCHEMA = CORE_ROOT.parent.parent / "spec" / "orchestration" / "role-artifact.schema.json"
PLATFORM_MAPPING = CORE_ROOT.parent.parent / "spec" / "orchestration" / "platform-adapter-mapping.json"
SEMANTIC_RULE_ROLES = {"static-audit", "runtime-evidence"}
SEVERITY_ORDER = {
    "PASS_WITHIN_FROZEN_SCOPE": 0,
    "NEEDS_REVISION": 1,
    "INCOMPLETE": 2,
    "BLOCKED": 2,
    "REJECT": 2,
}
TASK_ID = re.compile(r"^AUDIT-[A-Z0-9][A-Z0-9._-]+$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_digest(value: Any) -> str:
    return foundation_digest_document(value)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid JSON file {path}: {error}") from error


def _write_new_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_domain_mapping() -> dict[str, Any]:
    mapping = _read_json(PLATFORM_MAPPING)
    required = {"canonicalRoles", "modeRoleSets", "roleDependencies", "loopOuterContract"}
    if not isinstance(mapping, dict) or not required.issubset(mapping):
        raise ContractError("platform adapter mapping is incomplete")
    roles = mapping["canonicalRoles"]
    dependencies = mapping["roleDependencies"]
    mode_roles = mapping["modeRoleSets"]
    if (not isinstance(roles, list) or len(roles) != len(set(roles))
            or not all(isinstance(role, str) and role for role in roles)):
        raise ContractError("canonicalRoles must be a unique non-empty string array")
    if not isinstance(dependencies, dict) or set(dependencies) != set(roles):
        raise ContractError("roleDependencies must bind every canonical role exactly once")
    for role, parents in dependencies.items():
        if (not isinstance(parents, list) or len(parents) != len(set(parents))
                or any(parent not in roles or parent == role for parent in parents)):
            raise ContractError(f"invalid roleDependencies for {role}")
    if not isinstance(mode_roles, dict) or not mode_roles:
        raise ContractError("modeRoleSets must be a non-empty object")
    for mode, active in mode_roles.items():
        if (not isinstance(mode, str) or not isinstance(active, list)
                or len(active) != len(set(active)) or any(role not in roles for role in active)):
            raise ContractError(f"invalid modeRoleSets entry: {mode}")
    return mapping


def _regular_relative_paths(root: Path, *, exclude_python_cache: bool = False) -> list[str]:
    if root.is_symlink() or not root.exists():
        raise ContractError(f"resource root is missing or unsafe: {root}")
    if root.is_file():
        return [root.name]
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not (
            exclude_python_cache and name == "__pycache__"
        ))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if exclude_python_cache and name.endswith(".pyc"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            paths.append(path.relative_to(root).as_posix())
    return sorted(paths, key=lambda value: value.encode("utf-8"))


def _closure_for_paths(root: Path, paths: list[str]) -> dict[str, Any]:
    return foundation_resource_closure(
        str(root.resolve()),
        [{"path": item, "role": "input"} for item in paths],
    )


def _closure_sha_by_path(root: Path, paths: list[str]) -> dict[str, str]:
    closure = _closure_for_paths(root, paths)
    return {item["path"]: item["sha256"] for item in closure["resources"]}


def _target_closure(target: Path) -> dict[str, Any]:
    if target.is_file() and not target.is_symlink():
        root = target.parent
        paths = [target.name]
    elif target.is_dir() and not target.is_symlink():
        root = target
        paths = _regular_relative_paths(target, exclude_python_cache=True)
    else:
        raise ContractError("frozen target is missing or unsafe")
    closure = _closure_for_paths(root, paths)
    return {
        "root": str(root.resolve()),
        "resource_count": len(closure["resources"]),
        "digest": closure["digest"],
    }


def _load_loop_contract(provider_root: Path, mapping: dict[str, Any]) -> dict[str, Any]:
    binding = mapping["loopOuterContract"].get("schemaProvider")
    required = {"name", "version", "manifestRelativePath", "manifestSha256", "deliveryResultRole"}
    if not isinstance(binding, dict) or set(binding) != required:
        raise ContractError("Loop schema-provider binding is incomplete")
    if not provider_root.is_absolute() or provider_root.is_symlink() or not provider_root.is_dir():
        raise ContractError("loop_provider_root must be an absolute real directory")
    manifest_rel = binding["manifestRelativePath"]
    manifest_path = _is_relative_regular_binding(provider_root, manifest_rel)
    manifest_sha = _closure_sha_by_path(provider_root, [manifest_rel])[manifest_rel]
    if manifest_sha != binding["manifestSha256"]:
        raise ContractError("Loop schema-provider manifest digest drift")
    provider_manifest = _read_json(manifest_path)
    unsigned = {key: value for key, value in provider_manifest.items() if key != "digest"}
    if provider_manifest.get("digest") != _canonical_digest(unsigned):
        raise ContractError("Loop schema-provider semantic digest drift")
    provider = provider_manifest.get("provider")
    if provider != {"name": binding["name"], "version": binding["version"]}:
        raise ContractError("Loop schema-provider identity drift")
    entries = [entry for entry in provider_manifest.get("schemas", [])
               if entry.get("role") == binding["deliveryResultRole"]]
    if len(entries) != 1:
        raise ContractError("Loop delivery-task-result Schema role is missing or duplicate")
    entry = entries[0]
    schema_path = _is_relative_regular_binding(provider_root, entry.get("path", ""))
    schema_sha = _closure_sha_by_path(provider_root, [entry["path"]])[entry["path"]]
    schema = _read_json(schema_path)
    if schema_sha != entry.get("sha256") or schema.get("$id") != entry.get("$id"):
        raise ContractError("Loop delivery-task-result Schema bytes or $id drift")
    if schema.get("$schema") != entry.get("dialect"):
        raise ContractError("Loop delivery-task-result Schema dialect drift")
    provenance = foundation_schema_provenance(entry["$id"])
    if provenance.get("sha256") != entry["sha256"]:
        raise ContractError("managed Bundle does not bind the Loop delivery result Schema")
    return {
        "provider_root": str(provider_root.resolve()),
        "provider": provider,
        "manifest": {"path": manifest_rel, "sha256": manifest_sha, "digest": provider_manifest["digest"]},
        "delivery_result_schema": {
            "role": entry["role"], "path": entry["path"], "$id": entry["$id"],
            "dialect": entry["dialect"], "sha256": entry["sha256"],
        },
    }


def _validate_selected_rules(selected_rules: Any) -> list[dict[str, Any]]:
    if not isinstance(selected_rules, list) or not selected_rules:
        raise ContractError("selected_rules must be a non-empty array")
    validation = validate_registry(REGISTRY, REGISTRY_SCHEMA)
    by_id = {entry["id"]: entry for entry in validation["entries"]}
    seen: set[str] = set()
    normalized = []
    for index, binding in enumerate(selected_rules):
        if not isinstance(binding, dict) or set(binding) != {"id", "revision", "source_sha256"}:
            raise ContractError(f"selected_rules[{index}] has an unexpected field set")
        rule_id = binding["id"]
        if rule_id in seen:
            raise ContractError(f"duplicate selected rule: {rule_id}")
        entry = by_id.get(rule_id)
        if entry is None:
            raise ContractError(f"unknown selected rule: {rule_id}")
        source_sha = _canonical_digest(entry)
        if binding["revision"] != entry["revision"]:
            raise ContractError(f"{rule_id}: rule revision drift")
        if binding["source_sha256"] != source_sha:
            raise ContractError(f"{rule_id}: rule source digest drift")
        seen.add(rule_id)
        normalized.append({
            "id": rule_id,
            "revision": entry["revision"],
            "severity": entry["severity"],
            "source_sha256": source_sha,
        })
    return sorted(normalized, key=lambda item: item["id"])


def _validate_compile_input(value: Any) -> tuple[
    dict[str, Any], list[str], list[dict[str, Any]], dict[str, Any], dict[str, Any]
]:
    required = {
        "schema_version", "audit_task_id", "delivery_cycle_id", "mode",
        "evidence_type", "target", "selected_rules", "role_payloads",
        "loop_provider_root", "loop_policy",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ContractError("compile input has an unexpected field set")
    if value["schema_version"] != "1.0":
        raise ContractError("unsupported compile input schema_version")
    if not TASK_ID.fullmatch(value["audit_task_id"] or ""):
        raise ContractError("audit_task_id is invalid")
    if not isinstance(value["delivery_cycle_id"], str) or not value["delivery_cycle_id"].strip():
        raise ContractError("delivery_cycle_id must be nonblank")
    mapping = _load_domain_mapping()
    mode_roles = mapping["modeRoleSets"]
    roles = mapping["canonicalRoles"]
    if value["mode"] not in mode_roles:
        raise ContractError("mode is invalid")
    if not isinstance(value["evidence_type"], str) or not value["evidence_type"].strip():
        raise ContractError("evidence_type must be nonblank")
    target = value["target"]
    if (not isinstance(target, dict)
            or set(target) != {"path", "tree_algorithm", "tree_sha256"}
            or not isinstance(target["path"], str)
            or not target["path"].startswith("/")
            or target["tree_algorithm"] != "foundation-resource-closure-v1"
            or not HEX64.fullmatch(target["tree_sha256"] or "")):
        raise ContractError("target binding is invalid")
    actual_target = _target_closure(Path(target["path"]))
    if actual_target["digest"] != target["tree_sha256"]:
        raise ContractError("frozen target closure digest drift")
    payloads = value["role_payloads"]
    if not isinstance(payloads, dict) or set(payloads) != set(roles):
        raise ContractError("role_payloads must bind all six canonical roles exactly once")
    if any(not isinstance(payloads[role], dict) for role in roles):
        raise ContractError("every role payload must be an object")
    policy = value["loop_policy"]
    policy_fields = {
        "task_phases", "planning_depth", "agent_nesting_depth", "max_concurrency",
        "max_total_agents", "max_repair_cycles", "max_gate_repair_cycles",
    }
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        raise ContractError("loop_policy must explicitly bind every Loop planning and repair field")
    if (not isinstance(policy["task_phases"], list) or not policy["task_phases"]
            or not all(isinstance(item, str) and item.strip() for item in policy["task_phases"])):
        raise ContractError("loop_policy.task_phases must be a non-empty string array")
    for field in policy_fields - {"task_phases"}:
        minimum = 0 if field in {"max_repair_cycles", "max_gate_repair_cycles"} else 1
        if not isinstance(policy[field], int) or isinstance(policy[field], bool) or policy[field] < minimum:
            raise ContractError(f"loop_policy.{field} is invalid")
    provider_root = Path(value["loop_provider_root"])
    loop_contract = _load_loop_contract(provider_root, mapping)
    return (
        value,
        mode_roles[value["mode"]],
        _validate_selected_rules(value["selected_rules"]),
        mapping,
        loop_contract,
    )


def compile_loop_audit(input_path: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise ContractError(f"output directory already exists: {output_dir}")
    value, active_roles, selected_rules, mapping, loop_contract = _validate_compile_input(
        _read_json(input_path)
    )
    output_dir.mkdir(parents=True)

    tasks = []
    payload_files = []
    prompt_files = []
    active_set = set(active_roles)
    for ordinal, role in enumerate(active_roles, start=1):
        workflow_task_id = f"{value['audit_task_id']}.{ordinal:02d}.{role}"
        dependencies = [
            {"node_id": f"{value['audit_task_id']}.{active_roles.index(dep) + 1:02d}.{dep}",
             "dependency_type": "data"}
            for dep in mapping["roleDependencies"][role] if dep in active_set
        ]
        payload_name = f"{ordinal:02d}-{role}.json"
        prompt_name = f"{ordinal:02d}-{role}.md"
        tasks.append({
            "workflow_task_id": workflow_task_id,
            "parent_id": None,
            "depends_on": dependencies,
            "write_set": [],
            "read_set": [],
            "prompt_path": f"prompts/{prompt_name}",
            "task_phases": list(value["loop_policy"]["task_phases"]),
            "payload_ref": {"path": f"node-payloads/{payload_name}"},
        })
        payload_files.append({
            "node_id": workflow_task_id,
            "path": payload_name,
            "content": {
                "schema_version": "1.0",
                "audit_task_id": value["audit_task_id"],
                "delivery_cycle_id": value["delivery_cycle_id"],
                "role": role,
                "mode": value["mode"],
                "evidence_type": value["evidence_type"],
                "target": value["target"],
                "selected_rules": selected_rules,
                "role_payload": value["role_payloads"][role],
                "output_contract": {
                    "kind": "sfa-role-artifact",
                    "schema_id": "skill-failure-auditor:orchestration:role-artifact:1.1.0",
                },
            },
        })
        prompt_files.append({
            "path": prompt_name,
            "content": (
                f"执行 SFA 语义职责 `{role}`。只读取冻结 payload 与目标证据；"
                "把职责成果写成 payload 指定的 SFA role-artifact。"
                "不得写审计目标，不得给出 Loop 验收决定。\n"
            ),
        })

    group_id = f"{value['audit_task_id']}.audit-group"
    for task in tasks:
        task["parent_id"] = group_id
    payload_files.append({
        "node_id": group_id,
        "path": "audit-group.json",
        "content": {
            "schema_version": "1.0",
            "audit_task_id": value["audit_task_id"],
            "role_task_ids": [task["workflow_task_id"] for task in tasks],
            "responsibility": "coordinate-only; domain acceptance remains outside Loop execution",
        },
    })
    prompt_files.append({
        "path": "audit-group.md",
        "content": (
            "按冻结依赖协调 SFA 职责任务。不得替职责补写成果，不得修改审计目标，"
            "不得写 Loop 验收决定。\n"
        ),
    })
    workflow_source_input = {
        "schema_version": "3.0",
        "delivery_task_id": value["audit_task_id"],
        "business_objective": "执行冻结的 Skill Failure Auditor 六职责审计",
        "topology_shape": "group_task_coordinator",
        "planning_depth": value["loop_policy"]["planning_depth"],
        "agent_nesting_depth": value["loop_policy"]["agent_nesting_depth"],
        "max_concurrency": value["loop_policy"]["max_concurrency"],
        "max_total_agents": value["loop_policy"]["max_total_agents"],
        "max_repair_cycles": value["loop_policy"]["max_repair_cycles"],
        "max_gate_repair_cycles": value["loop_policy"]["max_gate_repair_cycles"],
        "workflow_tasks": tasks,
        "workflow_groups": [{
            "workflow_group_id": group_id,
            "parent_id": None,
            "depends_on": [],
            "task_ids": [task["workflow_task_id"] for task in tasks],
            "child_group_ids": [],
            "prompt_path": "prompts/audit-group.md",
            "write_set": [],
            "read_set": [],
            "payload_ref": {"path": "node-payloads/audit-group.json"},
        }],
        "node_payload_files": payload_files,
        "prompt_files": prompt_files,
    }
    acceptance_source = {
        "criteria": [{
            "criterion_id": "SFA-DOMAIN-001",
            "description": "SFA validates the exact Loop result and role-artifact set without writing Loop acceptance",
            "owner_scope": "client",
            "evidence_mode": "semantic",
            "semantic_evidence_contract": {
                "evidence_kind": "sfa-loop-audit-domain-report",
                "minimum_count": len(active_roles),
                "require_non_empty_files": True,
            },
        }],
    }
    _write_new_json(output_dir / "workflow-source-input.json", workflow_source_input)
    _write_new_json(output_dir / "acceptance-source.json", acceptance_source)
    output_digests = _closure_sha_by_path(
        output_dir, ["workflow-source-input.json", "acceptance-source.json"]
    )
    manifest = {
        "schema_version": "1.0",
        "kind": "sfa-loop-audit-compilation",
        "audit_task_id": value["audit_task_id"],
        "delivery_cycle_id": value["delivery_cycle_id"],
        "mode": value["mode"],
        "target": value["target"],
        "loop_contract": loop_contract,
        "loop_policy": value["loop_policy"],
        "active_roles": active_roles,
        "selected_rules": selected_rules,
        "files": {
            "workflow_source_input": {
                "path": "workflow-source-input.json",
                "sha256": output_digests["workflow-source-input.json"],
            },
            "acceptance_source": {
                "path": "acceptance-source.json",
                "sha256": output_digests["acceptance-source.json"],
            },
        },
    }
    manifest["compilation_digest"] = _canonical_digest(manifest)
    _write_new_json(output_dir / "compilation-manifest.json", manifest)
    return manifest


def _is_relative_regular_binding(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ContractError(f"non-canonical relative evidence path: {raw}")
    candidate = root / path
    if not candidate.is_file() or candidate.is_symlink():
        raise ContractError(f"evidence path is not a regular file: {raw}")
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ContractError(f"evidence path escapes result root: {raw}")
    return candidate


def _validate_artifact(artifact: dict[str, Any], role: str, manifest: dict[str, Any],
                       evidence_root: Path, evidence_digests: dict[str, str]) -> list[dict[str, Any]]:
    schema = _read_json(ROLE_ARTIFACT_SCHEMA)
    require_production_validate(artifact, schema)
    if artifact["task_id"] != manifest["audit_task_id"] or artifact["role"] != role:
        raise ContractError(f"{role}: artifact identity mismatch")
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    if artifact["artifact_sha256"] != _canonical_digest(unsigned):
        raise ContractError(f"{role}: artifact digest mismatch")
    selected = {item["id"]: item for item in manifest["selected_rules"]}
    results = artifact["rule_results"]
    ids = [item["id"] for item in results]
    if len(ids) != len(set(ids)) or any(rule_id not in selected for rule_id in ids):
        raise ContractError(f"{role}: rule result set contains duplicate or unknown ids")
    if role in SEMANTIC_RULE_ROLES and set(ids) != set(selected):
        raise ContractError(f"{role}: selected rule set mismatch")
    for result in results:
        binding = selected[result["id"]]
        if result["revision"] != binding["revision"] or result["severity"] != binding["severity"]:
            raise ContractError(f"{role}: rule revision or severity drift for {result['id']}")
        if result["status"] != "UNCHECKED" and not result["evidence_refs"]:
            raise ContractError(f"{role}: checked rule has no evidence for {result['id']}")
        if (result["status"] == "UNCHECKED" and binding["severity"] in {"critical", "high"}
                and artifact["semantic_status"] == "PASS_WITHIN_FROZEN_SCOPE"):
            raise ContractError(f"{role}: pass exceeds unchecked high-severity evidence")
        for ref in result["evidence_refs"]:
            _is_relative_regular_binding(evidence_root, ref["path"])
            if evidence_digests.get(ref["path"]) != ref["sha256"]:
                raise ContractError(f"{role}: evidence digest mismatch: {ref['path']}")
    for finding in artifact["findings"]:
        if re.fullmatch(r"FM-[0-9]+", finding.get("id", "")):
            raise ContractError(f"{role}: selected rule was misfiled as a finding")
        for ref in finding["evidence_refs"]:
            _is_relative_regular_binding(evidence_root, ref["path"])
            if evidence_digests.get(ref["path"]) != ref["sha256"]:
                raise ContractError(f"{role}: finding evidence digest mismatch: {ref['path']}")
    if SEVERITY_ORDER[artifact["semantic_status"]] > SEVERITY_ORDER[artifact["conclusion_ceiling"]]:
        raise ContractError(f"{role}: semantic status exceeds conclusion ceiling")
    return results


def validate_loop_audit(manifest_path: Path, results_root: Path, output_path: Path) -> dict[str, Any]:
    if output_path.exists():
        raise ContractError(f"output file already exists: {output_path}")
    manifest = _read_json(manifest_path)
    if manifest.get("kind") != "sfa-loop-audit-compilation":
        raise ContractError("compilation manifest kind mismatch")
    unsigned = {key: value for key, value in manifest.items() if key != "compilation_digest"}
    if manifest.get("compilation_digest") != _canonical_digest(unsigned):
        raise ContractError("compilation manifest digest mismatch")
    mapping = _load_domain_mapping()
    recorded_loop_contract = manifest.get("loop_contract")
    if not isinstance(recorded_loop_contract, dict):
        raise ContractError("compilation manifest lacks the Loop provider binding")
    loop_contract = _load_loop_contract(Path(recorded_loop_contract.get("provider_root", "")), mapping)
    if loop_contract != recorded_loop_contract:
        raise ContractError("Loop provider binding drifted after compilation")
    target = Path(manifest["target"]["path"])
    if (manifest["target"].get("tree_algorithm") != "foundation-resource-closure-v1"
            or _target_closure(target)["digest"] != manifest["target"]["tree_sha256"]):
        raise ContractError("frozen target is missing or its tree digest drifted")
    _validate_selected_rules([
        {key: item[key] for key in ("id", "revision", "source_sha256")}
        for item in manifest["selected_rules"]
    ])

    result_paths = _regular_relative_paths(results_root)
    result_digests = _closure_sha_by_path(results_root, result_paths)
    failures = []
    role_reports = []
    seen_files = sorted(path.name for path in results_root.glob("*.delivery-task-result.json")
                        if path.is_file())
    expected_files = sorted(f"{role}.delivery-task-result.json" for role in manifest["active_roles"])
    if seen_files != expected_files:
        failures.append({"code": "LOOP_RESULT_SET_MISMATCH", "expected": expected_files, "actual": seen_files})
    for ordinal, role in enumerate(manifest["active_roles"], start=1):
        path = results_root / f"{role}.delivery-task-result.json"
        if not path.is_file():
            continue
        result = _read_json(path)
        schema_binding = loop_contract["delivery_result_schema"]
        require_production_validate_by_schema_id(
            result,
            schema_binding["$id"],
            expected_sha256=schema_binding["sha256"],
        )
        expected_task_id = f"{manifest['audit_task_id']}.{ordinal:02d}.{role}"
        if result["delivery_task_id"] != expected_task_id:
            failures.append({"code": "LOOP_RESULT_IDENTITY_MISMATCH", "role": role})
            continue
        if result["execution_status"] != "SUCCEEDED" or result["changed_files"]:
            failures.append({"code": "LOOP_RESULT_NOT_READ_ONLY_SUCCEEDED", "role": role})
            continue
        candidates = []
        for raw in result["evidence_paths"]:
            try:
                evidence_path = _is_relative_regular_binding(results_root, raw)
                candidate = _read_json(evidence_path)
                if isinstance(candidate, dict) and candidate.get("role") == role:
                    candidates.append((raw, evidence_path, candidate))
            except ContractError:
                continue
        if len(candidates) != 1:
            failures.append({"code": "ROLE_ARTIFACT_BINDING_MISMATCH", "role": role,
                             "candidate_count": len(candidates)})
            continue
        raw, artifact_path, artifact = candidates[0]
        try:
            _validate_artifact(artifact, role, manifest, results_root, result_digests)
        except ContractError as error:
            failures.append({"code": "ROLE_ARTIFACT_INVALID", "role": role, "detail": str(error)})
            continue
        role_reports.append({
            "role": role,
            "delivery_task_result_sha256": result_digests[path.name],
            "artifact": {"path": raw, "sha256": result_digests[raw]},
            "semantic_status": artifact["semantic_status"],
            "conclusion_ceiling": artifact["conclusion_ceiling"],
        })

    status = "COMPLETE" if not failures else "INCOMPLETE"
    report = {
        "schema_version": "1.0",
        "kind": "sfa-loop-audit-domain-report",
        "status": status,
        "audit_task_id": manifest["audit_task_id"],
        "compilation_digest": manifest["compilation_digest"],
        "roles": role_reports,
        "failures": failures,
        "conclusion_ceiling": ("BLOCKED" if failures else
            max((item["conclusion_ceiling"] for item in role_reports),
                key=lambda value: SEVERITY_ORDER[value]) if role_reports else "BLOCKED"
        ),
        "loop_acceptance_written": False,
    }
    report["report_digest"] = _canonical_digest(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_new_json(output_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    compile_parser = sub.add_parser("compile-loop-audit")
    compile_parser.add_argument("--input", required=True)
    compile_parser.add_argument("--output-dir", required=True)
    validate_parser = sub.add_parser("validate-loop-audit")
    validate_parser.add_argument("--compilation-manifest", required=True)
    validate_parser.add_argument("--results-root", required=True)
    validate_parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "compile-loop-audit":
            result = compile_loop_audit(Path(args.input), Path(args.output_dir))
        else:
            result = validate_loop_audit(
                Path(args.compilation_manifest), Path(args.results_root), Path(args.output)
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status", "COMPLETE") == "COMPLETE" else 1
    except ContractError as error:
        print(json.dumps({"status": "REJECTED", "reason": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

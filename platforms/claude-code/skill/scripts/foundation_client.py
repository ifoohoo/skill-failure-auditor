#!/usr/bin/env python3
"""Thin Python transport to the managed Foundation Quickstart runner.

Validation, Task/Result construction, resource closure, and exchange
verification remain Foundation-owned. This module has no local fallback.
"""
from __future__ import annotations
from functools import lru_cache
import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from common import ContractError

MECHANISMS_CLI = Path(__file__).resolve().parent.parent / "foundation" / "quickstart-profile" / "mechanisms-cli.mjs"
FOUNDATION_PIN = Path(__file__).resolve().parent.parent / "foundation" / "foundation-pin.json"
FOUNDATION_PROJECTION = MECHANISMS_CLI.parent / "foundation-projection.json"
FOUNDATION_SCHEMA_IDS = {
    "attempt-manifest.schema.json",
    "audit-result.schema.json",
    "continuation-package.schema.json",
    "failure-mode.schema.json",
    "source-manifest.schema.json",
    "skill-failure-auditor:orchestration:task-package:2.1.0",
    "skill-failure-auditor:orchestration:result:2.1.0",
    "skill-failure-auditor:orchestration:role-artifact:1.1.0",
}
SCHEMA_NAME_TO_FOUNDATION_ID = {
    "attempt-manifest.schema.json": "attempt-manifest.schema.json",
    "audit-result.schema.json": "audit-result.schema.json",
    "continuation-package.schema.json": "continuation-package.schema.json",
    "failure-mode.schema.json": "failure-mode.schema.json",
    "source-manifest.schema.json": "source-manifest.schema.json",
    "task-package.schema.json": "skill-failure-auditor:orchestration:task-package:2.1.0",
    "result.schema.json": "skill-failure-auditor:orchestration:result:2.1.0",
    "role-artifact.schema.json": "skill-failure-auditor:orchestration:role-artifact:1.1.0",
}


def _required_node_version() -> str:
    try:
        pin = json.loads(FOUNDATION_PIN.read_text(encoding="utf-8"))
        version = pin["runtime"]["node"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ContractError(f"FOUNDATION_RUNTIME_PIN_INVALID: {error}") from error
    if not isinstance(version, str) or re.fullmatch(r"v22\.\d+\.\d+", version) is None:
        raise ContractError(f"FOUNDATION_RUNTIME_PIN_INVALID: expected an exact Node 22 version, got {version!r}")
    return version


def _assert_absolute_real_file(raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: SFA_FOUNDATION_NODE is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: Node path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: Node path contains a symlink: {current}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {error}") from error
    if path != resolved or not path.is_file():
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: Node path must name a canonical regular file")
    return resolved


@lru_cache(maxsize=4)
def _validate_node_runtime(raw: str, required_version: str) -> str:
    executable = _assert_absolute_real_file(raw)
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {error}") from error
    actual = completed.stdout.strip()
    if completed.returncode != 0 or actual != required_version:
        detail = completed.stderr.strip() or actual or f"exit {completed.returncode}"
        raise ContractError(
            f"FOUNDATION_RUNTIME_UNAVAILABLE: Node version must be {required_version}; got {detail}"
        )
    return str(executable)


def _resolve_node_executable(node: str | None = None) -> str:
    raw = node if node is not None else os.environ.get("SFA_FOUNDATION_NODE")
    return _validate_node_runtime(raw or "", _required_node_version())


def _invoke_mechanism(operation: str, params: dict[str, Any], *, node: str | None = None) -> dict[str, Any]:
    executable = _resolve_node_executable(node)
    try:
        completed = subprocess.run(
            [executable, str(MECHANISMS_CLI)],
            input=json.dumps({"operation": operation, "params": params}, ensure_ascii=False),
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as error:
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {error}") from error
    if completed.returncode != 0:
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {completed.stderr.strip() or completed.stdout.strip()}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: mechanisms CLI stdout is not JSON") from error
    if not isinstance(result, dict):
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: mechanisms CLI returned non-object")
    return result

def production_validate(document: Any, schema: dict[str, Any], *, node: str | None = None) -> dict[str, Any]:
    schema_id = schema.get("$id")
    if schema_id not in FOUNDATION_SCHEMA_IDS:
        raise ContractError(f"FOUNDATION_SCHEMA_NOT_MANAGED: {schema_id}")
    result = _invoke_mechanism("validate-by-schema-id", {"schemaId":schema_id,"document":document}, node=node)
    errors = result.get("errors", []); first = errors[0] if errors else {}
    path = first.get("instancePath") or "$"
    if path != "$": path = f"${path}"
    if first.get("keyword") == "required" and first.get("params", {}).get("missingProperty"):
        path += f"/{first['params']['missingProperty']}"
    return {"accepted":result.get("valid") is True,
            "path":None if result.get("valid") is True else path,
            "category":None if result.get("valid") is True else first.get("keyword", "unknown"),
            "details":[] if result.get("valid") is True else errors,
            "authority":"foundation"}

def require_production_validate(document: Any, schema: dict[str, Any], *, node: str | None = None) -> None:
    """Reject a managed document unless the Foundation validator accepts it."""
    result = production_validate(document, schema, node=node)
    if result["accepted"]:
        return
    path = result.get("path") or "$"
    details = result.get("details") or []
    detail = details[0].get("message") if details and isinstance(details[0], dict) else None
    raise ContractError(f"{path}: {detail or result.get('category') or 'schema validation failed'}")


@lru_cache(maxsize=1)
def _managed_consumer_schemas() -> dict[str, dict[str, Any]]:
    try:
        projection = json.loads(FOUNDATION_PROJECTION.read_text(encoding="utf-8"))
        schemas = projection["source"]["consumerSchemas"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ContractError(f"FOUNDATION_PROVENANCE_INVALID: {error}") from error
    if not isinstance(schemas, list):
        raise ContractError("FOUNDATION_PROVENANCE_INVALID: consumerSchemas must be an array")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in schemas:
        if (not isinstance(entry, dict) or not isinstance(entry.get("$id"), str)
                or not isinstance(entry.get("sha256"), str)):
            raise ContractError("FOUNDATION_PROVENANCE_INVALID: malformed consumer schema entry")
        if entry["$id"] in by_id:
            raise ContractError(f"FOUNDATION_PROVENANCE_INVALID: duplicate schema $id {entry['$id']}")
        by_id[entry["$id"]] = entry
    return by_id


def foundation_schema_provenance(schema_id: str) -> dict[str, Any]:
    """Resolve one managed Schema from the adjacent Bundle provenance."""
    entry = _managed_consumer_schemas().get(schema_id)
    if entry is None:
        raise ContractError(f"FOUNDATION_SCHEMA_NOT_MANAGED: {schema_id}")
    return dict(entry)


def require_production_validate_by_schema_id(
    document: Any,
    schema_id: str,
    *,
    expected_sha256: str,
    node: str | None = None,
) -> None:
    """Validate through an exact provider-to-Bundle provenance binding."""
    provenance = foundation_schema_provenance(schema_id)
    if provenance.get("sha256") != expected_sha256:
        raise ContractError(f"FOUNDATION_SCHEMA_PROVENANCE_DRIFT: {schema_id}")
    result = _invoke_mechanism(
        "validate-by-schema-id",
        {"schemaId": schema_id, "document": document},
        node=node,
    )
    if result.get("valid") is True:
        return
    errors = result.get("errors", [])
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    instance_path = first.get("instancePath") or "$"
    if instance_path != "$":
        instance_path = f"${instance_path}"
    missing = first.get("params", {}).get("missingProperty")
    if first.get("keyword") == "required" and missing:
        instance_path += f"/{missing}"
    raise ContractError(
        f"{instance_path}: {first.get('message') or first.get('keyword') or 'schema validation failed'}"
    )

def foundation_digest_document(document: Any, *, node: str | None = None) -> str:
    return str(_invoke_mechanism(
        "digest-document", {"document": document}, node=node,
    )["digest"])

def foundation_resource_closure(workspace_root: str, resources: list[dict[str, Any]],
                                *, node: str | None = None) -> dict[str, Any]:
    return _invoke_mechanism(
        "resource-closure", {"root": workspace_root, "resources": resources}, node=node,
    )

def foundation_file_sha256(path: Path, *, node: str | None = None) -> str:
    """Raw-byte sha256 of one contained file via a single-resource closure."""
    target = Path(path)
    closure = foundation_resource_closure(
        str(target.parent.resolve()),
        [{"path": target.name, "role": "input"}],
        node=node,
    )
    resources = closure.get("resources") or []
    if not resources or not isinstance(resources[0], dict) or not isinstance(resources[0].get("sha256"), str):
        raise ContractError("FOUNDATION_CLOSURE_INVALID: resource-closure returned no usable digest")
    return resources[0]["sha256"]

def foundation_prepare_exchange(workspace_root: str, *, observation_path: str, operation_id: str,
                                method: str, parameters: dict[str, Any] | None = None,
                                run: str = "", stage: str = "", attempt: int = 1,
                                node: str | None = None) -> dict[str, Any]:
    options = {"observationPath":observation_path,"operationId":operation_id,"method":method,
               "run":run,"stage":stage,"attempt":attempt}
    if parameters: options["parameters"] = parameters
    return _invoke_mechanism("create-task", {"root": workspace_root, **options}, node=node)

def foundation_wrap_result(task: dict[str, Any], *, state: str = "succeeded", summary: str = "",
                           outputs: list[dict[str, Any]] | None = None,
                           evidence: list[dict[str, Any]] | None = None,
                           domain_result: Any = None, errors: list[dict[str, Any]] | None = None,
                           node: str | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {"task":task,"state":state}
    for name,value in (("summary",summary),("outputs",outputs),("evidence",evidence),
                       ("domainResult",domain_result),("errors",errors)):
        if value is not None and value != "" and value != []: options[name] = value
    return _invoke_mechanism("wrap-result", options, node=node)

def foundation_verify_exchange(workspace_root: str, *, task: dict[str, Any],
                               result: dict[str, Any], node: str | None = None) -> dict[str, Any]:
    return _invoke_mechanism(
        "verify-exchange", {"root": workspace_root, "task": task, "result": result}, node=node,
    )


# Inline Node transport that drives the managed publishFileExclusive against
# the bound bundle runner. The runner URL is spliced in at runtime via
# json.dumps so the script itself stays free of host-specific paths.
_PUBLISH_TRANSPORT = r'''
import { publishFileExclusive } from __RUNNER_URL__;

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
  const request = JSON.parse(new TextDecoder("utf-8").decode(Buffer.concat(chunks)));
  const { root, relPath, dataBase64, mode } = request;
  const data = Buffer.from(dataBase64, "base64");
  const receipt = await publishFileExclusive(root, relPath, data, { mode });
  process.stdout.write(JSON.stringify({ ok: true, receipt }) + "\n");
}

main().catch((cause) => {
  process.stderr.write(JSON.stringify({
    ok: false,
    error: {
      name: cause?.name ?? "Error",
      code: cause?.code ?? null,
      kind: cause?.details?.kind ?? null,
      message: cause?.message ?? String(cause),
    },
  }) + "\n");
  process.exitCode = 2;
});
'''

# Stable kinds the transport may surface from the managed harness (SFC2004
# details.kind values observed for publishFileExclusive; the full set is kept
# to fail closed on any path-domain refusal).
_FOUNDATION_PUBLISH_REFUSED_KINDS = {
    "exclusive-publish-conflict",
    "invalid-root",
    "unsafe-state-entry",
    "symlink-escape",
    "realpath-escape",
    "path-traversal",
    "invalid-path",
}


def foundation_publish_file_exclusive(path: Path, content: bytes, mode: int = 0o600,
                                      *, node: str | None = None) -> None:
    """Exclusively publish one file with the managed Foundation harness.

    Consumer-retained shell (W4.L3): the wrapper keeps the local contract
    surface - automatic parent-directory creation and the 0o600 default mode -
    while the atomic exclusive write, post-commit byte/mode/identity
    verification, and directory fsync are entirely Foundation-owned. There is
    no local fallback writer; any transport or harness failure refuses the
    write (fail-closed). The target is passed as root=parent with a bare
    filename relPath: the harness canonicalizes the parent (realpath), which
    reproduces the local writer's behavior for symlinked parents, and its own
    containment checks cover the target itself.
    """
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise ContractError(f"refusing to overwrite existing path: {target}")
    executable = _resolve_node_executable(node)
    target.parent.mkdir(parents=True, exist_ok=True)
    script = _PUBLISH_TRANSPORT.replace("__RUNNER_URL__", json.dumps(str(MECHANISMS_CLI.parent / "runner.mjs")))
    payload = json.dumps({
        "root": str(target.parent),
        "relPath": target.name,
        "dataBase64": base64.b64encode(content).decode("ascii"),
        "mode": mode,
    }, ensure_ascii=False)
    try:
        completed = subprocess.run(
            [executable, "--input-type=module", "-e", script],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {error}") from error
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        parsed: Any = None
        try:
            parsed = json.loads(stderr.splitlines()[-1])
        except json.JSONDecodeError:
            pass
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            error = parsed["error"]
            kind = error.get("kind")
            message = error.get("message") or stderr
            if kind == "exclusive-publish-conflict":
                raise ContractError(f"refusing to overwrite existing path: {target}")
            if kind in _FOUNDATION_PUBLISH_REFUSED_KINDS:
                raise ContractError(f"FOUNDATION_PUBLISH_REFUSED: {kind}: {message}")
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {stderr or completed.stdout.strip()}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: transport stdout is not JSON") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: transport returned non-ok")

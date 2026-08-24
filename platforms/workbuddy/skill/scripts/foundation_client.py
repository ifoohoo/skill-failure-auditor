#!/usr/bin/env python3
"""Thin Python transport to the managed Foundation Quickstart runner.

Schema validation, resource closure, digesting and exclusive publication remain
Foundation-owned. This module has no local fallback and no target executor.
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
}
SCHEMA_NAME_TO_FOUNDATION_ID = {
    "attempt-manifest.schema.json": "attempt-manifest.schema.json",
    "audit-result.schema.json": "audit-result.schema.json",
    "continuation-package.schema.json": "continuation-package.schema.json",
    "failure-mode.schema.json": "failure-mode.schema.json",
    "source-manifest.schema.json": "source-manifest.schema.json",
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

# Inline Node transport that drives the managed publishFileExclusive against
# the bound bundle runner. The runner URL is spliced in at runtime via
# json.dumps so the script itself stays free of host-specific paths. Parent
# directory creation and the exclusive no-replace contract are entirely
# runner-owned publish-file-exclusive(createParents); this module carries no local fallback
# semantics.
_PUBLISH_TRANSPORT = r'''
import { publishFileExclusive } from __RUNNER_URL__;

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
  const request = JSON.parse(new TextDecoder("utf-8").decode(Buffer.concat(chunks)));
  const { root, relPath, dataBase64, mode } = request;
  const data = Buffer.from(dataBase64, "base64");
  const receipt = await publishFileExclusive(root, relPath, data, { createParents: true, mode });
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


def _raise_root_to_existing_ancestor(target: Path) -> tuple[Path, str]:
    """Raise the publish root to the nearest existing ancestor of the target.

    The runner's createParents option creates missing directories below the
    root, but the root itself must already exist (strictTarget realpaths the
    root). The wrapper therefore walks up to the first existing ancestor and
    passes the remaining path as a deep relPath; this is pure path
    computation - no mkdir, no target-existence pre-check, no local kind
    mapping. The exclusive no-replace contract and all containment checks stay
    entirely Foundation-owned.
    """
    cursor = target.parent
    relative = target.name
    while not cursor.exists():
        relative = f"{cursor.name}/{relative}"
        cursor = cursor.parent
    return cursor, relative


def foundation_publish_file_exclusive(path: Path, content: bytes, mode: int = 0o600,
                                      *, node: str | None = None) -> None:
    """Exclusively publish one file through the managed Foundation harness.

    Direct-call shell (D3): the wrapper carries no local semantics anymore -
    automatic parent-directory creation (createParents: true) and the
    exclusive no-replace contract are entirely Foundation-owned, and the
    0o600 default mode is the call-site default passed through explicitly.
    There is no local fallback writer; any transport or harness failure
    refuses the write (fail-closed). The target is passed as root=nearest
    existing ancestor + deep relPath: the harness canonicalizes the root
    (realpath), creates the missing parent chain below it, and its
    containment checks cover every component of the target path.
    """
    target = Path(path)
    root, relative = _raise_root_to_existing_ancestor(target)
    executable = _resolve_node_executable(node)
    script = _PUBLISH_TRANSPORT.replace("__RUNNER_URL__", json.dumps(str(MECHANISMS_CLI.parent / "runner.mjs")))
    payload = json.dumps({
        "root": str(root),
        "relPath": relative,
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
            raise ContractError(f"FOUNDATION_PUBLISH_REFUSED: {message}")
        raise ContractError(f"FOUNDATION_RUNTIME_UNAVAILABLE: {stderr or completed.stdout.strip()}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: transport stdout is not JSON") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: transport returned non-ok")

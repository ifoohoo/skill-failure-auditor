#!/usr/bin/env python3
"""连接受管 Foundation Quickstart 运行时的薄传输桥接层。

Schema 结构校验、资源闭包、规范化摘要与独占文件发布完全由锁定的 Foundation Bundle 承担；
本模块不携带本地回退校验器或目标技能执行器，遇到软链接或版本漂移的 Node 路径直接拒绝。
"""
from __future__ import annotations
from functools import lru_cache
import base64
import json
import os
import re
import shutil
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


_RANGE_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|=)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_EXACT_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    match = _EXACT_VERSION_RE.fullmatch(raw.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _parse_node_range(expr: str) -> tuple[tuple[str, tuple[int, int, int]], ...]:
    if not isinstance(expr, str) or not expr.strip():
        raise ContractError(f"FOUNDATION_RUNTIME_PIN_INVALID: empty nodeRange {expr!r}")
    clauses: list[tuple[str, tuple[int, int, int]]] = []
    for token in expr.strip().split():
        match = _RANGE_CLAUSE_RE.fullmatch(token)
        if match is None:
            raise ContractError(f"FOUNDATION_RUNTIME_PIN_INVALID: invalid nodeRange clause {token!r}")
        op = match.group(1) or "="
        bound = (
            int(match.group(2)),
            int(match.group(3) or 0),
            int(match.group(4) or 0),
        )
        clauses.append((op, bound))
    return tuple(clauses)


def _satisfies_range(
    version: tuple[int, int, int],
    clauses: tuple[tuple[str, tuple[int, int, int]], ...],
) -> bool:
    for op, bound in clauses:
        if op == ">=" and not (version >= bound):
            return False
        if op == "<=" and not (version <= bound):
            return False
        if op == ">" and not (version > bound):
            return False
        if op == "<" and not (version < bound):
            return False
        if op == "=" and not (version == bound):
            return False
    return True


def _required_node_range() -> str:
    try:
        pin = json.loads(FOUNDATION_PIN.read_text(encoding="utf-8"))
        runtime = pin["runtime"]
        node_range = runtime.get("nodeRange")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ContractError(f"FOUNDATION_RUNTIME_PIN_INVALID: {error}") from error
    if not isinstance(node_range, str) or not node_range.strip():
        raise ContractError(
            f"FOUNDATION_RUNTIME_PIN_INVALID: expected runtime.nodeRange, got {node_range!r}"
        )
    _parse_node_range(node_range)
    return node_range.strip()


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


@lru_cache(maxsize=8)
def _validate_node_runtime(raw: str, required_range: str) -> str:
    clauses = _parse_node_range(required_range)
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
    parsed = _parse_node_version(actual) if completed.returncode == 0 else None
    if parsed is None or not _satisfies_range(parsed, clauses):
        detail = completed.stderr.strip() or actual or f"exit {completed.returncode}"
        raise ContractError(
            f"FOUNDATION_RUNTIME_UNAVAILABLE: Node version must satisfy {required_range}; got {detail}"
        )
    return str(executable)


def _discover_node_from_path(required_range: str) -> str:
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate_raw = shutil.which("node", path=entry)
        if not candidate_raw:
            continue
        try:
            resolved = str(Path(candidate_raw).resolve(strict=True))
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            return _validate_node_runtime(resolved, required_range)
        except ContractError:
            continue
    raise ContractError(
        f"FOUNDATION_RUNTIME_UNAVAILABLE: SFA_FOUNDATION_NODE is unset and no node on PATH satisfies {required_range}"
    )


def resolve_node_executable(node: str | None = None) -> str:
    required_range = _required_node_range()
    if node is not None:
        return _validate_node_runtime(node, required_range)
    env_node = os.environ.get("SFA_FOUNDATION_NODE")
    if env_node is not None:
        return _validate_node_runtime(env_node, required_range)
    return _discover_node_from_path(required_range)


_resolve_node_executable = resolve_node_executable


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


def _validation_failure_message(result: dict[str, Any]) -> str:
    errors = result["errors"]
    first = errors[0]
    instance_path = first.get("instancePath") or "$"
    if instance_path != "$":
        instance_path = f"${instance_path}"
    missing = first.get("params", {}).get("missingProperty")
    if first.get("keyword") == "required" and missing:
        instance_path += f"/{missing}"
    return (
        f"{instance_path}: "
        f"{first.get('message') or first.get('keyword') or 'schema validation failed'}"
    )


def _require_validation_result_shape(result: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {"valid", "errors"}:
        raise ContractError(f"FOUNDATION_BATCH_RESULT_INVALID: {label} validation result is malformed")
    if not isinstance(result["valid"], bool) or not isinstance(result["errors"], list):
        raise ContractError(f"FOUNDATION_BATCH_RESULT_INVALID: {label} validation result is malformed")
    if result["valid"] is True and result["errors"]:
        raise ContractError(f"FOUNDATION_BATCH_RESULT_INVALID: {label} valid result carries errors")
    if result["valid"] is False and not result["errors"]:
        raise ContractError(f"FOUNDATION_BATCH_RESULT_INVALID: {label} invalid result carries no errors")
    for error in result["errors"]:
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("keyword"), str)
            or not isinstance(error.get("instancePath"), str)
            or not isinstance(error.get("schemaPath"), str)
            or not isinstance(error.get("message"), str)
            or not isinstance(error.get("params"), dict)
        ):
            raise ContractError(f"FOUNDATION_BATCH_RESULT_INVALID: {label} error entry is malformed")
    return result


def require_production_validate_many(
    documents: list[Any],
    schema: dict[str, Any],
    *,
    node: str | None = None,
) -> list[dict[str, Any]]:
    """Validate an ordered document batch through one Foundation invocation.

    The bridge owns transport binding only. Foundation remains the schema
    authority; this function verifies that every returned item is bound to its
    input position and schema id before accepting the batch.
    """
    schema_id = schema.get("$id") if isinstance(schema, dict) else None
    if schema_id not in FOUNDATION_SCHEMA_IDS:
        raise ContractError(f"FOUNDATION_SCHEMA_NOT_MANAGED: {schema_id}")
    if not isinstance(documents, list) or not documents:
        raise ContractError("FOUNDATION_BATCH_REQUEST_INVALID: documents must be a non-empty list")
    requests = [
        {"schemaId": schema_id, "document": document}
        for document in documents
    ]
    response = _invoke_mechanism(
        "validate-many-by-schema-id",
        {"requests": requests},
        node=node,
    )
    if set(response) != {"results"} or not isinstance(response.get("results"), list):
        raise ContractError("FOUNDATION_BATCH_RESULT_INVALID: results must be the only array field")
    results = response["results"]
    if len(results) != len(requests):
        raise ContractError(
            "FOUNDATION_BATCH_RESULT_INVALID: "
            f"result count {len(results)} does not match input count {len(requests)}"
        )
    validated: list[dict[str, Any]] = []
    for index, (request, item) in enumerate(zip(requests, results)):
        label = f"result[{index}]"
        if not isinstance(item, dict) or set(item) != {"inputIndex", "schemaId", "result"}:
            raise ContractError(f"FOUNDATION_BATCH_RESULT_INVALID: {label} is malformed")
        if (
            not isinstance(item["inputIndex"], int)
            or isinstance(item["inputIndex"], bool)
            or item["inputIndex"] != index
        ):
            raise ContractError(
                f"FOUNDATION_BATCH_RESULT_INVALID: {label} inputIndex does not bind input order"
            )
        if item["schemaId"] != request["schemaId"]:
            raise ContractError(
                f"FOUNDATION_BATCH_RESULT_INVALID: {label} schemaId does not bind the request"
            )
        validation = _require_validation_result_shape(item["result"], label=label)
        if validation["valid"] is not True:
            raise ContractError(f"$[{index}]: {_validation_failure_message(validation)}")
        validated.append(validation)
    return validated


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
    details = result.get("details") or []
    if details and isinstance(details[0], dict):
        raise ContractError(_validation_failure_message({"valid": False, "errors": details}))
    path = result.get("path") or "$"
    raise ContractError(f"{path}: {result.get('category') or 'schema validation failed'}")


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


def foundation_read_file_strict(
    root: str,
    rel_path: str,
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Read one contained file through the managed Harness readFileStrict."""
    return _invoke_mechanism(
        "read-file-strict",
        {"root": root, "path": rel_path, "encoding": "utf8"},
        node=node,
    )


def foundation_resolve_contained(
    root: str,
    rel_path: str,
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Reject paths that escape the caller-supplied root via resolveContained."""
    return _invoke_mechanism(
        "resolve-contained",
        {"root": root, "path": rel_path},
        node=node,
    )


PROFESSIONAL_CONCLUSION_SCHEMA_ID = (
    "https://contracts.skill-family.example/v1/professional-conclusion.json"
)
SESSION_TRACE_BUNDLE_SCHEMA_ID = (
    "https://contracts.skill-family.example/v1/session-trace-bundle.json"
)


def foundation_validate_session_trace_bundle(
    document: Any,
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Validate a SessionTraceBundle (2020-12 schema + cross-field invariants) via Foundation."""
    result = _invoke_mechanism(
        "validate-session-trace-bundle",
        {"document": document},
        node=node,
    )
    if not isinstance(result, dict) or not isinstance(result.get("valid"), bool) or not isinstance(result.get("errors"), list):
        raise ContractError("FOUNDATION_BATCH_RESULT_INVALID: session-trace-bundle validation result is malformed")
    return result


def foundation_verify_session_trace_bundle_seal(
    document: Any,
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Verify SessionTraceBundle schema, cross-field invariants, and canonical seal digest via Foundation."""
    result = _invoke_mechanism(
        "verify-session-trace-bundle-seal",
        {"document": document},
        node=node,
    )
    if not isinstance(result, dict) or not isinstance(result.get("valid"), bool) or not isinstance(result.get("errors"), list):
        raise ContractError("FOUNDATION_BATCH_RESULT_INVALID: verify-session-trace-bundle-seal result is malformed")
    return result


def foundation_verify_raw_locator_slice(
    root: str,
    rel_path: str,
    *,
    byte_offset: int,
    byte_length: int,
    expected_slice_sha256: str,
    node: str | None = None,
) -> dict[str, Any]:
    """Verify a raw transcript byte slice under root via Foundation readFileStrict + sha256."""
    result = _invoke_mechanism(
        "verify-raw-locator-slice",
        {
            "root": root,
            "path": rel_path,
            "byteOffset": byte_offset,
            "byteLength": byte_length,
            "expectedSliceSha256": expected_slice_sha256,
        },
        node=node,
    )
    if not isinstance(result, dict) or not isinstance(result.get("valid"), bool):
        raise ContractError("FOUNDATION_BATCH_RESULT_INVALID: verify-raw-locator-slice result is malformed")
    return result


def require_session_trace_bundle(document: Any, *, node: str | None = None) -> dict[str, Any]:
    """Reject a SessionTraceBundle unless Foundation accepts both schema and invariants."""
    result = foundation_validate_session_trace_bundle(document, node=node)
    if result["valid"] is True:
        if not isinstance(document, dict):
            raise ContractError("session-trace-bundle rejected: document is not an object")
        return document
    raise ContractError(f"session-trace-bundle rejected: {_validation_failure_message(result)}")


def foundation_validate_professional_conclusion(
    document: Any,
    *,
    node: str | None = None,
) -> dict[str, Any]:
    """Validate a common proof through the Bundle's validate-by-schema-id bridge.

    The official professional-conclusion Schema is admitted at Bundle build time.
    This function only names that $id; it does not copy Ajv or return Node-rewritten
    document bytes.
    """
    foundation_schema_provenance(PROFESSIONAL_CONCLUSION_SCHEMA_ID)
    result = _invoke_mechanism(
        "validate-by-schema-id",
        {"schemaId": PROFESSIONAL_CONCLUSION_SCHEMA_ID, "document": document},
        node=node,
    )
    return _require_validation_result_shape(result, label="professional-conclusion")


def require_professional_conclusion(document: Any, *, node: str | None = None) -> dict[str, Any]:
    """Reject a common proof unless Foundation accepts the public schema.

    On success return the caller-supplied Python object, not validator output.
    """
    result = foundation_validate_professional_conclusion(document, node=node)
    if result["valid"] is True:
        if not isinstance(document, dict):
            raise ContractError("professional-conclusion rejected: document is not an object")
        return document
    raise ContractError(f"professional-conclusion rejected: {_validation_failure_message(result)}")

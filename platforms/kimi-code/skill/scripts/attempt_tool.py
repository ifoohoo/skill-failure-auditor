#!/usr/bin/env python3
"""Create, record, seal, and verify append-only technical attempts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    ContractError,
    canonical_json_bytes,
    load_json,
    normalize_relative_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
    validate_schema,
    write_bytes_exclusive,
    write_json_exclusive,
)


SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST_SCHEMA = SCRIPT_DIR.parent / "references" / "attempt-manifest.schema.json"
ATTEMPT_ID_RE = re.compile(r"^ATTEMPT-[A-Z0-9][A-Z0-9._-]+$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
OUTCOMES = {
    "FAILED",
    "BLOCKED",
    "CANDIDATE_SUBMITTED",
    "SELF_AUDIT_SUBMITTED",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def unsigned_digest(value: dict[str, Any], digest_field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(digest_field, None)
    return sha256_bytes(canonical_json_bytes(unsigned))


def create_attempt(args: argparse.Namespace) -> dict[str, Any]:
    if ATTEMPT_ID_RE.fullmatch(args.attempt_id) is None:
        raise ContractError("attempt id must match ATTEMPT-[A-Z0-9][A-Z0-9._-]+")
    require_sha256(args.candidate_sha256, "candidate sha256")
    require_sha256(args.criteria_commitment_sha256, "criteria commitment sha256")
    write_set = sorted({normalize_relative_path(path) for path in args.write_path})
    if not write_set:
        raise ContractError("at least one --write-path is required")

    args.root.mkdir(parents=True, exist_ok=True)
    attempt = args.root / args.attempt_id
    try:
        attempt.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ContractError(f"attempt already exists: {attempt}") from error
    (attempt / "records").mkdir(mode=0o700)
    (attempt / "blobs").mkdir(mode=0o700)

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "attempt_id": args.attempt_id,
        "candidate_sha256": args.candidate_sha256,
        "criteria_commitment_sha256": args.criteria_commitment_sha256,
        "allowed_write_set": write_set,
        "created_at": args.created_at or now_iso(),
        "created_by": args.created_by,
    }
    manifest["manifest_sha256"] = unsigned_digest(manifest, "manifest_sha256")
    schema = load_json(MANIFEST_SCHEMA)
    validate_schema(manifest, schema, schema)
    write_json_exclusive(attempt / "manifest.json", manifest)
    return {"status": "CREATED", "attempt": str(attempt), **manifest}


def verify_manifest(attempt: Path) -> dict[str, Any]:
    manifest = load_json(attempt / "manifest.json")
    schema = load_json(MANIFEST_SCHEMA)
    validate_schema(manifest, schema, schema)
    if manifest["manifest_sha256"] != unsigned_digest(manifest, "manifest_sha256"):
        raise ContractError("attempt manifest self digest mismatch")
    if attempt.name != manifest["attempt_id"]:
        raise ContractError("attempt directory name does not match manifest id")
    return manifest


def verify_record(record_path: Path, attempt: Path) -> dict[str, Any]:
    record = load_json(record_path)
    expected_keys = {
        "schema_version",
        "record_id",
        "kind",
        "artifact_name",
        "blob_path",
        "artifact_size",
        "artifact_sha256",
        "recorded_at",
        "record_sha256",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise ContractError(f"record has unexpected field set: {record_path}")
    if record["record_sha256"] != unsigned_digest(record, "record_sha256"):
        raise ContractError(f"record self digest mismatch: {record_path}")
    require_sha256(record["artifact_sha256"], "artifact sha256")
    if record["blob_path"] != f"blobs/{record['artifact_sha256']}":
        raise ContractError(f"record blob path is not content addressed: {record_path}")
    blob = attempt / record["blob_path"]
    if blob.is_symlink() or not blob.is_file():
        raise ContractError(f"record blob missing or non-regular: {blob}")
    if blob.stat().st_size != record["artifact_size"]:
        raise ContractError(f"record blob size mismatch: {blob}")
    if sha256_file(blob) != record["artifact_sha256"]:
        raise ContractError(f"record blob digest mismatch: {blob}")
    return record


def list_records(attempt: Path) -> list[dict[str, Any]]:
    record_dir = attempt / "records"
    records: list[dict[str, Any]] = []
    for path in sorted(record_dir.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or re.fullmatch(r"\d{4}-[a-z][a-z0-9_-]{1,63}\.json", path.name) is None:
            raise ContractError(f"unexpected record path: {path}")
        records.append(verify_record(path, attempt))
    return records


def record_references(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": record["record_id"],
            "kind": record["kind"],
            "artifact_sha256": record["artifact_sha256"],
            "record_sha256": record["record_sha256"],
        }
        for record in records
    ]


def record_artifact(args: argparse.Namespace) -> dict[str, Any]:
    attempt = args.attempt
    verify_manifest(attempt)
    if (attempt / "seal.json").exists() or (attempt / "seal.json").is_symlink():
        raise ContractError("attempt is already sealed")
    if KIND_RE.fullmatch(args.kind) is None:
        raise ContractError("record kind must be lowercase alphanumeric with _ or -")
    if args.artifact.is_symlink() or not args.artifact.is_file():
        raise ContractError("artifact must be a regular file")

    existing = list_records(attempt)
    digest = sha256_file(args.artifact)
    blob = attempt / "blobs" / digest
    if blob.exists():
        if blob.is_symlink() or not blob.is_file() or sha256_file(blob) != digest:
            raise ContractError(f"existing content-addressed blob is invalid: {blob}")
    else:
        write_bytes_exclusive(blob, args.artifact.read_bytes(), mode=0o400)

    record_id = f"REC-{len(existing) + 1:04d}"
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "record_id": record_id,
        "kind": args.kind,
        "artifact_name": args.artifact.name,
        "blob_path": f"blobs/{digest}",
        "artifact_size": args.artifact.stat().st_size,
        "artifact_sha256": digest,
        "recorded_at": args.recorded_at or now_iso(),
    }
    record["record_sha256"] = unsigned_digest(record, "record_sha256")
    record_path = attempt / "records" / f"{len(existing) + 1:04d}-{args.kind}.json"
    write_json_exclusive(record_path, record, mode=0o400)
    return {"status": "RECORDED", **record}


def verify_seal(
    attempt: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    seal_path = attempt / "seal.json"
    if not seal_path.exists():
        return None
    seal = load_json(seal_path)
    expected_keys = {
        "schema_version",
        "attempt_id",
        "manifest_sha256",
        "outcome",
        "reason_code",
        "sealed_at",
        "record_count",
        "records",
        "records_set_sha256",
        "seal_sha256",
    }
    if not isinstance(seal, dict) or set(seal) != expected_keys:
        raise ContractError("seal has unexpected field set")
    if seal["seal_sha256"] != unsigned_digest(seal, "seal_sha256"):
        raise ContractError("seal self digest mismatch")
    if seal["outcome"] not in OUTCOMES:
        raise ContractError("seal outcome is not allowed")
    if seal["attempt_id"] != manifest["attempt_id"]:
        raise ContractError("seal attempt id does not match manifest")
    if seal["manifest_sha256"] != manifest["manifest_sha256"]:
        raise ContractError("seal does not bind the current attempt manifest")
    expected_record_refs = record_references(records)
    if seal["records"] != expected_record_refs or seal["record_count"] != len(records):
        raise ContractError("seal record set does not match attempt records")
    expected_set_sha = sha256_bytes(canonical_json_bytes(expected_record_refs))
    if seal["records_set_sha256"] != expected_set_sha:
        raise ContractError("seal records set digest mismatch")
    return seal


def verify_layout(attempt: Path) -> None:
    allowed_root = {"manifest.json", "records", "blobs", "seal.json"}
    for child in attempt.iterdir():
        if child.name not in allowed_root:
            raise ContractError(f"unexpected path in attempt: {child}")
        if child.is_symlink():
            raise ContractError(f"symlink is not allowed in attempt: {child}")
    for blob in (attempt / "blobs").iterdir():
        if blob.is_symlink() or not blob.is_file() or re.fullmatch(r"[0-9a-f]{64}", blob.name) is None:
            raise ContractError(f"unexpected blob path: {blob}")


def verify_attempt(
    attempt: Path,
    expected_seal_file_sha256: str | None = None,
) -> dict[str, Any]:
    if attempt.is_symlink() or not attempt.is_dir():
        raise ContractError("attempt must be a real directory")
    verify_layout(attempt)
    manifest = verify_manifest(attempt)
    records = list_records(attempt)
    seal = verify_seal(attempt, manifest, records)
    record_refs = record_references(records)
    seal_file_sha256 = sha256_file(attempt / "seal.json") if seal else None
    if seal is None and expected_seal_file_sha256 is not None:
        raise ContractError("cannot bind an open attempt to a seal file digest")
    if seal is not None and expected_seal_file_sha256 is not None:
        require_sha256(expected_seal_file_sha256, "expected seal file sha256")
        if seal_file_sha256 != expected_seal_file_sha256:
            raise ContractError("seal file digest does not match external frozen digest")
        status = "VERIFIED_SEALED_BOUND"
        seal_binding_status = "EXTERNAL_DIGEST_MATCH"
    elif seal is not None:
        status = "SEALED_PENDING_EXTERNAL_BINDING"
        seal_binding_status = "UNBOUND"
    else:
        status = "VERIFIED_OPEN"
        seal_binding_status = "NOT_APPLICABLE"
    return {
        "schema_version": "1.0",
        "status": status,
        "attempt_id": manifest["attempt_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "record_count": len(records),
        "records_set_sha256": sha256_bytes(canonical_json_bytes(record_refs)),
        "sealed": seal is not None,
        "outcome": seal["outcome"] if seal else None,
        "seal_sha256": seal["seal_sha256"] if seal else None,
        "seal_file_sha256": seal_file_sha256,
        "seal_binding_status": seal_binding_status,
        "write_set_verification_status": "NOT_VERIFIED",
        "formal_acceptance_eligible": False,
    }


def seal_attempt(args: argparse.Namespace) -> dict[str, Any]:
    if args.outcome not in OUTCOMES:
        raise ContractError(f"outcome must be one of {sorted(OUTCOMES)}")
    if REASON_RE.fullmatch(args.reason_code) is None:
        raise ContractError("reason code must be uppercase alphanumeric with underscores")
    attempt = args.attempt
    manifest = verify_manifest(attempt)
    if (attempt / "seal.json").exists() or (attempt / "seal.json").is_symlink():
        raise ContractError("attempt is already sealed")
    records = list_records(attempt)
    record_refs = record_references(records)
    seal: dict[str, Any] = {
        "schema_version": "1.0",
        "attempt_id": manifest["attempt_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "outcome": args.outcome,
        "reason_code": args.reason_code,
        "sealed_at": args.sealed_at or now_iso(),
        "record_count": len(records),
        "records": record_refs,
        "records_set_sha256": sha256_bytes(canonical_json_bytes(record_refs)),
    }
    seal["seal_sha256"] = unsigned_digest(seal, "seal_sha256")
    write_json_exclusive(attempt / "seal.json", seal, mode=0o400)
    verification = verify_attempt(attempt)
    if verification["status"] != "SEALED_PENDING_EXTERNAL_BINDING":
        raise ContractError("new seal did not enter external-binding pending state")
    return {
        "status": "SEALED_PENDING_EXTERNAL_BINDING",
        **seal,
        "seal_file_sha256": verification["seal_file_sha256"],
        "seal_binding_status": "UNBOUND",
        "formal_acceptance_eligible": False,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subcommands = root.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--attempt-id", required=True)
    create.add_argument("--candidate-sha256", required=True)
    create.add_argument("--criteria-commitment-sha256", required=True)
    create.add_argument("--write-path", action="append", required=True)
    create.add_argument("--created-by", required=True)
    create.add_argument("--created-at")

    record = subcommands.add_parser("record")
    record.add_argument("--attempt", type=Path, required=True)
    record.add_argument("--kind", required=True)
    record.add_argument("--artifact", type=Path, required=True)
    record.add_argument("--recorded-at")

    seal = subcommands.add_parser("seal")
    seal.add_argument("--attempt", type=Path, required=True)
    seal.add_argument("--outcome", required=True)
    seal.add_argument("--reason-code", required=True)
    seal.add_argument("--sealed-at")

    verify = subcommands.add_parser("verify")
    verify.add_argument("--attempt", type=Path, required=True)
    verify.add_argument("--expected-seal-file-sha256")
    verify.add_argument("--output", type=Path)
    return root


def emit(value: dict[str, Any], output: Path | None = None) -> None:
    if output:
        write_json_exclusive(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "create":
            emit(create_attempt(args))
        elif args.command == "record":
            emit(record_artifact(args))
        elif args.command == "seal":
            emit(seal_attempt(args))
        elif args.command == "verify":
            emit(
                verify_attempt(args.attempt, args.expected_seal_file_sha256),
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

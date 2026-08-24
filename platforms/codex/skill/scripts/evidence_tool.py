#!/usr/bin/env python3
"""Build and verify content-addressed evidence indexes and coverage ledgers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from common import (
    ContractError,
    list_regular_files,
    load_json,
    load_jsonl,
    sha256_bytes,
    write_json_exclusive,
)
from foundation_client import foundation_digest_document, foundation_file_sha256
from registry_tool import DEFAULT_REGISTRY, validate_selection_artifact


DEFAULT_CHUNK_SIZE = 64 * 1024


def logical_path(input_root: Path, file_path: Path) -> str:
    if input_root.is_file():
        return input_root.name
    return file_path.relative_to(input_root).as_posix()


def build_index(input_root: Path, chunk_size: int) -> dict[str, Any]:
    if chunk_size < 1024 or chunk_size > 1024 * 1024:
        raise ContractError("chunk size must be between 1024 and 1048576 bytes")
    files = list_regular_files(input_root)
    if not files:
        raise ContractError("evidence input must contain at least one regular file")
    file_records: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    total_bytes = 0
    global_index = 0

    for file_path in files:
        relative = logical_path(input_root, file_path)
        size = file_path.stat().st_size
        digest = foundation_file_sha256(file_path)
        file_records.append({"path": relative, "size": size, "sha256": digest})
        total_bytes += size
        offset = 0
        with file_path.open("rb") as handle:
            while True:
                content = handle.read(chunk_size)
                if not content:
                    break
                end = offset + len(content)
                chunks.append(
                    {
                        "id": f"CHUNK-{global_index:06d}",
                        "index": global_index,
                        "file_path": relative,
                        "start": offset,
                        "end": end,
                        "length": len(content),
                        "sha256": sha256_bytes(content),
                    }
                )
                global_index += 1
                offset = end
        if offset != size:
            raise ContractError(f"short read while indexing {file_path}")

    if total_bytes == 0 or not chunks:
        raise ContractError("evidence input must contain at least one non-empty chunk")
    file_set_sha = foundation_digest_document(file_records)
    index: dict[str, Any] = {
        "schema_version": "1.0",
        "input_kind": "file" if input_root.is_file() else "directory",
        "logical_root": input_root.name,
        "chunk_size": chunk_size,
        "file_count": len(file_records),
        "total_bytes": total_bytes,
        "file_set_sha256": file_set_sha,
        "files": file_records,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
    index["index_sha256"] = foundation_digest_document(index)
    return index


def validate_index_document(index: Any) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "input_kind",
        "logical_root",
        "chunk_size",
        "file_count",
        "total_bytes",
        "file_set_sha256",
        "files",
        "chunk_count",
        "chunks",
        "index_sha256",
    }
    if not isinstance(index, dict) or set(index) != expected_keys:
        raise ContractError("evidence index has an unexpected field set")
    unsigned = dict(index)
    declared_digest = unsigned.pop("index_sha256")
    if declared_digest != foundation_digest_document(unsigned):
        raise ContractError("evidence index self digest mismatch")
    if index["schema_version"] != "1.0" or index["input_kind"] not in {
        "file",
        "directory",
    }:
        raise ContractError("evidence index header is invalid")
    if (
        not isinstance(index["chunk_size"], int)
        or isinstance(index["chunk_size"], bool)
        or index["chunk_size"] < 1024
        or index["chunk_size"] > 1024 * 1024
    ):
        raise ContractError("evidence index chunk size is invalid")
    if not isinstance(index["files"], list) or not isinstance(index["chunks"], list):
        raise ContractError("evidence index collections are invalid")
    if index["file_count"] != len(index["files"]):
        raise ContractError("evidence index file count mismatch")
    if index["chunk_count"] != len(index["chunks"]):
        raise ContractError("evidence index chunk count mismatch")
    if (
        index["file_count"] <= 0
        or index["total_bytes"] <= 0
        or index["chunk_count"] <= 0
    ):
        raise ContractError("evidence index cannot describe an empty evidence set")

    file_map: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for record in index["files"]:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise ContractError("evidence index file record has an unexpected field set")
        path = record["path"]
        if not isinstance(path, str) or not path or path in file_map:
            raise ContractError("evidence index contains an invalid or duplicate file path")
        if (
            not isinstance(record["size"], int)
            or isinstance(record["size"], bool)
            or record["size"] < 0
        ):
            raise ContractError(f"evidence index file size is invalid: {path}")
        if (
            not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in record["sha256"])
        ):
            raise ContractError(f"evidence index file digest is invalid: {path}")
        file_map[path] = record
        total_bytes += record["size"]
    if total_bytes != index["total_bytes"]:
        raise ContractError("evidence index total byte count mismatch")
    if index["file_set_sha256"] != foundation_digest_document(index["files"]):
        raise ContractError("evidence index file set digest mismatch")

    chunks_by_file: dict[str, list[dict[str, Any]]] = {path: [] for path in file_map}
    for expected_index, chunk in enumerate(index["chunks"]):
        if not isinstance(chunk, dict) or set(chunk) != {
            "id",
            "index",
            "file_path",
            "start",
            "end",
            "length",
            "sha256",
        }:
            raise ContractError("evidence index chunk has an unexpected field set")
        if chunk["id"] != f"CHUNK-{expected_index:06d}" or chunk["index"] != expected_index:
            raise ContractError("evidence index chunk ids or indexes are not contiguous")
        if chunk["file_path"] not in file_map:
            raise ContractError(f"evidence index chunk references unknown file: {chunk['id']}")
        if (
            not isinstance(chunk["start"], int)
            or not isinstance(chunk["end"], int)
            or not isinstance(chunk["length"], int)
            or min(chunk["start"], chunk["end"], chunk["length"]) < 0
            or chunk["end"] - chunk["start"] != chunk["length"]
            or chunk["length"] <= 0
            or chunk["length"] > index["chunk_size"]
        ):
            raise ContractError(f"evidence index chunk range is invalid: {chunk['id']}")
        if (
            not isinstance(chunk["sha256"], str)
            or len(chunk["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in chunk["sha256"])
        ):
            raise ContractError(f"evidence index chunk digest is invalid: {chunk['id']}")
        chunks_by_file[chunk["file_path"]].append(chunk)
    for path, chunks in chunks_by_file.items():
        offset = 0
        for chunk in chunks:
            if chunk["start"] != offset:
                raise ContractError(f"evidence index chunk coverage has a gap or overlap: {path}")
            offset = chunk["end"]
        if offset != file_map[path]["size"]:
            raise ContractError(f"evidence index chunk coverage does not conserve file size: {path}")
    return index


def verify_index(input_root: Path, index_path: Path) -> dict[str, Any]:
    index = validate_index_document(load_json(index_path))
    recomputed = build_index(input_root, index["chunk_size"])
    if recomputed != index:
        raise ContractError("input bytes, chunk set, or index metadata drifted")
    return index


def resolve_file(input_root: Path, relative: str) -> Path:
    if input_root.is_file():
        if relative != input_root.name:
            raise ContractError("file index logical path mismatch")
        return input_root
    path = input_root / relative
    resolved_root = input_root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ContractError(f"indexed file escapes input root: {relative}") from error
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"indexed path is no longer a regular file: {relative}")
    return path


def extract_chunk(input_root: Path, index_path: Path, chunk_id: str) -> bytes:
    index = verify_index(input_root, index_path)
    matches = [chunk for chunk in index["chunks"] if chunk["id"] == chunk_id]
    if len(matches) != 1:
        raise ContractError(f"chunk id must resolve exactly once: {chunk_id}")
    chunk = matches[0]
    file_path = resolve_file(input_root, chunk["file_path"])
    with file_path.open("rb") as handle:
        handle.seek(chunk["start"])
        content = handle.read(chunk["length"])
    if len(content) != chunk["length"] or sha256_bytes(content) != chunk["sha256"]:
        raise ContractError(f"chunk bytes drifted: {chunk_id}")
    return content


def search_index(input_root: Path, index_path: Path, needle: bytes) -> dict[str, Any]:
    if not needle:
        raise ContractError("search needle must not be empty")
    index = verify_index(input_root, index_path)
    chunks_by_file: dict[str, list[dict[str, Any]]] = {}
    for chunk in index["chunks"]:
        chunks_by_file.setdefault(chunk["file_path"], []).append(chunk)

    matches: list[dict[str, Any]] = []
    for file_record in index["files"]:
        relative = file_record["path"]
        content = resolve_file(input_root, relative).read_bytes()
        cursor = 0
        while True:
            offset = content.find(needle, cursor)
            if offset < 0:
                break
            end = offset + len(needle)
            chunk_ids = [
                chunk["id"]
                for chunk in chunks_by_file.get(relative, [])
                if chunk["start"] < end and chunk["end"] > offset
            ]
            matches.append(
                {
                    "file_path": relative,
                    "start": offset,
                    "end": end,
                    "chunk_ids": chunk_ids,
                }
            )
            cursor = offset + 1
    result = {
        "schema_version": "1.0",
        "index_sha256": index["index_sha256"],
        "needle_sha256": sha256_bytes(needle),
        "match_count": len(matches),
        "matches": matches,
    }
    result["result_sha256"] = foundation_digest_document(result)
    return result


def _build_coverage_from_verified_inputs(
    index: dict[str, Any],
    selection: dict[str, Any],
    records_path: Path,
) -> tuple[dict[str, Any], int]:
    selected_ids = {
        item["id"] for item in selection["selection_context"]["selected_rules"]
    }
    records = load_jsonl(records_path)
    expected_chunks = {chunk["id"]: chunk for chunk in index.get("chunks", [])}
    if not expected_chunks:
        raise ContractError("coverage cannot be complete for an empty evidence index")
    if not records:
        raise ContractError("coverage requires at least one audited chunk record")
    if len(expected_chunks) != index.get("chunk_count"):
        raise ContractError("index chunk ids are not unique")

    seen: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    unknown: list[str] = []
    digest_mismatches: list[str] = []
    allowed_keys = {"chunk_id", "chunk_sha256", "status", "rule_ids", "finding_count"}
    for record_index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != allowed_keys:
            raise ContractError(f"coverage record {record_index} has an unexpected field set")
        chunk_id = record["chunk_id"]
        if chunk_id in seen:
            duplicates.append(chunk_id)
        else:
            seen[chunk_id] = record
        chunk = expected_chunks.get(chunk_id)
        if chunk is None:
            unknown.append(chunk_id)
            continue
        if record["chunk_sha256"] != chunk["sha256"]:
            digest_mismatches.append(chunk_id)
        if record["status"] != "AUDITED":
            raise ContractError(f"coverage record {chunk_id} is not AUDITED")
        if not isinstance(record["rule_ids"], list) or not record["rule_ids"]:
            raise ContractError(f"coverage record {chunk_id} has invalid rule_ids")
        if (
            not all(isinstance(item, str) and item for item in record["rule_ids"])
            or len(record["rule_ids"]) != len(set(record["rule_ids"]))
        ):
            raise ContractError(f"coverage record {chunk_id} has duplicate or invalid rule_ids")
        actual_rule_ids = set(record["rule_ids"])
        if actual_rule_ids != selected_ids:
            missing_rules = sorted(selected_ids - actual_rule_ids)
            extra_rules = sorted(actual_rule_ids - selected_ids)
            raise ContractError(
                f"coverage record {chunk_id} rule set mismatch; "
                f"missing={missing_rules}, extra={extra_rules}"
            )
        if (
            not isinstance(record["finding_count"], int)
            or isinstance(record["finding_count"], bool)
            or record["finding_count"] < 0
        ):
            raise ContractError(f"coverage record {chunk_id} has invalid finding_count")

    missing = sorted(set(expected_chunks) - set(seen))
    invalid = bool(duplicates or unknown or digest_mismatches)
    incomplete = bool(missing)
    status = "INVALID" if invalid else ("INCOMPLETE" if incomplete else "COMPLETE")
    ledger: dict[str, Any] = {
        "schema_version": "1.0",
        "status": status,
        "index_sha256": index.get("index_sha256"),
        "selection_sha256": selection["selection_sha256"],
        "input_chunk_count": len(expected_chunks),
        "record_count": len(records),
        "unique_audited_count": len(set(seen) & set(expected_chunks)),
        "missing_chunk_ids": missing,
        "duplicate_chunk_ids": sorted(set(duplicates)),
        "unknown_chunk_ids": sorted(set(unknown)),
        "digest_mismatch_chunk_ids": sorted(set(digest_mismatches)),
        "conservation_holds": status == "COMPLETE",
        "records_sha256": foundation_file_sha256(records_path),
    }
    ledger["ledger_sha256"] = foundation_digest_document(ledger)
    return ledger, 0 if status == "COMPLETE" else 2


def build_coverage(
    input_root: Path,
    index_path: Path,
    selection_path: Path,
    registry_path: Path,
    records_path: Path,
) -> tuple[dict[str, Any], int]:
    index = verify_index(input_root, index_path)
    selection, _ = validate_selection_artifact(selection_path, registry_path)
    return _build_coverage_from_verified_inputs(index, selection, records_path)


def emit_json(value: dict[str, Any], output: Path | None) -> None:
    if output:
        write_json_exclusive(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subcommands = root.add_subparsers(dest="command", required=True)

    index = subcommands.add_parser("index")
    index.add_argument("--input", type=Path, required=True)
    index.add_argument("--output", type=Path, required=True)
    index.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)

    verify = subcommands.add_parser("verify")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--index", type=Path, required=True)
    verify.add_argument("--output", type=Path)

    extract = subcommands.add_parser("extract")
    extract.add_argument("--input", type=Path, required=True)
    extract.add_argument("--index", type=Path, required=True)
    extract.add_argument("--chunk-id", required=True)

    search = subcommands.add_parser("search")
    search.add_argument("--input", type=Path, required=True)
    search.add_argument("--index", type=Path, required=True)
    search.add_argument("--needle", required=True)
    search.add_argument("--output", type=Path)

    coverage = subcommands.add_parser("coverage")
    coverage.add_argument("--input", type=Path, required=True)
    coverage.add_argument("--index", type=Path, required=True)
    coverage.add_argument("--selection", type=Path, required=True)
    coverage.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    coverage.add_argument("--records", type=Path, required=True)
    coverage.add_argument("--output", type=Path, required=True)

    strict = subcommands.add_parser("strict-jsonl")
    strict.add_argument("--input", type=Path, required=True)
    strict.add_argument("--output", type=Path)
    return root


def output_inside_input(input_root: Path, output: Path) -> bool:
    if input_root.is_file():
        return output.resolve() == input_root.resolve()
    try:
        output.resolve().relative_to(input_root.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "index":
            if output_inside_input(args.input, args.output):
                raise ContractError("index output must be outside the indexed input")
            write_json_exclusive(args.output, build_index(args.input, args.chunk_size))
            return 0
        if args.command == "verify":
            index = verify_index(args.input, args.index)
            emit_json(
                {
                    "schema_version": "1.0",
                    "status": "VERIFIED",
                    "index_sha256": index["index_sha256"],
                    "file_count": index["file_count"],
                    "chunk_count": index["chunk_count"],
                    "total_bytes": index["total_bytes"],
                },
                args.output,
            )
            return 0
        if args.command == "extract":
            sys.stdout.buffer.write(extract_chunk(args.input, args.index, args.chunk_id))
            return 0
        if args.command == "search":
            emit_json(
                search_index(args.input, args.index, args.needle.encode("utf-8")),
                args.output,
            )
            return 0
        if args.command == "coverage":
            ledger, exit_code = build_coverage(
                args.input,
                args.index,
                args.selection,
                args.registry,
                args.records,
            )
            write_json_exclusive(args.output, ledger)
            return exit_code
        if args.command == "strict-jsonl":
            records = load_jsonl(args.input)
            result = {
                "schema_version": "1.0",
                "status": "VALID",
                "physical_line_count": len(records),
                "parsed_record_count": len(records),
                "input_sha256": foundation_file_sha256(args.input),
            }
            emit_json(result, args.output)
            return 0
        raise ContractError(f"unsupported command: {args.command}")
    except (ContractError, OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

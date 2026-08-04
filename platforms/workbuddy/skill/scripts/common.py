#!/usr/bin/env python3
"""Shared deterministic helpers for skill-failure-auditor tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


class ContractError(ValueError):
    """Raised when an input violates a fail-closed contract."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(text: str, label: str = "JSON") -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except (json.JSONDecodeError, ContractError) as error:
        raise ContractError(f"{label} parse failed: {error}") from error


def load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"JSON input must be a regular file: {path}")
    return strict_json_loads(path.read_text(encoding="utf-8"), str(path))


def load_jsonl(path: Path) -> list[Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"JSONL input must be a regular file: {path}")
    records: list[Any] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line_number, line in enumerate(handle, 1):
            physical = line.rstrip("\r\n")
            if not physical.strip():
                raise ContractError(f"{path}:{line_number}: blank JSONL line")
            records.append(strict_json_loads(physical, f"{path}:{line_number}"))
    return records


def write_bytes_exclusive(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ContractError(f"refusing to overwrite existing path: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError as error:
            raise ContractError(f"refusing to overwrite existing path: {path}") from error
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def write_json_exclusive(path: Path, value: Any, mode: int = 0o600) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    write_bytes_exclusive(path, content.encode("utf-8"), mode=mode)


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("path must be a non-empty string")
    if "\\" in value:
        raise ContractError(f"backslash is not allowed in portable path: {value}")
    candidate = Path(value)
    if candidate.is_absolute():
        raise ContractError(f"path must be relative: {value}")
    parts = candidate.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ContractError(f"path must be canonical without dot segments: {value}")
    normalized = candidate.as_posix()
    if normalized != value:
        raise ContractError(f"path must use canonical POSIX spelling: {value}")
    return normalized


def list_regular_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise ContractError(f"input root must not be a symlink: {root}")
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise ContractError(f"input must be a regular file or directory: {root}")
    files: list[Path] = []
    for current_root, directories, names in os.walk(root, followlinks=False):
        current = Path(current_root)
        for directory in directories:
            if (current / directory).is_symlink():
                raise ContractError(f"symlink directory is not allowed: {current / directory}")
        for name in names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ContractError(f"non-regular input is not allowed: {path}")
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().encode("utf-8"))


def parse_iso_datetime(value: str) -> None:
    if not isinstance(value, str):
        raise ContractError("date-time value must be a string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"invalid date-time: {value}") from error


_SUPPORTED_KEYWORDS = {
    # Metadata (ignored)
    "$schema", "$id", "title", "description", "$defs", "definitions",
    # References
    "$ref",
    # Type
    "type",
    # Generic
    "const", "enum",
    # Object
    "required", "properties", "additionalProperties",
    # Array
    "items", "minItems", "maxItems", "uniqueItems", "contains",
    # String
    "minLength", "pattern", "format",
    # Number
    "minimum", "maximum",
    # Composition / conditional
    "allOf", "anyOf", "oneOf", "not",
    "if", "then", "else",
}


def validate_schema(instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, path: str = "$") -> None:
    """Validate the JSON-Schema subset used by this skill.

    支持关键字：$ref, type (含 union), const, enum, required, properties,
    additionalProperties, items, minItems, maxItems, uniqueItems, contains,
    minLength, pattern, format, minimum, maximum, allOf, anyOf, oneOf, not,
    if/then/else。遇到未实现关键字即 ContractError（失败关闭）。
    """
    root = root or schema

    # 失败关闭：检测未支持关键字
    for keyword in schema:
        if keyword not in _SUPPORTED_KEYWORDS:
            raise ContractError(f"{path}: unsupported schema keyword {keyword!r}")

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ContractError(f"{path}: unsupported schema reference {ref!r}")
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate_schema(instance, target, root, path)
        return

    expected_type = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if expected_type:
        if isinstance(expected_type, list):
            matched = False
            for t in expected_type:
                python_type = type_map.get(t)
                if python_type and isinstance(instance, python_type):
                    if t in {"integer", "number"} and isinstance(instance, bool):
                        continue
                    matched = True
                    break
            if not matched:
                raise ContractError(f"{path}: expected one of {expected_type}")
        else:
            python_type = type_map[expected_type]
            if expected_type in {"integer", "number"} and isinstance(instance, bool):
                raise ContractError(f"{path}: expected {expected_type}")
            if not isinstance(instance, python_type):
                raise ContractError(f"{path}: expected {expected_type}")

    if "const" in schema and instance != schema["const"]:
        raise ContractError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise ContractError(f"{path}: value is not in enum")

    # ── 对象校验 ──
    if isinstance(instance, dict):
        required = schema.get("required", [])
        missing = sorted(set(required) - set(instance))
        if missing:
            raise ContractError(f"{path}: missing required keys {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                raise ContractError(f"{path}: unexpected keys {extras}")
        for key, value in instance.items():
            if key in properties:
                validate_schema(value, properties[key], root, f"{path}.{key}")

    # ── 数组校验 ──
    if isinstance(instance, list):
        minimum = schema.get("minItems")
        if minimum is not None and len(instance) < minimum:
            raise ContractError(f"{path}: expected at least {minimum} items")
        maximum_items = schema.get("maxItems")
        if maximum_items is not None and len(instance) > maximum_items:
            raise ContractError(f"{path}: expected at most {maximum_items} items")
        if schema.get("uniqueItems"):
            seen: set[bytes] = set()
            for item in instance:
                encoded = canonical_json_bytes(item)
                if encoded in seen:
                    raise ContractError(f"{path}: duplicate array item")
                seen.add(encoded)
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                validate_schema(item, item_schema, root, f"{path}[{index}]")
        if "contains" in schema:
            contains_schema = schema["contains"]
            found = False
            for item in instance:
                try:
                    validate_schema(item, contains_schema, root, path)
                    found = True
                    break
                except ContractError:
                    continue
            if not found:
                raise ContractError(f"{path}: no item matches 'contains' schema")

    # ── 字符串校验 ──
    if isinstance(instance, str):
        minimum = schema.get("minLength")
        if minimum is not None and len(instance) < minimum:
            raise ContractError(f"{path}: string shorter than {minimum}")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, instance) is None:
            raise ContractError(f"{path}: string does not match {pattern}")
        if schema.get("format") == "date-time":
            parse_iso_datetime(instance)

    # ── 数值校验 ──
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and instance < minimum:
            raise ContractError(f"{path}: value below minimum {minimum}")
        if maximum is not None and instance > maximum:
            raise ContractError(f"{path}: value above maximum {maximum}")

    # ── 组合关键字 ──
    if "allOf" in schema:
        for index, sub_schema in enumerate(schema["allOf"]):
            validate_schema(instance, sub_schema, root, f"{path}.allOf[{index}]")

    if "anyOf" in schema:
        matched = False
        for sub_schema in schema["anyOf"]:
            try:
                validate_schema(instance, sub_schema, root, path)
                matched = True
                break
            except ContractError:
                continue
        if not matched:
            raise ContractError(f"{path}: no schema in 'anyOf' matched")

    if "oneOf" in schema:
        match_count = 0
        for sub_schema in schema["oneOf"]:
            try:
                validate_schema(instance, sub_schema, root, path)
                match_count += 1
            except ContractError:
                continue
        if match_count != 1:
            raise ContractError(f"{path}: expected exactly 1 match in 'oneOf', got {match_count}")

    if "not" in schema:
        try:
            validate_schema(instance, schema["not"], root, path)
        except ContractError:
            pass  # Expected: 'not' means the sub-schema must fail
        else:
            raise ContractError(f"{path}: instance must not match 'not' schema")

    # ── 条件关键字 if/then/else ──
    if "if" in schema:
        condition_met = True
        try:
            validate_schema(instance, schema["if"], root, path)
        except ContractError:
            condition_met = False
        if condition_met:
            if "then" in schema:
                validate_schema(instance, schema["then"], root, f"{path}.then")
        else:
            if "else" in schema:
                validate_schema(instance, schema["else"], root, f"{path}.else")


def require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError(f"{label} must be a 64-character lowercase SHA-256")
    return value


def ensure_exact_set(actual: Iterable[str], expected: Iterable[str], label: str) -> None:
    actual_set = set(actual)
    expected_set = set(expected)
    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        raise ContractError(f"{label} set mismatch; missing={missing}, extra={extra}")


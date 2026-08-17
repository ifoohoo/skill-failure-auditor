#!/usr/bin/env python3
"""Shared deterministic helpers for skill-failure-auditor tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    """Raised when an input violates a fail-closed contract."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def write_json_exclusive(path: Path, value: Any, mode: int = 0o600) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    from foundation_client import foundation_publish_file_exclusive
    foundation_publish_file_exclusive(path, content.encode("utf-8"), mode=mode)


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


def require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError(f"{label} must be a 64-character lowercase SHA-256")
    return value

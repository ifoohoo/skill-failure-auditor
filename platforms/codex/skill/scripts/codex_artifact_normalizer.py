#!/usr/bin/env python3
"""把 Codex 原生职责输出归一化为带确定性摘要的职责成果。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from common import (
        ContractError,
        load_json,
        write_json_exclusive,
    )
    from foundation_client import foundation_digest_document, foundation_file_sha256, require_production_validate
except ModuleNotFoundError:  # 源码树直接执行；安装投影中 common.py 与本文件同目录。
    source_core_scripts = Path(__file__).resolve().parents[3] / "core" / "scripts"
    sys.path.insert(0, str(source_core_scripts))
    from common import (  # type: ignore[no-redef]
        ContractError,
        load_json,
        write_json_exclusive,
    )
    from foundation_client import (  # type: ignore[no-redef]
        foundation_digest_document,
        foundation_file_sha256,
        require_production_validate,
    )


def _schema_path() -> Path:
    installed = Path(__file__).resolve().parent.parent / "references" / "role-artifact.schema.json"
    if installed.is_file():
        return installed
    source = (
        Path(__file__).resolve().parents[4]
        / "spec"
        / "orchestration"
        / "role-artifact.schema.json"
    )
    if source.is_file():
        return source
    raise ContractError("role-artifact.schema.json is unavailable")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def normalize(task_package_path: Path, role: str, source_path: Path, output_path: Path) -> dict:
    package = load_json(task_package_path)
    if not isinstance(package, dict):
        raise ContractError("task package must be a JSON object")

    expected_package_digest = foundation_digest_document({**package, "package_digest": ""})
    if package.get("package_digest") != expected_package_digest:
        raise ContractError("TASK_PACKAGE_DIGEST_DRIFT")
    if package.get("platform") != "codex":
        raise ContractError("TASK_PACKAGE_PLATFORM_MISMATCH")
    if role not in package.get("expected_roles", []):
        raise ContractError("ROLE_NOT_EXPECTED")

    output_root = package.get("output_root")
    if not isinstance(output_root, str) or not Path(output_root).is_absolute():
        raise ContractError("TASK_PACKAGE_OUTPUT_ROOT_INVALID")
    work_root = (Path(output_root) / "work").resolve()

    if not source_path.is_absolute() or not output_path.is_absolute():
        raise ContractError("ARTIFACT_PATH_MUST_BE_ABSOLUTE")
    if source_path.is_symlink() or not source_path.is_file():
        raise ContractError("SOURCE_ARTIFACT_NOT_REGULAR")
    source_resolved = source_path.resolve(strict=True)
    output_resolved = output_path.resolve(strict=False)
    if not _is_within(source_resolved, work_root):
        raise ContractError("SOURCE_ARTIFACT_PATH_NOT_ALLOWED")
    if not _is_within(output_resolved, work_root):
        raise ContractError("OUTPUT_ARTIFACT_PATH_NOT_ALLOWED")
    if source_resolved == output_resolved:
        raise ContractError("SOURCE_AND_OUTPUT_MUST_DIFFER")
    if output_path.exists() or output_path.is_symlink():
        raise ContractError("OUTPUT_ARTIFACT_ALREADY_EXISTS")

    source_sha256_before = foundation_file_sha256(source_path)
    artifact = load_json(source_path)
    if not isinstance(artifact, dict):
        raise ContractError("source artifact must be a JSON object")

    declared_digest = artifact.pop("artifact_sha256", None)
    computed_digest = foundation_digest_document(artifact)
    normalized = {**artifact, "artifact_sha256": computed_digest}

    schema = load_json(_schema_path())
    if not isinstance(schema, dict):
        raise ContractError("role artifact schema must be a JSON object")
    require_production_validate(normalized, schema)

    if normalized.get("task_id") != package.get("task_id"):
        raise ContractError("ARTIFACT_TASK_ID_MISMATCH")
    if normalized.get("platform") != "codex":
        raise ContractError("ARTIFACT_PLATFORM_MISMATCH")
    if normalized.get("role") != role:
        raise ContractError("ARTIFACT_ROLE_MISMATCH")

    write_json_exclusive(output_path, normalized, mode=0o600)
    source_sha256_after = foundation_file_sha256(source_path)
    if source_sha256_after != source_sha256_before:
        raise ContractError("SOURCE_ARTIFACT_CHANGED_DURING_NORMALIZATION")

    return {
        "status": "NORMALIZED",
        "role": role,
        "source_path": str(source_resolved),
        "source_file_sha256": source_sha256_before,
        "declared_artifact_sha256": declared_digest,
        "artifact_sha256": computed_digest,
        "output_path": str(output_resolved),
        "output_file_sha256": foundation_file_sha256(output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-package", type=Path, required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = normalize(args.task_package, args.role, args.source, args.output)
    except (ContractError, OSError, UnicodeError) as error:
        print(json.dumps({"status": "REJECTED", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

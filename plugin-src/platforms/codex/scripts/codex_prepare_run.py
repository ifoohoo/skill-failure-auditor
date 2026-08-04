#!/usr/bin/env python3
"""Codex prepare-run 适配器：就绪前机械创建原始 rollout 目录。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def engine_path() -> Path:
    installed = Path(__file__).resolve().with_name("orchestration_engine.py")
    if installed.is_file():
        return installed
    source = Path(__file__).resolve().parents[3] / "core" / "scripts" / "orchestration_engine.py"
    return source


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--platform", required=True)
    known, _ = parser.parse_known_args()
    if known.platform != "codex":
        print(json.dumps({"status": "REJECTED", "reason": "INVALID_PLATFORM"}))
        return 1

    completed = subprocess.run(
        [sys.executable, str(engine_path()), "prepare-run", *sys.argv[1:]],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        return completed.returncode

    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print(json.dumps({"status": "REJECTED", "reason": "PREPARE_OUTPUT_INVALID"}))
        return 1
    if response.get("status") != "READY_FOR_ISOLATED_TASKS":
        sys.stdout.write(completed.stdout)
        return 1

    output_root = Path(known.output_root).resolve()
    work = output_root / "work"
    raw = work / "raw"
    try:
        if not work.is_dir() or work.is_symlink():
            raise OSError("work root is not a real directory")
        raw.mkdir()
        if not raw.is_dir() or raw.is_symlink():
            raise OSError("raw root is not a real directory")
    except OSError as error:
        print(json.dumps({
            "status": "REJECTED",
            "reason": "RAW_ROLLOUT_DIRECTORY_CREATE_FAILED",
            "detail": str(error),
        }, ensure_ascii=False))
        return 1

    sys.stdout.write(completed.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

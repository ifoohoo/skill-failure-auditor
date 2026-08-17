#!/usr/bin/env python3
"""Batch E S10/S11: Foundation publishFileExclusive delegation tests.

Cover the consumer-retained contract surface of
foundation_publish_file_exclusive (transport + parent creation + mode) and
the write_json_exclusive byte contract after stripping the local atomic
writer. Fail-closed: no local fallback path exists.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common  # noqa: E402
import foundation_client  # noqa: E402

NODE22 = "/opt/homebrew/Cellar/node@22/22.23.2/bin/node"


@pytest.fixture(autouse=True)
def _node_env(monkeypatch):
    monkeypatch.setenv("SFA_FOUNDATION_NODE", NODE22)
    yield


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def test_absent_target_publishes_exact_bytes_and_default_mode(workdir: Path):
    target = workdir / "manifest.json"
    content = b'{"schema_version": "1.0"}\n'
    foundation_client.foundation_publish_file_exclusive(target, content)
    assert target.read_bytes() == content
    assert (target.stat().st_mode & 0o777) == 0o600


def test_explicit_mode_honored(workdir: Path):
    target = workdir / "seal.json"
    foundation_client.foundation_publish_file_exclusive(target, b"x", mode=0o400)
    assert (target.stat().st_mode & 0o777) == 0o400


def test_missing_parents_created(workdir: Path):
    target = workdir / "a" / "b" / "c" / "deep.json"
    foundation_client.foundation_publish_file_exclusive(target, b"deep")
    assert target.read_bytes() == b"deep"
    assert target.parent.is_dir()


def test_existing_different_bytes_refused(workdir: Path):
    target = workdir / "existing.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"first")
    with pytest.raises(common.ContractError, match="refusing to overwrite existing path"):
        foundation_client.foundation_publish_file_exclusive(target, b"second")
    assert target.read_bytes() == b"first"


def test_existing_identical_bytes_refused_negative_idempotency(workdir: Path):
    target = workdir / "same.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"same")
    with pytest.raises(common.ContractError, match="refusing to overwrite existing path"):
        foundation_client.foundation_publish_file_exclusive(target, b"same")
    assert target.read_bytes() == b"same"


def test_symlink_target_refused(workdir: Path):
    target = workdir / "link.json"
    target.symlink_to("nowhere")
    with pytest.raises(common.ContractError, match="refusing to overwrite existing path"):
        foundation_client.foundation_publish_file_exclusive(target, b"x")
    assert target.is_symlink()


def test_binary_bytes_preserved(workdir: Path):
    target = workdir / "blob.bin"
    content = bytes([0x00, 0x01, 0xFF, 0xFE, 0x80, 0x41])
    foundation_client.foundation_publish_file_exclusive(target, content)
    assert target.read_bytes() == content


def test_runtime_unavailable_fail_closed_no_fallback(workdir: Path, monkeypatch):
    """If the Foundation transport cannot run, refuse - never write locally."""
    target = workdir / "never.json"

    def _boom(*_args, **_kwargs):
        raise common.ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: test injection")

    monkeypatch.setattr(foundation_client, "_resolve_node_executable", _boom)
    with pytest.raises(common.ContractError, match="FOUNDATION_RUNTIME_UNAVAILABLE"):
        foundation_client.foundation_publish_file_exclusive(target, b"x")
    assert not target.exists()


def test_write_json_exclusive_byte_contract(workdir: Path):
    """Serialization must stay canonical (ensure_ascii=False, sort_keys, indent=2) + newline."""
    value = {
        "title": "中文标题 with unicode: 中文 ✓",
        "count": 42,
        "flags": [True, False, None],
        "nested": {"a": 1, "b": 2},
        "empty": {},
    }
    path = workdir / "record.json"
    common.write_json_exclusive(path, value)
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    assert path.read_text(encoding="utf-8") == expected
    assert path.read_bytes().decode("utf-8") == expected
    assert (path.stat().st_mode & 0o777) == 0o600


def test_write_json_exclusive_explicit_mode(workdir: Path):
    path = workdir / "mode.json"
    common.write_json_exclusive(path, {"a": 1}, mode=0o400)
    assert (path.stat().st_mode & 0o777) == 0o400


def test_write_json_exclusive_refuses_existing(workdir: Path):
    path = workdir / "existing.json"
    common.write_json_exclusive(path, {"a": 1})
    with pytest.raises(common.ContractError, match="refusing to overwrite existing path"):
        common.write_json_exclusive(path, {"a": 2})

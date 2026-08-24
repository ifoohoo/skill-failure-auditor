#!/usr/bin/env python3
"""Phase 3 回补：write_json_exclusive 字节契约最小测试集。

D3 删除本文件后，write_json_exclusive 的字节契约（精确序列化形态、
默认/显式 mode、排他拒绝、运行时不可用 fail-closed）在现存测试中
零断言，仅剩语义层间接覆盖。本文件按 phase3-write-json-coverage-survey.md
§5 回补最小集：3 条覆盖 E1-E4（P3A5），E6（运行时不可用 fail-closed
无回退）按 C0-3 裁决于 2026-08-20 回补（本文件第 4 条）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common  # noqa: E402

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


def test_write_json_exclusive_byte_contract(workdir: Path):
    """E1/E2: 序列化形态（ensure_ascii=False/sort_keys/indent=2 + 尾随换行）与默认 mode 0o600。"""
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
    """E3: 显式 mode=0o400 逐字生效。"""
    path = workdir / "mode.json"
    common.write_json_exclusive(path, {"a": 1}, mode=0o400)
    assert (path.stat().st_mode & 0o777) == 0o400


def test_write_json_exclusive_refuses_existing(workdir: Path):
    """E4: 二次写入既有路径 → 传输层排他拒绝，原文件字节不变。"""
    path = workdir / "existing.json"
    common.write_json_exclusive(path, {"a": 1})
    original = path.read_bytes()
    with pytest.raises(common.ContractError, match="refusing to overwrite existing path"):
        common.write_json_exclusive(path, {"a": 2})
    assert path.read_bytes() == original


def test_runtime_unavailable_fail_closed_no_fallback(workdir: Path, monkeypatch):
    """E6: 运行时不可用（node 解析/spawn 不可达）→ ContractError，绝不本地降级写入。

    C0-3 裁决回补（原 test_runtime_unavailable_fail_closed_no_fallback 随 D3 删除，
    phase3-write-json-coverage-survey.md §4 判定 E6 零覆盖）：故障注入
    foundation_client._resolve_node_executable 抛 ContractError（运行时不可用面），
    write_json_exclusive 必须 fail-closed 且目标文件不落盘。
    """
    import foundation_client

    def _inject_unavailable(*_args, **_kwargs):
        raise common.ContractError("FOUNDATION_RUNTIME_UNAVAILABLE: injected for test")

    monkeypatch.setattr(foundation_client, "_resolve_node_executable", _inject_unavailable)
    path = workdir / "unavailable.json"
    with pytest.raises(common.ContractError, match="FOUNDATION_RUNTIME_UNAVAILABLE"):
        common.write_json_exclusive(path, {"a": 1})
    assert not path.exists()

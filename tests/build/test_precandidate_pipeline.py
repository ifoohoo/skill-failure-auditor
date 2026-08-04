"""预候选流水线：派生清单、证书绑定、不可覆盖和隔离正向集成。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = PACKAGE_ROOT.parent.parent
PLATFORM_BUILDER_PATH = PACKAGE_ROOT / "scripts" / "build" / "build_platforms.py"
CANDIDATE_BUILDER_PATH = PACKAGE_ROOT / "scripts" / "release" / "build_public_candidate.py"
GENERATED_PLATFORMS = PACKAGE_ROOT / "generated" / "platforms"
VERSION = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))["version"]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CANDIDATE_BUILDER_PATH), *args],
        cwd=WORKSPACE, capture_output=True, text=True, timeout=120,
    )


def test_claude_prompt_manifest_drift_is_detected_then_mechanically_synced(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builder = load_module("sfa_platform_builder_test", PLATFORM_BUILDER_PATH)
    derived = tmp_path / "claude-prompt-manifest.json"
    derived.write_text('{"stale":true}\n', encoding="utf-8")
    monkeypatch.setattr(builder, "CLAUDE_PROMPT_MANIFEST", derived)

    failures = builder.sync_claude_prompt_manifest(check=True)
    assert [item["reason"] for item in failures] == ["CLAUDE_PROMPT_MANIFEST_DRIFT"]
    assert builder.sync_claude_prompt_manifest(check=False) == []
    assert builder.sync_claude_prompt_manifest(check=True) == []


def test_stage_builder_refuses_second_write_and_preserves_first_manifest(tmp_path: Path) -> None:
    stage = tmp_path / "candidate-stage"
    first = run_builder("--version", VERSION, "--stage-out", str(stage),
                        "--generated-platforms", str(GENERATED_PLATFORMS))
    assert first.returncode == 0, first.stdout + first.stderr
    manifest = stage / "candidate-manifest.json"
    before = sha256_file(manifest)

    second = run_builder("--version", VERSION, "--stage-out", str(stage),
                         "--generated-platforms", str(GENERATED_PLATFORMS))
    assert second.returncode != 0
    assert json.loads(second.stdout)["reason"] == "CANDIDATE_OR_STAGE_EXISTS"
    assert sha256_file(manifest) == before


def test_atomic_noreplace_rename_preserves_existing_target(tmp_path: Path) -> None:
    builder = load_module("sfa_candidate_builder_atomic", CANDIDATE_BUILDER_PATH)
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "candidate-manifest.json").write_text("new\n", encoding="utf-8")
    existing = target / "candidate-manifest.json"
    existing.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        builder.atomic_rename_noreplace(source, target)
    assert existing.read_text(encoding="utf-8") == "existing\n"
    assert (source / "candidate-manifest.json").is_file()


@pytest.mark.parametrize(
    "field,expected_code",
    [
        ("sourceTreeDigest", "PREFLIGHT_CERTIFICATE_SOURCE_DIGEST"),
        ("generatedTreeDigest", "PREFLIGHT_CERTIFICATE_GENERATED_DIGEST"),
        ("expectedCandidateDigest", "PREFLIGHT_CERTIFICATE_CANDIDATE_DIGEST"),
    ],
)
def test_certificate_rejects_bound_content_drift(
        tmp_path: Path, field: str, expected_code: str) -> None:
    builder = load_module(f"sfa_candidate_builder_{field}", CANDIDATE_BUILDER_PATH)
    _, _, candidate_digest = builder.collect_payload(GENERATED_PLATFORMS)
    certificate = {
        "schemaVersion": "1.0",
        "version": VERSION,
        "sourceTreeDigest": builder.source_tree_digest(),
        "generatedTreeDigest": builder.tree_digest(GENERATED_PLATFORMS),
        "expectedCandidateDigest": candidate_digest,
        "testPlanDigest": "a" * 64,
        "allChecksPassed": True,
        "results": [],
    }
    certificate["certificateSha256"] = builder.certificate_digest(certificate)
    cert_path = tmp_path / "preflight.json"
    cert_path.write_text(json.dumps(certificate), encoding="utf-8")
    assert builder.validate_certificate(
        cert_path, version=VERSION, candidate_digest=candidate_digest,
        generated_platforms=GENERATED_PLATFORMS,
    ) == []

    certificate[field] = "0" * 64
    certificate["certificateSha256"] = builder.certificate_digest(certificate)
    cert_path.write_text(json.dumps(certificate), encoding="utf-8")
    codes = {item["code"] for item in builder.validate_certificate(
        cert_path, version=VERSION, candidate_digest=candidate_digest,
        generated_platforms=GENERATED_PLATFORMS,
    )}
    assert expected_code in codes


def test_existing_real_candidate_is_rejected_before_write() -> None:
    # 隔离预检的递归包测试没有真实候选；外层测试负责验证真实不可覆盖边界。
    if os.environ.get("SFA_PREFLIGHT_ACTIVE") == "1":
        assert not (PACKAGE_ROOT / "dist" / "candidate" / VERSION).exists()
        return

    manifest = PACKAGE_ROOT / "dist" / "candidate" / VERSION / "candidate-manifest.json"
    assert manifest.is_file()
    before = sha256_file(manifest)
    result = run_builder("--version", VERSION, "--certificate", "/tmp/does-not-matter.json")
    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "CANDIDATE_OR_STAGE_EXISTS"
    assert sha256_file(manifest) == before


def test_preflight_rejects_existing_candidate_without_certificate_output(tmp_path: Path) -> None:
    if os.environ.get("SFA_PREFLIGHT_ACTIVE") == "1":
        assert not (PACKAGE_ROOT / "dist" / "candidate" / VERSION).exists()
        return

    certificate = tmp_path / "must-not-exist.json"
    result = subprocess.run(
        ["node", "scripts/preflight.mjs", "--version", VERSION,
         "--certificate-out", str(certificate)],
        cwd=WORKSPACE, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "TARGET_CANDIDATE_ALREADY_EXISTS"
    assert not certificate.exists()


def test_preflight_passes_in_isolated_workspace_without_materializing_candidate(
        tmp_path: Path) -> None:
    # preflight 自身运行完整包测试；环境标志防止该正向集成再次递归启动 preflight。
    if os.environ.get("SFA_PREFLIGHT_ACTIVE") == "1":
        return

    isolated = tmp_path / "workspace"
    skip = shutil.ignore_patterns(
        "node_modules", ".git", "__pycache__", ".pytest_cache", "dist",
        "generated", ".codex", "evidence", "control", "versions",
        "authorizations", "artifacts",
    )
    shutil.copytree(WORKSPACE, isolated, ignore=skip)

    certificate = isolated / "preflight-certificate.json"
    result = subprocess.run(
        ["node", "scripts/preflight.mjs", "--version", VERSION,
         "--certificate-out", str(certificate)],
        cwd=isolated, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    output = json.loads(result.stdout)
    assert output["verdict"] == "PASS"
    assert certificate.is_file()
    assert not (isolated / "packages" / "skill-failure-auditor" / "dist" /
                "candidate" / VERSION).exists()
    cert = json.loads(certificate.read_text(encoding="utf-8"))
    assert cert["allChecksPassed"] is True
    assert cert["expectedCandidateDigest"] == output["expectedCandidateDigest"]

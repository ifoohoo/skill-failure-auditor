"""R4 Release snapshot 测试：正向 + 7 类负向用例 + 附加边界用例。

每个测试用 /tmp 合成夹具候选树 + project.yaml，经 CLI 调用 snapshot.mjs。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[4]
SNAPSHOT_SCRIPT = WORKSPACE / "scripts" / "snapshot.mjs"


@pytest.fixture()
def tmp_evidence_dir(monkeypatch, tmp_path):
    """将失败证据目录重定向到 pytest tmp，避免污染真实 evidence/。"""
    d = tmp_path / "snapshot-failures"
    d.mkdir()
    monkeypatch.setenv("SFA_SNAPSHOT_FAILURE_EVIDENCE_DIR", str(d))
    return d


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def compute_candidate_digest(files: list[dict]) -> str:
    """按 path\\x00sha256\\n 排序拼接重算候选摘要。"""
    sorted_files = sorted(files, key=lambda f: f["path"])
    acc = b"".join(
        f["path"].encode() + b"\x00" + f["sha256"].encode() + b"\n"
        for f in sorted_files
    )
    return sha256_bytes(acc)


def build_candidate(tmpdir: Path, version: str, file_contents: dict[str, bytes]) -> tuple[Path, dict]:
    """在 tmpdir 下创建候选目录与 manifest。返回 (candidate_root, manifest_dict)。"""
    cand = tmpdir / "candidate" / version
    cand.mkdir(parents=True, exist_ok=True)

    manifest_files = []
    for rel_path, content in sorted(file_contents.items()):
        fp = cand / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
        manifest_files.append({
            "path": rel_path,
            "sha256": sha256_bytes(content),
            "source": f"skill-failure-auditor/{rel_path}",
        })

    digest = compute_candidate_digest(manifest_files)
    manifest = {
        "schemaVersion": "1.0",
        "version": version,
        "product": "skill-failure-auditor",
        "publicRepo": "ifoohoo/skill-failure-auditor",
        "fileCount": len(manifest_files),
        "files": manifest_files,
        "candidateDigest": digest,
    }
    (cand / "candidate-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return cand, manifest


def build_project_yaml(tmpdir: Path, version: str, content_files: list[dict],
                       generated_metadata: list[dict] | None = None) -> Path:
    """写最小 project.yaml。"""
    prefix = f"packages/skill-failure-auditor/dist/candidate/{version}/"
    py = tmpdir / "project.yaml"
    lines = [
        "apiVersion: release-skill/v1",
        "kind: ReleaseProject",
        "project:",
        "  name: test",
        "releaseUnits:",
        "- id: test",
        "  publicFiles:",
    ]
    for cf in content_files:
        lines.append(f"  - from: {prefix}{cf['from']}")
        lines.append(f"    to: {cf['to']}")
        lines.append(f"    mode: preserve")

    if generated_metadata is not None:
        lines.append("  generatedMetadata:")
        for gm in generated_metadata:
            lines.append(f"  - from: {prefix}{gm['from']}")
            lines.append(f"    to: {gm['to']}")
            lines.append(f"    mode: preserve")

    lines.extend([
        "  requiredPublicFiles: []",
        "hooks:",
        "  test:",
        "    command: [echo]",
        "verificationGates: []",
        "policy:",
        "  forbiddenPaths: []",
        "  forbiddenContentPatterns: []",
    ])
    py.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return py


def build_v03_project_yaml(tmpdir: Path, version: str,
                           content_files: list[dict]) -> Path:
    """release-skill v0.3：候选清单以显式 metadata 目标映射。"""
    prefix = f"packages/skill-failure-auditor/dist/candidate/{version}/"
    py = tmpdir / "project-v03.yaml"
    lines = [
        "apiVersion: release-skill/v1",
        "kind: ReleaseProject",
        "project:",
        "  name: test",
        "releaseUnits:",
        "- id: test",
        "  publicFiles:",
    ]
    for cf in content_files:
        lines.append(f"  - from: {prefix}{cf['from']}")
        lines.append("    sourceScope: workspace")
        lines.append(f"    to: {cf['to']}")
        lines.append("    mode: preserve")
    lines.extend([
        f"  - from: {prefix}candidate-manifest.json",
        "    sourceScope: workspace",
        "    to: generated-metadata/candidate-manifest.json",
        "    mode: preserve",
        "  requiredPublicFiles: []",
        "hooks: {}",
        "verificationGates: []",
        "policy:",
        "  forbiddenPaths: []",
        "  forbiddenContentPatterns: []",
    ])
    py.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return py


def run_snapshot(candidate_root: Path, project_yaml: Path, version: str,
                 extra_args: list[str] | None = None, workspace: Path | None = None) -> dict:
    """运行 snapshot.mjs 并返回 JSON 结果。"""
    ws = workspace or WORKSPACE
    cmd = [
        "node", str(SNAPSHOT_SCRIPT),
        "--candidate-version", version,
        "--candidate-root", str(candidate_root),
        "--project-yaml", str(project_yaml),
    ]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(ws), timeout=30,
    )
    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError:
        return {
            "verdict": "PARSE_ERROR",
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
        }, result.returncode


# ── 标准夹具 ──────────────────────────────────────────────────────────

SAMPLE_FILES = {
    "README.md": b"# Test Package\n",
    "LICENSE": b"MIT License\n",
    "src/main.py": b"print('hello')\n",
}

VERSION = "1.0.0-test"


def make_standard_setup(tmp_path: Path, extra_content_files: list[dict] | None = None,
                         extra_file_contents: dict[str, bytes] | None = None,
                         generated_metadata: list[dict] | None = "default"):
    """创建标准候选 + project.yaml。"""
    files = dict(SAMPLE_FILES)
    if extra_file_contents:
        files.update(extra_file_contents)

    cand, manifest = build_candidate(tmp_path, VERSION, files)

    content_files = [{"from": p, "to": p} for p in sorted(files.keys())]
    if extra_content_files:
        content_files.extend(extra_content_files)

    if generated_metadata == "default":
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
    else:
        gm = generated_metadata

    py = build_project_yaml(tmp_path, VERSION, content_files, gm)
    return cand, manifest, py


# ── 正向测试 ──────────────────────────────────────────────────────────

class TestReleaseSnapshotPositive:
    def test_pass_verdict(self, tmp_path: Path):
        """集合相等、逐文件一致 → verdict PASS，退出 0。"""
        cand, manifest, py = make_standard_setup(tmp_path)
        result, rc = run_snapshot(cand, py, VERSION)
        assert result["verdict"] == "PASS", f"failures: {result.get('failures')}"
        assert rc == 0
        assert result["fileCount"] == len(SAMPLE_FILES)
        assert result["generatedMetadataCount"] == 1

    def test_release_skill_v03_inline_metadata_is_not_content(self, tmp_path: Path):
        """v0.3 显式 metadata 目标仍保持候选内容集合守恒。"""
        cand, manifest = build_candidate(tmp_path, VERSION, SAMPLE_FILES)
        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES)]
        py = build_v03_project_yaml(tmp_path, VERSION, content_files)

        result, rc = run_snapshot(cand, py, VERSION)

        assert rc == 0, result
        assert result["verdict"] == "PASS"
        assert result["candidateDigest"] == manifest["candidateDigest"]
        assert result["fileCount"] == len(SAMPLE_FILES)
        assert result["generatedMetadataCount"] == 1
        assert result["candidateDigest"] == manifest["candidateDigest"]

    def test_tmpdir_cleaned(self, tmp_path: Path):
        """默认运行后临时目录已清理。"""
        cand, manifest, py = make_standard_setup(tmp_path)
        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 0
        release_root = result.get("releaseRoot")
        assert release_root is not None
        # releaseRoot 的父目录是 tmpDir，应已被清理
        tmp_dir = Path(release_root).parent
        assert not tmp_dir.exists(), f"临时目录未清理: {tmp_dir}"

    def test_keep_preserves_tmpdir(self, tmp_path: Path):
        """--keep 保留目录。"""
        cand, manifest, py = make_standard_setup(tmp_path)
        out_dir = tmp_path / "output"
        result, rc = run_snapshot(cand, py, VERSION, extra_args=["--keep", "--out", str(out_dir)])
        assert rc == 0
        assert out_dir.exists()
        assert (out_dir / "snapshot-manifest.json").exists()


# ── 负向测试（R-AC-13 逐项）───────────────────────────────────────────

@pytest.mark.usefixtures("tmp_evidence_dir")
class TestReleaseSnapshotNegative:
    def test_release_missing_file(self, tmp_path: Path):
        """候选有、Release 无 → RELEASE_MISSING_FILE。"""
        files = dict(SAMPLE_FILES)
        files["extra-in-candidate.txt"] = b"extra"
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # project.yaml 只包含 SAMPLE_FILES 的文件，缺少 extra-in-candidate.txt
        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        assert result["verdict"] == "FAIL"
        codes = [f["code"] for f in result["failures"]]
        assert "RELEASE_MISSING_FILE" in codes

    def test_release_extra_file(self, tmp_path: Path):
        """Release 有、候选无 → RELEASE_EXTRA_FILE。"""
        cand, manifest = build_candidate(tmp_path, VERSION, SAMPLE_FILES)

        # project.yaml 多了一个文件
        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        content_files.append({"from": "nonexistent.txt", "to": "nonexistent.txt"})
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "RELEASE_EXTRA_FILE" in codes

    def test_duplicate_release_destination(self, tmp_path: Path):
        """同目标重复 → DUPLICATE_RELEASE_DESTINATION。"""
        files = dict(SAMPLE_FILES)
        files["alt/readme.md"] = b"alt readme"
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        content_files = [{"from": p, "to": p} for p in sorted(files.keys())]
        # 增加一条重复目标
        content_files.append({"from": "alt/readme.md", "to": "README.md"})
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "DUPLICATE_RELEASE_DESTINATION" in codes

    def test_snapshot_digest_mismatch(self, tmp_path: Path):
        """错摘要 → SNAPSHOT_DIGEST_MISMATCH。"""
        files = dict(SAMPLE_FILES)
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # 篡改文件内容但不更新 manifest
        (cand / "README.md").write_bytes(b"# Tampered\n")

        content_files = [{"from": p, "to": p} for p in sorted(files.keys())]
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "SNAPSHOT_DIGEST_MISMATCH" in codes

    def test_candidate_version_mismatch(self, tmp_path: Path):
        """错版本 → CANDIDATE_VERSION_MISMATCH。"""
        cand, manifest = build_candidate(tmp_path, VERSION, SAMPLE_FILES)
        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        # 传入不匹配的版本号
        result, rc = run_snapshot(cand, py, "2.0.0-wrong")
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "CANDIDATE_VERSION_MISMATCH" in codes or "CANDIDATE_MANIFEST_MISSING" in codes

    def test_candidate_digest_recompute_mismatch(self, tmp_path: Path):
        """候选 manifest 声明 digest 与重算不符 → CANDIDATE_DIGEST_RECOMPUTE_MISMATCH。"""
        files = dict(SAMPLE_FILES)
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # 篡改 manifest 中的 candidateDigest
        mf = cand / "candidate-manifest.json"
        m = json.loads(mf.read_text(encoding="utf-8"))
        m["candidateDigest"] = "0" * 64  # 错误摘要
        mf.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")

        content_files = [{"from": p, "to": p} for p in sorted(files.keys())]
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "CANDIDATE_DIGEST_RECOMPUTE_MISMATCH" in codes

    def test_generated_metadata_masquerading_as_content(self, tmp_path: Path):
        """把 candidate-manifest.json 声明进内容 publicFiles → GENERATED_METADATA_MASQUERADING_AS_CONTENT。"""
        files = dict(SAMPLE_FILES)
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # candidate-manifest.json 在 publicFiles 而非 generatedMetadata
        content_files = [{"from": p, "to": p} for p in sorted(files.keys())]
        content_files.append({"from": "candidate-manifest.json", "to": "candidate-manifest.json"})
        py = build_project_yaml(tmp_path, VERSION, content_files, generated_metadata=None)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "GENERATED_METADATA_MASQUERADING_AS_CONTENT" in codes


# ── 附加边界用例 ──────────────────────────────────────────────────────

@pytest.mark.usefixtures("tmp_evidence_dir")
class TestReleaseSnapshotBoundary:
    def test_destination_traversal(self, tmp_path: Path):
        """目录穿越 dest ../evil → DESTINATION_TRAVERSAL。"""
        files = dict(SAMPLE_FILES)
        files["evil.txt"] = b"evil"
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        content_files.append({"from": "evil.txt", "to": "../evil.txt"})
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "DESTINATION_TRAVERSAL" in codes

    def test_symlink_escape(self, tmp_path: Path):
        """符号链接源 → SYMLINK_ESCAPE。"""
        files = dict(SAMPLE_FILES)
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # 创建符号链接
        real_file = tmp_path / "real_secret.txt"
        real_file.write_bytes(b"secret")
        symlink_path = cand / "symlinked.txt"
        symlink_path.symlink_to(real_file)

        # 更新 manifest 包含 symlink
        m = json.loads((cand / "candidate-manifest.json").read_text(encoding="utf-8"))
        m["files"].append({
            "path": "symlinked.txt",
            "sha256": sha256_bytes(b"secret"),
            "source": "skill-failure-auditor/symlinked.txt",
        })
        m["fileCount"] = len(m["files"])
        m["candidateDigest"] = compute_candidate_digest(m["files"])
        (cand / "candidate-manifest.json").write_text(
            json.dumps(m, indent=2) + "\n", encoding="utf-8"
        )

        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        content_files.append({"from": "symlinked.txt", "to": "symlinked.txt"})
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "SYMLINK_ESCAPE" in codes

    def test_project_yaml_public_files_drift(self, tmp_path: Path):
        """project.yaml publicFiles 漂移 → PROJECT_YAML_PUBLIC_FILES_DRIFT。"""
        files = dict(SAMPLE_FILES)
        files["missing-from-yaml.txt"] = b"missing"
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # project.yaml 只包含部分文件
        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "PROJECT_YAML_PUBLIC_FILES_DRIFT" in codes or "RELEASE_MISSING_FILE" in codes

    def test_source_file_missing(self, tmp_path: Path):
        """源文件不存在 → SOURCE_FILE_MISSING。"""
        files = dict(SAMPLE_FILES)
        cand, manifest = build_candidate(tmp_path, VERSION, files)

        # project.yaml 引用不存在的文件
        content_files = [{"from": p, "to": p} for p in sorted(SAMPLE_FILES.keys())]
        content_files.append({"from": "ghost.txt", "to": "ghost.txt"})
        gm = [{"from": "candidate-manifest.json", "to": "generated-metadata/candidate-manifest.json"}]
        py = build_project_yaml(tmp_path, VERSION, content_files, gm)

        # 还需要在 manifest 中添加 ghost.txt 以避免 RELEASE_EXTRA_FILE
        m = json.loads((cand / "candidate-manifest.json").read_text(encoding="utf-8"))
        m["files"].append({
            "path": "ghost.txt",
            "sha256": sha256_bytes(b"ghost"),
            "source": "skill-failure-auditor/ghost.txt",
        })
        m["fileCount"] = len(m["files"])
        m["candidateDigest"] = compute_candidate_digest(m["files"])
        (cand / "candidate-manifest.json").write_text(
            json.dumps(m, indent=2) + "\n", encoding="utf-8"
        )

        result, rc = run_snapshot(cand, py, VERSION)
        assert rc == 1
        codes = [f["code"] for f in result["failures"]]
        assert "SOURCE_FILE_MISSING" in codes

"""版本 SSOT 一致性：底层表驱动覆盖 + 一个 verify 集成篡改。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# ─── 路径 ──────────────────────────────────────────────────────────────

REAL_WORKSPACE = Path(__file__).resolve().parents[4]
VERIFY_MJS = REAL_WORKSPACE / "scripts" / "verify.mjs"
VERSION_SOURCE_MJS = REAL_WORKSPACE / "scripts" / "version_source.mjs"

SSOT_REL = "packages/skill-failure-auditor/package.json"

PLUGSRC = "packages/skill-failure-auditor/plugin-src/platforms"
PLUGIN_COORDS = [
    (f"{PLUGSRC}/claude-code/.claude-plugin/plugin.json", "claude"),
    (f"{PLUGSRC}/codex/.codex-plugin/plugin.json", "codex"),
    (f"{PLUGSRC}/workbuddy/.codebuddy-plugin/plugin.json", "workbuddy"),
    (f"{PLUGSRC}/kimi-code/kimi.plugin.json", "kimi"),
]

# ─── 工具函数 ──────────────────────────────────────────────────────────


def run_verify(workspace: Path) -> tuple[int, str]:
    """运行 verify.mjs，返回 (exit_code, combined_output)。"""
    r = subprocess.run(
        ["node", str(VERIFY_MJS)],
        cwd=str(workspace), capture_output=True, text=True, timeout=60,
    )
    out = (r.stdout + "\n" + r.stderr).strip()
    return r.returncode, out


def run_version_check(workspace: Path) -> dict:
    """通过 node -e 调用 version_source.check，返回结果 dict。"""
    code_text = textwrap.dedent(f"""\
        import {{ verifyVersionCoherence }} from "{VERSION_SOURCE_MJS.as_posix()}";
        const r = verifyVersionCoherence("{workspace.as_posix()}");
        console.log(JSON.stringify(r));
    """)
    r = subprocess.run(
        ["node", "--input-type=module", "-e", code_text],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, f"node 执行失败: {r.stderr}"
    return json.loads(r.stdout)


def tamper_json(workspace: Path, rel_path: str, mutator):
    """事务式修改 JSON 文件：改→返回还原函数。"""
    fp = workspace / rel_path
    original = fp.read_text(encoding="utf-8")
    data = json.loads(original)
    mutator(data)
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def restore():
        fp.write_text(original, encoding="utf-8")
    return restore


def tamper_yaml(workspace: Path, rel_path: str, old_text: str, new_text: str):
    """事务式修改 YAML 文件：改→返回还原函数。"""
    fp = workspace / rel_path
    original = fp.read_text(encoding="utf-8")
    tampered = original.replace(old_text, new_text)
    fp.write_text(tampered, encoding="utf-8")

    def restore():
        fp.write_text(original, encoding="utf-8")
    return restore


# ─── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def _session_copy():
    """会话级：复制工作区（排除重目录）。"""
    dst = Path("/tmp") / f"sfa-vc-test-{os.getpid()}"
    if dst.exists():
        shutil.rmtree(dst)

    def ignore(dir, files):
        # 版本一致性用例只需要源码、根脚本与发布配置。控制面、运行证据、
        # 旧候选和授权材料既不属于被测输入，也可能包含指向隔离区外的软链；
        # copytree 默认跟随软链，复制整仓会让无关的悬空证据在 setup 阶段
        # 阻断全部参数化用例。
        skip = {
            "node_modules", ".git", "__pycache__", ".pytest_cache", "dist",
            ".codex", "evidence", "control", "versions", "authorizations",
            "artifacts", "generated",
        }
        return {f for f in files if f in skip}

    shutil.copytree(str(REAL_WORKSPACE), str(dst), ignore=ignore)
    build = subprocess.run(
        [sys.executable,
         str(dst / "packages/skill-failure-auditor/scripts/build/build_platforms.py"),
         "--out", str(dst / "packages/skill-failure-auditor/generated/platforms")],
        cwd=str(dst), capture_output=True, text=True, timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    yield dst
    shutil.rmtree(dst, ignore_errors=True)


@pytest.fixture()
def workspace(_session_copy):
    """每个测试独立的工作区副本。"""
    # 生成唯一目录名（不预先创建，由 copytree 创建）
    dst = Path(tempfile.gettempdir()) / f"sfa-vc-{os.getpid()}-{os.urandom(4).hex()}"
    shutil.copytree(str(_session_copy), str(dst))
    yield dst
    shutil.rmtree(dst, ignore_errors=True)


def package_root(workspace: Path) -> Path:
    return workspace / "packages" / "skill-failure-auditor"


def get_ssot(workspace: Path) -> str:
    pkg = json.loads((package_root(workspace) / "package.json").read_text(encoding="utf-8"))
    return pkg["version"]


# ─── 参数化负向测试：verify 子进程 ─────────────────────────────────────

@pytest.mark.parametrize("desc,rel_path,mutator,expected_code", [
    (
        "claude plugin.json version tampered",
        f"{PLUGSRC}/claude-code/.claude-plugin/plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
        "VERSION_COORDINATE_DRIFT",
    ),
])
def test_verify_fails_on_coordinate_tamper(workspace, desc, rel_path, mutator, expected_code):
    """verify.mjs 在单一坐标篡改时应 FAIL 并包含精确失败码。"""
    restore = tamper_json(workspace, rel_path, mutator)
    try:
        code, out = run_verify(workspace)
        assert code != 0, f"verify 应 FAIL: {out}"
        assert expected_code in out, f"应含 {expected_code}，输出: {out}"
    finally:
        restore()

# ─── 参数化负向测试：version_source.check 直接调用 ────────────────────

@pytest.mark.parametrize("desc,rel_path,mutator,expected_code", [
    (
        "product package.json: SSOT tampered",
        SSOT_REL,
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
        "VERSION_COORDINATE_DRIFT",
    ),
    (
        "product package.json: invalid semver",
        SSOT_REL,
        lambda d: d.__setitem__("version", "not-a-version"),
        "VERSION_SSOT_INVALID",
    ),
    (
        "claude plugin.json: version drift",
        f"{PLUGSRC}/claude-code/.claude-plugin/plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
        "VERSION_COORDINATE_DRIFT",
    ),
    (
        "codex plugin.json: version drift",
        f"{PLUGSRC}/codex/.codex-plugin/plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
        "VERSION_COORDINATE_DRIFT",
    ),
    (
        "workbuddy plugin.json: version drift",
        f"{PLUGSRC}/workbuddy/.codebuddy-plugin/plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
        "VERSION_COORDINATE_DRIFT",
    ),
    (
        "kimi.plugin.json: version drift (double fail: coordinate + kimi-compat)",
        f"{PLUGSRC}/kimi-code/kimi.plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
        "VERSION_COORDINATE_DRIFT",
    ),
])
def test_check_function_fails_on_tamper(workspace, desc, rel_path, mutator, expected_code):
    """version_source.check 函数在单一坐标篡改时返回 ok=False + 精确失败码。"""
    restore = tamper_json(workspace, rel_path, mutator)
    try:
        result = run_version_check(workspace)
        assert result["ok"] is False, f"check 应返回 ok=False: {result}"
        codes = [f["code"] for f in result["failures"]]
        assert expected_code in codes, f"应含 {expected_code}，实际 {codes}"
    finally:
        restore()


def test_check_function_fails_on_project_yaml_source_drift(workspace):
    """project.yaml version.source 改为别的文件 → VERSION_SOURCE_RULE_DRIFT。"""
    restore = tamper_yaml(
        workspace, ".release-skill/project.yaml",
        "source: package.json", "source: wrong-file.json",
    )
    try:
        result = run_version_check(workspace)
        assert result["ok"] is False
        codes = [f["code"] for f in result["failures"]]
        assert "VERSION_SOURCE_RULE_DRIFT" in codes, f"应含 VERSION_SOURCE_RULE_DRIFT: {codes}"
    finally:
        restore()


# ─── 特殊负向：generated 投影篡改（需 generated 存在）─────────────────

def test_generated_projection_drift(workspace):
    """generated 投影 plugin.json version 篡改 → VERSION_COORDINATE_DRIFT。"""
    gen_claude = (package_root(workspace) / "generated" / "platforms" /
                  "claude-code" / "skill" / ".claude-plugin" / "plugin.json")
    assert gen_claude.exists(), "硬门禁夹具必须先生成投影，不得用 skip 隐藏缺失前置条件"

    restore = tamper_json(
        workspace,
        "packages/skill-failure-auditor/generated/platforms/claude-code/skill/.claude-plugin/plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
    )
    try:
        result = run_version_check(workspace)
        assert result["ok"] is False
        codes = [f["code"] for f in result["failures"]]
        assert "VERSION_COORDINATE_DRIFT" in codes
        coords = [f.get("coordinate", "") for f in result["failures"]]
        assert any("generated/" in c for c in coords), f"应提及 generated 坐标: {result['failures']}"
    finally:
        restore()


# ─── 特殊负向：候选目录 manifest 篡改 ──────────────────────────────────

def test_candidate_manifest_drift(workspace):
    """候选目录 manifest.version 与 SSOT 不符 → VERSION_CANDIDATE_MANIFEST_DRIFT。"""
    ssot = get_ssot(workspace)

    # R5 不生成 candidate.2 目录；创建临时假目录
    candidate_dir = package_root(workspace) / "dist" / "candidate" / ssot
    manifest_path = candidate_dir / "candidate-manifest.json"
    created = False
    if not candidate_dir.exists():
        candidate_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({"version": ssot, "files": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        created = True

    original = manifest_path.read_text(encoding="utf-8")
    tampered_data = json.loads(original)
    tampered_data["version"] = "99.99.99-manifest-tampered"
    manifest_path.write_text(
        json.dumps(tampered_data, indent=2) + "\n", encoding="utf-8",
    )
    try:
        result = run_version_check(workspace)
        assert result["ok"] is False
        codes = [f["code"] for f in result["failures"]]
        assert "VERSION_CANDIDATE_MANIFEST_DRIFT" in codes, f"应含 MANIFEST_DRIFT: {codes}"
    finally:
        manifest_path.write_text(original, encoding="utf-8")
        if created:
            shutil.rmtree(candidate_dir, ignore_errors=True)


# ─── kimi.plugin.json 篡改连带 .kimi-plugin 相等检查 ──────────────────

def test_kimi_tamper_triggers_kimi_compat_drift(workspace):
    """kimi.plugin.json 篡改时连带 kimi-compat 相等检查失败。"""
    restore = tamper_json(
        workspace,
        f"{PLUGSRC}/kimi-code/kimi.plugin.json",
        lambda d: d.__setitem__("version", "99.99.99-tampered"),
    )
    try:
        result = run_version_check(workspace)
        assert result["ok"] is False
        codes = [f["code"] for f in result["failures"]]
        assert "VERSION_COORDINATE_DRIFT" in codes
        coords = [f.get("coordinate", "") for f in result["failures"]]
        has_compat = any("kimiCompat" in c for c in coords)
        assert has_compat, f"应含 kimi-compat 漂移: {result['failures']}"
    finally:
        restore()


# ─── 正向测试 ──────────────────────────────────────────────────────────

def test_verify_passes_on_clean_state():
    """当前仓库状态 verify PASS（含 version-coherence）。"""
    r = subprocess.run(
        ["node", str(VERIFY_MJS)],
        cwd=str(REAL_WORKSPACE), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"verify 应 PASS: {r.stdout}\n{r.stderr}"
    data = json.loads(r.stdout)
    vc = next(r for r in data["results"] if r["id"] == "version-coherence")
    assert vc["status"] == "PASS", f"version-coherence 应 PASS: {vc}"


def test_ssot_value_is_correct():
    """SSOT 是合法候选版本；测试不绑定某一轮版本号。"""
    pkg = json.loads(
        (REAL_WORKSPACE / "packages" / "skill-failure-auditor" / "package.json")
        .read_text(encoding="utf-8")
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+-candidate\.\d+", pkg["version"])

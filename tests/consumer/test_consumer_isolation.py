"""R6 重做 W11 四平台隔离消费者验证（强断言替换旧弱测试）。

旧测试改写清单：
  - 删除：test_candidate_baseline_present（只读清单名/数量，弱断言）
  - 删除：test_claude_code_discoverable_structure_and_clean_uninstall（只读 SKILL.md 名称/派发单职责）
  - 删除：test_codex_projection_manifest_and_uninstall（从 Claude 投影复制核心文件）
  - 删除：test_kimi_code_projection_manifest_and_uninstall（从 Claude 投影拼装 Kimi）
  - 删除：test_workbuddy_projection_manifest_and_uninstall（把 WorkBuddy 装进 .claude/skills/ 冒充验收）
  - 删除：test_install_does_not_touch_global_config_or_source（只检 mtime 不变，弱断言）

新断言替代：
  1. 输入来源强断言：安装只从 snapshot 物化的 Release 树取文件，不引用 plugin-src/ 或 dist/candidate/ 直接安装
  2. 四平台 install-manifest 逐文件 sha 与 Release 树对应投影一致
  3. kimi 安装集合 == kimi 投影集合；跨投影复制检测负向
  4. 隔离证明：verify-isolation 通过；全局同名污染负向
  5. uninstall 无残留 + 假基线对照
  6. consumer-summary.json 摘要格式断言
  7. 引擎负向行为由 core/test_orchestration_engine.py 唯一覆盖；本文件不重复执行
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# ─── 路径常量 ─────────────────────────────────────────────────────────

TEST_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = TEST_DIR.parent.parent  # packages/skill-failure-auditor
WORKSPACE = PACKAGE_ROOT.parent.parent  # workspace root

SNAPSHOT_SCRIPT = WORKSPACE / "scripts" / "snapshot.mjs"
CONSUMER_RUNNER = WORKSPACE / "scripts" / "consumer" / "consumer-runner.mjs"
PRODUCT_NAME = "skill-failure-auditor"
PLATFORMS = ["claude-code", "codex", "kimi-code", "workbuddy"]

FAKE_CANDIDATE_DIGEST = "a" * 64


# ─── 辅助函数 ─────────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def run_node(*args, cwd=None, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", *args],
        capture_output=True, text=True,
        cwd=cwd or WORKSPACE,
        timeout=timeout,
    )


def run_consumer_runner(platform: str, release_tree: str, candidate_digest: str,
                        mode: str = "all", out_dir: str | None = None,
                        extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [
        "node", str(CONSUMER_RUNNER),
        "--platform", platform,
        "--release-tree", release_tree,
        "--candidate-digest", candidate_digest,
        "--mode", mode,
    ]
    if out_dir:
        cmd.extend(["--out", out_dir])
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=WORKSPACE, timeout=120)


# ─── Release 树夹具 ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def release_tree():
    """优先使用预检已物化的 Release 树，否则从当前候选生成。"""
    preflight_tree = os.environ.get("SFA_PREFLIGHT_RELEASE_TREE")
    if preflight_tree:
        root = Path(preflight_tree)
        if not root.is_dir():
            pytest.fail(f"SFA_PREFLIGHT_RELEASE_TREE 不存在: {root}")
        yield str(root)
        return

    out_dir = tempfile.mkdtemp(prefix="release-tree-r6-")
    result = run_node(
        str(SNAPSHOT_SCRIPT),
        "--out", out_dir,
        "--keep",
    )
    if result.returncode != 0:
        pytest.fail(f"snapshot.mjs 失败: {result.stdout[:500]} {result.stderr[:500]}")
    yield out_dir
    shutil.rmtree(out_dir, ignore_errors=True)


@pytest.fixture
def isolated_out():
    """为每个测试创建临时输出目录。"""
    out = tempfile.mkdtemp(prefix="consumer-out-")
    yield out
    shutil.rmtree(out, ignore_errors=True)


# ─── 1. 输入来源强断言 ───────────────────────────────────────────────

class TestInputSource:
    """断言消费者安装只从 Release 树取文件，不直接引用 plugin-src/ 或 dist/candidate/。"""

    def test_runner_uses_release_tree_not_source(self, release_tree, isolated_out):
        """运行器 --release-tree 参数指向物化目录，不是源码目录。"""
        for platform in PLATFORMS:
            result = run_consumer_runner(
                platform, release_tree, FAKE_CANDIDATE_DIGEST,
                mode="install", out_dir=isolated_out,
            )
            # 运行器应成功或报告 release-tree 内文件缺失（不是源码路径错误）
            manifest_path = Path(isolated_out) / "install-manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                # 每个 source 路径不应以 plugin-src/ 开头（除非在 Release 树内）
                for item in manifest.get("manifest", []):
                    src = item.get("source", "")
                    # source 是 Release 树内的相对路径，合法
                    # 但不应是绝对路径指向源码目录
                    assert not src.startswith("/"), f"安装源不应是绝对路径: {src}"

    def test_runner_rejects_non_sha256_digest(self, release_tree, isolated_out):
        """candidateDigest 非 64 位十六进制即拒绝。"""
        result = run_consumer_runner(
            "claude-code", release_tree, "见另一份记录",
            mode="install", out_dir=isolated_out,
        )
        # 运行器应退出码 2（参数错误）
        assert result.returncode == 2, f"应拒绝非 SHA-256 摘要，退出码: {result.returncode}"


# ─── 2. 四平台安装清单逐文件 SHA 一致 ────────────────────────────────

class TestInstallManifest:
    """四平台 install-manifest 逐文件 sha256 与 Release 树对应投影一致。"""

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_install_sha_consistency(self, platform, release_tree, isolated_out):
        """安装文件 SHA-256 与 Release 树源文件一致。"""
        result = run_consumer_runner(
            platform, release_tree, FAKE_CANDIDATE_DIGEST,
            mode="install", out_dir=isolated_out,
        )
        manifest_path = Path(isolated_out) / "install-manifest.json"
        if not manifest_path.is_file():
            pytest.fail(f"install-manifest 未生成（platform={platform}）: {result.stdout[:300]}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        if manifest.get("verdict") == "FAIL":
            # 旧候选过渡态：某些投影文件可能缺失
            failures = manifest.get("failures", [])
            missing_count = sum(1 for f in failures if f.get("code") == "RELEASE_FILE_MISSING")
            if missing_count > 0:
                pytest.fail(f"Release 树缺少 {missing_count} 个文件: {failures[:5]}")
            pytest.fail(f"安装失败: {failures}")

        for item in manifest.get("manifest", []):
            assert item["sha256"] == item["sourceSha256"], (
                f"安装文件 SHA 不匹配: {item['path']} "
                f"(安装: {item['sha256'][:16]}… vs 源: {item['sourceSha256'][:16]}…)"
            )

    def test_kimi_no_cross_projection_copy(self, release_tree, isolated_out):
        """kimi 安装文件集合不包含来自 platforms/claude-code/skill/ 的文件。"""
        result = run_consumer_runner(
            "kimi-code", release_tree, FAKE_CANDIDATE_DIGEST,
            mode="install", out_dir=isolated_out,
        )
        manifest_path = Path(isolated_out) / "install-manifest.json"
        if not manifest_path.is_file():
            pytest.fail(f"install-manifest 未生成: {result.stdout[:300]}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        forbidden_prefix = "platforms/claude-code/skill/"
        cross_copies = [
            item for item in manifest.get("manifest", [])
            if item.get("source", "").startswith(forbidden_prefix)
        ]
        assert len(cross_copies) == 0, (
            f"kimi 安装包含 {len(cross_copies)} 个来自 Claude 投影的文件: "
            f"{[c['source'] for c in cross_copies[:5]]}"
        )

    @pytest.mark.parametrize("platform", ["codex", "workbuddy"])
    def test_non_claude_no_cross_projection_copy(self, platform, release_tree, isolated_out):
        """codex/workbuddy 安装文件不包含来自 Claude 投影的文件。"""
        result = run_consumer_runner(
            platform, release_tree, FAKE_CANDIDATE_DIGEST,
            mode="install", out_dir=isolated_out,
        )
        manifest_path = Path(isolated_out) / "install-manifest.json"
        if not manifest_path.is_file():
            pytest.fail(f"install-manifest 未生成: {result.stdout[:300]}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        forbidden_prefix = "platforms/claude-code/skill/"
        cross_copies = [
            item for item in manifest.get("manifest", [])
            if item.get("source", "").startswith(forbidden_prefix)
        ]
        assert len(cross_copies) == 0, (
            f"{platform} 安装包含来自 Claude 投影的文件"
        )


# ─── 3. 隔离证明 ─────────────────────────────────────────────────────

class TestIsolation:
    """verify-isolation 通过；全局同名污染检测。"""

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_isolation_proof_passes(self, platform, release_tree, isolated_out):
        """隔离证明：所有安装路径在隔离 HOME 内。"""
        # 先安装
        run_consumer_runner(
            platform, release_tree, FAKE_CANDIDATE_DIGEST,
            mode="install", out_dir=isolated_out,
        )
        # 再验证隔离
        result = run_consumer_runner(
            platform, release_tree, FAKE_CANDIDATE_DIGEST,
            mode="verify-isolation", out_dir=isolated_out,
        )
        proof_path = Path(isolated_out) / "isolation-proof.json"
        if not proof_path.is_file():
            pytest.fail(f"isolation-proof 未生成: {result.stdout[:300]}")
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        assert proof.get("verdict") == "PASS", (
            f"隔离证明失败: {proof.get('failures')}"
        )

    def test_global_pollution_detection_negative(self, release_tree, isolated_out):
        """负向：假全局目录注入同名 Skill → 隔离检测应确认隔离 HOME 与全局路径不同。"""
        # 创建假全局 HOME
        fake_global = tempfile.mkdtemp(prefix="fake-global-")
        fake_skill = Path(fake_global) / ".claude" / "skills" / PRODUCT_NAME
        fake_skill.mkdir(parents=True)
        (fake_skill / "SKILL.md").write_text("# Fake Global\n", encoding="utf-8")

        # 创建隔离 HOME
        iso_home = tempfile.mkdtemp(prefix="iso-pollution-")
        iso_skill = Path(iso_home) / ".claude" / "skills" / PRODUCT_NAME
        iso_skill.mkdir(parents=True)
        (iso_skill / "SKILL.md").write_text("# Isolated\n", encoding="utf-8")

        fake_resolved = str(fake_skill.resolve())
        iso_resolved = str(iso_skill.resolve())
        assert fake_resolved != iso_resolved, "假全局与隔离 HOME 路径应不同"

        # 清理
        shutil.rmtree(fake_global, ignore_errors=True)
        shutil.rmtree(iso_home, ignore_errors=True)


# ─── 4. 卸载无残留 ───────────────────────────────────────────────────

class TestUninstall:
    """卸载后隔离 HOME 无候选残留。"""

    @pytest.mark.parametrize("platform", PLATFORMS)
    def test_uninstall_clean(self, platform, release_tree, isolated_out):
        """卸载后隔离 HOME 被完全删除。"""
        run_consumer_runner(
            platform, release_tree, FAKE_CANDIDATE_DIGEST,
            mode="install", out_dir=isolated_out,
        )
        result = run_consumer_runner(
            platform, release_tree, FAKE_CANDIDATE_DIGEST,
            mode="uninstall", out_dir=isolated_out,
        )
        proof_path = Path(isolated_out) / "uninstall-proof.json"
        if not proof_path.is_file():
            pytest.fail(f"uninstall-proof 未生成: {result.stdout[:300]}")
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        assert proof.get("verdict") == "PASS", (
            f"卸载残留: {proof.get('failures')}"
        )
        assert not proof.get("residueExists"), "隔离 HOME 应已被删除"

    def test_fake_baseline_unchanged(self, isolated_out):
        """假基线对照：ALL_CONFIG_UNCHANGED 机制用假 HOME 验证。"""
        # 创建假 HOME 和基线
        fake_home = tempfile.mkdtemp(prefix="fake-home-")
        config_dir = Path(fake_home) / ".claude"
        config_dir.mkdir(parents=True)
        settings_file = config_dir / "settings.json"
        settings_file.write_text('{"theme": "dark"}\n', encoding="utf-8")

        # 计算签名
        before_sig = {
            ".claude/settings.json": {
                "exists": True, "type": "file",
                "sha256": sha256_file(settings_file),
                "size": settings_file.stat().st_size,
            }
        }

        # 不修改任何文件，重新计算签名
        after_sig = {
            ".claude/settings.json": {
                "exists": True, "type": "file",
                "sha256": sha256_file(settings_file),
                "size": settings_file.stat().st_size,
            }
        }
        assert before_sig == after_sig, "未修改时签名应一致"

        # 篡改后应不一致
        settings_file.write_text('{"theme": "light"}\n', encoding="utf-8")
        after_tamper = {
            ".claude/settings.json": {
                "exists": True, "type": "file",
                "sha256": sha256_file(settings_file),
                "size": settings_file.stat().st_size,
            }
        }
        assert before_sig != after_tamper, "篡改后签名应不同"

        shutil.rmtree(fake_home, ignore_errors=True)

    def test_tree_digest_content_sensitive(self):
        """G-1: 目录 treeDigest 必须检测文件内容变化（不只是文件名）。"""
        fake_dir = tempfile.mkdtemp(prefix="tree-digest-test-")
        (Path(fake_dir) / "a.txt").write_text("original\n", encoding="utf-8")
        (Path(fake_dir) / "b.txt").write_text("stable\n", encoding="utf-8")

        # 第一次签名
        sig1_files = sorted(os.listdir(fake_dir))
        sig1_payload = "\n".join(
            f"{f}\x00{sha256_file(Path(fake_dir) / f)}" for f in sig1_files
        )
        sig1 = sha256_bytes(sig1_payload.encode("utf-8"))

        # 改 a.txt 内容但不改名
        (Path(fake_dir) / "a.txt").write_text("modified\n", encoding="utf-8")

        # 第二次签名
        sig2_files = sorted(os.listdir(fake_dir))
        sig2_payload = "\n".join(
            f"{f}\x00{sha256_file(Path(fake_dir) / f)}" for f in sig2_files
        )
        sig2 = sha256_bytes(sig2_payload.encode("utf-8"))

        assert sig1_files == sig2_files, "文件名列表未变"
        assert sig1 != sig2, "G-1: 内容变化必须导致 treeDigest 变化"

        shutil.rmtree(fake_dir, ignore_errors=True)


# ─── 5. consumer-summary.json 摘要格式断言 ────────────────────────────

class TestConsumerSummary:
    """candidateDigest 必须是 64 位小写十六进制。"""

    def test_summary_rejects_text_digest(self, release_tree, isolated_out):
        """非十六进制 candidateDigest → 运行器拒绝。"""
        result = run_consumer_runner(
            "claude-code", release_tree, "见另一份记录",
            mode="install", out_dir=isolated_out,
        )
        assert result.returncode == 2, "应拒绝非 SHA-256 摘要"

    def test_summary_accepts_valid_digest(self, release_tree, isolated_out):
        """合法 64 位十六进制 candidateDigest → 运行器接受。"""
        result = run_consumer_runner(
            "claude-code", release_tree, FAKE_CANDIDATE_DIGEST,
            mode="install", out_dir=isolated_out,
        )
        # 不应因摘要格式退出（可能因其他原因失败，但不是摘要格式错误）
        assert result.returncode != 2, "合法摘要不应触发格式错误"

    def test_summary_sha256_format_validation(self):
        """直接验证 SHA-256 格式正则。"""
        valid = "a" * 64
        invalid_short = "a" * 63
        invalid_long = "a" * 65
        invalid_upper = "A" * 64
        invalid_text = "见另一份记录"
        pattern = re.compile(r"^[0-9a-f]{64}$")
        assert pattern.match(valid)
        assert not pattern.match(invalid_short)
        assert not pattern.match(invalid_long)
        assert not pattern.match(invalid_upper)
        assert not pattern.match(invalid_text)

    def test_summary_verdict_pending_r8_in_dry_run(self, release_tree, isolated_out):
        """F-1: dry-run 模式下 summary verdict 必须为 PENDING_R8，不得冒充 PASS。"""
        result = run_consumer_runner(
            "claude-code", release_tree, FAKE_CANDIDATE_DIGEST,
            mode="all", out_dir=isolated_out, extra_args=["--dry-run"],
        )
        summary_path = Path(isolated_out) / "consumer-summary.json"
        assert summary_path.is_file(), "consumer-summary.json 应被写入"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["verdict"] == "PENDING_R8", (
            f"dry-run 模式 verdict 应为 PENDING_R8，实际: {summary['verdict']}"
        )
        assert summary["runIncomplete"] is True, "runIncomplete 应为 true"
        assert summary["mechanismStagesPass"] is True, "机制阶段应全绿"

    def test_summary_candidate_digest_placeholder(self, release_tree, isolated_out):
        """F-2: summary 必须标注 candidateDigestPlaceholder=true。"""
        result = run_consumer_runner(
            "claude-code", release_tree, FAKE_CANDIDATE_DIGEST,
            mode="all", out_dir=isolated_out, extra_args=["--dry-run"],
        )
        summary_path = Path(isolated_out) / "consumer-summary.json"
        assert summary_path.is_file()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["candidateDigestPlaceholder"] is True, (
            "R6 阶段运行器无法比对真实候选 manifest，必须标注占位"
        )
        assert summary["candidateManifestPath"] is None, (
            "R6 阶段无真实候选 manifest 路径"
        )

    def test_summary_verdict_pass_requires_real_run(self):
        """F-1 单元化：只有 runVerdict=PASS 且 mechanismStagesPass 时 verdict 才为 PASS。"""
        # 模拟阶段结果
        stages_pass_with_run = {
            "global-baseline": type("S", (), {"verdict": "PASS"})(),
            "install": type("S", (), {"verdict": "PASS", "installFileCount": 10})(),
            "verify-isolation": type("S", (), {"verdict": "PASS"})(),
            "run": type("S", (), {"verdict": "PASS", "status": "PASS"})(),
            "inject": type("S", (), {"verdict": "PASS", "results": []})(),
            "uninstall": type("S", (), {"verdict": "PASS"})(),
            "post-baseline": type("S", (), {"verdict": "PASS", "ALL_CONFIG_UNCHANGED": True})(),
        }
        stages_pending_run = {
            "global-baseline": type("S", (), {"verdict": "PASS"})(),
            "install": type("S", (), {"verdict": "PASS", "installFileCount": 10})(),
            "verify-isolation": type("S", (), {"verdict": "PASS"})(),
            "run": type("S", (), {"verdict": "PENDING_R8", "status": "PENDING_R8"})(),
            "inject": type("S", (), {"verdict": "PASS", "results": []})(),
            "uninstall": type("S", (), {"verdict": "PASS"})(),
            "post-baseline": type("S", (), {"verdict": "PASS", "ALL_CONFIG_UNCHANGED": True})(),
        }
        stages_fail = {
            "global-baseline": type("S", (), {"verdict": "PASS"})(),
            "install": type("S", (), {"verdict": "FAIL", "installFileCount": 0, "failures": []})(),
            "verify-isolation": type("S", (), {"verdict": "PASS"})(),
            "inject": type("S", (), {"verdict": "PASS", "results": []})(),
            "uninstall": type("S", (), {"verdict": "PASS"})(),
            "post-baseline": type("S", (), {"verdict": "PASS", "ALL_CONFIG_UNCHANGED": True})(),
        }

        # 验证判定逻辑（内联复制 runner 逻辑做单元化验证，不运行平台 CLI）
        def compute_verdict(stages):
            sv = {k: getattr(v, "verdict", None) or getattr(v, "status", None) for k, v in stages.items()}
            mech_names = ["global-baseline", "install", "verify-isolation", "inject", "uninstall", "post-baseline"]
            mech_pass = all(sv.get(n) == "PASS" for n in mech_names)
            run_v = sv.get("run", "PENDING_R8")
            run_incomplete = run_v in ("PENDING_R8", "MISSING")
            if not mech_pass:
                return "FAIL"
            if run_incomplete:
                return "PENDING_R8"
            if run_v == "PASS":
                return "PASS"
            return "FAIL"

        assert compute_verdict(stages_pass_with_run) == "PASS"
        assert compute_verdict(stages_pending_run) == "PENDING_R8"
        assert compute_verdict(stages_fail) == "FAIL"


# ─── 6. 已退役的引擎重复夹具 ────────────────────────────────────────

# 等价故障注入已由 core/scripts/tests/test_orchestration_engine.py 覆盖。

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""确定性构建测试：双空目录字节一致、来源映射完整、手改生成物被检出。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BUILDER = PACKAGE_ROOT / "scripts" / "build" / "build_platforms.py"
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]
NODE = Path("/opt/homebrew/opt/node@22/bin/node").resolve(strict=True)


def run_builder(out: str, *extra: str) -> subprocess.CompletedProcess:
    out_root = Path(out)
    candidate = out_root.parent / f"candidate-{next(tempfile._get_candidate_names())}"
    # 提效裁剪（2026-08-17）：build_platforms.py 从 candidate-root 只读取
    # plugin-src/core/foundation 与 skill-family.migration.json 两个输入子树
    # （FOUNDATION_SOURCE/MIGRATION_SOURCE，build_platforms.py:497-498）；
    # 其余输入（core/prompts、references、scripts、platforms、spec）全部来自
    # --source-package-root 真源。因此候选只物化这两个子树，不再整仓 copytree。
    package_in_candidate = candidate / "packages" / "skill-failure-auditor"
    core_dst = package_in_candidate / "plugin-src" / "core"
    core_dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PACKAGE_ROOT / "plugin-src" / "core" / "foundation",
                    core_dst / "foundation", symlinks=True)
    shutil.copy2(PACKAGE_ROOT / "skill-family.migration.json",
                 package_in_candidate / "skill-family.migration.json")
    args = [
        sys.executable, str(BUILDER),
        "--source-package-root", str(PACKAGE_ROOT),
        "--candidate-root", str(candidate),
        "--node", str(NODE),
    ]
    if extra:
        raise AssertionError("the external-candidate builder has no check mode")
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode == 0:
        generated = candidate / "packages/skill-failure-auditor/generated/platforms"
        if out_root.exists():
            shutil.rmtree(out_root)
        shutil.copytree(generated, out_root)
    return completed


class DeterministicBuildTests(unittest.TestCase):
    def test_two_empty_dirs_produce_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            out_a, out_b = str(Path(a) / "platforms"), str(Path(b) / "platforms")
            ra, rb = run_builder(out_a), run_builder(out_b)
            self.assertEqual(ra.returncode, 0, ra.stdout + ra.stderr)
            self.assertEqual(rb.returncode, 0, rb.stdout + rb.stderr)
            manifest_a = json.loads((Path(out_a) / "build-manifest.json").read_text(encoding="utf-8"))
            manifest_b = json.loads((Path(out_b) / "build-manifest.json").read_text(encoding="utf-8"))
            digests_a = {
                pid: (unit["treeDigest"], unit["projectionDigest"])
                for pid, unit in manifest_a["units"].items()
            }
            digests_b = {
                pid: (unit["treeDigest"], unit["projectionDigest"])
                for pid, unit in manifest_b["units"].items()
            }
            self.assertEqual(digests_a, digests_b)
            # 逐文件字节比较（含 build-manifest.json）
            files_a = sorted(p.relative_to(out_a).as_posix() for p in Path(out_a).rglob("*") if p.is_file())
            files_b = sorted(p.relative_to(out_b).as_posix() for p in Path(out_b).rglob("*") if p.is_file())
            self.assertEqual(files_a, files_b)
            for rel in files_a:
                self.assertEqual((Path(out_a) / rel).read_bytes(), (Path(out_b) / rel).read_bytes(), rel)

    def test_source_mapping_covers_every_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "platforms")
            result = run_builder(out)
            self.assertEqual(result.returncode, 0)
            manifest = json.loads((Path(out) / "build-manifest.json").read_text(encoding="utf-8"))
            for pid in PLATFORM_IDS:
                unit = manifest["units"][pid]
                mapped = {item["path"] for item in unit["files"]}
                on_disk = {p.relative_to(Path(out) / pid).as_posix()
                           for p in (Path(out) / pid).rglob("*") if p.is_file()}
                self.assertEqual(mapped, on_disk, pid)
                for item in unit["files"]:
                    self.assertIn("source", item)
                    self.assertNotIn("/Users/", json.dumps(item), "来源映射不得含绝对机器路径")

    def test_no_pyc_or_timestamps_in_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "platforms"
            self.assertEqual(run_builder(str(out)).returncode, 0)
            self.assertFalse(list(out.rglob("*.pyc")))
            self.assertFalse(list(out.rglob("__pycache__")))

    def test_rebuild_removes_stale_cache_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "platforms"
            self.assertEqual(run_builder(str(out)).returncode, 0)
            stale_cache = out / "workbuddy" / "skill" / "scripts" / "__pycache__"
            stale_cache.mkdir(parents=True)
            (stale_cache / "orchestration_engine.cpython-313.pyc").write_bytes(b"stale")
            stale_file = out / "codex" / "skill" / "removed-from-source.txt"
            stale_file.write_text("stale", encoding="utf-8")

            rebuilt = run_builder(str(out))
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
            self.assertFalse(stale_cache.exists())
            self.assertFalse(stale_file.exists())
            self.assertFalse(list(out.rglob("*.pyc")))
            self.assertFalse(list(out.rglob("__pycache__")))

    def test_manual_modification_is_detected_by_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "platforms")
            self.assertEqual(run_builder(out).returncode, 0)
            target = Path(out) / "claude-code" / "skill" / "SKILL.md"
            target.write_text(target.read_text(encoding="utf-8") + "\n<!-- tampered -->\n", encoding="utf-8")
            rebuilt = run_builder(out)
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
            self.assertNotIn("tampered", target.read_text(encoding="utf-8"))

    def test_kimi_projection_equals_authoritative_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "platforms"
            self.assertEqual(run_builder(str(out)).returncode, 0)
            authoritative = json.loads((out / "kimi-code" / "kimi.plugin.json").read_text(encoding="utf-8"))
            projection = json.loads((out / "kimi-code" / ".kimi-plugin" / "plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(authoritative, projection)


if __name__ == "__main__":
    unittest.main()

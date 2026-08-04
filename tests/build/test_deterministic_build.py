"""确定性构建测试：双空目录字节一致、来源映射完整、手改生成物被检出。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BUILDER = PACKAGE_ROOT / "scripts" / "build" / "build_platforms.py"
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]


def run_builder(out: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BUILDER), "--out", out, *extra],
                          capture_output=True, text=True)


class DeterministicBuildTests(unittest.TestCase):
    def test_two_empty_dirs_produce_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            out_a, out_b = str(Path(a) / "platforms"), str(Path(b) / "platforms")
            ra, rb = run_builder(out_a), run_builder(out_b)
            self.assertEqual(ra.returncode, 0, ra.stdout + ra.stderr)
            self.assertEqual(rb.returncode, 0, rb.stdout + rb.stderr)
            da = json.loads(ra.stdout)["digests"]
            db = json.loads(rb.stdout)["digests"]
            self.assertEqual(da, db)
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

    def test_manual_modification_is_detected_by_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "platforms")
            self.assertEqual(run_builder(out).returncode, 0)
            ok = run_builder(out, "--check")
            self.assertEqual(ok.returncode, 0, ok.stdout)
            target = Path(out) / "claude-code" / "skill" / "SKILL.md"
            target.write_text(target.read_text(encoding="utf-8") + "\n<!-- tampered -->\n", encoding="utf-8")
            bad = run_builder(out, "--check")
            self.assertEqual(bad.returncode, 1)
            self.assertIn("DRIFT", bad.stdout)

    def test_kimi_projection_equals_authoritative_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "platforms"
            self.assertEqual(run_builder(str(out)).returncode, 0)
            authoritative = json.loads((out / "kimi-code" / "kimi.plugin.json").read_text(encoding="utf-8"))
            projection = json.loads((out / "kimi-code" / ".kimi-plugin" / "plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(authoritative, projection)


if __name__ == "__main__":
    unittest.main()

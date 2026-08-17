"""WorkBuddy 真实运行驱动只接受显式、隔离的专用登录 HOME。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[4]
RUNNER = WORKSPACE / "scripts" / "blackbox" / "run-platform.mjs"


class WorkBuddyHomeBoundaryTests(unittest.TestCase):
    def _run(self, home: str | None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="workbuddy-runner-test-") as temporary:
            root = Path(temporary)
            args = [
                "node", str(RUNNER),
                "--platform", "workbuddy",
                "--release-tree", str(root / "release"),
                "--target", str(root / "target"),
                "--out", str(root / "out"),
                "--candidate-digest", "0" * 64,
                "--dry-run",
            ]
            if home is not None:
                args.extend(["--workbuddy-home", home])
            return subprocess.run(args, cwd=WORKSPACE, capture_output=True, text=True)

    def test_rejects_absent_relative_missing_real_or_workspace_home(self) -> None:
        cases = {
            "absent": None,
            "relative": "relative-home",
            "missing": "/tmp/skill-failure-auditor-missing-workbuddy-home",
            "real-home": str(Path.home()),
            "workspace": str(WORKSPACE),
        }
        for label, home in cases.items():
            with self.subTest(label=label):
                result = self._run(home)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_accepts_dedicated_home_without_printing_its_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="workbuddy-auth-home-") as auth_home:
            result = self._run(auth_home)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DRY RUN", result.stdout)
            self.assertNotIn(auth_home, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

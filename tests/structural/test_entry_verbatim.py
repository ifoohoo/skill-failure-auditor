"""R-AC-01 发布入口逐字执行测试：四平台 prepare-run 命令从发布态入口文本正则提取，
仅按入口自身定义的占位符规则替换，逐字执行 → 退出 0 且 stdout 状态 READY_FOR_ISOLATED_TASKS。
测试驱动不得补齐入口未写的参数（若提取的命令缺 --platform，测试必须失败）。

另含负向用例：把 claude-code 入口 prepare-run 的 --platform 行去掉后执行 → 退出非零。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
GENERATED = PACKAGE_ROOT / "generated" / "platforms"
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]


def _extract_prepare_run_block(skill_md_path: Path) -> str:
    """从 SKILL.md 中提取共享 prepare-run 或 Codex 准备适配器代码块。"""
    text = skill_md_path.read_text(encoding="utf-8")
    # 按代码块分隔符切分，找到包含 prepare-run 的 bash 块
    parts = re.split(r"(```(?:bash|text)\s*\n|```)", text)
    in_block = False
    for i, part in enumerate(parts):
        if re.match(r"```bash\s*\n", part):
            in_block = True
            continue
        if part == "```":
            in_block = False
            continue
        if in_block and ("prepare-run" in part or "codex_prepare_run.py" in part):
            return part.strip()
    raise ValueError(f"No prepare-run block found in {skill_md_path}")


def _substitute_placeholders(cmd: str, platform_id: str, target: str,
                            output_root: str, prompts_root: str) -> str:
    """按入口文本定义的占位符规则替换。只替换入口文本中明确出现的占位符，
    不添加入口文本未写的参数。"""
    # SKILL_ROOT 替换为投影 skill 目录
    skill_dir = GENERATED / platform_id / "skill"
    cmd = cmd.replace('"$SKILL_ROOT/scripts/orchestration_engine.py"',
                      f'"{skill_dir}/scripts/orchestration_engine.py"')
    cmd = cmd.replace("$SKILL_ROOT/prompts", f'"{prompts_root}"')
    cmd = cmd.replace("$SKILL_ROOT", str(skill_dir))

    # 占位符替换
    cmd = cmd.replace('"<新任务标识>"', '"AUDIT-VERBATIM-TEST"')
    cmd = cmd.replace('"<static|runtime|combined>"', '"static"')
    cmd = cmd.replace('"<目标绝对路径>"', f'"{target}"')
    cmd = cmd.replace('"<证据类型>"', '"skill-source"')
    cmd = cmd.replace('"<尚不存在的输出目录绝对路径>"', f'"{output_root}"')

    return cmd


class VerbatimEntryExecutionTests(unittest.TestCase):
    """四平台发布入口逐字执行测试。"""

    def setUp(self) -> None:
        # 创建目标夹具（临时目录含一个简单文件）
        self._target_dir = tempfile.mkdtemp(prefix="verbatim-target-")
        (Path(self._target_dir) / "test-skill.md").write_text("# Test Skill\n", encoding="utf-8")
        # 输出目录必须不存在（prepare-run 要求）
        self._output_root = tempfile.mkdtemp(prefix="verbatim-output-")
        shutil.rmtree(self._output_root)  # prepare-run requires non-existent

    def tearDown(self) -> None:
        shutil.rmtree(self._target_dir, ignore_errors=True)
        shutil.rmtree(self._output_root, ignore_errors=True)

    def _run_verbatim(self, platform_id: str) -> subprocess.CompletedProcess:
        skill_md = GENERATED / platform_id / "skill" / "SKILL.md"
        self.assertTrue(skill_md.is_file(), f"Published SKILL.md not found: {skill_md}")

        raw_cmd = _extract_prepare_run_block(skill_md)
        prompts_root = str(GENERATED / platform_id / "skill" / "prompts")
        cmd = _substitute_placeholders(raw_cmd, platform_id,
                                       self._target_dir, self._output_root,
                                       prompts_root)
        # 逐字执行——不补任何参数
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=30)

    def test_claude_code_verbatim_prepare_run(self) -> None:
        result = self._run_verbatim("claude-code")
        self.assertEqual(result.returncode, 0,
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "READY_FOR_ISOLATED_TASKS")

    def test_codex_verbatim_prepare_run(self) -> None:
        result = self._run_verbatim("codex")
        self.assertEqual(result.returncode, 0,
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "READY_FOR_ISOLATED_TASKS")

    def test_kimi_code_verbatim_prepare_run(self) -> None:
        result = self._run_verbatim("kimi-code")
        self.assertEqual(result.returncode, 0,
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "READY_FOR_ISOLATED_TASKS")

    def test_workbuddy_verbatim_prepare_run(self) -> None:
        result = self._run_verbatim("workbuddy")
        self.assertEqual(result.returncode, 0,
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "READY_FOR_ISOLATED_TASKS")

    def test_negative_claude_missing_platform_fails(self) -> None:
        """负向用例：把 claude-code 入口 prepare-run 的 --platform 行去掉后执行 → 退出非零。
        这是对 claude 旧缺陷（漏 --platform）的回归守卫。"""
        skill_md = GENERATED / "claude-code" / "skill" / "SKILL.md"
        raw_cmd = _extract_prepare_run_block(skill_md)
        prompts_root = str(GENERATED / "claude-code" / "skill" / "prompts")
        cmd = _substitute_placeholders(raw_cmd, "claude-code",
                                       self._target_dir, self._output_root,
                                       prompts_root)
        # 去掉 --platform 行
        cmd_no_platform = re.sub(r"--platform\s+\S+\s*\\?\s*\n?", "", cmd)
        # 确认去掉后确实不含 --platform
        self.assertNotIn("--platform", cmd_no_platform)
        result = subprocess.run(cmd_no_platform, shell=True, capture_output=True,
                                text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0,
                            f"Expected non-zero exit when --platform is missing, "
                            f"got 0. stdout: {result.stdout}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""确定性四平台构建器（W7 + R1 + R2 扩展）。

从唯一核心 plugin-src/core 与四薄适配源 plugin-src/platforms/<id> 生成
generated/platforms/<id>/ 安装快照与来源映射。无时间戳、无绝对路径、
按路径排序、排除 __pycache__/*.pyc；两次运行字节一致。
--check 模式重算并比对已生成树，手改即 DRIFT。

R1 扩展：
- 提示词从 core/prompts/ 取源（共享权威库），不再从各平台 prompts/ 读取。
- 三份编排 Schema（task-package/result/role-artifact）与 platform-adapter-mapping.json
  打包进各投影的 references/（安装态自包含）。

R2 扩展：
- 四个平台各生成完整 Skill 树（入口文本 + 核心脚本 + 核心参考 + 提示词 +
  平台编排参考 + 映射表 + Schema + 清单）。核心文件逐字节来自同一核心源
  （单核心多投影），平台差异只在入口文本、平台编排参考与 manifest。

R3 扩展：
- 平台可携带仅服务于自身适配层的确定性脚本；脚本按平台目录复制，禁止覆盖
  同名核心脚本。没有平台脚本的投影保持文件集合与字节不变。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CORE = PACKAGE_ROOT / "plugin-src" / "core"
PLATFORMS = PACKAGE_ROOT / "plugin-src" / "platforms"
SPEC = PACKAGE_ROOT / "spec"
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]
ORCHESTRATION_SCHEMAS = [
    "task-package.schema.json",
    "result.schema.json",
    "role-artifact.schema.json",
]

# 平台 → 入口文件（相对于 platforms/<id>/）
PLATFORM_ENTRY_FILES = {
    "claude-code": "SKILL.md",
    "codex": "SKILL.md",
    "kimi-code": "SKILL.md",
    "workbuddy": "SKILL.md",
}

# 平台 → 编排参考文件（相对于 platforms/<id>/references/）
PLATFORM_ORCHESTRATION_REFS = {
    "claude-code": "claude-code-orchestration.md",
    "codex": "codex-orchestration.md",
    "kimi-code": "kimi-code-orchestration.md",
    "workbuddy": "workbuddy-orchestration.md",
}

# 平台 → 清单目录名
PLATFORM_MANIFEST_DIRS = {
    "claude-code": ".claude-plugin",
    "codex": ".codex-plugin",
    "kimi-code": None,  # kimi 使用 kimi.plugin.json + .kimi-plugin/
    "workbuddy": ".codebuddy-plugin",
}

CLAUDE_PROMPT_MANIFEST = PLATFORMS / "claude-code" / "claude-prompt-manifest.json"
MAPPING_PATH = SPEC / "orchestration" / "platform-adapter-mapping.json"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _claude_prompt_manifest_data() -> dict:
    """从规范角色、Claude 原生类型映射和核心提示词机械生成清单。"""
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    roles = mapping["canonicalRoles"]
    role_types = mapping["platforms"]["claude-code"]["roleToNativeAgentType"]
    prompts = []
    for role in roles:
        prompt = CORE / "prompts" / f"{role}.md"
        if not prompt.is_file():
            raise FileNotFoundError(f"missing core prompt: {prompt}")
        prompts.append({
            "role": role,
            "agent_type": role_types[role],
            "path": f"prompts/{role}.md",
            "sha256": sha256_file(prompt),
        })
    return {
        "schema_version": "2.1",
        "platform": "claude-code",
        "allowed_builtin_agents": ["Plan", "Explore", "general-purpose"],
        "prompts": prompts,
    }


def _canonical_json_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sync_claude_prompt_manifest(*, check: bool) -> list[dict]:
    """同步或校验源码侧派生清单；返回结构化失败列表。"""
    expected = _canonical_json_bytes(_claude_prompt_manifest_data())
    if check:
        if not CLAUDE_PROMPT_MANIFEST.is_file():
            return [{"platform": "claude-code", "path": str(CLAUDE_PROMPT_MANIFEST),
                     "reason": "MISSING_DERIVED_MANIFEST"}]
        if CLAUDE_PROMPT_MANIFEST.read_bytes() != expected:
            return [{"platform": "claude-code", "path": str(CLAUDE_PROMPT_MANIFEST),
                     "reason": "CLAUDE_PROMPT_MANIFEST_DRIFT"}]
        return []
    CLAUDE_PROMPT_MANIFEST.write_bytes(expected)
    return []


def clean_copy(src: Path, dst: Path, mapping: list, rel_prefix: str, source_label: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    mapping.append({"path": rel_prefix, "sha256": sha256_file(dst), "source": source_label})


def copy_platform_entry(src: Path, dst: Path, mapping: list,
                        rel_prefix: str, source_label: str) -> None:
    """复制安装入口，并移除只服务于公开源码闭包检查的隐藏标记。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    content = src.read_text(encoding="utf-8").replace("<!-- source-only -->", "")
    dst.write_text(content, encoding="utf-8")
    mapping.append({"path": rel_prefix, "sha256": sha256_file(dst), "source": source_label})


def _copy_core_prompts(skill: Path, mapping: list) -> None:
    """从 core/prompts/ 复制共享角色提示词到投影 skill/prompts/。"""
    core_prompts = CORE / "prompts"
    if not core_prompts.is_dir():
        return
    for f in sorted(core_prompts.glob("*.md")):
        clean_copy(f, skill / "prompts" / f.name, mapping,
                   f"skill/prompts/{f.name}", f"core/prompts/{f.name}")


def _copy_core_references(skill: Path, mapping: list) -> None:
    """从 core/references/ 复制共享参考文件到投影 skill/references/。"""
    core_refs = CORE / "references"
    if not core_refs.is_dir():
        return
    for f in sorted(core_refs.iterdir()):
        if f.is_file():
            clean_copy(f, skill / "references" / f.name, mapping,
                       f"skill/references/{f.name}", f"core/references/{f.name}")


def _copy_core_scripts(skill: Path, mapping: list) -> None:
    """从 core/scripts/ 复制核心脚本到投影 skill/scripts/。"""
    for f in sorted((CORE / "scripts").glob("*.py")):
        clean_copy(f, skill / "scripts" / f.name, mapping,
                   f"skill/scripts/{f.name}", f"core/scripts/{f.name}")


def _copy_platform_scripts(platform_id: str, skill: Path, mapping: list) -> None:
    """复制平台专属确定性适配脚本，并拒绝覆盖核心脚本。"""
    scripts = PLATFORMS / platform_id / "scripts"
    if not scripts.is_dir():
        return
    for source in sorted(scripts.glob("*.py")):
        target = skill / "scripts" / source.name
        if (CORE / "scripts" / source.name).exists():
            raise FileExistsError(
                f"platform script collides with core script: {platform_id}/{source.name}"
            )
        clean_copy(source, target, mapping,
                   f"skill/scripts/{source.name}",
                   f"platforms/{platform_id}/scripts/{source.name}")


def _copy_orchestration_schemas(skill: Path, mapping: list) -> None:
    """把三份编排 Schema 打包进投影 references/（安装态自包含）。"""
    spec_orch = SPEC / "orchestration"
    for name in ORCHESTRATION_SCHEMAS:
        src = spec_orch / name
        if src.is_file():
            clean_copy(src, skill / "references" / name, mapping,
                       f"skill/references/{name}", f"spec/orchestration/{name}")


def _copy_mapping(skill: Path, mapping: list) -> None:
    """把 platform-adapter-mapping.json 打包进投影 references/。"""
    clean_copy(SPEC / "orchestration" / "platform-adapter-mapping.json",
               skill / "references" / "platform-adapter-mapping.json", mapping,
               "skill/references/platform-adapter-mapping.json",
               "spec/orchestration/platform-adapter-mapping.json")


def _copy_platform_orchestration_ref(platform_id: str, skill: Path, mapping: list) -> None:
    """复制平台专属编排参考到投影 skill/references/。"""
    ref_name = PLATFORM_ORCHESTRATION_REFS[platform_id]
    src = PLATFORMS / platform_id / "references" / ref_name
    clean_copy(src, skill / "references" / ref_name, mapping,
               f"skill/references/{ref_name}",
               f"platforms/{platform_id}/references/{ref_name}")


def assemble(platform_id: str, out_root: Path) -> dict:
    platform_out = out_root / platform_id
    skill = platform_out / "skill"
    mapping: list[dict] = []

    src = PLATFORMS / platform_id

    # ── 1. 平台入口文本（SKILL.md）──
    entry_file = PLATFORM_ENTRY_FILES[platform_id]
    copy_platform_entry(src / entry_file, skill / entry_file, mapping,
                        f"skill/{entry_file}", f"platforms/{platform_id}/{entry_file}")

    # ── 2. 核心脚本（逐字节来自同一核心源）──
    _copy_core_scripts(skill, mapping)

    # ── 2.1 平台专属确定性适配脚本 ──
    _copy_platform_scripts(platform_id, skill, mapping)

    # ── 3. 核心参考文件（规则、Schema 等）──
    _copy_core_references(skill, mapping)

    # ── 4. 核心提示词（共享权威库）──
    _copy_core_prompts(skill, mapping)

    # ── 5. 平台编排参考 ──
    _copy_platform_orchestration_ref(platform_id, skill, mapping)

    # ── 6. 映射表 ──
    _copy_mapping(skill, mapping)

    # ── 7. 三份编排 Schema ──
    _copy_orchestration_schemas(skill, mapping)

    # ── 8. 平台清单（manifest）──
    if platform_id == "claude-code":
        clean_copy(src / ".claude-plugin" / "plugin.json",
                   skill / ".claude-plugin" / "plugin.json", mapping,
                   "skill/.claude-plugin/plugin.json",
                   "platforms/claude-code/.claude-plugin/plugin.json")
        prompt_manifest = skill / "claude-prompt-manifest.json"
        prompt_manifest.write_bytes(_canonical_json_bytes(_claude_prompt_manifest_data()))
        mapping.append({
            "path": "skill/claude-prompt-manifest.json",
            "sha256": sha256_file(prompt_manifest),
            "source": "generated-from:core/prompts+platform-adapter-mapping.json",
        })
    elif platform_id == "codex":
        clean_copy(src / ".codex-plugin" / "plugin.json",
                   platform_out / ".codex-plugin" / "plugin.json", mapping,
                   ".codex-plugin/plugin.json",
                   "platforms/codex/.codex-plugin/plugin.json")
    elif platform_id == "kimi-code":
        # kimi.plugin.json 是手写权威源
        clean_copy(src / "kimi.plugin.json",
                   platform_out / "kimi.plugin.json", mapping,
                   "kimi.plugin.json", "platforms/kimi-code/kimi.plugin.json")
        # .kimi-plugin/plugin.json 由其机械生成（字段完全相等）
        data = json.loads((src / "kimi.plugin.json").read_text(encoding="utf-8"))
        proj = platform_out / ".kimi-plugin" / "plugin.json"
        proj.parent.mkdir(parents=True, exist_ok=True)
        proj.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        mapping.append({"path": ".kimi-plugin/plugin.json", "sha256": sha256_file(proj),
                        "source": "generated-from:kimi.plugin.json"})
    elif platform_id == "workbuddy":
        clean_copy(src / ".codebuddy-plugin" / "plugin.json",
                   platform_out / ".codebuddy-plugin" / "plugin.json", mapping,
                   ".codebuddy-plugin/plugin.json",
                   "platforms/workbuddy/.codebuddy-plugin/plugin.json")

    # ── 9. 平台 manifest ──
    clean_copy(src / "platform-manifest.json",
               platform_out / "platform-manifest.json", mapping,
               "platform-manifest.json",
               f"platforms/{platform_id}/platform-manifest.json")

    status = "assembled"
    mapping.sort(key=lambda e: e["path"])
    return {"platformId": platform_id, "status": status, "fileCount": len(mapping), "files": mapping}


def tree_digest(files: list[dict]) -> str:
    acc = b""
    for item in files:
        acc += item["path"].encode("utf-8") + b"\x00" + item["sha256"].encode("ascii") + b"\n"
    return hashlib.sha256(acc).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(PACKAGE_ROOT / "generated" / "platforms"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    out_root = Path(args.out)
    derived_failures = sync_claude_prompt_manifest(check=args.check)
    if derived_failures:
        print(json.dumps({"status": "FAIL", "failures": derived_failures},
                         ensure_ascii=False, indent=2))
        return 1
    units = {}
    for pid in PLATFORM_IDS:
        units[pid] = assemble(pid, out_root) if not args.check else None
    if args.check:
        failures = []
        for pid in PLATFORM_IDS:
            built = assemble(pid, Path(args.out + ".verify-tmp-" + pid))
            try:
                for item in built["files"]:
                    actual = Path(args.out + ".verify-tmp-" + pid) / pid / item["path"]
                    on_disk = out_root / pid / item["path"]
                    if not on_disk.is_file():
                        failures.append({"platform": pid, "path": item["path"], "reason": "MISSING"})
                    elif sha256_file(on_disk) != item["sha256"]:
                        failures.append({"platform": pid, "path": item["path"], "reason": "DRIFT"})
            finally:
                shutil.rmtree(args.out + ".verify-tmp-" + pid, ignore_errors=True)
        # 根清单漂移校验（与生成逻辑同一来源）
        root_manifests_check = {
            ".claude-plugin/plugin.json": PLATFORMS / "claude-code/.claude-plugin/plugin.json",
            ".codex-plugin/plugin.json": PLATFORMS / "codex/.codex-plugin/plugin.json",
            ".codebuddy-plugin/plugin.json": PLATFORMS / "workbuddy/.codebuddy-plugin/plugin.json",
            "kimi.plugin.json": PLATFORMS / "kimi-code/kimi.plugin.json",
        }
        for rel, src in root_manifests_check.items():
            dst = PACKAGE_ROOT / rel
            if not dst.is_file():
                failures.append({"platform": "root", "path": rel, "reason": "MISSING"})
            elif sha256_file(dst) != sha256_file(src):
                failures.append({"platform": "root", "path": rel, "reason": "DRIFT"})
        kimi_src = json.loads((PLATFORMS / "kimi-code/kimi.plugin.json").read_text(encoding="utf-8"))
        kimi_dst_path = PACKAGE_ROOT / ".kimi-plugin" / "plugin.json"
        if not kimi_dst_path.is_file():
            failures.append({"platform": "root", "path": ".kimi-plugin/plugin.json", "reason": "MISSING"})
        elif json.loads(kimi_dst_path.read_text(encoding="utf-8")) != kimi_src:
            failures.append({"platform": "root", "path": ".kimi-plugin/plugin.json", "reason": "DRIFT"})
        print(json.dumps({"status": "PASS" if not failures else "FAIL", "failures": failures},
                         ensure_ascii=False, indent=2))
        return 0 if not failures else 1
    # 包根清单（生成物：全部派生自平台权威源，禁止手改；漂移由 --check 检出）
    root_manifests = {
        ".claude-plugin/plugin.json": PLATFORMS / "claude-code/.claude-plugin/plugin.json",
        ".codex-plugin/plugin.json": PLATFORMS / "codex/.codex-plugin/plugin.json",
        ".codebuddy-plugin/plugin.json": PLATFORMS / "workbuddy/.codebuddy-plugin/plugin.json",
        "kimi.plugin.json": PLATFORMS / "kimi-code/kimi.plugin.json",
    }
    for rel, src in root_manifests.items():
        dst = PACKAGE_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    kimi_data = json.loads((PLATFORMS / "kimi-code/kimi.plugin.json").read_text(encoding="utf-8"))
    kimi_proj = PACKAGE_ROOT / ".kimi-plugin" / "plugin.json"
    kimi_proj.parent.mkdir(parents=True, exist_ok=True)
    kimi_proj.write_text(json.dumps(kimi_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for pid, unit in units.items():
        unit["treeDigest"] = tree_digest(unit["files"])
    build_manifest = {"schemaVersion": "1.0", "builder": "scripts/build/build_platforms.py",
                      "builderSha256": sha256_file(Path(__file__)), "units": units}
    out_root.parent.mkdir(parents=True, exist_ok=True)
    (out_root / "build-manifest.json").write_text(json.dumps(build_manifest, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")
    print(json.dumps({"status": "BUILT", "out": str(out_root),
                      "digests": {pid: u["treeDigest"] for pid, u in units.items()}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

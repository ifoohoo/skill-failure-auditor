#!/usr/bin/env python3
"""在仓外候选根生成 Foundation core 与四个平台投影。

现行入口只接受显式源码包、候选根和 Node 路径。完整平台树先写入临时目录，
随后把 145 个受管输出复制到尚未存在的候选路径；本脚本不写 live 产品树。

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
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CORE = PACKAGE_ROOT / "plugin-src" / "core"
PLATFORMS = PACKAGE_ROOT / "plugin-src" / "platforms"
SPEC = PACKAGE_ROOT / "spec"
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]
MIGRATION_MANIFEST = PACKAGE_ROOT / "skill-family.migration.json"
FOUNDATION_SOURCE = CORE / "foundation"
MIGRATION_SOURCE = MIGRATION_MANIFEST
NODE_EXECUTABLE: str | None = None
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

MAPPING_PATH = SPEC / "orchestration" / "platform-adapter-mapping.json"
TRIGGER_POLICY_PATH = CORE / "trigger-policy.json"
FRONTMATTER_NAME_TOKEN = "{{SHARED_SKILL_NAME}}"
FRONTMATTER_DESCRIPTION_TOKEN = "{{SHARED_TRIGGER_DESCRIPTION}}"
APPLICABILITY_GATE_TOKEN = "{{SHARED_APPLICABILITY_GATE}}"


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


def clean_copy(src: Path, dst: Path, mapping: list, rel_prefix: str, source_label: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    mapping.append({"path": rel_prefix, "sha256": sha256_file(dst), "source": source_label})


def _load_trigger_policy() -> dict:
    policy = json.loads(TRIGGER_POLICY_PATH.read_text(encoding="utf-8"))
    if set(policy) != {"schema_version", "name", "description",
                       "positive_signal_terms", "applicability_gate"}:
        raise ValueError("trigger-policy.json has unexpected fields")
    if policy["schema_version"] != "1.0":
        raise ValueError("unsupported trigger policy schema")
    for key in ("name", "description", "applicability_gate"):
        if not isinstance(policy[key], str) or not policy[key].strip():
            raise ValueError(f"trigger policy field is empty: {key}")
    if (not isinstance(policy["positive_signal_terms"], list)
            or not policy["positive_signal_terms"]
            or any(not isinstance(term, str) or not term for term in policy["positive_signal_terms"])):
        raise ValueError("trigger policy positive_signal_terms must be non-empty strings")
    if any(term not in policy["description"] for term in policy["positive_signal_terms"]):
        raise ValueError("every trigger signal term must occur in the shared description")
    return policy


def copy_platform_entry(src: Path, dst: Path, mapping: list,
                        rel_prefix: str, source_label: str) -> None:
    """从平台模板与共享触发策略机械渲染安装入口。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    content = src.read_text(encoding="utf-8")
    for token in (FRONTMATTER_NAME_TOKEN, FRONTMATTER_DESCRIPTION_TOKEN,
                  APPLICABILITY_GATE_TOKEN):
        if content.count(token) != 1:
            raise ValueError(f"platform entry must contain {token} exactly once: {src}")
    policy = _load_trigger_policy()
    content = (content
               .replace(FRONTMATTER_NAME_TOKEN, policy["name"])
               .replace(FRONTMATTER_DESCRIPTION_TOKEN, policy["description"])
               .replace(APPLICABILITY_GATE_TOKEN, policy["applicability_gate"])
               .replace("<!-- source-only -->", ""))
    dst.write_text(content, encoding="utf-8")
    mapping.append({"path": rel_prefix, "sha256": sha256_file(dst),
                    "source": f"rendered-from:{source_label}+core/trigger-policy.json"})


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
    for pattern in ("*.py", "*.mjs"):
        for f in sorted((CORE / "scripts").glob(pattern)):
            clean_copy(f, skill / "scripts" / f.name, mapping,
                       f"skill/scripts/{f.name}", f"core/scripts/{f.name}")


def _copy_foundation_bundle(skill: Path, mapping: list) -> None:
    """把一次生成并验证的 Foundation Bundle 原样复制到平台投影。"""
    foundation = FOUNDATION_SOURCE
    bundle = foundation / "quickstart-profile"
    pin = foundation / "foundation-pin.json"
    if not bundle.is_dir() or not pin.is_file():
        raise FileNotFoundError(f"missing Foundation bundle or pin: {foundation}")
    for source in sorted(path for path in foundation.rglob("*") if path.is_file()):
        rel = source.relative_to(foundation)
        target_rel = Path("skill") / "foundation" / rel
        clean_copy(source, skill / "foundation" / rel, mapping,
                   target_rel.as_posix(),
                   f"core/foundation/{rel.as_posix()}")


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


def _copy_migration_manifest(platform_out: Path, mapping: list) -> None:
    """把包根 skill-family.migration.json 复制到平台投影根（四平台同一源）。"""
    if not MIGRATION_SOURCE.is_file():
        raise FileNotFoundError(f"missing migration manifest: {MIGRATION_SOURCE}")
    clean_copy(MIGRATION_SOURCE, platform_out / "skill-family.migration.json", mapping,
               "skill-family.migration.json", "skill-family.migration.json")


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

    # ── 7.1 Foundation 离线 Bundle（四平台同一字节源）──
    _copy_foundation_bundle(skill, mapping)

    # ── 8. 平台清单（manifest）──
    if platform_id == "claude-code":
        clean_copy(src / ".claude-plugin" / "plugin.json",
                   skill / ".claude-plugin" / "plugin.json", mapping,
                   "skill/.claude-plugin/plugin.json",
                   "platforms/claude-code/.claude-plugin/plugin.json")
        clean_copy(src / ".claude-plugin" / "marketplace.json",
                   skill / ".claude-plugin" / "marketplace.json", mapping,
                   "skill/.claude-plugin/marketplace.json",
                   "platforms/claude-code/.claude-plugin/marketplace.json")
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
        clean_copy(src / ".agents" / "plugins" / "marketplace.json",
                   platform_out / ".agents" / "plugins" / "marketplace.json", mapping,
                   ".agents/plugins/marketplace.json",
                   "platforms/codex/.agents/plugins/marketplace.json")
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

    # ── 10. migration manifest（四平台同一源）──
    _copy_migration_manifest(platform_out, mapping)

    status = "assembled"
    mapping.sort(key=lambda e: e["path"])
    return {"platformId": platform_id, "status": status, "fileCount": len(mapping), "files": mapping}


def tree_digest(files: list[dict]) -> str:
    acc = b""
    for item in files:
        acc += item["path"].encode("utf-8") + b"\x00" + item["sha256"].encode("ascii") + b"\n"
    return hashlib.sha256(acc).hexdigest()


def _check_path_no_symlinks(path: Path) -> None:
    """逐段检查绝对路径链，任一已存在分量为符号链接即失败关闭。

    不先 resolve() 再检查——直接对原始路径分量逐段 is_symlink()。
    最终分量必须存在且为普通文件。
    """
    parts = path.parts
    if not parts or parts[0] != "/":
        raise ValueError(f"路径必须是绝对路径: {path}")
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"路径链中存在符号链接: {current}")
    if not path.exists():
        raise FileNotFoundError(f"路径不存在: {path}")
    if not path.is_file():
        raise ValueError(f"路径不是普通文件: {path}")


def _check_relative_path_no_symlinks(root: Path, rel: Path) -> None:
    """检查投影根目录下的相对路径链是否存在符号链接。

    只检查 root 以下的路径分量，跳过系统级祖先路径。
    最终分量必须存在且为普通文件。
    """
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"路径链中存在符号链接: {current}")
    full = root / rel
    if not full.exists():
        raise FileNotFoundError(f"路径不存在: {full}")
    if not full.is_file():
        raise ValueError(f"路径不是普通文件: {full}")


def _resolve_node_executable() -> str:
    """校验调用方显式传入的隔离 Node，不读取环境或 sibling 工具链。"""
    import re
    import subprocess as _sp

    if NODE_EXECUTABLE is None:
        raise ValueError("--node is required")
    raw_path = Path(NODE_EXECUTABLE)
    if not raw_path.is_absolute():
        raise ValueError(f"--node 必须是绝对路径: {NODE_EXECUTABLE}")
    # 逐段检查路径链，不先 resolve() 再检查
    _check_path_no_symlinks(raw_path)
    node_path = raw_path.resolve()
    try:
        completed = _sp.run(
            [str(node_path), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Node --version 失败: {completed.stderr.strip()}")
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", completed.stdout.strip())
        if not m:
            raise RuntimeError(f"无法解析 Node 版本: {completed.stdout.strip()}")
        version_tuple = tuple(int(x) for x in m.groups())
    except (_sp.TimeoutExpired, FileNotFoundError) as exc:
        raise RuntimeError(f"无法执行 Node --version: {exc}") from exc
    if version_tuple < (22, 22, 2) or version_tuple >= (23, 0, 0):
        raise RuntimeError(
            f"Node 版本 {version_tuple} 不在要求范围 >=22.22.2 <23.0.0"
        )
    return str(node_path)


def projection_digest(files: list[dict], platform_out: Path | None = None) -> str:
    """计算投影摘要：排除 platform-manifest.json，与 Audit _foundation_platform_payload_digest 一致。

    只使用 Foundation runner computeResourceClosure；本地 Python SHA 拼接
    不得作为生产回退。
    """
    paths = [item["path"] for item in files if item["path"] != "platform-manifest.json"]

    if platform_out is None:
        raise ValueError("projection_digest 需要 platform_out 以定位 Foundation runner")

    runner_path = platform_out / "skill" / "foundation" / "quickstart-profile" / "runner.mjs"
    runner_rel = Path("skill") / "foundation" / "quickstart-profile" / "runner.mjs"
    _check_relative_path_no_symlinks(platform_out, runner_rel)

    import subprocess as _sp
    import json as _json

    node_executable = _resolve_node_executable()
    request = _json.dumps({
        "runner": str(runner_path.resolve()),
        "operation": "resource-closure",
        "root": str(platform_out),
        "resources": [{"path": rel, "role": "input"} for rel in paths],
    })
    script = r"""
import { pathToFileURL } from "node:url";
let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const input = JSON.parse(raw);
const runner = await import(pathToFileURL(input.runner).href);
const value = await runner.computeResourceClosure({
  root: input.root,
  resources: input.resources,
});
process.stdout.write(`${JSON.stringify(value)}\n`);
"""
    completed = _sp.run(
        [node_executable, "--input-type=module", "--eval", script],
        input=request, capture_output=True, text=True, timeout=30,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()[:500]
        raise RuntimeError(
            f"Foundation runner computeResourceClosure 失败 (exit={completed.returncode}): {stderr}"
        )
    result = _json.loads(completed.stdout)
    if "digest" not in result:
        raise RuntimeError("Foundation runner 输出缺少 digest 字段")
    return result["digest"]


def inject_projection_digest(platform_out: Path, proj_digest: str) -> None:
    """把 projectionDigest 注入 platform-manifest.json。"""
    manifest_path = platform_out / "platform-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"platform-manifest.json 不存在: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["projectionDigest"] = proj_digest
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _copy_new_file(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"candidate path already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    destination.chmod(source.stat().st_mode & 0o7777)


def main() -> int:
    global FOUNDATION_SOURCE, MIGRATION_SOURCE, NODE_EXECUTABLE
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-package-root", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    source_root = Path(args.source_package_root)
    candidate_root = Path(args.candidate_root)
    if not source_root.is_absolute() or source_root.resolve() != PACKAGE_ROOT.resolve():
        raise ValueError("--source-package-root must identify this consumer package")
    if not candidate_root.is_absolute() or not candidate_root.is_dir():
        raise ValueError("--candidate-root must be an existing external candidate directory")
    FOUNDATION_SOURCE = candidate_root / "packages/skill-failure-auditor/plugin-src/core/foundation"
    MIGRATION_SOURCE = candidate_root / "packages/skill-failure-auditor/skill-family.migration.json"
    NODE_EXECUTABLE = args.node
    _resolve_node_executable()

    units: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="sfa-platform-candidate-") as temp:
        stage = Path(temp) / "platforms"
        for pid in PLATFORM_IDS:
            unit = assemble(pid, stage)
            unit["treeDigest"] = tree_digest(unit["files"])
            unit["projectionDigest"] = projection_digest(unit["files"], stage / pid)
            inject_projection_digest(stage / pid, unit["projectionDigest"])
            for entry in unit["files"]:
                if entry["path"] == "platform-manifest.json":
                    entry["sha256"] = sha256_file(stage / pid / "platform-manifest.json")
            units[pid] = unit
            # The platform projection is one managed tree.  Copying only the
            # Foundation subtree leaves core Skill bytes and projectionDigest
            # stale after source changes.
            selected = sorted((stage / pid).rglob("*"))
            for source in selected:
                if not source.is_file():
                    continue
                rel = source.relative_to(stage).as_posix()
                _copy_new_file(source, candidate_root / "packages/skill-failure-auditor/generated/platforms" / rel)

        build_manifest = {"schemaVersion": "1.0", "builder": "scripts/build/build_platforms.py",
                          "builderSha256": sha256_file(Path(__file__)), "units": units}
        manifest = candidate_root / "packages/skill-failure-auditor/generated/platforms/build-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(build_manifest, ensure_ascii=False, indent=2) + "\n")
    generated_count = sum(len(unit["files"]) for unit in units.values()) + 1
    print(json.dumps({"status": "PREPARED_EXTERNAL", "platforms": PLATFORM_IDS,
                      "generatedOutputCount": generated_count, "liveWrites": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""在仓外候选根生成四个平台的只读审计投影。

四个平台共享同一份核心 SKILL、脚本、参考资料和 Foundation Bundle。
平台差异只限安装清单、发现路径和客户端版本约束；SFA 不投影执行器、
角色提示词、任务包、委派映射或目标技能运行入口。
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
PLATFORM_IDS = ["claude-code", "codex", "kimi-code", "workbuddy"]
MIGRATION_MANIFEST = PACKAGE_ROOT / "skill-family.migration.json"
FOUNDATION_SOURCE = CORE / "foundation"
MIGRATION_SOURCE = MIGRATION_MANIFEST
NODE_EXECUTABLE: str | None = None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_copy(src: Path, dst: Path, mapping: list, rel: str, source: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    mapping.append({"path": rel, "sha256": sha256_file(dst), "source": source})


def _copy_core_references(skill: Path, mapping: list) -> None:
    for source in sorted((CORE / "references").iterdir()):
        if source.is_file():
            clean_copy(source, skill / "references" / source.name, mapping,
                       f"skill/references/{source.name}", f"core/references/{source.name}")


def _copy_core_scripts(skill: Path, mapping: list) -> None:
    for pattern in ("*.py", "*.mjs"):
        for source in sorted((CORE / "scripts").glob(pattern)):
            clean_copy(source, skill / "scripts" / source.name, mapping,
                       f"skill/scripts/{source.name}", f"core/scripts/{source.name}")


def _copy_foundation_bundle(skill: Path, mapping: list) -> None:
    bundle = FOUNDATION_SOURCE / "quickstart-profile"
    pin = FOUNDATION_SOURCE / "foundation-pin.json"
    if not bundle.is_dir() or not pin.is_file():
        raise FileNotFoundError(f"missing Foundation bundle or pin: {FOUNDATION_SOURCE}")
    for source in sorted(path for path in FOUNDATION_SOURCE.rglob("*") if path.is_file()):
        rel = source.relative_to(FOUNDATION_SOURCE)
        clean_copy(source, skill / "foundation" / rel, mapping,
                   (Path("skill") / "foundation" / rel).as_posix(),
                   f"core/foundation/{rel.as_posix()}")


def _copy_migration_manifest(platform_out: Path, mapping: list) -> None:
    if not MIGRATION_SOURCE.is_file():
        raise FileNotFoundError(f"missing migration manifest: {MIGRATION_SOURCE}")
    clean_copy(MIGRATION_SOURCE, platform_out / "skill-family.migration.json", mapping,
               "skill-family.migration.json", "skill-family.migration.json")


def assemble(platform_id: str, out_root: Path) -> dict:
    platform_out = out_root / platform_id
    skill = platform_out / "skill"
    source = PLATFORMS / platform_id
    mapping: list[dict] = []

    clean_copy(CORE / "SKILL.md", skill / "SKILL.md", mapping,
               "skill/SKILL.md", "core/SKILL.md")
    _copy_core_scripts(skill, mapping)
    _copy_core_references(skill, mapping)
    _copy_foundation_bundle(skill, mapping)

    if platform_id == "claude-code":
        clean_copy(source / ".claude-plugin" / "plugin.json",
                   skill / ".claude-plugin" / "plugin.json", mapping,
                   "skill/.claude-plugin/plugin.json",
                   "platforms/claude-code/.claude-plugin/plugin.json")
        clean_copy(source / ".claude-plugin" / "marketplace.json",
                   skill / ".claude-plugin" / "marketplace.json", mapping,
                   "skill/.claude-plugin/marketplace.json",
                   "platforms/claude-code/.claude-plugin/marketplace.json")
    elif platform_id == "codex":
        clean_copy(source / ".codex-plugin" / "plugin.json",
                   platform_out / ".codex-plugin" / "plugin.json", mapping,
                   ".codex-plugin/plugin.json",
                   "platforms/codex/.codex-plugin/plugin.json")
        clean_copy(source / ".agents" / "plugins" / "marketplace.json",
                   platform_out / ".agents" / "plugins" / "marketplace.json", mapping,
                   ".agents/plugins/marketplace.json",
                   "platforms/codex/.agents/plugins/marketplace.json")
    elif platform_id == "kimi-code":
        clean_copy(source / "kimi.plugin.json", platform_out / "kimi.plugin.json", mapping,
                   "kimi.plugin.json", "platforms/kimi-code/kimi.plugin.json")
        data = json.loads((source / "kimi.plugin.json").read_text(encoding="utf-8"))
        projection = platform_out / ".kimi-plugin" / "plugin.json"
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8")
        mapping.append({"path": ".kimi-plugin/plugin.json",
                        "sha256": sha256_file(projection),
                        "source": "generated-from:kimi.plugin.json"})
    else:
        clean_copy(source / ".codebuddy-plugin" / "plugin.json",
                   platform_out / ".codebuddy-plugin" / "plugin.json", mapping,
                   ".codebuddy-plugin/plugin.json",
                   "platforms/workbuddy/.codebuddy-plugin/plugin.json")

    clean_copy(source / "platform-manifest.json", platform_out / "platform-manifest.json",
               mapping, "platform-manifest.json",
               f"platforms/{platform_id}/platform-manifest.json")
    _copy_migration_manifest(platform_out, mapping)
    mapping.sort(key=lambda item: item["path"])
    return {"platformId": platform_id, "status": "assembled",
            "fileCount": len(mapping), "files": mapping}


def tree_digest(files: list[dict]) -> str:
    payload = b"".join(
        item["path"].encode("utf-8") + b"\x00" + item["sha256"].encode("ascii") + b"\n"
        for item in files
    )
    return hashlib.sha256(payload).hexdigest()


def _check_path_no_symlinks(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError(f"路径必须是绝对路径: {path}")
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"路径链中存在符号链接: {current}")
    if not path.is_file():
        raise ValueError(f"路径不是普通文件: {path}")


def _check_relative_path_no_symlinks(root: Path, rel: Path) -> None:
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"路径链中存在符号链接: {current}")
    if not (root / rel).is_file():
        raise ValueError(f"路径不是普通文件: {root / rel}")


def _resolve_node_executable() -> str:
    import re
    import subprocess

    if NODE_EXECUTABLE is None:
        raise ValueError("--node is required")
    raw = Path(NODE_EXECUTABLE)
    _check_path_no_symlinks(raw)
    completed = subprocess.run([str(raw.resolve()), "--version"], capture_output=True,
                               text=True, timeout=10)
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", completed.stdout.strip())
    if completed.returncode != 0 or not match:
        raise RuntimeError(f"无法解析 Node 版本: {completed.stdout.strip()}")
    version = tuple(int(value) for value in match.groups())
    if version < (22, 22, 2) or version >= (23, 0, 0):
        raise RuntimeError(f"Node 版本 {version} 不在要求范围 >=22.22.2 <23.0.0")
    return str(raw.resolve())


def projection_digest(files: list[dict], platform_out: Path) -> str:
    import subprocess

    paths = [item["path"] for item in files if item["path"] != "platform-manifest.json"]
    runner_rel = Path("skill/foundation/quickstart-profile/runner.mjs")
    _check_relative_path_no_symlinks(platform_out, runner_rel)
    request = json.dumps({
        "runner": str((platform_out / runner_rel).resolve()),
        "operation": "resource-closure",
        "root": str(platform_out),
        "resources": [{"path": rel, "role": "input"} for rel in paths],
    })
    script = '''
import { pathToFileURL } from "node:url";
let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const input = JSON.parse(raw);
const runner = await import(pathToFileURL(input.runner).href);
const value = await runner.computeResourceClosure({root: input.root, resources: input.resources});
process.stdout.write(`${JSON.stringify(value)}\n`);
'''
    completed = subprocess.run([_resolve_node_executable(), "--input-type=module", "--eval", script],
                               input=request, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise RuntimeError(f"Foundation resource closure 失败: {completed.stderr.strip()[:500]}")
    return json.loads(completed.stdout)["digest"]


def inject_projection_digest(platform_out: Path, digest: str) -> None:
    manifest_path = platform_out / "platform-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["projectionDigest"] = digest
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
        for platform_id in PLATFORM_IDS:
            unit = assemble(platform_id, stage)
            unit["treeDigest"] = tree_digest(unit["files"])
            unit["projectionDigest"] = projection_digest(unit["files"], stage / platform_id)
            inject_projection_digest(stage / platform_id, unit["projectionDigest"])
            for entry in unit["files"]:
                if entry["path"] == "platform-manifest.json":
                    entry["sha256"] = sha256_file(stage / platform_id / "platform-manifest.json")
            units[platform_id] = unit
            for source in sorted((stage / platform_id).rglob("*")):
                if source.is_file():
                    rel = source.relative_to(stage)
                    _copy_new_file(source,
                                   candidate_root / "packages/skill-failure-auditor/generated/platforms" / rel)

        manifest = candidate_root / "packages/skill-failure-auditor/generated/platforms/build-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"schemaVersion": "1.0",
                                     "builder": "scripts/build/build_platforms.py",
                                     "builderSha256": sha256_file(Path(__file__)),
                                     "units": units}, ensure_ascii=False, indent=2) + "\n")
    generated_count = sum(len(unit["files"]) for unit in units.values()) + 1
    print(json.dumps({"status": "PREPARED_EXTERNAL", "platforms": PLATFORM_IDS,
                      "generatedOutputCount": generated_count, "liveWrites": 0},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

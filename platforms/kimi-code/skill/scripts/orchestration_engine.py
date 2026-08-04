#!/usr/bin/env python3
"""平台无关编排引擎（统一编排协议 v2.1）。

职责：prepare-run / write-result / validate-result-set / validate-execution-set / finalize-run。
平台派发不在本引擎内：适配器按 spec/orchestration/platform-adapter-mapping.json
做语法映射与回执归一化，把每职责结果写成 <role>.json；本引擎只做冻结、
集合守恒、Schema 校验、三层对象验证、依赖序强制、语义状态单调传播与失败关闭。

v2.1 变更（R1 重建编排结果真实性合同）：
- 三层对象拆分：L1 原生回执、L2 职责成果、L3 归一化结果外壳；
- write-result 要求 receipt-file、artifact-file、非空 outputs（COMPLETED）；
- validate-result-set 真实 JSON Schema 校验并收集语义状态；
- finalize-run 单调传播语义状态，失败时写 machine-report.json 不写 finalization.json；
- 依赖序由状态机强制（登记时校验前置成果已存在且通过验证）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPTS_DIR.parent
SPEC_ROOT = CORE_ROOT.parent.parent / "spec" / "orchestration"

sys.path.insert(0, str(SCRIPTS_DIR))
from common import ContractError, canonical_json_bytes, validate_schema  # noqa: E402
from evidence_tool import DEFAULT_CHUNK_SIZE, build_index, verify_index  # noqa: E402
from registry_tool import build_selection, validate_registry  # noqa: E402

SCHEMA_VERSION = "2.1"
ROLES = ["scope-routing", "static-audit", "runtime-evidence", "evaluation-integrity",
         "adversarial-challenge", "result-synthesis"]
PLATFORMS = ["claude-code", "codex", "kimi-code", "workbuddy"]
# 结果必填字段由 result.schema.json required 定义；引擎通过 validate_schema 真实校验。
RESULT_STATUS_FAIL = {"FAILED", "TIMEOUT", "SCHEMA_INVALID", "RECEIPT_MISMATCH"}

# 语义状态严重度序（数值越大越严格）
SEVERITY = {
    "PASS_WITHIN_FROZEN_SCOPE": 0,
    "NEEDS_REVISION": 1,
    "INCOMPLETE": 2,
    "BLOCKED": 2,
    "REJECT": 2,
}
SEMANTIC_FAIL = {"INCOMPLETE", "BLOCKED", "REJECT"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(obj) -> bytes:
    return canonical_json_bytes(obj)


def _load_mapping() -> dict:
    """自包含优先：安装态把映射表打包进 references/；源码态回退到 spec/orchestration/。"""
    bundled = CORE_ROOT / "references" / "platform-adapter-mapping.json"
    source = SPEC_ROOT / "platform-adapter-mapping.json"
    path = bundled if bundled.is_file() else source
    if not path.is_file():
        raise ContractError("platform-adapter-mapping.json not found (neither bundled nor source)")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_schema(name: str) -> dict:
    """自包含优先加载 Schema：bundled references/ > spec/orchestration/。"""
    bundled = CORE_ROOT / "references" / name
    source = SPEC_ROOT / name
    path = bundled if bundled.is_file() else source
    if not path.is_file():
        raise ContractError(f"Schema {name} not found (neither bundled nor source)")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_role_dependencies() -> dict:
    return _load_mapping().get("roleDependencies", {})


def _get_expected_native_agent_type(platform: str, role: str) -> str | None:
    mapping = _load_mapping()
    pdata = mapping["platforms"].get(platform, {})
    r2n = pdata.get("roleToNativeAgentType")
    if r2n is not None:
        return r2n.get(role)
    expected = pdata.get("expectedMapping")
    if expected is not None:
        return expected.get(role)
    return None


def _get_platform_receipt_kind(platform: str) -> str:
    mapping = _load_mapping()
    pdata = mapping["platforms"].get(platform, {})
    kind = pdata.get("receiptKind")
    if not kind:
        raise ContractError(f"receiptKind not configured for platform {platform}")
    return kind


def _path_in_allowed(absolute_path: str, allowed: list[str]) -> bool:
    """检查绝对路径是否在允许写集内（resolve 后前缀比较，拒绝目录逃逸）。"""
    resolved = str(Path(absolute_path).resolve())
    for allowed_path in allowed:
        allowed_resolved = str(Path(allowed_path).resolve())
        if resolved == allowed_resolved or resolved.startswith(allowed_resolved + os.sep):
            return True
    return False


def _path_in_subject(absolute_path: str, subject_path: str) -> bool:
    """允许职责成果只读引用冻结目标本身，但不扩大允许写集。"""
    resolved = Path(absolute_path).resolve()
    subject = Path(subject_path).resolve()
    if subject.is_file():
        return resolved == subject
    try:
        resolved.relative_to(subject)
        return True
    except ValueError:
        return False


def _validate_evidence_ref(ref: dict, package: dict, allowed_paths: list[str]) -> str | None:
    """验证职责成果中的证据引用。

    目标证据可以使用 evidence-index files[*].path 的精确相对路径，也可以使用其解析后的
    绝对路径；两种形式都必须同时匹配索引摘要和实际文件摘要。输出目录中的编排证据仍只
    接受绝对路径，并受 allowed_write_paths 限制。
    """
    raw_path = ref.get("path", "")
    if not raw_path:
        return None

    path_obj = Path(raw_path)
    if path_obj.is_absolute() and _path_in_allowed(raw_path, allowed_paths):
        if not path_obj.is_file():
            return "ARTIFACT_EVIDENCE_NOT_FOUND"
        if _sha256_file(path_obj) != ref.get("sha256"):
            return "ARTIFACT_EVIDENCE_DIGEST_MISMATCH"
        return None

    evidence_path = Path(package["evidence_index"]["path"])
    try:
        evidence_index = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "ARTIFACT_EVIDENCE_INDEX_INVALID"
    indexed = {item["path"]: item["sha256"] for item in evidence_index.get("files", [])}

    target = Path(package["target"]["path"]).resolve()
    if path_obj.is_absolute():
        resolved = path_obj.resolve()
        try:
            relative = (resolved.relative_to(target) if target.is_dir()
                        else resolved.relative_to(target.parent)).as_posix()
        except ValueError:
            return "ARTIFACT_EVIDENCE_PATH_NOT_ALLOWED"
        if target.is_file() and resolved != target:
            return "ARTIFACT_EVIDENCE_PATH_NOT_ALLOWED"
    else:
        # 索引路径是规范化 POSIX 相对路径；拒绝目录逃逸、空段和语义等价改写。
        if (raw_path != path_obj.as_posix() or raw_path.startswith("/")
                or any(part in ("", ".", "..") for part in path_obj.parts)):
            return "ARTIFACT_EVIDENCE_PATH_NOT_ALLOWED"
        relative = raw_path
        resolved = ((target if target.is_dir() else target.parent) / path_obj).resolve()
        if not _path_in_subject(str(resolved), str(target)):
            return "ARTIFACT_EVIDENCE_PATH_NOT_ALLOWED"

    expected_digest = indexed.get(relative)
    if expected_digest is None:
        return "ARTIFACT_EVIDENCE_PATH_NOT_INDEXED"
    if ref.get("sha256") != expected_digest:
        return "ARTIFACT_EVIDENCE_DIGEST_MISMATCH"
    if not resolved.is_file():
        return "ARTIFACT_EVIDENCE_NOT_FOUND"
    if _sha256_file(resolved) != expected_digest:
        return "ARTIFACT_EVIDENCE_DIGEST_MISMATCH"
    return None


def _binding(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": _sha256_file(path)}


def _subject_tree_sha256(target: Path) -> str:
    """tree-sha256-v1(src)：单文件取其字节；目录递归（排除 __pycache__/*.pyc/符号链接）。"""
    if target.is_file():
        return _sha256_file(target)
    entries = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            full = Path(dirpath) / fn
            if fn.endswith(".pyc") or full.is_symlink() or not full.is_file():
                continue
            rel = full.relative_to(target).as_posix()
            entries.append((rel, _sha256_file(full)))
    entries.sort(key=lambda e: e[0].encode("utf-8"))
    acc = b""
    for rel, digest in entries:
        acc += rel.encode("utf-8") + b"\x00" + digest.encode("ascii") + b"\n"
    return _sha256_bytes(acc)


def _validate_frozen_subject(package: dict) -> tuple[str | None, str | None]:
    """分别验证目标树绑定与证据索引绑定，不混用两种摘要语义。"""
    try:
        schema = _load_schema("task-package.schema.json")
        validate_schema(package, schema, schema)
    except ContractError as error:
        return "TASK_PACKAGE_SCHEMA_INVALID", str(error)

    target_binding = package["target"]
    target = Path(target_binding["path"])
    if not target.exists():
        return "FROZEN_SUBJECT_MISSING", str(target)
    if target_binding["tree_algorithm"] != "tree-sha256-v1":
        return "FROZEN_SUBJECT_ALGORITHM_MISMATCH", target_binding["tree_algorithm"]
    if _subject_tree_sha256(target) != target_binding["tree_sha256"]:
        return "FROZEN_SUBJECT_TREE_DRIFT", str(target)

    source_binding = package["source_manifest"]
    source_path = Path(source_binding["path"])
    if not source_path.is_file():
        return "SOURCE_MANIFEST_MISSING", str(source_path)
    if _sha256_file(source_path) != source_binding["sha256"]:
        return "SOURCE_MANIFEST_BINDING_DIGEST_MISMATCH", str(source_path)
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return "SOURCE_MANIFEST_INVALID", str(error)
    expected_source = {
        "schema_version": "1.0",
        "task_id": package["task_id"],
        "target": str(target.resolve()),
        "tree_algorithm": target_binding["tree_algorithm"],
        "tree_sha256": target_binding["tree_sha256"],
    }
    if source != expected_source:
        return "SOURCE_MANIFEST_SUBJECT_MISMATCH", str(source_path)

    evidence_binding = package["evidence_index"]
    evidence_path = Path(evidence_binding["path"])
    if not evidence_path.is_file():
        return "EVIDENCE_INDEX_MISSING", str(evidence_path)
    if _sha256_file(evidence_path) != evidence_binding["sha256"]:
        return "EVIDENCE_INDEX_BINDING_DIGEST_MISMATCH", str(evidence_path)
    try:
        verify_index(target, evidence_path)
    except ContractError as error:
        return "EVIDENCE_INDEX_DRIFT", str(error)
    return None, None


def _validate_receipt(receipt: dict, package: dict, expected_role: str, allowed_paths: list) -> str | None:
    """验证 L1 原生回执。返回 None 表示通过，否则返回失败码。"""
    if receipt.get("platform") != package["platform"]:
        return "WRONG_PLATFORM_RECEIPT"
    if receipt.get("task_id") != package["task_id"]:
        return "RECEIPT_TASK_ID_MISMATCH"
    if receipt.get("role") != expected_role:
        return "RECEIPT_ROLE_MISMATCH"

    expected_kind = _get_platform_receipt_kind(package["platform"])
    if receipt.get("kind") != expected_kind:
        return "WRONG_RECEIPT_KIND"

    raw_record = receipt.get("raw_record", {})
    raw_path = raw_record.get("path", "")
    if not _path_in_allowed(raw_path, allowed_paths):
        return "RAW_RECORD_PATH_NOT_ALLOWED"
    if not Path(raw_path).is_file():
        return "RAW_RECORD_NOT_FOUND"
    if _sha256_file(Path(raw_path)) != raw_record.get("sha256"):
        return "RAW_RECORD_DIGEST_MISMATCH"

    expected_native = _get_expected_native_agent_type(package["platform"], expected_role)
    if expected_native is not None and receipt.get("native_agent_type") != expected_native:
        return "WRONG_NATIVE_AGENT_TYPE"

    return None


def _validate_artifact_file(artifact_path: str, package: dict, role: str,
                            allowed_paths: list) -> tuple[str | None, dict | None]:
    """验证 L2 职责成果文件。返回 (failure_code, artifact_data)。"""
    if not Path(artifact_path).is_file():
        return "ARTIFACT_NOT_FOUND", None

    artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    art_schema = _load_schema("role-artifact.schema.json")
    try:
        validate_schema(artifact, art_schema, art_schema)
    except ContractError as e:
        return "ARTIFACT_SCHEMA_INVALID", None

    if artifact.get("platform") != package["platform"]:
        return "ARTIFACT_PLATFORM_MISMATCH", None
    if artifact.get("task_id") != package["task_id"]:
        return "ARTIFACT_TASK_ID_MISMATCH", None
    if artifact.get("role") != role:
        return "ARTIFACT_ROLE_MISMATCH", None

    # 验证 findings.evidence_refs 路径、索引成员资格与摘要
    for finding in artifact.get("findings", []):
        for ref in finding.get("evidence_refs", []):
            failure = _validate_evidence_ref(ref, package, allowed_paths)
            if failure:
                return failure, None

    # 验证 artifact_sha256
    unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    computed = _sha256_bytes(_canonical(unsigned))
    if computed != artifact.get("artifact_sha256"):
        return "ARTIFACT_DIGEST_MISMATCH", None

    return None, artifact


# ─── prepare-run ───────────────────────────────────────────────────────

def prepare_run(args) -> int:
    import re
    if not re.fullmatch(r"AUDIT-[A-Z0-9][A-Z0-9._-]+", args.task_id):
        print(json.dumps({"status": "REJECTED", "reason": "INVALID_TASK_ID"}))
        return 1
    if args.platform not in PLATFORMS:
        print(json.dumps({"status": "REJECTED", "reason": "INVALID_PLATFORM"}))
        return 1
    if args.mode not in ("static", "runtime", "combined"):
        print(json.dumps({"status": "REJECTED", "reason": "INVALID_MODE"}))
        return 1
    output_root = Path(args.output_root)
    if output_root.exists():
        print(json.dumps({"status": "REJECTED", "reason": "OUTPUT_ROOT_EXISTS"}))
        return 1
    target = Path(args.target)
    if not target.exists():
        print(json.dumps({"status": "REJECTED", "reason": "TARGET_MISSING"}))
        return 1

    expected_roles = _load_mapping()["modeRoleSets"][args.mode]
    prompts_root = Path(args.prompts_root)
    prompt_bindings = []
    for role in expected_roles:
        p = prompts_root / f"{role}.md"
        if not p.is_file():
            print(json.dumps({"status": "REJECTED", "reason": "PROMPT_MISSING", "role": role}))
            return 1
        prompt_bindings.append({"role": role, "path": str(p.resolve()), "sha256": _sha256_file(p)})

    # 规则筛选：FM-01..FM-28 全选（skill 目标），复用核心 registry_tool
    registry_path = CORE_ROOT / "references" / "failure-modes.jsonl"
    schema_path = CORE_ROOT / "references" / "failure-mode.schema.json"
    validation = validate_registry(registry_path, schema_path)
    bundle = build_selection(validation, args.mode, "skill", {args.evidence_type}, 28)
    if bundle["selection"]["status"] not in ("SELECTED",):
        print(json.dumps({"status": "REJECTED", "reason": "SELECTION_INCOMPLETE",
                          "detail": bundle["selection"]["status"]}))
        return 1

    output_root.mkdir(parents=True)
    (output_root / "agent-results").mkdir()
    (output_root / "work").mkdir()
    selection_path = output_root / "selection.json"
    selection_path.write_text(json.dumps(bundle["selection"], ensure_ascii=False, indent=2), encoding="utf-8")
    source_manifest_path = output_root / "source-manifest.json"
    source_manifest = {
        "schema_version": "1.0",
        "task_id": args.task_id,
        "target": str(target.resolve()),
        "tree_algorithm": "tree-sha256-v1",
        "tree_sha256": _subject_tree_sha256(target),
    }
    source_manifest_path.write_text(json.dumps(source_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence_index_path = output_root / "evidence-index.json"
    evidence_index = build_index(target, DEFAULT_CHUNK_SIZE)
    evidence_index_path.write_text(json.dumps(evidence_index, ensure_ascii=False, indent=2), encoding="utf-8")
    prompts_manifest_path = output_root / "prompts-manifest.json"
    prompts_manifest_path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "task_id": args.task_id,
                                                 "prompts": prompt_bindings}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    allowed = [
        str((output_root / "agent-results").resolve()),
        str((output_root / "work").resolve()),
    ]

    package = {
        "schema_version": "2.1",
        "task_id": args.task_id,
        "platform": args.platform,
        "mode": args.mode,
        "target": {
            "path": str(target.resolve()),
            "tree_algorithm": source_manifest["tree_algorithm"],
            "tree_sha256": source_manifest["tree_sha256"],
        },
        "evidence_type": args.evidence_type,
        "output_root": str(output_root.resolve()),
        "allowed_write_paths": allowed,
        "source_manifest": _binding(source_manifest_path),
        "evidence_index": _binding(evidence_index_path),
        "registry": _binding(registry_path),
        "selection": _binding(selection_path),
        "prompt_manifest": _binding(prompts_manifest_path),
        "prompts": prompt_bindings,
        "expected_roles": list(expected_roles),
        "acceptance_criteria": ["集合守恒", "失败关闭", "判据隔离", "回执内容绑定"],
        "package_digest": "",
    }
    package["package_digest"] = _sha256_bytes(_canonical({**package, "package_digest": ""}))
    package_path = output_root / "task-package.json"
    package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")

    # 回读验证任务包摘要
    reread = json.loads(package_path.read_text(encoding="utf-8"))
    expect = _sha256_bytes(_canonical({**reread, "package_digest": ""}))
    if reread["package_digest"] != expect:
        print(json.dumps({"status": "REJECTED", "reason": "PACKAGE_DIGEST_READBACK_MISMATCH"}))
        return 1

    # 任务包 Schema 校验（R-AC-04）
    try:
        tp_schema = _load_schema("task-package.schema.json")
        validate_schema(reread, tp_schema, tp_schema)
    except ContractError as e:
        print(json.dumps({"status": "REJECTED", "reason": "TASK_PACKAGE_SCHEMA_INVALID",
                          "detail": str(e)}))
        return 1

    print(json.dumps({"status": "READY_FOR_ISOLATED_TASKS",
                      "task_package": str(package_path), "expected_roles": list(expected_roles)},
                     ensure_ascii=False))
    return 0


# ─── write-result ──────────────────────────────────────────────────────

def write_result(args) -> int:
    """登记子智能体结果（L3 外壳）：验证 L1 回执、L2 成果、输出文件，强制依赖序。"""
    package_path = Path(args.task_package)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    expect_digest = _sha256_bytes(_canonical({**package, "package_digest": ""}))
    if package.get("package_digest") != expect_digest:
        print(json.dumps({"status": "REJECTED", "reason": "TASK_PACKAGE_DIGEST_DRIFT"}))
        return 1
    frozen_failure, frozen_detail = _validate_frozen_subject(package)
    if frozen_failure:
        print(json.dumps({"status": "REJECTED", "reason": frozen_failure,
                          "detail": frozen_detail}, ensure_ascii=False))
        return 1

    role = args.role
    if role not in package["expected_roles"]:
        print(json.dumps({"status": "REJECTED", "reason": "ROLE_NOT_EXPECTED", "role": role}))
        return 1
    if args.status not in ("COMPLETED", "FAILED", "TIMEOUT"):
        print(json.dumps({"status": "REJECTED", "reason": "INVALID_STATUS"}))
        return 1

    allowed_paths = package.get("allowed_write_paths", [])

    # ── COMPLETED 强制要求回执、成果、非空输出 ──
    if args.status == "COMPLETED":
        if not args.receipt_file:
            print(json.dumps({"status": "REJECTED", "reason": "MISSING_RECEIPT"}))
            return 1
        if not args.artifact_file:
            print(json.dumps({"status": "REJECTED", "reason": "MISSING_ARTIFACT"}))
            return 1

    # ── 读取 outputs ──
    if args.outputs_file:
        try:
            outputs = json.loads(Path(args.outputs_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(json.dumps({"status": "REJECTED", "reason": "OUTPUTS_UNPARSABLE", "detail": str(error)}))
            return 1
    else:
        outputs = json.loads(args.outputs_json) if args.outputs_json else []
    if not isinstance(outputs, list):
        print(json.dumps({"status": "REJECTED", "reason": "OUTPUTS_NOT_A_LIST"}))
        return 1

    if args.status == "COMPLETED" and len(outputs) < 1:
        print(json.dumps({"status": "REJECTED", "reason": "EMPTY_OUTPUTS"}))
        return 1

    # ── 验证并读取回执 ──
    receipt = None
    if args.receipt_file:
        receipt_path = Path(args.receipt_file)
        if not receipt_path.is_file():
            print(json.dumps({"status": "REJECTED", "reason": "RECEIPT_NOT_FOUND"}))
            return 1
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        # Schema 校验
        result_schema = _load_schema("result.schema.json")
        receipt_schema_def = result_schema["properties"]["native_receipt"]
        try:
            validate_schema(receipt, receipt_schema_def, result_schema)
        except ContractError:
            print(json.dumps({"status": "REJECTED", "reason": "RECEIPT_SCHEMA_INVALID"}))
            return 1
        # 回执 role 与登记角色精确比较（不覆写，不静默修正）
        fail = _validate_receipt(receipt, package, role, allowed_paths)
        if fail:
            print(json.dumps({"status": "REJECTED", "reason": fail}))
            return 1

    # ── 验证并读取成果 ──
    artifact_data = None
    if args.artifact_file:
        artifact_path_str = args.artifact_file
        if not Path(artifact_path_str).is_file():
            print(json.dumps({"status": "REJECTED", "reason": "ARTIFACT_NOT_FOUND"}))
            return 1
        if not _path_in_allowed(artifact_path_str, allowed_paths):
            print(json.dumps({"status": "REJECTED", "reason": "ARTIFACT_PATH_NOT_ALLOWED"}))
            return 1
        fail, artifact_data = _validate_artifact_file(artifact_path_str, package, role, allowed_paths)
        if fail:
            print(json.dumps({"status": "REJECTED", "reason": fail}))
            return 1

    # ── 验证输出文件 ──
    seen_output_paths = set()
    for item in outputs:
        if not isinstance(item, dict) or set(item.keys()) != {"path", "sha256"}:
            print(json.dumps({"status": "REJECTED", "reason": "OUTPUT_INVALID_SHAPE"}))
            return 1
        if not _path_in_allowed(item["path"], allowed_paths):
            print(json.dumps({"status": "REJECTED", "reason": "OUTPUT_PATH_NOT_ALLOWED"}))
            return 1
        if not Path(item["path"]).is_file():
            print(json.dumps({"status": "REJECTED", "reason": "OUTPUT_NOT_FOUND",
                              "path": item["path"]}))
            return 1
        actual_sha = _sha256_file(Path(item["path"]))
        if actual_sha != item["sha256"]:
            print(json.dumps({"status": "REJECTED", "reason": "WRONG_OUTPUT_DIGEST",
                              "path": item["path"]}))
            return 1
        resolved = str(Path(item["path"]).resolve())
        if resolved in seen_output_paths:
            print(json.dumps({"status": "REJECTED", "reason": "DUPLICATE_OUTPUT_PATH",
                              "path": item["path"]}))
            return 1
        seen_output_paths.add(resolved)

    # ── 依赖序强制（R-AC-07）──
    role_deps = _load_role_dependencies()
    results_dir = Path(package["output_root"]) / "agent-results"
    deps = role_deps.get(role, [])
    expected_set = set(package["expected_roles"])
    for dep in deps:
        if dep not in expected_set:
            continue
        dep_file = results_dir / f"{dep}.json"
        if not dep_file.is_file():
            print(json.dumps({"status": "REJECTED", "reason": "DEPENDENCY_NOT_SATISFIED",
                              "role": role, "missing_dependency": dep}))
            return 1
        try:
            dep_result = json.loads(dep_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(json.dumps({"status": "REJECTED", "reason": "DEPENDENCY_NOT_SATISFIED",
                              "role": role, "missing_dependency": dep}))
            return 1
        dep_schema = _load_schema("result.schema.json")
        try:
            validate_schema(dep_result, dep_schema, dep_schema)
        except ContractError:
            print(json.dumps({"status": "REJECTED", "reason": "DEPENDENCY_NOT_SATISFIED",
                              "role": role, "dependency_schema_invalid": dep}))
            return 1
        if dep_result.get("status") != "COMPLETED":
            print(json.dumps({"status": "REJECTED", "reason": "DEPENDENCY_NOT_SATISFIED",
                              "role": role, "dependency_not_completed": dep}))
            return 1

    # ── 守卫：重复写入 ──
    target = results_dir / f"{role}.json"
    attempt = args.attempt
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        prior = existing.get("attempt") if isinstance(existing, dict) else None
        if not isinstance(prior, int):
            print(json.dumps({"status": "REJECTED", "reason": "RESULT_FILE_PREEXISTS_NOT_ENGINE_WRITTEN",
                              "role": role,
                              "detail": "结果文件已存在但非引擎写出的合法结果对象；清理该越权文件后重试"}))
            return 1
        if existing.get("status") == "COMPLETED" or attempt <= prior:
            print(json.dumps({"status": "REJECTED", "reason": "DUPLICATE_OUTPUT", "role": role,
                              "detail": "重试必须以递增 attempt 且仅在先前失败后"}))
            return 1

    # ── 构造结果外壳 ──
    body = {
        "schema_version": SCHEMA_VERSION,
        "task_id": package["task_id"],
        "platform": package["platform"],
        "role": role,
        "status": args.status,
        "attempt": attempt,
        "native_receipt": receipt,
        "outputs": outputs,
        "artifact": {"path": str(Path(args.artifact_file).resolve()),
                      "sha256": _sha256_file(Path(args.artifact_file))} if args.artifact_file else None,
    }
    if args.error:
        body["error"] = args.error

    result = {
        **body,
        "result_sha256": _sha256_bytes(_canonical(body)),
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "WRITTEN", "role": role, "result_sha256": result["result_sha256"],
                      "path": str(target)}, ensure_ascii=False))
    return 0


# ─── validate-result-set ───────────────────────────────────────────────

def validate_result_set(args, *, semantic_failures_are_incomplete: bool = True) -> dict:
    package = json.loads(Path(args.task_package).read_text(encoding="utf-8"))
    expect_digest = _sha256_bytes(_canonical({**package, "package_digest": ""}))
    if package.get("package_digest") != expect_digest:
        return {"status": "REJECTED", "reason": "TASK_PACKAGE_DIGEST_DRIFT"}
    frozen_failure, frozen_detail = _validate_frozen_subject(package)
    if frozen_failure:
        return {"status": "REJECTED", "reason": frozen_failure, "detail": frozen_detail}

    expected = list(package["expected_roles"])
    results_dir = Path(args.results_dir)
    allowed_paths = package.get("allowed_write_paths", [])
    failures = []
    results = []
    semantic_statuses = []

    # 加载全部 Schema
    result_schema = _load_schema("result.schema.json")
    art_schema = _load_schema("role-artifact.schema.json")

    # 平台→回执 kind 映射
    mapping = _load_mapping()
    platform_kind_map = {}
    for pid, pdata in mapping["platforms"].items():
        rk = pdata.get("receiptKind")
        if rk:
            platform_kind_map[pid] = rk

    # 精确文件集合比较
    seen_files = sorted(p.name for p in results_dir.iterdir() if p.is_file()) if results_dir.is_dir() else []
    expected_files = sorted(f"{role}.json" for role in expected)
    if seen_files != expected_files:
        missing = sorted(set(expected_files) - set(seen_files))
        extra = sorted(set(seen_files) - set(expected_files))
        if missing:
            failures.append({"code": "MISSING_OUTPUT", "roles": [m[:-5] for m in missing]})
        if extra:
            failures.append({"code": "EXTRA_OUTPUT", "files": extra})

    for role in expected:
        path = results_dir / f"{role}.json"
        if not path.is_file():
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            failures.append({"code": "SCHEMA_INVALID", "role": role, "detail": "invalid json"})
            continue

        # 真实 JSON Schema 校验
        try:
            validate_schema(result, result_schema, result_schema)
        except ContractError as e:
            failures.append({"code": "SCHEMA_INVALID", "role": role, "detail": str(e)})
            continue

        # 身份一致性
        if (result.get("schema_version") != SCHEMA_VERSION
                or result.get("task_id") != package["task_id"]
                or result.get("platform") != package["platform"]
                or result.get("role") != role):
            failures.append({"code": "ROLE_OR_IDENTITY_MISMATCH", "role": role})
            continue

        if not isinstance(result.get("attempt"), int) or result["attempt"] < 1:
            failures.append({"code": "SCHEMA_INVALID", "role": role, "detail": "attempt"})
            continue

        status = result.get("status")
        if status in RESULT_STATUS_FAIL:
            failures.append({"code": status, "role": role, "error": result.get("error")})
            continue
        if status != "COMPLETED":
            failures.append({"code": "SCHEMA_INVALID", "role": role, "detail": f"status {status}"})
            continue

        # 回执验证
        receipt = result.get("native_receipt", {})
        if receipt.get("platform") != package["platform"]:
            failures.append({"code": "WRONG_PLATFORM_RECEIPT", "role": role})
            continue
        if receipt.get("task_id") != package["task_id"]:
            failures.append({"code": "RECEIPT_TASK_ID_MISMATCH", "role": role})
            continue
        if receipt.get("role") != role:
            failures.append({"code": "RECEIPT_ROLE_MISMATCH", "role": role})
            continue
        expected_kind = platform_kind_map.get(package["platform"])
        if receipt.get("kind") != expected_kind:
            failures.append({"code": "WRONG_RECEIPT_KIND", "role": role})
            continue
        expected_native = _get_expected_native_agent_type(package["platform"], role)
        if expected_native is not None and receipt.get("native_agent_type") != expected_native:
            failures.append({"code": "WRONG_NATIVE_AGENT_TYPE", "role": role})
            continue

        # raw_record 回读重算
        raw_record = receipt.get("raw_record", {})
        raw_path = raw_record.get("path", "")
        if raw_path:
            if not _path_in_allowed(raw_path, allowed_paths):
                failures.append({"code": "RAW_RECORD_PATH_NOT_ALLOWED", "role": role})
                continue
            if not Path(raw_path).is_file():
                failures.append({"code": "RAW_RECORD_NOT_FOUND", "role": role})
                continue
            if _sha256_file(Path(raw_path)) != raw_record.get("sha256"):
                failures.append({"code": "RAW_RECORD_DIGEST_MISMATCH", "role": role})
                continue

        # 成果验证
        artifact_binding = result.get("artifact", {})
        art_path_str = artifact_binding.get("path", "")
        if not art_path_str:
            failures.append({"code": "MISSING_ARTIFACT_BINDING", "role": role})
            continue
        if not _path_in_allowed(art_path_str, allowed_paths):
            failures.append({"code": "ARTIFACT_PATH_NOT_ALLOWED", "role": role})
            continue
        if not Path(art_path_str).is_file():
            failures.append({"code": "ARTIFACT_NOT_FOUND", "role": role})
            continue
        if _sha256_file(Path(art_path_str)) != artifact_binding.get("sha256"):
            failures.append({"code": "ARTIFACT_BINDING_DIGEST_MISMATCH", "role": role})
            continue

        # 加载并验证成果 Schema 与摘要
        try:
            artifact = json.loads(Path(art_path_str).read_text(encoding="utf-8"))
            validate_schema(artifact, art_schema, art_schema)
        except (OSError, json.JSONDecodeError, ContractError) as e:
            failures.append({"code": "ARTIFACT_SCHEMA_INVALID", "role": role, "detail": str(e)})
            continue

        if (artifact.get("platform") != package["platform"]
                or artifact.get("task_id") != package["task_id"]
                or artifact.get("role") != role):
            failures.append({"code": "ARTIFACT_IDENTITY_MISMATCH", "role": role})
            continue

        unsigned_art = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
        computed_art_sha = _sha256_bytes(_canonical(unsigned_art))
        if computed_art_sha != artifact.get("artifact_sha256"):
            failures.append({"code": "ARTIFACT_DIGEST_MISMATCH", "role": role})
            continue

        evidence_ref_failure = None
        for finding in artifact.get("findings", []):
            for ref in finding.get("evidence_refs", []):
                evidence_ref_failure = _validate_evidence_ref(ref, package, allowed_paths)
                if evidence_ref_failure:
                    break
            if evidence_ref_failure:
                break
        if evidence_ref_failure:
            failures.append({"code": evidence_ref_failure, "role": role})
            continue

        sem_status = artifact.get("semantic_status")
        conc_ceiling = artifact.get("conclusion_ceiling")
        semantic_statuses.append({"role": role, "semantic_status": sem_status,
                                  "conclusion_ceiling": conc_ceiling})

        # 语义状态越过结论上限检查
        if sem_status and conc_ceiling:
            if SEVERITY.get(sem_status, 99) > SEVERITY.get(conc_ceiling, 99):
                failures.append({"code": "SEMANTIC_STATUS_EXCEEDS_CEILING", "role": role,
                                 "semantic_status": sem_status, "conclusion_ceiling": conc_ceiling})
                continue

        # 输出验证
        for output in result.get("outputs", []):
            opath = output.get("path", "")
            if not _path_in_allowed(opath, allowed_paths):
                failures.append({"code": "OUTPUT_PATH_NOT_ALLOWED", "role": role})
                continue
            if not Path(opath).is_file():
                failures.append({"code": "OUTPUT_NOT_FOUND", "role": role, "path": opath})
                continue
            if _sha256_file(Path(opath)) != output.get("sha256"):
                failures.append({"code": "WRONG_OUTPUT_DIGEST", "role": role, "path": opath})
                continue

        # 结果摘要重算（覆盖除 result_sha256 外的全部规范化字段，含 schema_version）
        body = {k: result.get(k) for k in
                ("schema_version", "task_id", "platform", "role", "status", "attempt",
                 "native_receipt", "outputs", "artifact")}
        if "error" in result:
            body["error"] = result["error"]
        if result.get("result_sha256") != _sha256_bytes(_canonical(body)):
            failures.append({"code": "RECEIPT_MISMATCH", "role": role})
            continue

        results.append({"role": role, "status": status, "sha256": result["result_sha256"],
                        "semantic_status": sem_status, "conclusion_ceiling": conc_ceiling})

    # 检查语义失败
    semantic_failures = [s for s in semantic_statuses if s["semantic_status"] in SEMANTIC_FAIL]
    if semantic_failures and semantic_failures_are_incomplete:
        for sf in semantic_failures:
            failures.append({"code": "INNER_SEMANTIC_FAILURE", "role": sf["role"],
                             "semantic_status": sf["semantic_status"]})

    if failures:
        return {"status": "INCOMPLETE", "failures": failures, "results": results,
                "semantic_summary": semantic_statuses}

    semantic_summary = {s["role"]: {"semantic_status": s["semantic_status"],
                                     "conclusion_ceiling": s["conclusion_ceiling"]}
                        for s in semantic_statuses}
    return {"status": "COMPLETE", "task_id": package["task_id"], "platform": package["platform"],
            "mode": package["mode"], "results": results,
            "task_package_sha256": _sha256_file(Path(args.task_package)),
            "semantic_summary": semantic_summary,
            "semantic_failures": semantic_failures}


def validate_execution_set(args) -> dict:
    """只裁定执行合同完整性；语义失败保留在输出中但不冒充结构失败。"""
    return validate_result_set(args, semantic_failures_are_incomplete=False)


# ─── finalize-run ──────────────────────────────────────────────────────

def _propagate_semantic(results_semantic: list) -> tuple[str, str]:
    """单调传播：语义状态和结论上限都取最严格值。"""
    if not results_semantic:
        return "BLOCKED", "BLOCKED"
    worst_status = max((s["semantic_status"] for s in results_semantic),
                       key=lambda s: SEVERITY.get(s, 99))
    strictest_ceiling = max((s["conclusion_ceiling"] for s in results_semantic),
                            key=lambda s: SEVERITY.get(s, 99))
    return worst_status, strictest_ceiling


def _verdict_from_ceiling(ceiling: str) -> str:
    """结论上限决定 run_verdict。"""
    if ceiling == "PASS_WITHIN_FROZEN_SCOPE":
        return "PASS_WITHIN_FROZEN_SCOPE"
    if ceiling == "NEEDS_REVISION":
        return "NEEDS_REVISION"
    return "BLOCKED"


def finalize_run(args) -> int:
    outcome = validate_execution_set(args)

    # 收集逐职责状态（无论成功失败）
    package = json.loads(Path(args.task_package).read_text(encoding="utf-8"))
    results_dir = Path(args.results_dir)
    per_role = []
    for role in package["expected_roles"]:
        rpath = results_dir / f"{role}.json"
        if rpath.is_file():
            try:
                rdata = json.loads(rpath.read_text(encoding="utf-8"))
                per_role.append({"role": role, "status": rdata.get("status")})
            except (OSError, json.JSONDecodeError):
                per_role.append({"role": role, "status": "UNPARSABLE"})
        else:
            per_role.append({"role": role, "status": "MISSING"})

    # ── 验证不通过：失败关闭 ──
    if outcome["status"] != "COMPLETE":
        machine_report = {
            "status": "BLOCKED",
            "reason": "RESULT_SET_INCOMPLETE",
            "failures": outcome.get("failures"),
            "semantic_summary": outcome.get("semantic_summary", []),
            "per_role_statuses": per_role,
        }
        output_root = Path(args.output_root)
        (output_root / "machine-report.json").write_text(
            json.dumps(machine_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "BLOCKED", "reason": "RESULT_SET_INCOMPLETE",
                          "failures": outcome.get("failures")}, ensure_ascii=False))
        return 1

    # ── 语义状态单调传播 ──
    sem_list = outcome.get("semantic_summary", [])
    sem_items = [{"semantic_status": v["semantic_status"],
                   "conclusion_ceiling": v["conclusion_ceiling"]}
                  for v in sem_list.values()] if isinstance(sem_list, dict) else []
    worst, ceiling = _propagate_semantic(sem_items)

    if worst in SEMANTIC_FAIL:
        machine_report = {
            "status": "BLOCKED",
            "reason": "SEMANTIC_FAILURE",
            "worst_semantic_status": worst,
            "semantic_summary": outcome.get("semantic_summary", {}),
            "per_role_statuses": per_role,
        }
        output_root = Path(args.output_root)
        (output_root / "machine-report.json").write_text(
            json.dumps(machine_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "BLOCKED", "reason": "SEMANTIC_FAILURE",
                          "worst_semantic_status": worst}, ensure_ascii=False))
        return 1

    # ── 全部通过 ──
    run_verdict = _verdict_from_ceiling(ceiling)
    result_set_for_digest = [{"role": r["role"], "result_sha256": r["sha256"],
                               "semantic_status": r["semantic_status"]}
                              for r in outcome["results"]]
    set_digest = _sha256_bytes(_canonical(result_set_for_digest))

    report_lines = [
        f"# 审计报告：{outcome['task_id']}",
        "",
        f"平台 {outcome['platform']}、模式 {outcome['mode']} 的 {len(outcome['results'])} 个语义职责全部 COMPLETED。",
        f"语义结论：{worst}；结论上限：{ceiling}；最终裁定：{run_verdict}。",
        "",
        "## 职责回执",
        "",
    ]
    for item in outcome["results"]:
        report_lines.append(
            f"- {item['role']}：COMPLETED（语义 {item['semantic_status']}），"
            f"结果校验和 {item['sha256']}")
    report_lines += ["", "## 附录（机器数据）", "",
                     f"- task_package_sha256: {outcome['task_package_sha256']}",
                     f"- result_set_sha256: {set_digest}",
                     f"- run_verdict: {run_verdict}", ""]
    report_path = Path(args.output_root) / "audit-report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    finalization = {
        "status": "FINALIZED",
        "task_id": outcome["task_id"],
        "task_package_sha256": outcome["task_package_sha256"],
        "result_set_sha256": set_digest,
        "report_sha256": _sha256_file(report_path),
        "run_verdict": run_verdict,
    }
    (Path(args.output_root) / "finalization.json").write_text(
        json.dumps(finalization, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(finalization, ensure_ascii=False))
    return 0


# ─── CLI 入口 ──────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("prepare-run")
    for flag in ("task-id", "platform", "mode", "target", "evidence-type", "output-root", "prompts-root"):
        p1.add_argument(f"--{flag}", required=True)

    p2 = sub.add_parser("validate-result-set")
    p2.add_argument("--task-package", required=True)
    p2.add_argument("--results-dir", required=True)

    p2e = sub.add_parser("validate-execution-set")
    p2e.add_argument("--task-package", required=True)
    p2e.add_argument("--results-dir", required=True)

    pw = sub.add_parser("write-result")
    pw.add_argument("--task-package", required=True)
    pw.add_argument("--role", required=True)
    pw.add_argument("--status", default="COMPLETED")
    pw.add_argument("--receipt-file", default=None)
    pw.add_argument("--artifact-file", default=None)
    pw.add_argument("--outputs-file", default=None)
    pw.add_argument("--outputs-json", default=None)
    pw.add_argument("--error", default=None)
    pw.add_argument("--attempt", type=int, default=1)

    p3 = sub.add_parser("finalize-run")
    p3.add_argument("--task-package", required=True)
    p3.add_argument("--results-dir", required=True)
    p3.add_argument("--output-root", required=True)

    args = parser.parse_args()
    try:
        if args.command == "prepare-run":
            return prepare_run(args)
        if args.command == "write-result":
            return write_result(args)
        if args.command == "validate-result-set":
            outcome = validate_result_set(args)
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return 0 if outcome["status"] == "COMPLETE" else 1
        if args.command == "validate-execution-set":
            outcome = validate_execution_set(args)
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return 0 if outcome["status"] == "COMPLETE" else 1
        return finalize_run(args)
    except ContractError as error:
        print(json.dumps({"status": "REJECTED", "reason": "CONTRACT_ERROR", "detail": str(error)},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

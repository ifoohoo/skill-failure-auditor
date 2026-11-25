#!/usr/bin/env python3
"""将已通过 validate-result 校验的机器审计结果渲染为结论前置的中文报告。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from common import ContractError, load_json, strict_json_loads
from foundation_client import foundation_publish_file_exclusive, require_production_validate


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = SCRIPT_DIR.parent / "references" / "failure-modes.jsonl"
AUDIT_SCHEMA = SCRIPT_DIR.parent / "references" / "audit-result.schema.json"

CONCLUSION_EXPLANATIONS = {
    "PASS_WITHIN_FROZEN_SCOPE": "冻结范围内的必过检查全部通过（不代表普遍正确）",
    "NEEDS_REVISION": "存在可以修复的缺陷",
    "REJECT": "命中红线组合或存在结构性缺陷，需要重写",
    "INCOMPLETE": "证据、覆盖或检查不完整，无法下最终结论",
    "BLOCKED": "缺少权限、独立性或外部状态，无法形成有效结论",
}

NEXT_ACTION_BY_CONCLUSION = {
    "PASS_WITHIN_FROZEN_SCOPE": (
        "审计器只负责诊断，不直接签发接受决定；请将本报告与冻结证据交由外部独立终审职责完成最终验收。"
    ),
    "NEEDS_REVISION": (
        "请受审目标维护者对照下文「逐条发现」完成最小必要修改，随后使用新的输出目录与尝试编号重新送审。"
    ),
    "REJECT": (
        "请受审目标维护者先重构命中红线或结构缺陷的验收与职责设计，补齐可执行验收工件后再重新送审。"
    ),
    "INCOMPLETE": (
        "请调用方补齐下文「未完成事项」列出的缺失证据或未覆盖分片后，在新的输出目录重新发起审计。"
    ),
    "BLOCKED": (
        "请调用方先解除权限、独立审阅职责或外部绑定前置条件，再由具备相应条件的职责继续处理。"
    ),
}

STATUS_EXPLANATIONS = {
    "HIT": "发现问题",
    "NOT_HIT": "未发现问题",
    "NOT_APPLICABLE": "不适用",
    "UNCHECKED": "未检查",
}

SEVERITY_EXPLANATIONS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
}

ACCEPTANCE_EXPLANATIONS = {
    "VERIFIED": "存在并已验证",
    "ABSENT": "缺失",
    "NOT_APPLICABLE": "不适用",
    "UNCHECKED": "未检查",
}

MODE_EXPLANATIONS = {
    "static": "静态定义审阅",
    "runtime": "已有运行材料审阅",
    "combined": "静态定义与已有运行材料联合审阅",
}

MODE_SCOPE_EXPLANATIONS = {
    "static": "本次只审阅静态定义，没有运行受检目标，也没有验证运行表现。",
    "runtime": "本次只审阅已经产生的运行材料，没有运行受检目标。",
    "combined": "本次审阅静态定义和已经产生的运行材料，没有运行受检目标。",
}


def load_registry_names(registry_path: Path) -> dict[str, str]:
    if registry_path.is_symlink() or not registry_path.is_file():
        raise ContractError(f"registry path must be a regular non-symlink file: {registry_path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(registry_path, flags)
    except OSError as error:
        raise ContractError(f"unable to open registry safely: {registry_path}: {error}") from error
    names: dict[str, str] = {}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ContractError(f"registry path must be a regular file: {registry_path}")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as handle:
            fd = -1
            for line_number, line in enumerate(handle, 1):
                physical = line.rstrip("\r\n")
                if not physical.strip():
                    raise ContractError(f"{registry_path}:{line_number}: blank JSONL line")
                entry = strict_json_loads(physical, f"{registry_path}:{line_number}")
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                    raise ContractError(f"{registry_path}:{line_number}: invalid registry entry")
                names[entry["id"]] = entry.get("name_zh", entry["id"])
    finally:
        if fd >= 0:
            os.close(fd)
    return names


def rule_label(result: dict[str, Any], names: dict[str, str]) -> str:
    name = names.get(result["id"])
    if name:
        return f"{result['id']}（{name}）"
    return result["id"]


def format_ref(ref: Any) -> str:
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        return f"分片 {ref.get('chunk_id', '?')}"
    return str(ref)
def render(result: dict[str, Any], names: dict[str, str]) -> str:
    lines: list[str] = []
    rule_results = result["known_rule_results"]
    hits = [item for item in rule_results if item["status"] == "HIT"]
    unchecked = [item for item in rule_results if item["status"] == "UNCHECKED"]
    not_hit_redlines = [
        item
        for item in rule_results
        if item["status"] == "NOT_HIT" and item["severity"] == "critical"
    ]
    conclusion = result["conclusion"]
    conclusion_text = CONCLUSION_EXPLANATIONS.get(conclusion, conclusion)
    next_action = NEXT_ACTION_BY_CONCLUSION.get(conclusion, "请结合下文核查明细由对应职责继续处理。")

    lines.append("# 审计报告")
    lines.append("")
    lines.append("## 总结")
    lines.append("")
    summary = (
        f"本次审计的对象是 `{result['subject']['path']}`，"
        f"审计方式为{MODE_EXPLANATIONS.get(result['mode'], result['mode'])}。"
        f"总体结论：{conclusion_text}。"
        f"共检查 {len(rule_results)} 条规则，发现 {len(hits)} 个问题"
    )
    if unchecked:
        summary += f"，另有 {len(unchecked)} 条规则因缺证据未能检查"
    summary += "。"
    if hits:
        top = hits[0]
        summary += f"最需要关注的是 {rule_label(top, names)}：{top['reason']}"
    lines.append(summary)
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    lines.append(f"- **总体结论**：**{conclusion}** —— {conclusion_text}。")
    lines.append(f"- **责任归属与下一步**：{next_action}")
    if result.get("self_audit"):
        lines.append(
            "- **自审声明**：本次审阅对象为审计技能自身或其同源副本，结论仅用于提交外部复核，不能作为接受依据。"
        )
    lines.append("")

    lines.append("## 结论适用范围")
    lines.append("")
    lines.append(MODE_SCOPE_EXPLANATIONS.get(result["mode"], "本次没有运行受检目标。"))
    lines.append("")
    lines.append(
        "结论只适用于本结果绑定的冻结材料。运行观察只适用于材料实际记录的模型、版本、"
        "配置、任务、时间和环境；材料未记录的信息保持未知，不能推及其他模型或后续运行。"
    )
    lines.append("")

    lines.append("## 逐条发现")
    lines.append("")
    if not hits:
        lines.append("本次审计没有发现命中的问题。")
        lines.append("")
    for index, item in enumerate(hits, 1):
        severity = SEVERITY_EXPLANATIONS.get(item["severity"], item["severity"])
        lines.append(f"### {index}. {rule_label(item, names)}（严重度：{severity}）")
        lines.append("")
        lines.append(f"- 故障与修复建议：{item['reason']}")
        refs = item.get("evidence_refs", [])
        if refs:
            rendered_refs = "、".join(format_ref(ref) for ref in refs)
            lines.append(f"- 证据位置：{rendered_refs}")
        lines.append("")

    if not_hit_redlines:
        lines.append("## 已检查且未发现问题的必查红线")
        lines.append("")
        for item in not_hit_redlines:
            lines.append(f"- {rule_label(item, names)}：{item['reason']}")
        lines.append("")

    lines.append("## 清单外的新问题")
    lines.append("")
    hypotheses = result.get("novel_hypotheses", [])
    real_hypotheses = [item for item in hypotheses if item.get("hypothesis")]
    if not real_hypotheses:
        lines.append("未提出清单外的新假设。")
        lines.append("")
    for item in real_hypotheses:
        lines.append(f"- **{item['id']}**：{item['hypothesis']}")
        lines.append(f"  - 可观察迹象：{item['observable_signal']}")
        lines.append(f"  - 怎样算被证伪：{item['falsifier']}")
        lines.append(f"  - 下一步验证：{item['next_probe']}")
    lines.append("")

    lines.append("## 未完成事项")
    lines.append("")
    unfinished: list[str] = []
    for item in unchecked:
        unfinished.append(f"{rule_label(item, names)} 未检查：{item['reason']}")
    for failure in result.get("hard_gate_failures", []):
        unfinished.append(f"必过检查未通过：{failure}")
    acceptance = result.get("executable_acceptance", {})
    if acceptance and acceptance.get("status") != "VERIFIED":
        status_text = ACCEPTANCE_EXPLANATIONS.get(acceptance["status"], acceptance["status"])
        unfinished.append(f"可执行验收工件{status_text}：{acceptance.get('reason', '')}")
    if not unfinished:
        lines.append("无。")
    else:
        for item in unfinished:
            lines.append(f"- {item}")
    lines.append("")

    lines.append("## 附录：机器数据")
    lines.append("")
    subject = result["subject"]
    subject_digest = subject.get("file_set_sha256") or subject.get("sha256") or "?"
    lines.append(f"- 审计标识：`{result['audit_id']}`")
    lines.append(f"- 覆盖状态：`{result['coverage_status']}`")
    lines.append(f"- 结果状态：`{result['status']}`")
    lines.append(f"- 目标文件集校验和：`{subject_digest}`")
    lines.append(f"- 登记表校验和：`{result.get('registry_sha256', '?')}`")
    lines.append(f"- 选择校验和：`{result.get('selection_sha256', '?')}`")
    lines.append("")
    lines.append("| 规则 | 严重度 | 状态 | 证据分片 |")
    lines.append("|---|---|---|---|")
    for item in rule_results:
        status_text = STATUS_EXPLANATIONS.get(item["status"], item["status"])
        severity = SEVERITY_EXPLANATIONS.get(item["severity"], item["severity"])
        refs = "、".join(format_ref(ref) for ref in item.get("evidence_refs", [])) or "—"
        lines.append(f"| {item['id']} | {severity} | {status_text} | {refs} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="已通过 evaluation_tool.py validate-result 校验的 audit-result.json 文件路径。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="待新建的中文审计报告输出路径（目标文件不得预先存在）。",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="用于映射中文规则名称的 failure-modes.jsonl 路径。",
    )
    args = parser.parse_args()
    try:
        result = load_json(args.input)
        schema = load_json(AUDIT_SCHEMA)
        require_production_validate(result, schema)
        names = load_registry_names(args.registry)
        report = render(result, names)
        foundation_publish_file_exclusive(args.output, report.encode("utf-8"), mode=0o644)
        return 0
    except (ContractError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

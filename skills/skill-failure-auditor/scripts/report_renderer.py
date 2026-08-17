#!/usr/bin/env python3
"""Render a validated audit-result JSON into a human-readable Markdown report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import ContractError, load_json
from foundation_client import foundation_publish_file_exclusive


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = SCRIPT_DIR.parent / "references" / "failure-modes.jsonl"

CONCLUSION_EXPLANATIONS = {
    "PASS_WITHIN_FROZEN_SCOPE": "冻结范围内的必过检查全部通过（不代表普遍正确）",
    "NEEDS_REVISION": "存在可以修复的缺陷",
    "REJECT": "命中红线组合或存在结构性缺陷，需要重写",
    "INCOMPLETE": "证据、覆盖或检查不完整，无法下最终结论",
    "BLOCKED": "缺少权限、独立性或外部状态，无法形成有效结论",
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
    "static": "静态审计",
    "runtime": "运行期审计",
    "combined": "静态加运行期联合审计",
}


def load_registry_names(registry_path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    with registry_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            names[entry["id"]] = entry.get("name_zh", entry["id"])
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
    lines.append(f"**{conclusion}** —— {conclusion_text}。")
    if result.get("self_audit"):
        lines.append("")
        lines.append("注意：这是技能对自身的一次审计，结论只能提交外部复核，不能作为接受依据。")
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
        lines.append(f"- 问题：{item['reason']}")
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
    parser.add_argument("--input", type=Path, required=True, help="validated audit-result JSON")
    parser.add_argument("--output", type=Path, required=True, help="human-readable Markdown report path")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args()
    try:
        result = load_json(args.input)
        if not isinstance(result, dict) or "known_rule_results" not in result:
            raise ContractError("input is not an audit-result JSON object")
        names = load_registry_names(args.registry)
        report = render(result, names)
        foundation_publish_file_exclusive(args.output, report.encode("utf-8"), mode=0o644)
        return 0
    except (ContractError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

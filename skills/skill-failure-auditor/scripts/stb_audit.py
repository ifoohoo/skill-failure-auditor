#!/usr/bin/env python3
"""只读核验会话追溯包（SessionTraceBundle），对照客观运行证据识别伪造完成与上下文失控。

本模块遵循 skill-failure-auditor 只读边界：
1. 不启动任何目标技能或外部进程（除受管 Foundation Bundle 校验、封存摘要与字节切片核验桥接外）；
2. STB 本身只承载客观采集事实，所有对账结论、红牌判定与共同证明均由本模块在受审目录外独立产出；
3. 可观测性缺失（如离线回放缺失 exitCode 或写集快照）一律标记为 unverifiable 并降级为 partial + limitations，绝不伪造通过或失败。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath
from typing import Any

from common import ContractError, normalize_relative_path, strict_json_loads, write_json_exclusive
from foundation_client import (
    foundation_digest_document,
    foundation_read_file_strict,
    foundation_resolve_contained,
    foundation_verify_raw_locator_slice,
    foundation_verify_session_trace_bundle_seal,
    require_professional_conclusion,
    require_session_trace_bundle,
)

STB_AUDIT_ENTRY = "evaluation_tool.py audit-stb"
REPRO_CANDIDATE_ENTRY = "evaluation_tool.py export-repro-candidate"
_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|auth|api[_-]?key|token|secret|password|cookie|private[_-]?key|credential)",
    re.IGNORECASE,
)
_REPRO_TEXT_REDACTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[^\r\n]*(?:\r?\n[A-Za-z0-9+/=]{8,}[^\r\n]*)*"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED_SECRET]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{20,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[REDACTED_GCP_KEY]"),
    (re.compile(r"\b(Bearer|Basic)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE), r"\1 [REDACTED_SECRET]"),
    (re.compile(r"(https?://)(?!\[REDACTED_AUTH\]@)([^/\s:@]+:[^/\s@]+@)", re.IGNORECASE), r"\1[REDACTED_AUTH]@"),
    (re.compile(r'(\\"(?:[A-Za-z0-9_-]*(?:authorization|auth|api[_-]?key|apikey|secret|token|password|access[_-]?key|client[_-]?secret|cookie|private[_-]?key|credential))\\"\s*:\s*\\")(?!\[REDACTED)([^"\\]+)(\\")', re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r'("(?:[A-Za-z0-9_-]*(?:authorization|auth|api[_-]?key|apikey|secret|token|password|access[_-]?key|client[_-]?secret|cookie|private[_-]?key|credential))"\s*:\s*")(?!\[REDACTED)([^"]+)(")', re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"('(?:[A-Za-z0-9_-]*(?:authorization|auth|api[_-]?key|apikey|secret|token|password|access[_-]?key|client[_-]?secret|cookie|private[_-]?key|credential))'\s*:\s*')(?!\[REDACTED)([^']+)(')", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"\b([A-Za-z0-9_-]*(?:authorization|api[_-]?key|apikey|secret|token|password|access[_-]?key|client[_-]?secret|cookie|private[_-]?key|credential)\s*:\s*)(['\"]?)(?!\[REDACTED)([^\s\"',;}\]]+)\2", re.IGNORECASE), r"\1\2[REDACTED_SECRET]\2"),
    (re.compile(r'\b([A-Za-z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|ACCESS_KEY|CLIENT_SECRET|CREDENTIAL)=\\")(?!\[REDACTED)([^"\\]+)(\\")', re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r'\b([A-Za-z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|ACCESS_KEY|CLIENT_SECRET|CREDENTIAL)=")(?!\[REDACTED)([^"]+)(")', re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"\b([A-Za-z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|ACCESS_KEY|CLIENT_SECRET|CREDENTIAL)=')(?!\[REDACTED)([^']+)(')", re.IGNORECASE), r"\1[REDACTED_SECRET]\3"),
    (re.compile(r"\b([A-Za-z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|ACCESS_KEY|CLIENT_SECRET|CREDENTIAL)=)(?!\[REDACTED|[\\\"'])([^\s\"'&;]+)", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"[A-Za-z]:(?:\\\\|\\|/)[U]sers(?:\\\\|\\|/)[^\\/\s\"':;),]+"), "C:/[REDACTED_USER_HOME]"),
    (re.compile(r"/[U]sers/[^/\s\"':;),]+"), "/[REDACTED_USER_HOME]"),
    (re.compile(r"/[h]ome/[^/\s\"':;),]+"), "/[REDACTED_USER_HOME]"),
    (re.compile(r"/[V]olumes/[^/\s\"':;),]+(?:\s(?!-)[^/\s\"':;),]+)*(?=/)"), "/[REDACTED_VOLUME]"),
    (re.compile(r"/[V]olumes/[^/\s\"':;),]+"), "/[REDACTED_VOLUME]"),
]
STB_CHECKED_RULES = [
    "FM-01",
    "FM-02",
    "FM-05",
    "FM-09",
    "FM-20",
    "FM-22",
    "FM-23",
    "FM-25",
    "FM-26",
    "FM-27",
]

OBSERVABILITY_FIELD_LABELS = {
    "perTurnUsage": "逐轮 Token 与窗口计量（perTurnUsage=absent）",
    "toolArgs": "工具调用参数（toolArgs=absent）",
    "toolOutput": "工具调用输出（toolOutput=absent）",
    "exitCode": "结构化命令退出码（exitCode=absent）",
    "osProcess": "操作系统进程组生命周期（osProcess=absent）",
    "writeObservation": "文件系统写集观测（writeObservation=absent）",
    "sandboxEvents": "沙箱拦截与放行事件（sandboxEvents=absent）",
    "exposedTools": "工具暴露清单（exposedTools=absent）",
}


def _safe_repo_rel_path(raw: str) -> str | None:
    """Normalize a repository-relative POSIX path without corrupting leading dotfiles (e.g. .github/).
    Returns None if raw is empty, absolute, or contains '..' traversal segments.
    """
    if not isinstance(raw, str):
        return None
    s = raw.replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    if not s or s == "." or s.startswith("/"):
        return None
    parts = PurePosixPath(s).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _matches_any_pattern(rel_path: str, patterns: list[str]) -> bool:
    norm = _safe_repo_rel_path(rel_path)
    if norm is None:
        return False
    for pat in patterns:
        clean = pat.replace("\\", "/").strip()
        while clean.startswith("./"):
            clean = clean[2:]
        if not clean or clean.startswith("/"):
            continue
        if clean == "**" or fnmatch.fnmatchcase(norm, clean):
            return True
        if clean.endswith("/**"):
            prefix = clean[:-3]
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
    return False


def _finding(
    rule_id: str,
    severity: str,
    summary: str,
    evidence: dict[str, Any],
    *,
    red_card: bool = False,
) -> dict[str, Any]:
    return {
        "ruleId": rule_id,
        "severity": severity,
        "redCard": red_card,
        "summary": summary,
        "evidence": evidence,
    }


def _collect_descendant_sessions(sessions_by_id: dict[str, dict[str, Any]], root_sid: str) -> set[str]:
    children_map: dict[str, list[str]] = {}
    for sid, sess in sessions_by_id.items():
        parent = sess.get("parentSessionId")
        if isinstance(parent, str) and parent:
            children_map.setdefault(parent, []).append(sid)
    owned: set[str] = set()
    queue = [root_sid]
    while queue:
        cur = queue.pop(0)
        if cur in owned:
            continue
        owned.add(cur)
        queue.extend(children_map.get(cur, []))
    return owned


def evaluate_stb_document(
    stb: dict[str, Any],
    *,
    provider_id: str,
    provider_version: str,
    stb_root: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and audit an in-memory STB dict, returning (reconciliation_report, professional_conclusion)."""
    # 1. Foundation schema + cross-field invariants (raises ContractError on structural/invariant breach)
    require_session_trace_bundle(stb)

    # 2. Foundation seal verification
    seal_res = foundation_verify_session_trace_bundle_seal(stb)
    bundle_id = stb["bundleId"]
    declared_digest = stb["seal"]["bundleDigestSha256"]
    if seal_res.get("valid") is True and seal_res.get("sealValid") is True:
        recomputed_digest = seal_res["bundleDigestSha256"]
        seal_valid = True
    elif "sealValid" in seal_res and seal_res.get("sealValid") is False:
        recomputed_digest = seal_res.get("recomputedBundleDigestSha256") or foundation_digest_document(
            {k: v for k, v in stb.items() if k != "seal"}
        )
        seal_valid = False
    else:
        unsigned_body = {k: v for k, v in stb.items() if k != "seal"}
        recomputed_digest = foundation_digest_document(unsigned_body)
        seal_valid = recomputed_digest == declared_digest

    findings: list[dict[str, Any]] = []
    context_alerts: list[dict[str, Any]] = []
    limitations: list[str] = []
    red_card_reasons: list[str] = []

    if not seal_valid:
        reason = f"STB 封存摘要失配：声明 {declared_digest}，重算 {recomputed_digest}"
        findings.append(
            _finding(
                "FM-27",
                "critical",
                reason,
                {
                    "bundleId": bundle_id,
                    "declaredBundleDigestSha256": declared_digest,
                    "recomputedBundleDigestSha256": recomputed_digest,
                },
                red_card=True,
            )
        )
        red_card_reasons.append(reason)

    evidence_by_id = {ev["evidenceId"]: ev for ev in stb.get("evidenceRefs", [])}
    source_by_id = {src["sourceId"]: src for src in stb.get("rawSources", [])}

    for src in stb.get("rawSources", []):
        cov = src.get("coverage", {})
        if (
            not cov.get("complete", False)
            or cov.get("readBytes", 0) < cov.get("totalBytes", 0)
            or cov.get("keptLines", 0) < cov.get("totalLines", 0)
            or bool(cov.get("droppedRanges"))
        ):
            msg = f"数据源 {src['sourceId']} 未完整覆盖原始日志（read={cov.get('readBytes')}/{cov.get('totalBytes')} bytes）"
            findings.append(
                _finding(
                    "FM-26",
                    "critical",
                    msg,
                    {"sourceId": src["sourceId"], "coverage": cov},
                )
            )
            limitations.append(msg)

    # 3. Optional rawLocator byte-slice verification when stb_root is supplied
    if stb_root is not None:
        root_path = Path(stb_root)
        locators_to_check: list[tuple[str, dict[str, Any]]] = []
        for sess in stb.get("sessions", []):
            for t in sess.get("turns", []):
                if isinstance(t.get("rawLocator"), dict):
                    locators_to_check.append((f"session:{sess['sessionId']}:turn:{t['turnIndex']}", t["rawLocator"]))
        for tc in stb.get("toolCalls", []):
            if isinstance(tc.get("rawLocator"), dict):
                locators_to_check.append((f"toolCall:{tc['callId']}", tc["rawLocator"]))
        for cl in stb.get("claims", []):
            if isinstance(cl.get("rawLocator"), dict):
                locators_to_check.append((f"claim:{cl['claimId']}", cl["rawLocator"]))

        missing_raw_files: set[str] = set()
        for owner_label, loc in locators_to_check:
            src_obj = source_by_id.get(loc["sourceId"])
            ev_obj = evidence_by_id.get(src_obj["evidenceId"]) if src_obj else None
            rel_raw = ev_obj.get("relativePath") if ev_obj else None
            if not isinstance(rel_raw, str) or not rel_raw:
                continue
            safe_rel = _safe_repo_rel_path(rel_raw)
            if safe_rel is None:
                reason = f"原始证据路径非法或越界：{rel_raw}"
                findings.append(_finding("FM-27", "critical", reason, {"owner": owner_label, "relativePath": rel_raw}, red_card=True))
                red_card_reasons.append(reason)
                continue
            candidate_file = root_path / safe_rel
            if not candidate_file.exists() and not candidate_file.is_symlink():
                missing_raw_files.add(safe_rel)
                continue
            try:
                slice_res = foundation_verify_raw_locator_slice(
                    stb_root,
                    safe_rel,
                    byte_offset=loc["byteOffset"],
                    byte_length=loc["byteLength"],
                    expected_slice_sha256=loc["sliceSha256"],
                )
            except ContractError as exc:
                slice_res = {"valid": False, "error": str(exc)}
            if slice_res.get("valid") is not True:
                reason = f"{owner_label} 的 rawLocator 字节切片摘要与原始证据文件 {safe_rel} 不一致"
                findings.append(
                    _finding(
                        "FM-27",
                        "critical",
                        reason,
                        {"owner": owner_label, "relativePath": safe_rel, "locator": loc},
                        red_card=True,
                    )
                )
                red_card_reasons.append(reason)
        for missing_rel in sorted(missing_raw_files):
            limitations.append(f"原始日志证据文件 {missing_rel} 未随 STB 同目录提供，跳过 rawLocator 字节切片复核")

    sessions_by_id: dict[str, dict[str, Any]] = {}
    writes_by_session: dict[str, list[dict[str, Any]]] = {}
    worker_model_ids: set[str] = set()
    worker_model_instr_sigs: set[tuple[str, str]] = set()
    worker_session_ids: set[str] = set()

    for session in stb.get("sessions", []):
        sid = session["sessionId"]
        sessions_by_id[sid] = session
        role = session["role"]
        obs_fields = session["observability"]["fields"]

        # Disclose all 8 observability absent fields in limitations
        for field_key, label in OBSERVABILITY_FIELD_LABELS.items():
            if obs_fields.get(field_key) == "absent":
                limitations.append(f"会话 {sid} 缺少{label}")

        if role in {"coordinator", "worker"}:
            worker_session_ids.add(sid)
            model_id = session["model"]["id"]
            worker_model_ids.add(model_id)
            instr_sha = session["contextLineage"].get("instructionsSha256")
            if isinstance(instr_sha, str) and instr_sha:
                worker_model_instr_sigs.add((model_id, instr_sha))

        # Reviewer exposedTools isolation check (FM-25)
        exposed_tools = session.get("exposedTools") or []
        if role == "reviewer":
            non_readonly_tools = [
                t["name"]
                for t in exposed_tools
                if isinstance(t, dict) and t.get("sideEffectClass") != "read_only"
            ]
            if non_readonly_tools:
                reason = f"审阅角色会话 {sid} 暴露了非只读工具 {non_readonly_tools}，违反只读审阅隔离边界"
                findings.append(
                    _finding(
                        "FM-25",
                        "critical",
                        reason,
                        {"sessionId": sid, "nonReadOnlyTools": non_readonly_tools},
                        red_card=True,
                    )
                )
                red_card_reasons.append(reason)

        write_scope = session["writeScope"]
        observed_writes = write_scope.get("observedWrites")
        allowed_patterns = write_scope.get("allowedPatterns", [])
        if isinstance(observed_writes, list):
            writes_by_session[sid] = list(observed_writes)
            if role == "reviewer" and observed_writes:
                reason = f"审阅角色会话 {sid} 产生了实际文件写入（共 {len(observed_writes)} 处），违反只读审阅隔离"
                findings.append(
                    _finding(
                        "FM-05",
                        "critical",
                        reason,
                        {"sessionId": sid, "observedWrites": observed_writes},
                        red_card=True,
                    )
                )
                findings.append(
                    _finding(
                        "FM-25",
                        "critical",
                        f"审阅会话 {sid} 的只读限制未被沙箱强制拦截",
                        {"sessionId": sid, "observedWrites": observed_writes},
                        red_card=True,
                    )
                )
                red_card_reasons.append(reason)
            elif observed_writes:
                unauthorized = [
                    w["path"]
                    for w in observed_writes
                    if _safe_repo_rel_path(w["path"]) is None
                    or not _matches_any_pattern(w["path"], allowed_patterns)
                ]
                if unauthorized:
                    reason = f"会话 {sid} 写入了非法越界或超出 allowedPatterns 的路径：{unauthorized}"
                    findings.append(
                        _finding(
                            "FM-25",
                            "critical",
                            reason,
                            {
                                "sessionId": sid,
                                "allowedPatterns": allowed_patterns,
                                "unauthorizedPaths": unauthorized,
                            },
                            red_card=True,
                        )
                    )
                    red_card_reasons.append(reason)

        # Context ratio checks (skip if perTurnUsage == absent)
        if obs_fields.get("perTurnUsage") != "absent":
            policy = session["appliedContextPolicy"]
            warn_bp = policy.get("warnRatioBp", 4000)
            mitigations = policy.get("mitigations", [])
            for turn in session.get("turns", []):
                ratio = turn.get("windowRatioBp")
                t_idx = turn.get("turnIndex", 0)
                if ratio is None:
                    continue
                if ratio > 10000:
                    findings.append(
                        _finding(
                            "FM-20",
                            "high",
                            f"会话 {sid} 第 {t_idx} 轮 windowRatioBp={ratio} 超过 10000 Bp（100%），疑似把累计输入当作单轮窗口占用",
                            {"sessionId": sid, "turnIndex": t_idx, "windowRatioBp": ratio},
                        )
                    )
                elif ratio >= warn_bp:
                    # Mitigation must occur on the alerting turn or the immediately following turn
                    mitigated = any(
                        t_idx <= m.get("turnIndex", -1) <= t_idx + 1
                        for m in mitigations
                    )
                    alert = {
                        "sessionId": sid,
                        "turnIndex": t_idx,
                        "windowRatioBp": ratio,
                        "warnRatioBp": warn_bp,
                        "mitigated": mitigated,
                    }
                    context_alerts.append(alert)
                    if not mitigated:
                        findings.append(
                            _finding(
                                "FM-09",
                                "high",
                                f"会话 {sid} 第 {t_idx} 轮窗口占用达到 {ratio} Bp（触及或超过预警线 {warn_bp} Bp）且未在当轮或紧邻下一轮执行减压动作",
                                alert,
                            )
                        )

    calls_by_id: dict[str, dict[str, Any]] = {}
    session_calls: dict[str, list[dict[str, Any]]] = {}
    for tc in stb.get("toolCalls", []):
        calls_by_id[tc["callId"]] = tc
        session_calls.setdefault(tc["sessionId"], []).append(tc)

    interactions = stb.get("interactions", [])

    # Partition claims so goal_completed is ALWAYS evaluated after all other claims
    indexed_claims = list(enumerate(stb.get("claims", [])))
    ordered_claims = [
        item for item in indexed_claims if item[1].get("claimType") != "goal_completed"
    ] + [
        item for item in indexed_claims if item[1].get("claimType") == "goal_completed"
    ]

    results_by_orig_idx: dict[int, dict[str, Any]] = {}
    evaluated_reconciliations: list[dict[str, Any]] = []

    for orig_idx, claim in ordered_claims:
        cid = claim["claimId"]
        sid = claim["sessionId"]
        ctype = claim["claimType"]
        is_critical = bool(claim.get("isCritical", False))
        cited_call_ids = list(claim.get("agentCitedCallIds", []))
        cited_ev_ids = list(claim.get("agentCitedEvidenceIds", []))
        session = sessions_by_id.get(sid, {})
        role = session.get("role", "unknown")
        obs_fields = session.get("observability", {}).get("fields", {})
        owned_sessions = _collect_descendant_sessions(sessions_by_id, sid)

        missing_calls = [x for x in cited_call_ids if x not in calls_by_id]
        missing_evs = [x for x in cited_ev_ids if x not in evidence_by_id]
        cited_calls = [calls_by_id[x] for x in cited_call_ids if x in calls_by_id]
        foreign_calls = [
            c["callId"] for c in cited_calls if c.get("sessionId") not in owned_sessions
        ]

        # Fail-closed default: unverifiable unless positively proven by objective records
        rec_status = "unverifiable"
        rec_reason = f"声明 {cid} ({ctype}) 缺少可独立核验的客观运行证据"
        triggered_rules: list[str] = []

        if missing_calls or missing_evs:
            rec_status = "contradicted"
            rec_reason = f"声明 {cid} 引用了不存在的调用或证据标识（missingCalls={missing_calls}, missingEvidence={missing_evs}）"
            triggered_rules.extend(["FM-02", "FM-01"])
        elif foreign_calls:
            rec_status = "contradicted"
            rec_reason = f"声明 {cid} 跨会话冒用了非本会话或受托子会话的工具调用 {foreign_calls}"
            triggered_rules.extend(["FM-02", "FM-01"])
        elif ctype in {"test_pass", "gate_pass", "rework_completed"}:
            if not cited_calls:
                rec_status = "contradicted"
                rec_reason = f"声明 {cid} 声称 {ctype} 完成，但未绑定任何真实命令执行记录"
                triggered_rules.extend(["FM-02", "FM-01"])
            else:
                failed_calls = [
                    c["callId"]
                    for c in cited_calls
                    if c["status"] != "succeeded"
                    or (c["exitCode"] is not None and c["exitCode"] != 0)
                ]
                absent_exit_calls = [
                    c["callId"]
                    for c in cited_calls
                    if c["exitCode"] is None or c["exitCodeSource"] == "absent"
                ]
                if failed_calls:
                    rec_status = "contradicted"
                    rec_reason = f"声明 {cid} 声称 {ctype} 通过，但所绑定工具调用 {failed_calls} 实际失败或退出码非零"
                    triggered_rules.extend(["FM-01", "FM-22"])
                elif absent_exit_calls:
                    rec_status = "unverifiable"
                    rec_reason = f"声明 {cid} 绑定的工具调用 {absent_exit_calls} 缺少结构化退出码（exitCode=absent），无法独立证实通过"
                    limitations.append(rec_reason)
                else:
                    rec_status = "verified"
                    rec_reason = "声明与引用的客观命令退出码（0）完全一致"
        elif ctype == "artifact_written":
            target_ref = claim["target"]["ref"]
            expected_sha = claim["target"].get("sha256")
            norm_target = _safe_repo_rel_path(target_ref)
            write_obs = session.get("writeScope", {}).get("observation", {}).get("method")
            owned_writes = [
                w
                for s_id in owned_sessions
                for w in writes_by_session.get(s_id, [])
            ]
            if norm_target is None:
                rec_status = "contradicted"
                rec_reason = f"声明 {cid} 指向非法或越界制品路径 {target_ref}"
                triggered_rules.extend(["FM-25", "FM-02", "FM-01"])
            else:
                same_path_writes = [
                    w for w in owned_writes if _safe_repo_rel_path(w["path"]) == norm_target
                ]
                matching_writes = [
                    w
                    for w in same_path_writes
                    if w["op"] in {"create", "modify"}
                    and (expected_sha is None or w.get("sha256After") == expected_sha)
                ]
                if matching_writes:
                    rec_status = "verified"
                    rec_reason = "制品写入路径与变更后摘要已由会话写集快照证实"
                elif same_path_writes:
                    rec_status = "contradicted"
                    rec_reason = f"声明 {cid} 声称写入制品 {target_ref}，但写集记录的操作类型或 sha256After 与声明失配"
                    triggered_rules.extend(["FM-02", "FM-01"])
                elif write_obs == "absent" or obs_fields.get("writeObservation") == "absent":
                    rec_status = "unverifiable"
                    rec_reason = f"声明 {cid} 声称写入制品 {target_ref}，但会话缺少写集快照观测（writeObservation=absent）"
                    limitations.append(rec_reason)
                else:
                    rec_status = "contradicted"
                    rec_reason = f"声明 {cid} 声称写入制品 {target_ref}，但本会话及受托子会话写集快照中不存在该文件变更"
                    triggered_rules.extend(["FM-02", "FM-01"])
        elif ctype == "independent_review_pass":
            session_writes = session.get("writeScope", {}).get("observedWrites") or []
            exposed_tools = session.get("exposedTools") or []
            has_write_tool = any(
                isinstance(t, dict) and t.get("sideEffectClass") != "read_only"
                for t in exposed_tools
            )
            forked_from = session.get("contextLineage", {}).get("forkedFromSessionId")
            if role != "reviewer" or session_writes or has_write_tool:
                rec_status = "contradicted"
                rec_reason = f"声明 {cid} 声称独立审阅通过，但出自非只读隔离审阅会话（role={role}, writes={len(session_writes)}, hasWriteTool={has_write_tool}）"
                triggered_rules.append("FM-05")
            elif isinstance(forked_from, str) and forked_from in worker_session_ids:
                rec_status = "contradicted"
                rec_reason = f"审阅会话 {sid} 直接自实现会话 {forked_from} 分叉继承上下文，不能冒充认知独立审阅"
                triggered_rules.append("FM-23")
            else:
                model_id = session["model"]["id"]
                instr_sha = session["contextLineage"].get("instructionsSha256")
                if instr_sha is None:
                    if model_id in worker_model_ids:
                        rec_status = "unverifiable"
                        rec_reason = f"审阅会话 {sid} 与实现会话使用相同模型 {model_id} 且缺少指令摘要（instructionsSha256=null），无法证实认知独立性"
                        limitations.append(rec_reason)
                    else:
                        rec_status = "verified"
                        rec_reason = "独立只读审阅会话模型异构且无写副作用"
                elif (model_id, instr_sha) in worker_model_instr_sigs:
                    rec_status = "contradicted"
                    rec_reason = f"审阅会话 {sid} 与实现会话复用完全相同的模型与指令摘要，不能冒充认知独立审阅"
                    triggered_rules.append("FM-23")
                else:
                    rec_status = "verified"
                    rec_reason = "独立只读审阅会话认知边界与只读约束已核实"
        elif ctype == "handoff_delivered":
            matched_handoff = any(
                ix.get("fromSessionId") in owned_sessions
                for ix in interactions
            )
            if matched_handoff or cited_ev_ids:
                rec_status = "verified"
                rec_reason = "交接投递已由跨会话交互账本或引用证据核实"
            else:
                rec_status = "contradicted"
                rec_reason = f"声明 {cid} 声称已完成任务交接投递，但交互账本中不存在交接记录"
                triggered_rules.extend(["FM-02", "FM-01"])
        elif ctype == "context_mitigated":
            mitigations = session.get("appliedContextPolicy", {}).get("mitigations") or []
            if mitigations:
                rec_status = "verified"
                rec_reason = "上下文减压缓解动作已在会话策略记录中核实"
            else:
                rec_status = "contradicted"
                rec_reason = f"声明 {cid} 声称已执行上下文减压动作，但会话 mitigations 记录为空"
                triggered_rules.extend(["FM-09", "FM-02"])
        elif ctype == "goal_completed":
            prior_contradicted = [
                r["claimId"] for r in evaluated_reconciliations if r["status"] == "contradicted"
            ]
            prior_unverifiable = [
                r["claimId"] for r in evaluated_reconciliations if r["status"] == "unverifiable"
            ]
            prior_verified = [
                r["claimId"] for r in evaluated_reconciliations if r["status"] == "verified"
            ]
            own_calls = [
                c
                for s_id in owned_sessions
                for c in session_calls.get(s_id, [])
            ]
            unrecovered_failures = False
            if own_calls:
                last_cmd_calls = [c for c in own_calls if c["category"] == "command_exec"]
                if last_cmd_calls and (
                    last_cmd_calls[-1]["status"] != "succeeded"
                    or (
                        last_cmd_calls[-1]["exitCode"] is not None
                        and last_cmd_calls[-1]["exitCode"] != 0
                    )
                ):
                    unrecovered_failures = True
            failed_cited = [
                c["callId"]
                for c in cited_calls
                if c["status"] != "succeeded"
                or (c["exitCode"] is not None and c["exitCode"] != 0)
            ]
            absent_cited = [
                c["callId"]
                for c in cited_calls
                if c["category"] == "command_exec" and (c["exitCode"] is None or c["exitCodeSource"] == "absent")
            ]
            if prior_contradicted or failed_cited or unrecovered_failures:
                rec_status = "contradicted"
                rec_reason = f"声明 {cid} 声称整体目标完成，但关键声明矛盾或末尾命令失败未恢复"
                triggered_rules.append("FM-01")
            elif prior_unverifiable or absent_cited or (not cited_calls and not prior_verified):
                rec_status = "unverifiable"
                rec_reason = f"声明 {cid} 声称目标完成，但存在未证实声明或缺少可独立核验的执行证据"
                limitations.append(rec_reason)
            else:
                rec_status = "verified"
                rec_reason = "整体目标完成声明与前置客观核验记录一致"
        else:
            limitations.append(rec_reason)

        if rec_status == "contradicted":
            is_red_card = is_critical or any(
                r in {"FM-01", "FM-02", "FM-05", "FM-22", "FM-25", "FM-27"} for r in triggered_rules
            )
            for rule_id in dict.fromkeys(triggered_rules):
                findings.append(
                    _finding(
                        rule_id,
                        "critical" if is_red_card else "high",
                        rec_reason,
                        {
                            "claimId": cid,
                            "claimType": ctype,
                            "sessionId": sid,
                            "citedCallIds": cited_call_ids,
                        },
                        red_card=is_red_card,
                    )
                )
            if is_red_card:
                red_card_reasons.append(rec_reason)

        rec_entry = {
            "claimId": cid,
            "sessionId": sid,
            "claimType": ctype,
            "isCritical": is_critical,
            "status": rec_status,
            "reason": rec_reason,
            "triggeredRules": list(dict.fromkeys(triggered_rules)),
        }
        evaluated_reconciliations.append(rec_entry)
        results_by_orig_idx[orig_idx] = rec_entry

    claim_reconciliations = [results_by_orig_idx[i] for i in range(len(indexed_claims))]
    dedup_limitations = list(dict.fromkeys(limitations))
    dedup_red_cards = list(dict.fromkeys(red_card_reasons))
    suspected_fabrication = len(dedup_red_cards) > 0

    if suspected_fabrication:
        verdict = "SUSPECTED_FABRICATION"
        prof_code = "FAILURE_DIAGNOSIS_FINDINGS"
        completion = "partial" if dedup_limitations else "complete"
        summary = f"检测到 {len(dedup_red_cards)} 项疑似伪造完成硬闸违规：{dedup_red_cards[0]}"
    elif findings:
        verdict = "FINDINGS"
        prof_code = "FAILURE_DIAGNOSIS_FINDINGS"
        completion = "partial" if dedup_limitations else "complete"
        summary = f"会话追溯包对账发现 {len(findings)} 项治理风险。"
    elif dedup_limitations:
        verdict = "PARTIAL_UNVERIFIABLE"
        prof_code = "FAILURE_DIAGNOSIS_INCOMPLETE"
        completion = "partial"
        summary = "会话追溯包未发现显式矛盾，但部分可观测性字段缺失，仅完成受限核验。"
    else:
        verdict = "PASS"
        prof_code = "FAILURE_DIAGNOSIS_CLEAR"
        completion = "complete"
        summary = "会话追溯包封存摘要、完成声明、写集边界与上下文水位全部核验通过。"

    reconciliation_report: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "skill-family.stb-reconciliation-report",
        "bundleId": bundle_id,
        "attemptId": stb["attempt"]["attemptId"],
        "subject": stb["subject"],
        "declaredBundleDigestSha256": declared_digest,
        "recomputedBundleDigestSha256": recomputed_digest,
        "sealValid": seal_valid,
        "verdict": verdict,
        "suspectedFabrication": suspected_fabrication,
        "redCardReasons": dedup_red_cards,
        "contextAlerts": context_alerts,
        "claimReconciliations": claim_reconciliations,
        "findings": findings,
        "checkedRules": list(STB_CHECKED_RULES),
        "limitations": dedup_limitations,
    }

    professional_conclusion: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "skill-family.professional-conclusion",
        "provider": {
            "id": provider_id,
            "version": provider_version,
            "entry": STB_AUDIT_ENTRY,
        },
        "subject": {
            "ref": f"stb:{bundle_id}",
            "revision": f"sha256:{recomputed_digest}",
        },
        "scope": {
            "checked": list(STB_CHECKED_RULES),
            "limitations": dedup_limitations,
        },
        "outcome": {
            "completion": completion,
            "code": prof_code,
            "summary": summary,
        },
        "details": reconciliation_report,
    }
    require_professional_conclusion(professional_conclusion)
    return reconciliation_report, professional_conclusion


def run_stb_audit(
    *,
    stb_root: str,
    stb_relative: str,
    output_root: str,
    report_relative: str,
    conclusion_relative: str,
    provider_id: str,
    provider_version: str,
) -> dict[str, Any]:
    """Read a contained STB strictly, evaluate it, and exclusively publish report + conclusion outside subject."""
    in_root = Path(stb_root)
    out_root = Path(output_root)
    if not in_root.is_absolute() or in_root.is_symlink() or not in_root.is_dir():
        raise ContractError(f"--stb-root must be a real absolute directory: {stb_root}")
    if not out_root.is_absolute() or out_root.is_symlink() or not out_root.is_dir():
        raise ContractError(f"--output-root must be a real absolute directory: {output_root}")

    in_root_str = str(in_root.resolve(strict=True))
    out_root_str = str(out_root.resolve(strict=True))

    stb_rel = normalize_relative_path(stb_relative)
    rep_rel = normalize_relative_path(report_relative)
    conc_rel = normalize_relative_path(conclusion_relative)

    foundation_resolve_contained(in_root_str, stb_rel)
    foundation_resolve_contained(out_root_str, rep_rel)
    foundation_resolve_contained(out_root_str, conc_rel)

    receipt = foundation_read_file_strict(in_root_str, stb_rel)
    content = receipt.get("content")
    if not isinstance(content, str):
        raise ContractError("STB file did not return utf8 text")
    stb_doc = strict_json_loads(content, "session-trace-bundle")
    if not isinstance(stb_doc, dict):
        raise ContractError("session-trace-bundle must be a JSON object")

    report, conclusion = evaluate_stb_document(
        stb_doc,
        provider_id=provider_id,
        provider_version=provider_version,
        stb_root=in_root_str,
    )
    write_json_exclusive(Path(out_root_str) / rep_rel, report)
    write_json_exclusive(Path(out_root_str) / conc_rel, conclusion)
    return {
        "status": "OK",
        "verdict": report["verdict"],
        "suspectedFabrication": report["suspectedFabrication"],
        "reportPath": str(Path(out_root_str) / rep_rel),
        "conclusionPath": str(Path(out_root_str) / conc_rel),
        "outcome": conclusion["outcome"],
    }


def scrub_repro_value(value: Any) -> Any:
    """Recursively scrub secrets, credentials, and personal host paths from exported repro payloads."""
    if isinstance(value, str):
        out = value
        for pat, repl in _REPRO_TEXT_REDACTION_RULES:
            out = pat.sub(repl, out)
        return out
    if isinstance(value, list):
        return [scrub_repro_value(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key in sorted(value.keys()):
            if _SENSITIVE_KEY_RE.search(str(key)):
                cleaned[key] = "[REDACTED_SECRET]"
            else:
                cleaned[key] = scrub_repro_value(value[key])
        return cleaned
    return value


def _sanitize_case_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip()).strip("-_")
    return (slug[:40] or "stb").upper()


def build_repro_candidate_from_stb(
    stb: dict[str, Any],
    *,
    provider_id: str,
    provider_version: str,
    stb_root: str | None = None,
    reconciliation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a scrubbed, digest-locked FB-2 failure reproduction candidate from a failing/fabricated STB."""
    report, _ = evaluate_stb_document(
        stb,
        provider_id=provider_id,
        provider_version=provider_version,
        stb_root=stb_root,
    )
    if reconciliation_report is not None:
        if (
            not isinstance(reconciliation_report, dict)
            or reconciliation_report.get("kind") != "skill-family.stb-reconciliation-report"
            or reconciliation_report.get("recomputedBundleDigestSha256") != report["recomputedBundleDigestSha256"]
            or reconciliation_report.get("verdict") != report["verdict"]
        ):
            raise ContractError(
                "supplied reconciliation report does not match recomputed STB audit state"
            )

    verdict = report["verdict"]
    if verdict not in {"SUSPECTED_FABRICATION", "FINDINGS"}:
        raise ContractError(
            f"export-repro-candidate requires SUSPECTED_FABRICATION or FINDINGS verdict (got {verdict})"
        )

    triggered_rules = sorted({f["ruleId"] for f in report.get("findings", []) if isinstance(f.get("ruleId"), str)})
    primary_summary_raw = (
        report["redCardReasons"][0]
        if report.get("redCardReasons")
        else (report["findings"][0]["summary"] if report.get("findings") else "STB failure detected")
    )
    failure_summary = scrub_repro_value(primary_summary_raw)

    observed_calls = []
    for idx, tc in enumerate(stb.get("toolCalls", [])):
        observed_calls.append(
            {
                "seq": idx,
                "toolName": scrub_repro_value(str(tc.get("toolName") or "tool")),
                "category": str(tc.get("category") or "other"),
                "sanitizedTarget": scrub_repro_value(str(tc.get("sanitizedCommandOrTarget") or "")),
                "status": str(tc.get("status") or "failed"),
                "exitCode": tc.get("exitCode") if isinstance(tc.get("exitCode"), int) else None,
            }
        )

    contradicted_claims = [
        {
            "claimId": scrub_repro_value(str(rec.get("claimId") or "")),
            "claimType": str(rec.get("claimType") or "unknown"),
            "triggeredRules": list(rec.get("triggeredRules") or []),
            "reason": scrub_repro_value(str(rec.get("reason") or "")),
        }
        for rec in report.get("claimReconciliations", [])
        if rec.get("status") == "contradicted"
    ]

    bundle_digest = report["recomputedBundleDigestSha256"]
    scrubbed_bundle_id = scrub_repro_value(str(stb["bundleId"]))
    slug_source = re.sub(r"\[REDACTED_[A-Z0-9_]+\]", "", scrubbed_bundle_id)
    case_id = f"REPRO-{_sanitize_case_slug(slug_source)}-{bundle_digest[:8]}"

    unsigned_body: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "skill-failure-auditor.repro-candidate",
        "feedbackLoopId": "FB-2",
        "provider": {
            "id": provider_id,
            "version": provider_version,
            "entry": REPRO_CANDIDATE_ENTRY,
        },
        "sourceBundle": {
            "bundleId": scrub_repro_value(stb["bundleId"]),
            "attemptId": scrub_repro_value(stb["attempt"]["attemptId"]),
            "recomputedBundleDigestSha256": bundle_digest,
            "verdict": verdict,
            "suspectedFabrication": bool(report["suspectedFabrication"]),
            "triggeredRuleIds": triggered_rules,
        },
        "subject": {
            "id": scrub_repro_value(stb["subject"]["id"]),
            "kind": stb["subject"]["kind"],
            "version": scrub_repro_value(stb["subject"].get("version")),
            "sourceTreeSha256": stb["subject"]["sourceTreeSha256"],
        },
        "redaction": {
            "applied": True,
            "policyId": "sfa-repro-scrubber-v1",
        },
        "humanReviewRequired": True,
        "proposedCase": {
            "caseId": case_id,
            "category": "fb2_failure_regression",
            "failureSummary": failure_summary,
            "observedToolCalls": observed_calls,
            "contradictedClaims": contradicted_claims,
            "assertions": [
                {"kind": "exit_code_zero"},
                {
                    "kind": "regex_match",
                    "pattern": "^(?:VERIFIED_REGRESSION_FIXED|SAFE_BOUNDARY_HELD)$",
                },
            ],
        },
    }
    candidate_digest = foundation_digest_document(unsigned_body)
    return {
        **unsigned_body,
        "candidateDigestSha256": candidate_digest,
    }


def run_export_repro_candidate(
    *,
    stb_root: str,
    stb_relative: str,
    output_root: str,
    candidate_relative: str,
    provider_id: str,
    provider_version: str,
    report_root: str | None = None,
    report_relative: str | None = None,
) -> dict[str, Any]:
    """Read a failing/fabricated STB strictly, scrub and build an FB-2 repro candidate, and publish exclusively."""
    in_root = Path(stb_root)
    out_root = Path(output_root)
    if not in_root.is_absolute() or in_root.is_symlink() or not in_root.is_dir():
        raise ContractError(f"--stb-root must be a real absolute directory: {stb_root}")
    if not out_root.is_absolute() or out_root.is_symlink() or not out_root.is_dir():
        raise ContractError(f"--output-root must be a real absolute directory: {output_root}")

    in_root_str = str(in_root.resolve(strict=True))
    out_root_str = str(out_root.resolve(strict=True))
    stb_rel = normalize_relative_path(stb_relative)
    cand_rel = normalize_relative_path(candidate_relative)

    foundation_resolve_contained(in_root_str, stb_rel)
    foundation_resolve_contained(out_root_str, cand_rel)

    stb_receipt = foundation_read_file_strict(in_root_str, stb_rel)
    stb_content = stb_receipt.get("content")
    if not isinstance(stb_content, str):
        raise ContractError("STB file did not return utf8 text")
    stb_doc = strict_json_loads(stb_content, "session-trace-bundle")
    if not isinstance(stb_doc, dict):
        raise ContractError("session-trace-bundle must be a JSON object")

    ext_report: dict[str, Any] | None = None
    if report_root is not None or report_relative is not None:
        if not report_root or not report_relative:
            raise ContractError("--report-root and --report must be provided together")
        rep_root_p = Path(report_root)
        if not rep_root_p.is_absolute() or rep_root_p.is_symlink() or not rep_root_p.is_dir():
            raise ContractError(f"--report-root must be a real absolute directory: {report_root}")
        rep_root_str = str(rep_root_p.resolve(strict=True))
        rep_rel = normalize_relative_path(report_relative)
        foundation_resolve_contained(rep_root_str, rep_rel)
        rep_receipt = foundation_read_file_strict(rep_root_str, rep_rel)
        rep_text = rep_receipt.get("content")
        if not isinstance(rep_text, str):
            raise ContractError("reconciliation report file did not return utf8 text")
        parsed_rep = strict_json_loads(rep_text, "stb-reconciliation-report")
        if not isinstance(parsed_rep, dict):
            raise ContractError("stb-reconciliation-report must be a JSON object")
        ext_report = parsed_rep

    candidate_doc = build_repro_candidate_from_stb(
        stb_doc,
        provider_id=provider_id,
        provider_version=provider_version,
        stb_root=in_root_str,
        reconciliation_report=ext_report,
    )
    out_file = Path(out_root_str) / cand_rel
    write_json_exclusive(out_file, candidate_doc)
    return {
        "status": "OK",
        "feedbackLoopId": "FB-2",
        "verdict": candidate_doc["sourceBundle"]["verdict"],
        "suspectedFabrication": candidate_doc["sourceBundle"]["suspectedFabrication"],
        "proposedCaseId": candidate_doc["proposedCase"]["caseId"],
        "candidateDigestSha256": candidate_doc["candidateDigestSha256"],
        "humanReviewRequired": True,
        "candidatePath": str(out_file),
    }

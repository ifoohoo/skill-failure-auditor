#!/usr/bin/env python3
"""跨仓故障归因只读诊断工具（US-074）。

只读分析故障描述、报错日志或结构化错误 JSON，结合目标工作区的宿主插件清单、
Git 提交摘要与内部依赖解析版本，定位主责仓库与次要关联组件，并可导出最小复现诊断包。
本脚本不修改目标工作区，不安装依赖，也不启动受检业务技能。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_LOG_BYTES = 4 * 1024 * 1024

INTERNAL_ECOSYSTEM_PACKAGES = (
    "skill-family-contracts",
    "skill-family-harness-node",
    "skill-family-engineering-kit",
    "artifact-graph",
    "agent-method-registry",
    "humanize-rewrite",
    "release-skill",
    "skill-family-test",
    "skill-family-studio",
    "skill-family-hub",
    "skill-failure-auditor",
)

PLUGIN_MANIFEST_RELPATHS = (
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "kimi.plugin.json",
    ".kimi-plugin/plugin.json",
    ".qoder-plugin/plugin.json",
    ".codebuddy-plugin/plugin.json",
)

HOST_SIGNAL_ENV_VARS = {
    "codex": ("CODEX_SANDBOX", "CODEX_CI", "CODEX_THREAD_ID", "CODEX_HOME"),
    "claude-code": ("CLAUDECODE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT"),
    "kimi-code": ("KIMI_CODE_HOME",),
    "qoder": ("QODER_HOME", "QODER_CONFIG_DIR"),
    "workbuddy": ("CODEBUDDY_CONFIG_DIR", "WORKBUDDY_CONFIG_DIR"),
    "codebuddy": ("CODEBUDDY_HOME", "CODEBUDDY_CONFIG_DIR"),
    "cursor": ("CURSOR_AGENT", "CURSOR_TRACE_ID", "CURSOR_CONFIG_DIR"),
    "deepseek-harness": ("DSH_HOME", "DEEPSEEK_HARNESS_HOME", "DSH_CONFIG_DIR"),
    "antigravity": ("ANTIGRAVITY_HOME", "ANTIGRAVITY_CLI_CONFIG", "GEMINI_CLI_HOME"),
    "grok": ("GROK_HOME", "GROK_CONFIG_DIR"),
}

_REDACT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(https?://)[^/\s:@\"']+:[^/\s\"',;)]+@"), r"\1<REDACTED_CREDENTIAL>@"),
    (re.compile(r"(Authorization\s*:\s*Basic\s+)[A-Za-z0-9+/=_\-]{6,}", re.I), r"\1<REDACTED_CREDENTIAL>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]{12,}", re.I), "Bearer <REDACTED_CREDENTIAL>"),
    (re.compile(r"\b(?:sk|ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_\-]{12,}\b"), "<REDACTED_CREDENTIAL>"),
    (
        re.compile(r"\b(?:sk|glpat)-[A-Za-z0-9_\-]{12,}(?:\.[A-Za-z0-9_\-]+)*\b"),
        "<REDACTED_CREDENTIAL>",
    ),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "<REDACTED_CREDENTIAL>"),
    (
        re.compile(
            r"""(?i)(\b(?:[a-z0-9_]*(?:api_?key|access_?token|auth_?token|refresh_?token|bearer_?token|client_?secret|secret_?key|npm_?token|github_?token|password|passwd|secret|token))\s*[:=]\s*["']?)(?!<REDACTED_CREDENTIAL>)(?!(?:true|false|null|none|undefined)\b)(?!<)[A-Za-z0-9._\-+/=:@!#$%^&*]{6,}"""
        ),
        r"\1<REDACTED_CREDENTIAL>",
    ),
    (re.compile(r"/Users/[A-Za-z0-9._\-\\]+(?: [A-Za-z0-9._\-\\]+){0,3}/"), "<HOME>/"),
    (re.compile(r"/Users/[A-Za-z0-9._\-\\]+(?: [A-Za-z0-9._\-\\]+){0,3}(?=[\"'`),;\r\n\]]|$)"), "<HOME>"),
    (re.compile(r"/Users/[A-Za-z0-9._\-\\]+"), "<HOME>"),
    (re.compile(r"/home/[A-Za-z0-9._\-\\]+(?: [A-Za-z0-9._\-\\]+){0,3}/"), "<HOME>/"),
    (re.compile(r"/home/[A-Za-z0-9._\-\\]+(?: [A-Za-z0-9._\-\\]+){0,3}(?=[\"'`),;\r\n\]]|$)"), "<HOME>"),
    (re.compile(r"/home/[A-Za-z0-9._\-\\]+"), "<HOME>"),
    (re.compile(r"/Volumes/[^/\r\n\t\"'<>|*?:\\]{1,80}/"), "<VOLUME>/"),
    (re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._\-]+(?: [A-Za-z0-9._\-]+){0,3}\\"), r"<HOME>\\"),
    (re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._\-]+"), "<HOME>"),
)


class TriageInputError(ValueError):
    """调用参数或输入路径不符合只读诊断约束时抛出的异常。"""


@dataclass(frozen=True)
class RepoRule:
    repo: str
    layer: str
    base_rank: int
    code_patterns: tuple[re.Pattern[str], ...]
    fingerprints: tuple[tuple[str, re.Pattern[str]], ...]
    summary_template: str
    default_actions: tuple[tuple[str, str], ...]


REPO_RULES: tuple[RepoRule, ...] = (
    RepoRule(
        repo="host-configuration",
        layer="宿主插件发现与本地配置层",
        base_rank=10,
        code_patterns=(
            re.compile(r"\bHOST_(?:MANIFEST_MISSING|SKILL_SHADOWED|DISCOVERY_FAILED)[A-Z0-9_]*\b"),
            re.compile(r"\bPLATFORM_(?:CLIENT_VERSION_BELOW_MINIMUM|MANIFEST_IDENTITY_INVALID|MANIFEST_INVALID|DEFAULT_CONFIGURATION_UNPROVEN)\b"),
        ),
        fingerprints=(
            ("manifest-missing", re.compile(r"(?:插件清单缺失|清单缺失|manifest\s+missing|plugin\.json\s+not\s+found)", re.I)),
            ("skill-shadowed", re.compile(r"(?:技能遮蔽|旧安装遮蔽|skill\s+shadowed|shadowed\s+by\s+existing|未被宿主发现)", re.I)),
            ("codebuddy-config", re.compile(r"\b(?:CODEBUDDY_CONFIG_DIR|CODEBUDDY_HOME|QODER_HOME|DSH_HOME|GROK_HOME|ANTIGRAVITY_HOME|CURSOR_AGENT)\b")),
            ("host-skills-dir", re.compile(r"(?:\.claude/skills|\.agents/skills|\.workbuddy/skills|\.codebuddy/skills|\.qoder/skills|\.cursor/skills|\.dsh/skills|\.grok/skills|\.kimi-code/skills|\.gemini/config)")),
        ),
        summary_template="宿主插件清单缺失、版本低于下限或本地旧安装目录遮蔽，需先核查宿主配置与发现路径。",
        default_actions=(
            ("node scripts/version_source.mjs --verify", "只读核对工作区各宿主插件清单是否存在且版本与唯一事实源一致。"),
            ("ls -la ~/.claude/skills ~/.agents/skills ~/.workbuddy/skills", "检查用户级技能目录是否存在同名旧安装或软链接遮蔽当前候选。"),
        ),
    ),
    RepoRule(
        repo="skill-family-foundation",
        layer="底座契约与受控运行时（Foundation）",
        base_rank=20,
        code_patterns=(
            re.compile(r"\b(?:SFC|SFH|SFEK|SPE)\d{3,5}[A-Z0-9_]*\b"),
            re.compile(r"\b(?:SFC|SFH|SFEK)_[A-Z0-9_]+\b"),
            re.compile(r"\bFOUNDATION_(?:RUNTIME|SCHEMA|PROVENANCE|PUBLISH|BATCH|CLOSURE)_[A-Z0-9_]+\b"),
        ),
        fingerprints=(
            ("skill-family-contracts", re.compile(r"\bskill-family-contracts\b")),
            ("skill-family-harness-node", re.compile(r"\bskill-family-harness-node\b")),
            ("skill-family-engineering-kit", re.compile(r"\bskill-family-engineering-kit\b")),
            ("skill-family-kit", re.compile(r"\bskill-family-kit\b")),
            ("quickstart-profile", re.compile(r"\bquickstart-profile\b")),
            ("exclusive-publish-conflict", re.compile(r"\bexclusive-publish-conflict\b")),
            ("verifyProjectProfile", re.compile(r"\bverifyProjectProfile\b")),
            ("check-relock", re.compile(r"\bcheck\s+relock\b")),
        ),
        summary_template="底层 Foundation 契约校验、受控路径收容、独占写入锁或运行时版本绑定触发拒绝。",
        default_actions=(
            ("node packages/skill-failure-auditor/scripts/build/verify_project_profile.mjs --project-root packages/skill-failure-auditor", "只读核验 Foundation 锁定版本、tarball 摘要与 profile.json 是否一致。"),
            ("node --version", "核对当前 PATH 与 SFA_FOUNDATION_NODE 指向的 Node.js 版本是否满足 pin 约束。"),
        ),
    ),
    RepoRule(
        repo="agent-method-registry",
        layer="共享方法注册与覆盖解析层",
        base_rank=30,
        code_patterns=(
            re.compile(r"\bAMR(?:\d{3,5}|_[A-Z0-9_]+|[A-Z0-9_]{2,})\b"),
            re.compile(r"\b(?:HUMANIZE_RESOLUTION_FAILED|ENTRY_DISABLED)\b"),
        ),
        fingerprints=(
            ("agent-method-registry", re.compile(r"\bagent-method-registry\b")),
            ("agent-methods.yaml", re.compile(r"\bagent-methods\.yaml\b")),
            ("registry-catalog", re.compile(r"\bregistry/catalog\.json\b")),
            ("registry-protocol", re.compile(r"\bprotocol:\s*registry-v[12]\b")),
        ),
        summary_template="共享逻辑方法注册表或项目级 agent-methods.yaml 覆盖配置解析失败。",
        default_actions=(
            ("npx agent-method-registry resolve --index <index.json> --ref <id> --host codex", "只读执行方法入口解析，复核索引目录与项目级 agent-methods.yaml 覆盖合并结果。"),
            ("检查 agent-methods.yaml 与目标 SKILL.md 实路径", "确认被指向的提供方技能未被禁用、无循环指向且入口文件存在。"),
        ),
    ),
    RepoRule(
        repo="humanize-rewrite",
        layer="中文表达优化与审校技能层（humanize-rewrite）",
        base_rank=35,
        code_patterns=(
            re.compile(r"\b(?:HRW?|HUMANIZE_REWRITE|SHUORENHUA|QWEN)_[A-Z0-9_]+\b"),
            re.compile(r"\bHUMANIZE_(?!RESOLUTION_FAILED\b)[A-Z0-9_]+\b"),
            re.compile(r"\bHRW?\d{3,5}[A-Z0-9_]*\b"),
        ),
        fingerprints=(
            ("humanize-rewrite", re.compile(r"\bhumanize-rewrite\b")),
            ("chinese-humanize-rewrite", re.compile(r"\bchinese-humanize-rewrite\b")),
            ("shuorenhua", re.compile(r"\bshuorenhua\b")),
        ),
        summary_template="humanize-rewrite 表达方法解析、文风门禁或中文重写流程触发错误。",
        default_actions=(
            ("检查 agent-methods.yaml 与 humanize-rewrite 技能入口", "只读确认当前项目声明的中文表达实现与本地技能目录均可用。"),
            ("node --version && python3 --version", "只读核对执行表达审校脚本所需的本地运行时环境。"),
        ),
    ),
    RepoRule(
        repo="artifact-graph",
        layer="需求设计测试追溯图谱与版本锁层",
        base_rank=40,
        code_patterns=(
            re.compile(r"\bACA(?:\d{3,5}|_[A-Z0-9_]+|[A-Z0-9_]{2,})\b"),
            re.compile(r"\bAG_[A-Z0-9_]+\b"),
            re.compile(r"\bSTALE_REVIEW\b"),
        ),
        fingerprints=(
            ("artifact-graph", re.compile(r"\bartifact-graph\b")),
            ("traceability-version-lock", re.compile(r"\btraceability-version-lock\.json\b")),
            ("artifact-graph-config", re.compile(r"\bartifact-graph\.config\.yaml\b")),
            ("version-lock-audit", re.compile(r"\bversion-lock\s+audit\b")),
            ("source_changed", re.compile(r'"status"\s*:\s*"source_changed"')),
        ),
        summary_template="制品追溯图谱关系断链、评审状态过期（STALE_REVIEW）或 traceability-version-lock.json 未同步。",
        default_actions=(
            ("npx artifact-graph validate --root .", "只读检查制品 frontmatter、ID 范围与上下游引用边是否合法。"),
            ("npx artifact-graph version-lock audit --root . --strict-missing-lock --format json", "只读定位发生变更或缺失版本锁的具体制品边。"),
        ),
    ),
    RepoRule(
        repo="release-skill",
        layer="公开发布流水线与快照守恒门禁层",
        base_rank=50,
        code_patterns=(
            re.compile(r"\bRS_[A-Z0-9_]+\b"),
            re.compile(r"\bHOOK_SIDE_EFFECT\b"),
            re.compile(r"\b(?:PROJECT_YAML_PUBLIC_FILES_DRIFT|GENERATED_METADATA_MASQUERADING_AS_CONTENT|RELEASE_MISSING_FILE|RELEASE_EXTRA_FILE|SNAPSHOT_DIGEST_MISMATCH|DUPLICATE_RELEASE_DESTINATION)\b"),
        ),
        fingerprints=(
            ("release-project-yaml", re.compile(r"\.release-skill/project\.yaml")),
            ("npx-release-skill", re.compile(r"\bnpx\s+release-skill\b")),
            ("verify-records", re.compile(r"\bverify-records\b")),
        ),
        summary_template="发布流水线配置漂移、候选公开快照集合不守恒或发布钩子产生了工作区脏文件副作用（HOOK_SIDE_EFFECT）。",
        default_actions=(
            ("npm run sync-release-project:check", "只读核对 .release-skill/project.yaml 公开文件映射是否与当前候选清单一致。"),
            ("node scripts/snapshot.mjs", "在临时目录只读验证公开快照物化与文件摘要守恒。"),
        ),
    ),
    RepoRule(
        repo="skill-family-studio",
        layer="本地可视化制品审阅与运行态监控台层（Studio）",
        base_rank=52,
        code_patterns=(
            re.compile(r"\b(?:SFS|STUDIO)_[A-Z0-9_]+\b"),
            re.compile(r"\b(?:SFST|SFS)\d{3,5}[A-Z0-9_]*\b"),
        ),
        fingerprints=(
            ("skill-family-studio", re.compile(r"\bskill-family-studio(?:-workspace)?\b")),
            ("runtime-registry", re.compile(r"\bruntime-registry\.json\b")),
            ("alignment-inspector", re.compile(r"\balignment-inspector\.mjs\b")),
            ("session-fleet", re.compile(r"\bsession-fleet\.mjs\b")),
        ),
        summary_template="Skill Family Studio 运行态监控注册、回环 API 校验或高阶需求对齐巡检触发阻断。",
        default_actions=(
            ("cat ~/.skill-family/runtime-registry.json 2>/dev/null || echo '未注册'", "只读核查本地 Studio 运行态注册表及最近记录的实例状态。"),
            ("node --version", "核对 Studio 启动与校验要求的 Node.js 22.x 运行时版本。"),
        ),
    ),
    RepoRule(
        repo="skill-family-hub",
        layer="技能族生命周期能力台账与插件市场快照层（Hub）",
        base_rank=55,
        code_patterns=(
            re.compile(r"\b(?:SFHUB|HUB)_[A-Z0-9_]+\b"),
            re.compile(r"\b(?:SFHUB|HUB)\d{3,5}[A-Z0-9_]*\b"),
        ),
        fingerprints=(
            ("skill-family-hub", re.compile(r"\bskill-family-hub(?:-workspace)?\b")),
            ("lifecycle-providers", re.compile(r"\blifecycle-providers\.mjs\b")),
            ("proposal-inbox", re.compile(r"\bproposal-inbox\.mjs\b")),
            ("public-snapshot", re.compile(r"\bpublic-snapshot/\d+")),
        ),
        summary_template="Skill Family Hub 能力台账、提案核收或插件市场快照完整性门禁触发阻断。",
        default_actions=(
            ("检查目标工作区 .release-skill/project.yaml 与插件清单协调性", "只读确认待入库插件版本、仓库坐标与公开市场索引声明一致。"),
            ("git status --short", "只读检查 Hub 快照或提案清单工作区是否存在未预期的局部变更。"),
        ),
    ),
    RepoRule(
        repo="skill-family-test",
        layer="跨技能族符合性测试与评测运行器层",
        base_rank=60,
        code_patterns=(
            re.compile(r"\bSFT(?:\d{3,5}|_[A-Z0-9_]+|[A-Z0-9_]{2,})\b"),
        ),
        fingerprints=(
            ("skill-family-test", re.compile(r"\bskill-family-test\b")),
            ("conformance-suite", re.compile(r"\bconformance[-_\s]suite\b", re.I)),
        ),
        summary_template="跨技能族测试运行器或符合性测试套件（SFT*）报告契约或环境前置不满足。",
        default_actions=(
            ("python3 -m pytest -q -p no:cacheprovider", "在显式绑定受管 Node 的 PATH 下复跑聚焦测试以确认报错复现。"),
            ("检查测试夹具隔离目录与环境变量继承", "确认子进程是否继承了正确的 Node.js 与 Python 运行时路径。"),
        ),
    ),
    RepoRule(
        repo="skill-failure-auditor",
        layer="技能可靠性失效只读审计层",
        base_rank=70,
        code_patterns=(
            re.compile(r"\bSFA_[A-Z0-9_]+\b"),
            re.compile(r"\bSFA\d{3,5}[A-Z0-9_]*\b"),
            re.compile(r"\bPREFLIGHT_CERTIFICATE_[A-Z0-9_]+\b"),
            re.compile(r"\b(?:HIGH_SEVERITY_UNCHECKED|NO_EXECUTABLE_ACCEPTANCE_ARTIFACT|PRIOR_ATTEMPT_NOT_BOUND)\b"),
            re.compile(r"\bREDLINE_FM0[156]_[A-Z0-9_]+\b"),
            re.compile(r"\bFAILURE_DIAGNOSIS_(?:CLEAR|FINDINGS|INCOMPLETE|UNAVAILABLE)\b"),
        ),
        fingerprints=(
            ("skill-failure-auditor", re.compile(r"\bskill-failure-auditor\b")),
            ("SFA_FOUNDATION_NODE", re.compile(r"\bSFA_FOUNDATION_NODE\b")),
            ("evaluation_tool.py", re.compile(r"\bevaluation_tool\.py\b")),
            ("registry_tool.py", re.compile(r"\bregistry_tool\.py\b")),
            ("evidence_tool.py", re.compile(r"\bevidence_tool\.py\b")),
            ("attempt_tool.py", re.compile(r"\battempt_tool\.py\b")),
        ),
        summary_template="技能失效审计规则覆盖、证据分片校验、预检证书或自审/复用状态机触发阻断。",
        default_actions=(
            ("npm run verify && npm run leakage-scan", "只读检查审计工作区结构一致性与公开边界零泄漏。"),
            ("python3 packages/skill-failure-auditor/plugin-src/core/scripts/registry_tool.py validate", "在设置 SFA_FOUNDATION_NODE 后只读校验内置 28 条失效模式规则锁。"),
        ),
    ),
    RepoRule(
        repo="domain-skill-plugin",
        layer="外部领域技能插件层",
        base_rank=80,
        code_patterns=(
            re.compile(r"\bPYTHON_OVERRIDE_INVALID\b"),
            re.compile(r"\bDOMAIN_PLUGIN_[A-Z0-9_]+\b"),
        ),
        fingerprints=(
            ("domain-skill-plugin", re.compile(r"\bdomain-skill-plugin\b")),
        ),
        summary_template="外部领域技能插件配置、Python 解释器覆盖项（PYTHON_OVERRIDE_INVALID）或自定义领域错误码触发失败。",
        default_actions=(
            ("python3 --version && which python3", "只读核对当前 Python 运行时及领域插件配置的 Python 覆盖路径是否有效。"),
            ("检查领域技能插件配置文件与虚拟环境路径", "确认自定义解释器路径为真实存在的可执行文件且无悬空软链接。"),
        ),
    ),
)

_CUSTOM_PREFIX_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_ALLOWED_CUSTOM_REPOS = {
    "domain-skill-plugin": "外部领域技能插件层（自定义前缀扩展）",
    "business-skill": "领域业务技能层（自定义前缀扩展）",
}


def _load_custom_triage_rules() -> tuple[RepoRule, ...]:
    """从 SFA_CUSTOM_TRIAGE_PREFIXES 解析无侵入的外部错误码前缀规则（如 PREFIX 或 PREFIX:business-skill）。"""
    raw = os.environ.get("SFA_CUSTOM_TRIAGE_PREFIXES", "").strip()
    if not raw:
        return ()
    grouped: dict[str, list[str]] = {}
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        if ":" in token:
            prefix_part, repo_part = token.split(":", 1)
            prefix = prefix_part.strip().upper()
            target_repo = repo_part.strip()
        else:
            prefix = token.upper()
            target_repo = "domain-skill-plugin"
        if not _CUSTOM_PREFIX_TOKEN_RE.fullmatch(prefix):
            continue
        if target_repo not in _ALLOWED_CUSTOM_REPOS:
            continue
        if prefix not in grouped.setdefault(target_repo, []):
            grouped[target_repo].append(prefix)

    custom_rules: list[RepoRule] = []
    for idx, (target_repo, prefixes) in enumerate(grouped.items()):
        patterns = tuple(
            re.compile(rf"\b{re.escape(pref)}(?:_[A-Z0-9_]+|\d{{2,5}})\b")
            for pref in prefixes
        )
        custom_rules.append(
            RepoRule(
                repo=target_repo,
                layer=_ALLOWED_CUSTOM_REPOS[target_repo],
                base_rank=78 + idx,
                code_patterns=patterns,
                fingerprints=(),
                summary_template=f"命中通过 SFA_CUSTOM_TRIAGE_PREFIXES 声明的外部领域错误码前缀（{', '.join(prefixes)}）。",
                default_actions=(
                    ("检查目标领域技能配置文件、输入参数与脚本 stderr", "只读核对命中自定义错误码前缀的外部领域组件配置与运行前置。"),
                    ("python3 --version && node --version", "只读确认外部领域技能依赖的本地解释器版本。"),
                ),
            )
        )
    return tuple(custom_rules)


def redact_text(text: str, workspace_abs: Path | None = None) -> str:
    """脱敏本机用户目录、工作区绝对前缀与疑似凭据，确保诊断包可安全分享。"""
    cleaned = text
    if workspace_abs is not None:
        ws_posix = workspace_abs.as_posix()
        if ws_posix and ws_posix != "/":
            cleaned = cleaned.replace(ws_posix, "<WORKSPACE>")
        ws_str = str(workspace_abs)
        if ws_str and ws_str != "/" and ws_str != ws_posix:
            cleaned = cleaned.replace(ws_str, "<WORKSPACE>")
    for pattern, replacement in _REDACT_RULES:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_LOG_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
def _probe_command_stdout(command: list[str], *, cwd: Path | None = None) -> str | None:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    out = completed.stdout.strip()
    return out if out else None


def collect_readonly_environment(workspace: Path) -> dict[str, Any]:
    """只读采集当前宿主特征、Node/Python 版本、工作区清单与内部依赖解析状态。"""
    node_version = _probe_command_stdout(["node", "--version"]) or "unavailable"
    foundation_node_raw = os.environ.get("SFA_FOUNDATION_NODE", "")
    if not foundation_node_raw:
        foundation_node_status = "unset"
    else:
        fn_path = Path(foundation_node_raw)
        if not fn_path.is_absolute():
            foundation_node_status = "invalid:non_absolute"
        elif any(Path(fn_path.anchor, *fn_path.parts[1:i]).is_symlink() for i in range(2, len(fn_path.parts) + 1)):
            foundation_node_status = "invalid:contains_symlink"
        elif not fn_path.is_file():
            foundation_node_status = "invalid:missing_regular_file"
        else:
            fn_ver = _probe_command_stdout([str(fn_path), "--version"])
            foundation_node_status = f"valid:{fn_ver}" if fn_ver else "invalid:unexecutable"

    host_signals = {
        host: any(bool(os.environ.get(var)) for var in env_vars)
        for host, env_vars in HOST_SIGNAL_ENV_VARS.items()
    }

    git_sha = _probe_command_stdout(["git", "rev-parse", "HEAD"], cwd=workspace) or "unavailable"
    root_pkg_data = _safe_read_json(workspace / "package.json")
    root_pkg_summary = None
    declared_deps: dict[str, str] = {}
    if root_pkg_data is not None:
        root_pkg_summary = {
            "name": root_pkg_data.get("name"),
            "version": root_pkg_data.get("version"),
            "node_engine": (root_pkg_data.get("engines") or {}).get("node")
            if isinstance(root_pkg_data.get("engines"), dict)
            else None,
        }
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            sec = root_pkg_data.get(section)
            if isinstance(sec, dict):
                for pkg_name in INTERNAL_ECOSYSTEM_PACKAGES:
                    if pkg_name in sec and isinstance(sec[pkg_name], str):
                        declared_deps[pkg_name] = sec[pkg_name]

    subpackages: list[dict[str, Any]] = []
    packages_dir = workspace / "packages"
    if packages_dir.is_dir() and not packages_dir.is_symlink():
        for child in sorted(packages_dir.iterdir(), key=lambda p: p.name):
            if child.is_symlink() or not child.is_dir():
                continue
            sub_pkg = _safe_read_json(child / "package.json")
            if sub_pkg is not None:
                subpackages.append({
                    "path": f"packages/{child.name}",
                    "name": sub_pkg.get("name"),
                    "version": sub_pkg.get("version"),
                })
                for section in ("dependencies", "devDependencies"):
                    sec = sub_pkg.get(section)
                    if isinstance(sec, dict):
                        for pkg_name in INTERNAL_ECOSYSTEM_PACKAGES:
                            if pkg_name in sec and isinstance(sec[pkg_name], str):
                                declared_deps.setdefault(pkg_name, sec[pkg_name])

            profile_data = _safe_read_json(child / "profile.json")
            pin_pkgs = (
                ((profile_data or {}).get("adoption") or {}).get("foundation_pin") or {}
            ).get("packages")
            if isinstance(pin_pkgs, dict):
                for pkg_name, meta in pin_pkgs.items():
                    if isinstance(meta, dict) and isinstance(meta.get("version"), str):
                        declared_deps.setdefault(pkg_name, f"pinned:{meta['version']}")

    manifest_roots = [workspace]
    if packages_dir.is_dir() and not packages_dir.is_symlink():
        for child in sorted(packages_dir.iterdir(), key=lambda p: p.name):
            if child.is_dir() and not child.is_symlink():
                manifest_roots.append(child)

    plugin_manifests: dict[str, dict[str, Any]] = {}
    for mroot in manifest_roots:
        prefix = "" if mroot == workspace else f"{mroot.relative_to(workspace).as_posix()}/"
        for rel in PLUGIN_MANIFEST_RELPATHS:
            mpath = mroot / rel
            if mpath.exists() or mpath.is_symlink():
                mjson = _safe_read_json(mpath)
                plugin_manifests[f"{prefix}{rel}"] = {
                    "present": mjson is not None,
                    "is_symlink": mpath.is_symlink(),
                    "name": mjson.get("name") if mjson else None,
                    "version": mjson.get("version") if mjson else None,
                }

    internal_dependencies: dict[str, dict[str, str | None]] = {}
    for pkg_name in INTERNAL_ECOSYSTEM_PACKAGES:
        installed_pkg = _safe_read_json(workspace / "node_modules" / pkg_name / "package.json")
        installed_ver = installed_pkg.get("version") if installed_pkg else None
        declared_ver = declared_deps.get(pkg_name)
        if declared_ver is not None or installed_ver is not None:
            internal_dependencies[pkg_name] = {
                "declared": declared_ver,
                "resolved": installed_ver if isinstance(installed_ver, str) else None,
            }

    return {
        "os": f"{platform.system()}-{platform.machine()}",
        "python_version": sys.version.split()[0],
        "node_version": node_version,
        "foundation_node_status": foundation_node_status,
        "detected_host_signals": host_signals,
        "workspace": {
            "root_display": f"<WORKSPACE:{workspace.name}>",
            "git_head_sha": git_sha,
            "root_package": root_pkg_summary,
            "subpackages": subpackages,
            "plugin_manifests": plugin_manifests,
            "internal_dependencies": internal_dependencies,
        },
    }


def _extract_structured_codes(raw_text: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("code", "errorCode", "error_code", "reason", "reason_code"):
                val = obj.get(key)
                if isinstance(val, str) and re.fullmatch(r"[A-Z][A-Z0-9_\-]{2,63}", val):
                    if val not in seen:
                        seen.add(val)
                        codes.append(val)
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    stripped = raw_text.strip()
    if stripped.startswith(("{", "[")):
        try:
            walk(json.loads(stripped))
            return codes
        except json.JSONDecodeError:
            pass
    for line in raw_text.splitlines():
        ls = line.strip()
        if ls.startswith("{") and ls.endswith("}"):
            try:
                walk(json.loads(ls))
            except json.JSONDecodeError:
                continue
    return codes
_EXTERNAL_DEFAULT_ACTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "host-configuration": (
        ("ls -la .claude-plugin/plugin.json .codex-plugin/plugin.json plugin.json 2>/dev/null || true", "只读核对当前工作区宿主插件清单是否存在且版本一致。"),
        ("ls -la ~/.claude/skills ~/.agents/skills ~/.workbuddy/skills ~/.gemini/config/plugins 2>/dev/null || true", "检查用户级技能与插件目录是否存在同名旧安装或软链接遮蔽当前候选。"),
    ),
    "skill-family-foundation": (
        ("ls -la profile.json package.json 2>/dev/null || true", "只读检查工作区或包目录内的 profile.json 与 Foundation 依赖声明版本。"),
        ("node --version", "核对当前 PATH 与运行时环境变量指向的 Node.js 版本是否满足 pin 约束。"),
    ),
    "agent-method-registry": (
        ("npx agent-method-registry resolve --index <index.json> --ref <id> --host codex", "只读执行方法入口解析，复核索引目录与项目级 agent-methods.yaml 覆盖合并结果。"),
        ("检查 agent-methods.yaml 与目标 SKILL.md 实路径", "确认被指向的提供方技能未被禁用、无循环指向且入口文件存在。"),
    ),
    "release-skill": (
        ("git status --short", "只读检查当前工作区是否存在发布钩子残留的脏文件或未预期变更。"),
        ("cat .release-skill/project.yaml 2>/dev/null || true", "只读核对当前工作区 .release-skill/project.yaml 中的公开表面映射与版本源配置。"),
    ),
    "skill-failure-auditor": (
        ("node --version && python3 --version", "只读核对执行失效审计所需的 Node.js（>=22.22.2 <23）与 Python 3 运行时环境。"),
        ("只读核对审计产出的 selection/coverage 与 audit-result.json 校验报错", "确认规则覆盖完整、可执行验收证据绑定有效且未触发自审阻断。"),
    ),
}


_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?")

def _build_foundation_upgrade_reason(
    pkg: str,
    locked: str,
    min_version: str,
    matched_signals: list[str],
) -> str:
    has_host_signal = any(sig.startswith("host:") for sig in matched_signals)
    has_cli_or_contract_signal = any(
        sig.startswith("foundation:") or sig.startswith("owner:")
        for sig in matched_signals
    )
    if has_host_signal and has_cli_or_contract_signal:
        capability_desc = (
            "antigravity/cursor/deepseek-harness 等新宿主适配支持，"
            "以及 0.39.0 四顶层 CLI 收敛与新版契约校验支持"
        )
    elif has_host_signal:
        capability_desc = "antigravity/cursor/deepseek-harness 等新宿主适配支持"
    else:
        capability_desc = "0.39.0 四顶层 CLI 收敛与新版契约校验支持"
    return (
        f"工作区锁定的 {pkg}@{locked} 低于 {min_version}，早期 Foundation 缺少 "
        f"{capability_desc}；请升级至 >={min_version} 后重新验证。"
    )


_VERSION_UPGRADE_RULES: tuple[dict[str, Any], ...] = (
    {
        "packages": (
            "skill-family-contracts",
            "skill-family-harness-node",
            "skill-family-engineering-kit",
        ),
        "owner_repo": "skill-family-foundation",
        "min_version": "0.39.0",
        "min_tuple": (0, 39, 0),
        "signals": (
            ("host:antigravity", re.compile(r"\bantigravity\b", re.I)),
            ("host:cursor", re.compile(r"\bcursor(?:-plugin|-agent)?\b", re.I)),
            ("host:deepseek-harness", re.compile(r"\bdeepseek-harness\b|\bdsh\b", re.I)),
            (
                "host:manifest",
                re.compile(
                    r"(?:plugin\.json|\.claude-plugin|\.codex-plugin|\.kimi-plugin|\.qoder-plugin|\.codebuddy-plugin|supportedHosts|HOST_[A-Z0-9_]+|PLATFORM_[A-Z0-9_]+|宿主|清单)"
                ),
            ),
            (
                "foundation:package",
                re.compile(r"\bskill-family-(?:contracts|harness-node|engineering-kit|kit)\b", re.I),
            ),
            (
                "foundation:cli-or-contract",
                re.compile(
                    r"(?:project-manifest|skill-family\.project-manifest|repoTier|\b(?:SFC|SFH|SFEK|SPE)\d+[A-Z0-9_]*\b|\b(?:SFC|SFH|SFEK)_[A-Z0-9_]+\b|\bFOUNDATION_[A-Z0-9_]+\b|quickstart-profile|verifyProjectProfile|check[\s\-]+relock|\bcli\b|子命令|契约|漂移|\bdrift\b|\bschema\b)",
                    re.I,
                ),
            ),
        ),
        "reason_builder": _build_foundation_upgrade_reason,
    },
    {
        "packages": ("artifact-graph",),
        "min_version": "1.2.0",
        "min_tuple": (1, 2, 0),
        "signals": (
            ("artifact-graph", re.compile(r"\bartifact-graph\b", re.I)),
            (
                "bidirectional-alignment",
                re.compile(
                    r"(?:bidirectional|upstream-bindings|traceability-version-lock|version-lock|双向|血缘|对齐|STALE_REVIEW|ACA(?:\d+|_[A-Z0-9_]+)|AG_[A-Z0-9_]+|frontmatter)",
                    re.I,
                ),
            ),
            ("host-or-manifest", re.compile(r"(?:plugin\.json|project-manifest|清单|宿主)", re.I)),
        ),
        "reason_template": (
            "工作区锁定的 {pkg}@{locked} 低于 {min_version}，缺少跨仓双向血缘对齐增强与安全 "
            "Frontmatter 解析修复；请升级至 >={min_version} 后刷新追溯版本锁与校验。"
        ),
    },
    {
        "packages": ("agent-method-registry",),
        "min_version": "0.3.0",
        "min_tuple": (0, 3, 0),
        "signals": (
            (
                "registry-resolution",
                re.compile(
                    r"(?:\bagent-method-registry\b|\bagent-methods\b|verifyProvider|resolveEntry|AMR(?:\d+|_[A-Z0-9_]+)|HUMANIZE_RESOLUTION_FAILED|方法解析)",
                    re.I,
                ),
            ),
            (
                "new-host-registry",
                re.compile(r"(?:\bantigravity\b|\bcursor\b|\bdeepseek-harness\b|supportedHosts|HOST_[A-Z0-9_]+|宿主|清单)", re.I),
            ),
        ),
        "reason_template": (
            "工作区锁定的 {pkg}@{locked} 低于 {min_version}，缺少 "
            "antigravity/cursor/deepseek-harness 等新宿主提供方目录与注册表适配；"
            "请升级至 >={min_version} 后重跑方法解析。"
        ),
    },
)


def _extract_semver_tuple(spec: str | None) -> tuple[str, tuple[int, int, int]] | None:
    if not isinstance(spec, str):
        return None
    trimmed = spec.strip()
    if not trimmed:
        return None
    if trimmed.startswith("pinned:"):
        trimmed = trimmed[len("pinned:") :].strip()
    if re.match(r"^(?:workspace:|file:|link:|git\+|https?:|npm:|\*|latest$)", trimmed, re.I):
        return None
    cleaned = re.sub(r"^[~^=<>v\s]+", "", trimmed)
    match = _SEMVER_RE.match(cleaned)
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return f"{major}.{minor}.{patch}", (major, minor, patch)


def _detect_version_upgrade_hints(
    augmented_text: str,
    environment: dict[str, Any],
    *,
    primary_owner_repo: str | None = None,
) -> list[dict[str, Any]]:
    deps = (environment.get("workspace") or {}).get("internal_dependencies") or {}
    if not isinstance(deps, dict) or not deps:
        return []

    hints: list[dict[str, Any]] = []
    for rule in _VERSION_UPGRADE_RULES:
        matched_signals = [
            label for label, pat in rule["signals"] if pat.search(augmented_text)
        ]
        owner_repo = rule.get("owner_repo")
        if owner_repo and primary_owner_repo == owner_repo:
            owner_label = f"owner:{owner_repo}"
            if owner_label not in matched_signals:
                matched_signals.append(owner_label)
        if not matched_signals:
            continue
        min_ver = rule["min_version"]
        min_tuple = rule["min_tuple"]
        reason_builder = rule.get("reason_builder")
        for pkg_name in rule["packages"]:
            ver_info = deps.get(pkg_name)
            if not isinstance(ver_info, dict):
                continue
            parsed = _extract_semver_tuple(ver_info.get("declared"))
            version_source = "declared"
            if parsed is None:
                parsed = _extract_semver_tuple(ver_info.get("resolved"))
                version_source = "resolved"
            if parsed is None:
                continue
            locked_str, locked_tuple = parsed
            if locked_tuple >= min_tuple:
                continue
            reason_zh = (
                reason_builder(pkg_name, locked_str, min_ver, matched_signals)
                if callable(reason_builder)
                else rule["reason_template"].format(
                    pkg=pkg_name,
                    locked=locked_str,
                    min_version=min_ver,
                )
            )
            hints.append({
                "package": pkg_name,
                "locked_version": locked_str,
                "declared_spec": ver_info.get("declared"),
                "resolved_version": ver_info.get("resolved"),
                "version_source": version_source,
                "recommended_version": min_ver,
                "matched_signals": list(matched_signals),
                "reason_zh": reason_zh,
            })
    return hints


def _is_sfa_workspace(workspace: Path) -> bool:
    """判断受检工作区是否为 SFA 自身工作区；外部工作区仅输出通用只读排查建议。"""
    return (
        (workspace / "packages" / "skill-failure-auditor" / "package.json").is_file()
        and (workspace / "scripts" / "version_source.mjs").is_file()
    )


def evaluate_symptom_text(
    combined_text: str,
    *,
    sources: list[str],
    workspace: Path,
) -> dict[str, Any]:
    """对采集到的症状文本执行错误码识别、跨层优先级裁决与环境快照整合。"""
    structured_codes = _extract_structured_codes(combined_text)
    augmented_text = combined_text
    if structured_codes:
        augmented_text = combined_text + "\n" + " ".join(structured_codes)

    active_rules = REPO_RULES + _load_custom_triage_rules()
    candidates: list[dict[str, Any]] = []
    for rule in active_rules:
        matched_codes: list[str] = []
        seen_codes: set[str] = set()
        for pat in rule.code_patterns:
            for m in pat.finditer(augmented_text):
                token = m.group(0)
                if token not in seen_codes:
                    seen_codes.add(token)
                    matched_codes.append(token)

        matched_fps: list[str] = []
        for fp_id, fp_pat in rule.fingerprints:
            if fp_pat.search(augmented_text):
                matched_fps.append(fp_id)

        if not matched_codes and not matched_fps:
            continue

        effective_rank = rule.base_rank
        if rule.repo == "release-skill" and "HOOK_SIDE_EFFECT" in seen_codes:
            # 发布钩子越界副作用是 release-skill 的直接门禁违约，仅次于宿主清单遮蔽
            effective_rank = 15

        has_code = len(matched_codes) > 0
        candidates.append({
            "rule": rule,
            "has_code": has_code,
            "effective_rank": effective_rank,
            "matched_codes": matched_codes,
            "matched_fingerprints": matched_fps,
        })

    # 排序准则：命中标准错误码优先于仅命中上下文指纹；同档内按因果根因层级（effective_rank 越小越接近根因）排序
    candidates.sort(key=lambda item: (0 if item["has_code"] else 1, item["effective_rank"]))

    environment = collect_readonly_environment(workspace)
    symptom_sha256 = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()

    if candidates:
        winner = candidates[0]
        w_rule: RepoRule = winner["rule"]
        confidence = "high" if winner["has_code"] else "medium"
        primary_owner = {
            "repo": w_rule.repo,
            "layer": w_rule.layer,
            "confidence": confidence,
            "matched_codes": winner["matched_codes"],
            "matched_fingerprints": winner["matched_fingerprints"],
            "summary_zh": w_rule.summary_template,
        }
        secondary_components: list[dict[str, Any]] = []
        for other in candidates[1:]:
            o_rule: RepoRule = other["rule"]
            if other["has_code"]:
                role = "co_occurring_gate"
                reason_zh = (
                    f"同时检测到 {o_rule.repo} 的错误码（{', '.join(other['matched_codes'])}），"
                    f"按跨仓因果链判断，先排除主责仓 {w_rule.repo} 的底层阻断后再复验本组件。"
                )
            else:
                role = "caller_or_propagation"
                reason_zh = (
                    f"日志包含 {o_rule.repo} 的调用路径或上下文特征（{', '.join(other['matched_fingerprints'])}），"
                    f"属于错误向上传播的调用方或外层门禁，非直接根因。"
                )
            secondary_components.append({
                "repo": o_rule.repo,
                "layer": o_rule.layer,
                "role": role,
                "matched_codes": other["matched_codes"],
                "matched_fingerprints": other["matched_fingerprints"],
                "reason_zh": reason_zh,
            })
        actions = (
            w_rule.default_actions
            if _is_sfa_workspace(workspace)
            else _EXTERNAL_DEFAULT_ACTIONS.get(w_rule.repo, w_rule.default_actions)
        )
        recommended_actions = [
            {
                "step": idx,
                "target_repo": w_rule.repo,
                "command_or_check": cmd,
                "purpose_zh": purpose,
            }
            for idx, (cmd, purpose) in enumerate(actions, start=1)
        ]
        if secondary_components:
            first_sec = secondary_components[0]
            recommended_actions.append({
                "step": len(recommended_actions) + 1,
                "target_repo": first_sec["repo"],
                "command_or_check": f"在 {w_rule.repo} 问题修复后复跑 {first_sec['repo']} 门禁",
                "purpose_zh": "确认底层问题解除后上层调用栈报错随之消除，避免在传播层打补丁。",
            })
    else:
        skill_match = re.search(r"skills/([a-z0-9][a-z0-9._\-]*)/", combined_text)
        biz_fps = [f"skill-path:{skill_match.group(1)}"] if skill_match else []
        confidence = "medium" if biz_fps else "low"
        summary_zh = (
            f"未检测到 Foundation、注册表、制品图谱或发布层标准错误码，症状集中在业务技能 {skill_match.group(1)}。"
            if skill_match
            else "未检测到已知底座或治理层标准错误码前缀；若非具体业务技能内部异常，请补充原始报错 JSON 或完整日志重试。"
        )
        primary_owner = {
            "repo": "business-skill",
            "layer": "领域业务技能层（或待补充原始报错信号）",
            "confidence": confidence,
            "matched_codes": [],
            "matched_fingerprints": biz_fps,
            "summary_zh": summary_zh,
        }
        secondary_components = []
        recommended_actions = [
            {
                "step": 1,
                "target_repo": "business-skill",
                "command_or_check": "检查目标业务技能 SKILL.md、输入参数与被调脚本的原始退出码及 stderr",
                "purpose_zh": "确认是否为业务参数缺失或领域脚本内部逻辑报错。",
            },
            {
                "step": 2,
                "target_repo": "skill-failure-auditor",
                "command_or_check": "使用 --log-file <完整错误日志> 重新运行 triage-symptom",
                "purpose_zh": "提供未经截断的原始报错输出，排除被摘要隐去的底层错误码。",
            },
        ]

    version_upgrade_hint = _detect_version_upgrade_hints(
        augmented_text,
        environment,
        primary_owner_repo=primary_owner["repo"],
    )

    return {
        "schema_version": "1.0",
        "kind": "skill-family.symptom-triage-report",
        "read_only": True,
        "primary_owner": primary_owner,
        "secondary_components": secondary_components,
        "input_summary": {
            "sources": sources,
            "total_chars": len(combined_text),
            "symptom_sha256": symptom_sha256,
            "structured_error_codes": structured_codes,
        },
        "environment": environment,
        "recommended_actions": recommended_actions,
        "versionUpgradeHint": version_upgrade_hint,
    }
CONFIDENCE_ZH = {
    "high": "高（命中标准错误码）",
    "medium": "中（命中组件特征指纹）",
    "low": "低（缺少明确错误码，需补充原始日志）",
}


def render_triage_markdown(report: dict[str, Any]) -> str:
    """将结构化跨仓归因结果渲染为结论前置的自然中文诊断单。"""
    primary = report["primary_owner"]
    secondary = report["secondary_components"]
    env = report["environment"]
    ws = env.get("workspace", {})
    inp = report["input_summary"]

    lines: list[str] = [
        "# 跨仓故障归因诊断单",
        "",
        "## 归因结论",
        "",
        f"- **主责仓库 / 归属层**：`{primary['repo']}`（{primary['layer']}）",
        f"- **归因置信度**：{CONFIDENCE_ZH.get(primary['confidence'], primary['confidence'])}",
        f"- **根因判断**：{primary['summary_zh']}",
    ]
    if primary["matched_codes"]:
        lines.append(f"- **命中标准错误码**：`{'`、`'.join(primary['matched_codes'])}`")
    if primary["matched_fingerprints"]:
        lines.append(f"- **命中特征指纹**：`{'`、`'.join(primary['matched_fingerprints'])}`")
    lines.append("- **只读保证**：本次诊断仅读取症状与环境元数据，未修改目标工作区或启动业务技能。")
    lines.append("")

    lines.append("## 次要关联组件与传播链")
    lines.append("")
    if not secondary:
        lines.append("本次报错未发现跨组件传播痕迹，问题集中在上述主责层。")
    else:
        for item in secondary:
            lines.append(f"- **`{item['repo']}`**（{item['layer']}）：{item['reason_zh']}")
    lines.append("")

    lines.append("## 环境与版本核查摘要")
    lines.append("")
    lines.append(f"- **操作系统 / Python / Node**：`{env.get('os')}` / Python `{env.get('python_version')}` / Node `{env.get('node_version')}`")
    lines.append(f"- **Foundation 受管 Node 状态**：`{env.get('foundation_node_status')}`")
    lines.append(f"- **目标工作区**：`{ws.get('root_display')}`（Git HEAD：`{ws.get('git_head_sha')}`）")
    active_hosts = [h for h, flag in (env.get("detected_host_signals") or {}).items() if flag]
    lines.append(f"- **探测到的宿主环境信号**：{', '.join(f'`{h}`' for h in active_hosts) if active_hosts else '未检测到特定宿主环境变量（普通终端或隔离环境）'}")

    deps = ws.get("internal_dependencies") or {}
    if deps:
        lines.append("- **生态内部依赖（声明 vs 实际解析）**：")
        for pkg_name, ver_info in sorted(deps.items()):
            lines.append(f"  - `{pkg_name}`：声明 `{ver_info.get('declared') or '未直接声明'}`，实际解析 `{ver_info.get('resolved') or '未安装于根 node_modules'}`")
    upgrade_hints = report.get("versionUpgradeHint") or []
    if upgrade_hints:
        lines.append("- **底座版本落后因果提醒（versionUpgradeHint）**：")
        for hint in upgrade_hints:
            lines.append(
                f"  - `{hint['package']}`：当前锁定 `{hint['locked_version']}`，建议升级至 `>={hint['recommended_version']}` — {hint['reason_zh']}"
            )
    lines.append("")

    lines.append("## 下一步只读排查与处理建议")
    lines.append("")
    for act in report["recommended_actions"]:
        lines.append(f"{act['step']}. **[{act['target_repo']}]** `{act['command_or_check']}`")
        lines.append(f"   - 目的：{act['purpose_zh']}")
    lines.append("")

    lines.append("## 附录：输入与诊断包元数据")
    lines.append("")
    lines.append(f"- 输入来源：`{', '.join(inp['sources'])}`（共 {inp['total_chars']} 字符）")
    lines.append(f"- 症状文本 SHA-256：`{inp['symptom_sha256']}`")
    if report.get("bundle_files"):
        lines.append("- 已生成的最小复现诊断包文件：")
        for bf in report["bundle_files"]:
            lines.append(f"  - `{bf['path']}`（`{bf['sha256']}`）")
    lines.append("")
    return "\n".join(lines)


def _ensure_bundle_outside_workspace(bundle_dir: Path, workspace: Path) -> None:
    """拒绝将诊断包写入受检工作区或其内部目录树，确保只读诊断不污染工作树。"""
    if bundle_dir.is_symlink():
        raise TriageInputError(f"--output-bundle 不能指向软链接路径：{bundle_dir}")
    resolved_workspace = workspace.resolve(strict=True)
    resolved_bundle = bundle_dir.resolve(strict=False)
    if resolved_bundle == resolved_workspace or resolved_bundle.is_relative_to(resolved_workspace):
        raise TriageInputError(
            f"--output-bundle 不能等于受检工作区或位于 --workspace 目录树内部：{bundle_dir}；"
            "请将 --output-bundle 指向受检工作区外的独立目录（如 /tmp/triage-bundle）。"
        )


def write_triage_bundle(
    bundle_dir: Path,
    report: dict[str, Any],
    excerpt_text: str,
    *,
    workspace: Path | None = None,
) -> list[dict[str, str]]:
    """在指定的全新或空目录内写出最小复现诊断包（JSON + 中文诊断单 + 脱敏摘录 + 环境快照）。"""
    if bundle_dir.is_symlink():
        raise TriageInputError(f"--output-bundle 不能指向软链接路径：{bundle_dir}")
    if workspace is not None:
        _ensure_bundle_outside_workspace(bundle_dir, workspace)
    if bundle_dir.exists():
        if not bundle_dir.is_dir():
            raise TriageInputError(f"--output-bundle 目标已存在且不是目录：{bundle_dir}")
        if any(bundle_dir.iterdir()):
            raise TriageInputError(
                f"--output-bundle 目录非空（拒绝覆盖既有诊断证据）：{bundle_dir}；请指定尚不存在的新目录。"
            )
    else:
        bundle_dir.mkdir(parents=True, exist_ok=False)

    written: list[dict[str, str]] = []

    def _write_one(filename: str, content_bytes: bytes) -> None:
        target = bundle_dir / filename
        with target.open("xb") as handle:
            handle.write(content_bytes)
        written.append({
            "path": filename,
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
        })

    _write_one("symptom-excerpt.txt", (excerpt_text[:16384].rstrip("\n") + "\n").encode("utf-8"))
    env_bytes = (json.dumps(report["environment"], ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_one("environment-snapshot.json", env_bytes)

    frozen_bundle_files = list(written)
    report_with_files = dict(report)
    report_with_files["bundle_files"] = frozen_bundle_files
    md_bytes = render_triage_markdown(report_with_files).encode("utf-8")
    _write_one("triage-report.md", md_bytes)

    json_bytes = (json.dumps(report_with_files, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_one("triage-report.json", json_bytes)
    return list(frozen_bundle_files)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage-symptom",
        description="跨仓故障归因只读诊断 CLI：根据报错症状、日志或错误 JSON 定位主责仓库，并可生成最小复现诊断包。",
    )
    parser.add_argument(
        "--symptom",
        action="append",
        default=[],
        help="故障描述文本或报错 JSON 字符串（可多次传入）。",
    )
    parser.add_argument(
        "--log-file",
        action="append",
        type=Path,
        default=[],
        help="待只读分析的报错日志文件或 JSON 文件路径（可多次传入，拒绝软链接）。",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="待只读采集版本与依赖状态的目标工作区根目录（默认为当前工作目录）。",
    )
    parser.add_argument(
        "--output-bundle",
        type=Path,
        default=None,
        help="可选：将最小复现诊断包（JSON + 中文诊断单 + 环境快照）写入受检工作区外的独立新目录（如 /tmp/triage-bundle）。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="向标准输出打印结构化 JSON 结果；未指定时默认打印中文 Markdown 诊断单。",
    )
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workspace_raw = args.workspace if args.workspace is not None else Path.cwd()
        if workspace_raw.is_symlink() or not workspace_raw.is_dir():
            raise TriageInputError(
                f"--workspace 必须指向真实存在的普通目录（不能为软链接或缺失路径）：{workspace_raw}"
            )
        workspace = workspace_raw.resolve(strict=True)

        if args.output_bundle is not None:
            _ensure_bundle_outside_workspace(args.output_bundle, workspace)

        chunks: list[str] = []
        sources: list[str] = []
        for idx, text in enumerate(args.symptom, start=1):
            if text and text.strip():
                chunks.append(text.strip())
                sources.append(f"symptom[{idx}]")

        for log_path in args.log_file:
            if log_path.is_symlink() or not log_path.is_file():
                raise TriageInputError(
                    f"--log-file 必须指向真实存在的普通文件（拒绝软链接或目录）：{log_path}"
                )
            if log_path.stat().st_size > MAX_LOG_BYTES:
                raise TriageInputError(
                    f"--log-file 超过单文件只读上限（{MAX_LOG_BYTES} 字节）：{log_path}；请先截取关键报错片段。"
                )
            content = log_path.read_text(encoding="utf-8", errors="replace")
            if content.strip():
                chunks.append(content.strip())
                sources.append(f"log:{log_path.name}")

        if not chunks:
            raise TriageInputError(
                "未提供有效故障输入：请至少传入非空的 --symptom <故障描述> 或 --log-file <报错日志文件>。"
            )

        raw_combined = "\n\n".join(chunks)
        redacted_combined = redact_text(raw_combined, workspace_abs=workspace)
        report = evaluate_symptom_text(redacted_combined, sources=sources, workspace=workspace)

        if args.output_bundle is not None:
            bundle_files = write_triage_bundle(
                args.output_bundle,
                report,
                redacted_combined,
                workspace=workspace,
            )
            report["bundle_files"] = bundle_files

        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(render_triage_markdown(report), end="")
        return 0
    except (TriageInputError, OSError) as error:
        print(f"ERROR [triage-symptom]: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run_cli())

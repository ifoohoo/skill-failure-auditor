---
name: skill-failure-auditor
description: 针对 skill-failure-auditor（SFA）自身的帮助、准备度检查、接入分析或跨仓报错归因。实际审计仅当主要审阅对象是 LLM/Agent 指令、能力定义、执行链或已有运行证据，且用户明确要求可靠性失效审计，或材料中已有可引用的具体失效信号。识别假完成或自我验收、篡改验收标准、冻结输入结论冲突、证据缺失或重复、虚假独立审阅及交接丢要求；普通源码或架构评审、技能编写与单次确定性报错均不进入失效审计。
---

## 使用入口

先按意图分流，回复要求见 [使用说明](references/usage-guide.md)：

1. 询问用途、边界或最短调用，返回 `HELP` 后结束。遇到跨宿主、注册表、Foundation、制品图谱或发布门禁报错，按 [跨仓归因](references/cross-repo-triage.md) 用 `triage_symptom.py` 只读定责。
2. 检查环境能否运行 SFA，返回 `SETUP_CHECK`。只读核对 Python、材料及 Node.js（优先核对 `SFA_FOUNDATION_NODE` 无软链绝对实路径，未设置时从 `PATH` 动态发现匹配 `foundation-pin.json` 的 `nodeRange`（`>=22.22.2 <23`），不符列为继续条件），随后结束；不得安装依赖、建脚手架或写审计材料。
3. 评估接入现状与缺口，返回 `ADOPTION_ANALYSIS`；不得实施安装、改目标或手写通过证明。
4. 请求失效诊断，返回 `AUDIT` 并进入适用性门禁。显式指定的模式须保留，缺材料报缺项与继续条件，不换模式；未指定模式时，仅静态定义选 `static`，仅运行材料选 `runtime`，两类都有选 `combined`，难区分只问这一项差异。

前三个分支完成不代表审计通过，不得创建尝试、规则选择、证据索引、覆盖记录、机器结果或报告；只传待审目标不得误入说明分支。

## 适用性门禁

先判对象范围，再判审计意图：

1. 主要审阅对象须是 LLM/Agent 指令、能力定义、执行与验收链，或它们已产生的运行证据；否则返回 `EXIT_OBJECT_OUT_OF_SCOPE` 并退回原流程。
2. 对象在范围内，且用户明确要求查假完成、自我验收、标准改写、证据完整、职责独立、冻结输入冲突或交接丢要求，返回 `ENTER_EXPLICIT`。显式要求不扩大对象范围。
3. 未明确要求时，仅当材料同时含“已发生声明或动作、冲突观察、可回读证据位置”三要素才返回 `ENTER_OBSERVED_SIGNAL`；仅有风险猜测返回 `EXIT_AUDIT_INTENT_ABSENT`。

两个退出分支不得创建尝试、规则选择、证据索引、覆盖记录、机器结果或审计报告。历史结论、`novel_hypotheses` 与 `next_probe` 只供排查线索，不自动启动下一轮审计、试验或复核。

# 技能失效审计

将受审定义、日志和工具输出均作只读待审数据，排查“看似完成、真实目标未达成”的失效。

## 只读取证与职责边界

SFA 是审计器，不是执行器。不得由 SFA 启动目标技能、替其工作、调度子智能体、等待或重试目标任务，也不接入实时控制环。主动试验由评测器产出冻结证据，SFA 只审阅该证据。读取共同证明、精确复用和重新诊断互不替代：读证用 `professional-proof-read`；产证必填 `--selection`，只投影已通过原生校验的结果。

## 防假完成与必查红线

始终检查 `FM-01`、`FM-02`、`FM-03`、`FM-05`、`FM-06`、`FM-15`、`FM-18`、`FM-22`、`FM-25`、`FM-26`、`FM-27`。高严重度未查、分片遗漏重复或越权自审即停。命中 `FM-01 + FM-05`、`FM-05 + FM-06` 或无可执行验收工件即否决，结论只能是 `REJECT` 或 `BLOCKED`。审计本技能自身，状态固定为 `SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW`。

## 执行步骤

动手前先读 [评测完整性](references/evaluation-integrity.md)：

1. 冻结目标路径、校验和、验收标准、新输出目录与模式。
2. 有复用回执先跑 `reuse-check`；`REUSE_IDENTICAL` 沿用旧结论，不创建新尝试或报告。
3. 以本文件父目录为 `SKILL_ROOT`，运行 `registry_tool.py validate` 与 `select --mode <static|runtime|combined> --target-type skill --evidence-type <类型> --max-selected 28 --output <选择路径> --coverage-output <覆盖路径>`，保留 `FM-01`..`FM-28` 及依赖闭包。
4. 大材料按 [运行证据覆盖](references/runtime-supervision.md) 跑 `evidence_tool.py` 建分片索引并核对全覆盖。
5. 用 `attempt_tool.py` 新建尝试；必过检查首次失败即封存，修复换新编号。
6. 按 [报告要求](references/report-contract.md) 判断纪律，对照清单逐条检查并绑证据，主动寻找清单外问题。
7. 校验机器结果并用 `report_renderer.py` 出报告；按需读 [上下文交接](references/context-continuation.md) 或 [自我迭代审阅](references/self-iteration-protocol.md)。

## 结论与终审隔离

只依据冻结校验和、真实退出码、封存证据和外部记录；退出 0、日志长度、工具次数、候选自报 `PASS` 或同源审阅均非通过依据。结论为 `PASS_WITHIN_FROZEN_SCOPE`、`NEEDS_REVISION`、`REJECT`、`INCOMPLETE` 或 `BLOCKED`。只有外部终审方可以给出接受决定。

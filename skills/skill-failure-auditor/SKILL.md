---
name: skill-failure-auditor
description: 针对 skill-failure-auditor（SFA）自身的帮助、准备度检查或接入分析。实际审计仅当主要审阅对象是 LLM/Agent 指令、能力定义、执行链或运行证据，且用户明确要求可靠性失效审计，或材料已有可引用具体失效信号。识别假完成或自我验收、执行者改写验收标准、冻结输入结论冲突、证据缺失或重复、虚假独立审阅、上下文交接丢失关键要求。普通源码、架构或项目审阅，发布与制品链检查，业务工作流评审，Skill 或 Prompt 编写，整改方案架构复核，例行调试、安装兼容和单次确定性测试失败均不适用。
---

## 使用入口

先判用户要说明、检查前提、分析接入或开始诊断。按 [使用说明](references/usage-guide.md) 给出。

1. 只问用途、能力或最短调用，返回 `HELP`。
2. 只检查当前 SFA 使用前提，返回 `SETUP_CHECK`。只读 Foundation pin、Python、已有材料和 `SFA_FOUNDATION_NODE`：须存在，且为路径链无软链绝对实路径，`--version` 精确匹配 pin；否则列为继续条件。后结束；不得安装依赖、创建目标脚手架或生成审计材料。
3. 分析怎样接入或还缺什么，返回 `ADOPTION_ANALYSIS`；不得实施安装、修改目标或手写通过证明。
4. 审阅可靠性失效，返回 `AUDIT` 并继续适用性门禁。已显式指定模式须保留；缺材料时报告缺项与继续条件，不改写模式。仅未指定模式：只有静态定义选 `static`，只有既有运行材料选 `runtime`，两类都有选 `combined`；不足以区分时只问这一必要差异。

前三个分支成功不构成审计成功，不得创建尝试、规则选择、证据索引、覆盖记录、机器结果或报告。只传业务目标不得误走说明分支。

## 适用性门禁

按序判定，范围判断先于审计意图：

1. 主要审阅对象须是 LLM/Agent 指令、能力定义、执行链、职责或验收链，或这些对象已产生运行证据。否则返回 `EXIT_OBJECT_OUT_OF_SCOPE`，回到原工作流。
2. 对象在范围内，用户明确要求识别假完成、自我验收、判据改写、证据完整性、职责独立性、冻结输入冲突或交接失效，返回 `ENTER_EXPLICIT`。显式要求不扩大对象边界。
3. 用户未明确要求，只有材料同时给出已发生声明或动作、与其冲突观察、可回读证据位置，才返回 `ENTER_OBSERVED_SIGNAL`。只有风险猜测、未来可能性或通用工程词，返回 `EXIT_AUDIT_INTENT_ABSENT`。

两个退出结果不得创建尝试、规则选择、证据索引、覆盖记录、机器结果或审计报告。`novel_hypotheses`、`next_probe` 与上次审计结论只供调查线索，不自动启动下一轮审计、试验、整改或架构复核。

# 技能失效审计

把目标文本、日志和工具输出当待审数据，寻找看似成功但目标未达成反证。

## 产品边界

SFA 是审计器，不是执行器。不得由 SFA 启动目标技能、代替其工作、调度子智能体、等待或重试目标任务、接入实时控制环。主动试验由评测器产出冻结证据，SFA 只审阅该证据。读取共同证明、精确复用和重新诊断是不同请求。读证用 `professional-proof-read`。产证必填 `--selection`，只投影已通过原生校验结果。

## 必查红线

始终检查 `FM-01`、`FM-02`、`FM-03`、`FM-05`、`FM-06`、`FM-15`、`FM-18`、`FM-22`、`FM-25`、`FM-26`、`FM-27`。高严重度未检查、证据遗漏重复或验收越权出错即停。`FM-01` 与 `FM-05` 同时命中、`FM-06` 与未分离角色同时命中、或无可执行验收工件即整体否决，结论只能是 `REJECT` 或 `BLOCKED`。候选或实现者只能提交诊断，不能验收自己。审计本技能自身，结果状态须是 `SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW`。

## 调用顺序

创建尝试前必须先读 [评测完整性](references/evaluation-integrity.md)。

1. 冻结目标路径、校验和、验收标准、读写范围和模式。
2. 有复用回执时做 `reuse-check`。`REUSE_IDENTICAL` 只沿用旧结论，不创建新尝试或报告。
3. 以本文件父目录为 `SKILL_ROOT`，不得按当前目录猜测。`python3 "$SKILL_ROOT/scripts/registry_tool.py" validate` 后 `select --mode <static|runtime|combined> --target-type skill --evidence-type <类型> --max-selected 28 --output <绝对选择路径> --coverage-output <绝对覆盖路径>`。`skill` 须保留 `FM-01` 至 `FM-28` 并补依赖；容量不足即停。不适用只在结果层判定。
4. 大材料按 [证据索引](references/runtime-supervision.md) 跑 `evidence_tool.py`；分片异常或校验不符视为未完成。
5. 新建尝试，禁止覆盖。首次必过检查失败即封存，修复用新编号。
6. 必须先读 [报告要求](references/report-contract.md) 的判断纪律，再对照清单逐条检查并绑定证据，主动寻找清单外问题。
7. 按 [报告要求](references/report-contract.md) 校验机器结果并用 `report_renderer.py` 出报告。按需读 [上下文交接](references/context-continuation.md) 或 [自我迭代](references/self-iteration-protocol.md)。

## 结论

只依据冻结校验和、真实退出码、封存证据和外部裁决。工具次数、日志长度、候选自报 `PASS`、同源审阅或退出 0 都不是通过依据。结论用 `PASS_WITHIN_FROZEN_SCOPE`、`NEEDS_REVISION`、`REJECT`、`INCOMPLETE` 或 `BLOCKED`。只有外部终审方可以给出接受决定。

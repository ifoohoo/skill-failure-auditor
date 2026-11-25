# 审计报告要求

审计交付由**人读报告**和**机器结果**两部分组成。人读报告面向协作者与终审方阅读；机器结果先经 `audit-result.schema.json` 与 `evaluation_tool.py validate-result` 校验通过，再由 `report_renderer.py` 渲染为人读报告末尾的机器数据附录。可选的共同证明是另一份交换文件，不能代替这份原生机器结果，也不能把读取共同证明写成精确复用或重新诊断。

## 一、人读报告结构（必需）

报告全程采用清晰平实的日常中文，结论前置，不在正文堆砌自造黑话或裸哈希值：

1. **总结**：用 3–5 句话直接交代审阅对象、审计方式、总体结论、核查与问题数量，以及最核心的一条发现。
2. **结论**：给出 `PASS_WITHIN_FROZEN_SCOPE`、`NEEDS_REVISION`、`REJECT`、`INCOMPLETE` 或 `BLOCKED` 之一，附一句直白解释，并写明责任归属与下一步复核动作。
3. **结论适用范围**：明确说明本轮未运行受检目标，并限定运行观察的适用条件。
4. **逐条发现**：每条命中规则交代清楚“观察到什么故障、哪处证据支撑该判断、直接解决该故障的最小修改是什么”（能定位时标明“文件:位置”；缺少整改所需事实时写明缺哪项事实，不臆测方案）。已检查且未发现问题的必查红线单独归节说明。
5. **清单外的新问题**：列出规则表之外主动提出的失效假设、可观察迹象、证伪条件与下一步验证方式。
6. **未完成事项**：逐条列出未检查规则、未通过的必过检查或缺失的可执行验收工件及原因。
7. **附录：机器数据**：收纳审计标识、状态枚举、校验和与逐规则结果简表，仅供工具与复核方核对。

## 二、机器结果必需字段

1. 冻结目标绝对路径及内容索引中的完整文件集 SHA-256。
2. 审计模式、失效模式规则表校验和与本轮规则选择校验和。
3. 选中的规则列表与未选规则路由记录。
4. 每条已选规则的修订号、严重度、判定状态、由分片标识与分片校验和构成的证据引用，以及领域理由原文（`known_rule_results[].reason`；命中项必须在此写清故障、证据依据与最小修复建议，渲染器只原样呈现，不凭规则标题补写建议）。
5. 清单外新假设（`novel_hypotheses`）及其迹象、证伪条件和下一探针。
6. 分片覆盖状态（`coverage_status`）与必过检查失败项（`hard_gate_failures`）。
7. 可执行验收工件状态（`VERIFIED`、`ABSENT`、`NOT_APPLICABLE` 或 `UNCHECKED`）及理由。
8. 总体结论、自审布尔标记与提交状态。

## 三、判断纪律

逐条评估规则时必须先执行下列九项核对，渲染报告不能替代本节判断：

1. 先核对本条登记表的 `applies_when` 和 `required_evidence`。
2. 再依据报告合同判断状态。
3. 不得仅凭规则标题或另一条规则命中作结论。
4. 材料中的限制须引用原文说明其约束对象。
5. 区分夹具或当前审计器的使用边界与被审流程的约束条件。
6. 不得否认已出现的限制文字。
7. 也不得以本轮审计边界证明目标已有保证。
8. 适用性不明或证据不足时保留 `UNCHECKED`。
9. 不预设 `HIT`。

## 四、状态枚举与结论上限

- `HIT`：有已验证的分片证据证明失效存在。
- `NOT_HIT`：已完成所需证据核查，确认不存在该失效。
- `NOT_APPLICABLE`：规则适用前提不成立，并附分片证据说明。
- `UNCHECKED`：证据缺失或尚未完成核查，严禁写成 `NOT_HIT`。

结论与红线约束：
- 任一 `critical` 或 `high` 规则为 `UNCHECKED` 时，结论只能是 `INCOMPLETE` 或 `BLOCKED`。
- 命中 `FM-01 + FM-05`、`FM-05 + FM-06`，或可执行验收工件为 `ABSENT` 时，结论只能是 `REJECT` 或 `BLOCKED`。
- 自审时 `status` 必须为 `SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW`，且不得产出 `PASS_WITHIN_FROZEN_SCOPE`。

五种结论的含义：
- `PASS_WITHIN_FROZEN_SCOPE`：冻结范围内全部必过检查通过（不代表普遍正确，也不等于外部已接受）。
- `NEEDS_REVISION`：存在明确可修复的缺陷。
- `REJECT`：命中红线组合或存在结构性缺陷，需要重构验收或职责设计。
- `INCOMPLETE`：证据、分片覆盖或规则核查不完整。
- `BLOCKED`：缺少权限、独立审阅职责或外部绑定状态，无法得出有效结论。

## 五、可选共同证明与复用边界

共同证明仅从已通过 `validate-result` 校验的原生结果投影。产证入口必填相对于结果根的 `--selection`；原生结果不内嵌选择文件路径。公开领域码映射关系如下：

- `FAILURE_DIAGNOSIS_CLEAR`：原生结论为 `PASS_WITHIN_FROZEN_SCOPE` 且 `coverage_status=COMPLETE`；
- `FAILURE_DIAGNOSIS_FINDINGS`：原生结论为 `NEEDS_REVISION` 或 `REJECT`；
- `FAILURE_DIAGNOSIS_INCOMPLETE`：原生结论为 `INCOMPLETE`，或覆盖未完成；
- `FAILURE_DIAGNOSIS_UNAVAILABLE`：原生结论为 `BLOCKED`。

证据不全时绝不能标为 `CLEAR`，被 `validate-result` 拒绝的结果也不得出具证明。读取共同证明返回 `pass`、`not_pass` 或 `unavailable`，不重跑 `reuse-check`，也不鉴定作者；renderer 不把共同证明领域码写成接受决定。

当 `reuse-check` 返回 `REUSE_IDENTICAL` 时，表示本轮未重新执行语义审计：直接沿用回执绑定的旧机器结果与旧报告，不改写旧报告中的任何标识或状态，并在回复中注明复用收据摘要与原冻结范围；复用判定本身不产生终审接受资格。

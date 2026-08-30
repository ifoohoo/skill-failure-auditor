# 尝试与评测完整性

## 验收标准先于实现

在候选修改前冻结评测规格、验收标准承诺、关键变异、角色权限和写集。实现开始后不得修改同一版本的验收标准。

## 尝试编号

创建：

```bash
python3 "$SKILL_ROOT/scripts/attempt_tool.py" create \
  --root /绝对路径/attempts \
  --attempt-id ATTEMPT-001 \
  --candidate-sha256 <64位校验和> \
  --criteria-commitment-sha256 <64位校验和> \
  --write-path versions/skill-failure-auditor-v2-candidate \
  --created-by implementer
```

记录证据后封存：

```bash
python3 "$SKILL_ROOT/scripts/attempt_tool.py" record \
  --attempt /绝对路径/attempts/ATTEMPT-001 \
  --kind verification \
  --artifact /绝对路径/verification.json

python3 "$SKILL_ROOT/scripts/attempt_tool.py" seal \
  --attempt /绝对路径/attempts/ATTEMPT-001 \
  --outcome FAILED \
  --reason-code HARD_GATE_FAILED
```

工具对所有文件只许新建、不许覆盖，并拒绝向已封存尝试新增记录或重开；这不是同一操作系统用户下的不可变存储。`seal` 只返回 `SEALED_PENDING_EXTERNAL_BINDING` 和 `seal_file_sha256`。外部控制者冻结该文件字节校验和后，使用 `verify --expected-seal-file-sha256 <冻结校验和>` 回读；只有 `VERIFIED_SEALED_BOUND` 证明封存字节与冻结时点一致，仍不产生接受资格。修复必须建新的尝试编号。

## 评测边界

- 先区分目标声称的结论层级：本地测试成功、候选自测、正式接受、独立审阅和外部权威裁定
  不是同一状态。普通本地测试输出 `PASS` 只证明该测试执行成功；如果它没有直接驱动正式
  接受，也没有冒充独立审阅或外部裁定，就不能仅因实现、测试和期望同仓而命中 FM-01、
  FM-05、FM-27 的高风险组合。此时应记录外部保证不在范围内，而不是虚构已有保证。
- 执行者不得选取更容易的测试或修改期望适配实际结果。
- 进程退出 0 不能替代结构化结果。
- 评分器异常、结果缺失、标识不匹配或高严重度规则未检查时出错即停。
- 同一模型的新进程只能证明进程隔离；没有不同认知来源时不得声称权威独立。
- 候选自审只能提交外部复核。

使用 `evaluation_tool.py validate-result --selection <选择文件> --registry <登记表>` 校验审计结果。结果必须绑定实际目标、内容索引、覆盖记录和覆盖清单；验证器会重算索引与清单，并把每条证据引用解析到已审分片。普通结果不能自报 `EXTERNALLY_REVIEWED`。使用 `grade` 对冻结用例集合做精确集合比较；最终接受只消费外部签名授权和其绑定证据。

## 完全同一复用

复用检查位于适用性门禁之后、创建新尝试之前。调用方必须明确提供上一轮制品；工具不扫描磁盘寻找“最近一次”结果。

完整审计的机器结果通过校验、规则选择为 `SELECTED`、覆盖为 `COMPLETE`、高严重度规则全部检查，且结论为 `PASS_WITHIN_FROZEN_SCOPE`、`NEEDS_REVISION` 或 `REJECT` 时，可以在尝试封存前生成回执：

```bash
python3 "$SKILL_ROOT/scripts/evaluation_tool.py" reuse-receipt-create \
  --result /绝对路径/audit-result.json \
  --report /绝对路径/report.md \
  --selection /绝对路径/selection.json \
  --attempt /绝对路径/attempts/ATTEMPT-001 \
  --output /绝对路径/audit-reuse-receipt.json
```

生成命令会重新校验结果、选择、证据覆盖、报告渲染、尝试清单，以及尝试中唯一的 `audit_result` 和 `audit_report` 记录。`INCOMPLETE`、`BLOCKED`、高严重度规则未检查、报告漂移或已封存尝试均拒绝生成回执。

生成后按固定顺序收尾：

1. 用 `attempt_tool.py record --kind audit_reuse_receipt` 把回执记录到原尝试；
2. 非自审以 `CANDIDATE_SUBMITTED` 封存，自审以 `SELF_AUDIT_SUBMITTED` 封存；
3. 由外部控制者冻结 `seal.json` 的原始字节 SHA-256；
4. 后续把该 SHA-256 传给 `reuse-check`。

后续检查示例：

```bash
python3 "$SKILL_ROOT/scripts/evaluation_tool.py" reuse-check \
  --subject /绝对路径/frozen-input \
  --mode combined \
  --evidence-type runtime-log \
  --criteria-commitment /绝对路径/criteria.json \
  --prior-result /绝对路径/prior/audit-result.json \
  --prior-report /绝对路径/prior/report.md \
  --prior-selection /绝对路径/prior/selection.json \
  --prior-attempt /绝对路径/prior/ATTEMPT-001 \
  --prior-reuse-receipt /绝对路径/prior/audit-reuse-receipt.json \
  --expected-prior-seal-file-sha256 <64位校验和> \
  --output /绝对路径/reuse-decision.json
```

只有目标规范路径和文件集、判据原始字节、模式、证据类型、登记表、28 条规则选择、审计器运行字节、旧结果、旧报告和外部绑定的封存尝试全部相同，才返回 `REUSE_IDENTICAL`。该结果不创建新 `audit_id`、尝试、覆盖记录或报告，也没有接受资格。任一合法身份变化返回 `FULL_AUDIT_REQUIRED`；旧制品格式错误、摘要篡改、重复必需记录或精确 Foundation 运行环境不可用时退出码为 2。当前请求要求刷新外部证据时必须添加 `--fresh-evidence-required`。

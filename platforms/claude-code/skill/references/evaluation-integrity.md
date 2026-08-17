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

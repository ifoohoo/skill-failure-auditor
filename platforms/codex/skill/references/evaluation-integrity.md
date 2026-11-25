# 尝试管理、防假完成与评测完整性

## 一、验收标准先于实现冻结

在修改候选实现或启动评测之前，必须先把评测规格、验收标准承诺、关键变异用例、角色权限和允许写入路径固定下来。工作开始后，任何执行职责都不得为了“让结果变绿”而放宽、删减或改写同一版本的验收标准。

## 二、防假完成四条铁律（`US-075`）

审阅任何技能或工作流时，必须严格区分以下四类不同性质的记录，防止用低层级状态冒充高层级验收：

1. **区分结论层级**：本地单测输出 `PASS`、候选执行者自测、共同证明读取（`professional-proof-read`）、独立语义审阅与外部终审接受是五种截然不同的状态。如果本地测试只是正常报告自身运行结果，并未冒充独立审阅或直接触发正式接受，就不能仅因实现与测试同仓而机械判中 `FM-01 + FM-05 + FM-27`；此时应如实注明“外部独立验收不在当前材料范围内”，而不是凭空补造保证或误判红线。
2. **退出码不等于任务完成**：脚本退出 `0`、生成了非空文件、工具调用次数多或日志很长，只代表进程走完，不能代替对结果内容与业务约束的精确核验。
3. **进程隔离不等于认知独立**：由同一执行职责启动的子进程或新会话只提供运行环境隔离；只要审阅方没有独立于实现者的职责边界与否决任务，就不能声称完成了独立终审。
4. **自审无接受权**：当受审目标就是当前审计技能自身或其字节级同源副本时，验证器会强制核对 `self_audit: true` 与 `SELF_AUDIT_SUBMITTED_FOR_EXTERNAL_REVIEW` 状态，任何试图自评通过的结果都会直接报错拒绝。

## 三、只追加尝试与外部封存绑定

使用 `attempt_tool.py` 创建、追记并封存单次尝试：

```bash
python3 "$SKILL_ROOT/scripts/attempt_tool.py" create \
  --root /absolute/path/attempts \
  --attempt-id ATTEMPT-001 \
  --candidate-sha256 <64位校验和> \
  --criteria-commitment-sha256 <64位校验和> \
  --write-path evidence/attempts/ATTEMPT-001 \
  --created-by auditor

python3 "$SKILL_ROOT/scripts/attempt_tool.py" record \
  --attempt /absolute/path/attempts/ATTEMPT-001 \
  --kind verification \
  --artifact /absolute/path/verification.json

python3 "$SKILL_ROOT/scripts/attempt_tool.py" seal \
  --attempt /absolute/path/attempts/ATTEMPT-001 \
  --outcome FAILED \
  --reason-code HARD_GATE_FAILED
```

尝试目录内所有文件均通过独占写入创建，禁止覆盖，且已封存的尝试拒绝继续追记或重开；必过检查一旦失败即封存当前尝试，后续修复必须启用新的 `attempt_id`。由于同一操作系统用户下本地文件权限不能代替外部防篡改证明，`seal` 仅返回 `SEALED_PENDING_EXTERNAL_BINDING` 与 `seal_file_sha256`；须由外部控制方保存该摘要，并在复核时传入 `verify --expected-seal-file-sha256 <冻结校验和>` 取得 `VERIFIED_SEALED_BOUND`。

使用 `evaluation_tool.py validate-result --input <结果> --selection <选择文件> --registry <规则表>` 校验机器结果时，工具会自动重算内容索引与分片覆盖清单，并核实每条证据引用都落在已审分片上。对冻结评测集打分时使用 `evaluation_tool.py grade` 做精确集合比对。

## 四、完全同一复用（`reuse-check`）

复用判定位于适用性门禁之后、创建新尝试之前；调用方必须显式传入上一轮全部制品路径，工具不会在磁盘上自动搜索“最近一次结果”。

当上一轮审计满足“机器结果通过校验、规则选择为 `SELECTED`、分片覆盖为 `COMPLETE`、高严重度规则无漏查，且结论为 `PASS_WITHIN_FROZEN_SCOPE`、`NEEDS_REVISION` 或 `REJECT`”时，可在封存前生成复用回执并按固定顺序收尾：

```bash
python3 "$SKILL_ROOT/scripts/evaluation_tool.py" reuse-receipt-create \
  --result /absolute/path/audit-result.json \
  --report /absolute/path/report.md \
  --selection /absolute/path/selection.json \
  --attempt /absolute/path/attempts/ATTEMPT-001 \
  --output /absolute/path/audit-reuse-receipt.json
```

1. 用 `attempt_tool.py record --kind audit_reuse_receipt` 将复用回执记入该次尝试；
2. 非自审以 `CANDIDATE_SUBMITTED` 封存，自审以 `SELF_AUDIT_SUBMITTED` 封存；
3. 由外部控制方记录 `seal.json` 的原始字节 SHA-256；
4. 下一轮调用 `reuse-check` 时传入 `--expected-prior-seal-file-sha256 <64位校验和>`：

```bash
python3 "$SKILL_ROOT/scripts/evaluation_tool.py" reuse-check \
  --subject /absolute/path/frozen-input \
  --mode combined \
  --evidence-type runtime-log \
  --criteria-commitment /absolute/path/criteria.json \
  --prior-result /absolute/path/prior/audit-result.json \
  --prior-report /absolute/path/prior/report.md \
  --prior-selection /absolute/path/prior/selection.json \
  --prior-attempt /absolute/path/prior/ATTEMPT-001 \
  --prior-reuse-receipt /absolute/path/prior/audit-reuse-receipt.json \
  --expected-prior-seal-file-sha256 <64位校验和> \
  --output /absolute/path/reuse-decision.json
```

仅当目标规范路径与文件集、验收承诺字节、审计模式、证据类型、规则表、28 条规则选择、审计器自身核心闭包、旧结果、旧报告和外部绑定的封存尝试十项身份全部一致时，才返回 `REUSE_IDENTICAL`；任一合法输入变化均返回 `FULL_AUDIT_REQUIRED`（若请求明确要求刷新外部证据，须附加 `--fresh-evidence-required`）。旧制品损坏、摘要不符或受管 Foundation 运行时不可用时，命令直接以退出码 `2` 终止。

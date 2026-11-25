# 运行证据审阅与分片覆盖

## 一、事后审阅定位

本指南适用于审阅已经结束的运行日志、工具调用流、多智能体协作记录、验证器输出与多文件证据包；单篇简短的静态定义无需拆分为多段，但正式机器结果仍需通过 `evidence_tool.py` 绑定真实分片引用。

这里规范的是**事后证据完整性核查**，而非实时运行监督：SFA 不启动、暂停、等待、重试或纠偏目标任务，也不向目标进程发送任何控制指令。

## 二、运行材料的事实边界

审阅运行证据前，先从原始记录中提取真实存在的模型标识、版本号、运行配置、任务输入、时间戳与宿主环境：

- 材料未记录的维度一律标为未知，不得拿当前审计宿主或维护者预期去补填；
- `runtime` 只代表“审阅已经产生的运行材料”，`combined` 只代表“联合审阅静态定义与已有运行材料”，两者都不代表 SFA 亲自运行了受审目标；
- 某一次运行在特定模型与配置下的表现，不能外推到其他模型、其他版本或后续运行。

## 三、真实进展的判定标准

评估长程任务或多阶段运行记录时，只有以下可验证状态变化才算真实进展：

- 权威门禁或验证器的真实退出码发生预期变化；
- 冻结制品完整进入契约规定的阶段终态；
- 新增封存证据的校验和有效，且分片无遗漏、无重复；
- 独立语义审阅产出了绑定具体证据位置的结构化整改意见。

工具调用次数、输出日志行数、运行耗时、改动文件数、执行者自报 `PASS` 或口头完成百分比，一律不得算作任务完成度。

## 四、内容寻址分片索引与覆盖守恒

1. **建立分片索引**（输出文件必须位于受审输入目录之外）：

```bash
python3 "$SKILL_ROOT/scripts/evidence_tool.py" index \
  --input /absolute/path/evidence \
  --output /absolute/path/audit-output/evidence-index.json
```

索引按固定字节窗口切分输入文件，记录文件集摘要、总字节数及每个分片的起止偏移与 SHA-256，连续双跑字节完全一致；空目录或零字节文件会直接报错拒绝。

2. **核验并按需读取分片**：

```bash
python3 "$SKILL_ROOT/scripts/evidence_tool.py" verify \
  --input /absolute/path/evidence \
  --index /absolute/path/audit-output/evidence-index.json
```

大日志使用 `extract --chunk-id CHUNK-000000` 或 `search --needle <关键字>` 定位具体分片，严禁只看首尾几行就推断中间分片没有问题。

3. **校验全分片覆盖守恒**：

```bash
python3 "$SKILL_ROOT/scripts/evidence_tool.py" coverage \
  --input /absolute/path/evidence \
  --index /absolute/path/audit-output/evidence-index.json \
  --selection /absolute/path/audit-output/selection.json \
  --registry "$SKILL_ROOT/references/failure-modes.jsonl" \
  --records /absolute/path/audit-output/coverage-records.jsonl \
  --output /absolute/path/audit-output/coverage-ledger.json
```

只有当“输入分片集合与已审分片集合完全相等、每个分片恰好出现一次、分片摘要与已选规则集合逐项匹配、JSONL 无空行或损坏行”全部成立时，覆盖状态才为 `COMPLETE`；任何缺片、重片或摘要不符都会返回非零退出码并阻断通过结论。审阅中若发现目标任务存在越权写入、篡改测试或静默吞错，应绑定具体分片记为审计发现，不得由 SFA 接管修复。

# 运行证据审阅与覆盖

## 适用范围

在审计已经结束的真实运行、长日志、多智能体工作流、验证器调用、进程证据或分片材料时读取本文件。静态短文本不必建立分片索引。

本文件规定的是事后证据完整性，不是实时监督协议。SFA 不启动、暂停、等待、重试或纠偏目标任务，也不向目标任务发送控制指令。

## 什么才算进展

只把以下内容计为进展：

- 权威检查的真实退出码变化；
- 冻结制品进入规定终态；
- 封存证据的校验和变化且无遗漏、无重复；
- 外部语义审阅形成结构化建议。

不得把工具调用数、日志长度、运行时长、文件数、候选自报 `PASS` 或主观百分比计为完成度。

## 内容寻址索引

执行：

```bash
python3 "$SKILL_ROOT/scripts/evidence_tool.py" index \
  --input /绝对路径/材料 \
  --output /绝对路径/evidence-index.json
```

索引固定按字节分片，记录输入校验和、总字节数、分片序号、起止偏移、长度和分片校验和。连续双跑必须逐字节一致。
空目录、零字节材料或零分片不是完整证据，索引命令必须失败。

读取前运行：

```bash
python3 "$SKILL_ROOT/scripts/evidence_tool.py" verify \
  --input /绝对路径/材料 \
  --index /绝对路径/evidence-index.json
```

使用 `extract` 只取相关分片。模型不得根据首尾样本推断中间已覆盖。

## 覆盖完整性

每个审计记录必须绑定一个分片标识。使用：

```bash
python3 "$SKILL_ROOT/scripts/evidence_tool.py" coverage \
  --input /绝对路径/材料 \
  --index /绝对路径/evidence-index.json \
  --selection /绝对路径/selection.json \
  --registry "$SKILL_ROOT/references/failure-modes.jsonl" \
  --records /绝对路径/audit-records.jsonl \
  --output /绝对路径/coverage-ledger.json
```

覆盖清单要求：

- 输入分片集合与已审计分片集合相等；
- 每个分片恰好出现一次；
- 分片校验和与索引一致；
- JSONL 每一物理行都能解析；
- 未命中不等于未检查；
- 零条覆盖记录不得凭空得到 `COMPLETE`。

分片缺失、分片重复、异常行、校验和对不上、空输入、空记录或无法读取，只能失败或得到 `INCOMPLETE`，不得得到 `COMPLETE`。

## 审阅检查点

审阅目标任务已有记录中的阶段终态、异常、阻断或纠偏事件。发现越权写入、语义跑偏、检查弱化或不可验证写入时，把对应证据标为发现或缺口；不得据此接管目标任务。

# 统一编排协议（v2.1，R1 重建编排结果真实性合同）

**总结：** 本协议把六种语义职责、三层对象模型、任务包、回执、成果、依赖序、集合守恒、单调传播与出错即停固化为平台无关合同。四个平台的差异只允许存在于清单、调用语法与回执提取层（`platform-adapter-mapping.json`）；业务规则、失败关闭与证据语义必须等价。

## 1. 语义角色（唯一规范 ID）

`scope-routing`、`static-audit`、`runtime-evidence`、`evaluation-integrity`、`adversarial-challenge`、`result-synthesis`。

- 历史别名只允许出现在适配层：`scope-and-routing → scope-routing`、`runtime-evidence-audit → runtime-evidence`。
- `adversarial-challenge` 不得参与实现；`result-synthesis` 只聚合合格证据，不替缺失职责补结论。

## 2. 模式与角色集合

- `static`：scope-routing、static-audit、evaluation-integrity、adversarial-challenge、result-synthesis（5）。
- `runtime`：scope-routing、runtime-evidence、evaluation-integrity、adversarial-challenge、result-synthesis（5）。
- `combined`：全 6 个。
- 每个角色在一次运行中恰好出现一次；任务包 `expected_roles` 与 `prompts` 角色集合必须精确相等（Schema 已强制）。

## 3. 三层对象模型（v2.1 新增）

### L1 原生回执（native_receipt）

平台适配器从原始事件归一化后的回执绑定。证明真实派发发生过。包含：
- `platform`：与结果 `platform` 一致；
- `task_id`：任务标识；
- `role`：语义职责；
- `kind`：平台回执类型（`claude-trace` / `codex-collaboration-receipt` / `kimi-stream-json` / `workbuddy-stream-json`），**删除 `none`**，任何 status 都不得出现 `kind=none`；
- `native_agent_type`：原生子智能体类型（大小写敏感，必须匹配 `platform-adapter-mapping.json` 映射值）；
- `invocation_id`：非空字符串，平台调用/线程标识；
- `raw_record`：`{path, sha256}`，原始记录的内容寻址绑定，`path` 必须在 `allowed_write_paths` 内，引擎回读重算；
- `completion`：`{kind: "exit_status"|"completion_event", value}`，平台进程退出状态或完成事件。

`COMPLETED`、`FAILED`、`TIMEOUT` 三种状态都必须绑定 `native_receipt`（证明派发确实发生）。

### L2 职责成果（role artifact）

独立 Schema `role-artifact.schema.json`。子智能体产出，主上下文不得伪造。包含：
- `schema_version`（"1.0"）、`task_id`、`platform`、`role`；
- `semantic_status`：枚举 `PASS_WITHIN_FROZEN_SCOPE / NEEDS_REVISION / REJECT / INCOMPLETE / BLOCKED`；
- `conclusion_ceiling`：同一枚举，表示该职责允许外层采用的最软结论上限；
- `findings`：数组，每项 `{id?, statement, evidence_refs:[{path, sha256}]}`。目标证据可使用
  `evidence-index.json/files[*].path` 的精确相对路径或其在冻结目标下解析出的绝对路径；必须
  同时匹配索引摘要与实际文件摘要。编排输出证据只接受 `allowed_write_paths` 内的绝对路径；
- `artifact_sha256`：覆盖除自身外全部规范化字段的 canonical JSON 摘要。

`COMPLETED` 结果必须绑定一个通过 Schema 与摘要验证的 artifact。

### L3 归一化结果外壳（result.schema.json v2.1）

`schema_version` = "2.1"，`required` 扩充为：`schema_version`、`task_id`、`platform`、`role`、`status`、`attempt`、`native_receipt`、`outputs`、`artifact`、`result_sha256`。

- `outputs`：数组，`COMPLETED` 时 `minItems >= 1`，items 仅 `{path, sha256}`（`additionalProperties: false`），`uniqueItems`，`path` 必须在 `allowed_write_paths` 内；
- `artifact`：`{path, sha256}` 必填绑定，`path` 在 `allowed_write_paths` 内，引擎回读重算；
- `result_sha256`：覆盖除 `result_sha256` 字段本身外的**全部**规范化内容（含 `native_receipt`、`outputs`、`artifact`）；
- `status` 枚举保持 `COMPLETED / FAILED / TIMEOUT / SCHEMA_INVALID / RECEIPT_MISMATCH`。

## 4. 依赖序 DAG（v2.1 新增）

```text
scope-routing → static-audit → evaluation-integrity → adversarial-challenge → result-synthesis
scope-routing → runtime-evidence → evaluation-integrity → adversarial-challenge → result-synthesis
```

定义在 `platform-adapter-mapping.json` 的 `roleDependencies` 字段。引擎在 `write-result` 时强制检查：

- 登记某角色前，其依赖与 `expected_roles` 的交集必须全部已存在且通过 Schema + 摘要验证；
- `result-synthesis` 因此天然最后；`adversarial-challenge` 必须在其依赖成果验证完成后；
- 早于依赖的登记必须失败关闭，错误码 `DEPENDENCY_NOT_SATISFIED`。

## 5. 任务包合同

`task-package.schema.json`（v2.0）：task_id（`AUDIT-` 前缀）、platform、mode、target、evidence_type、output_root（只许新建）、allowed_write_paths（v2.1 扩充为 `[agent-results/, work/]` 两个子目录）、source_manifest/evidence_index/registry/selection/prompt_manifest 绑定、prompts、expected_roles、acceptance_criteria、package_digest。

## 5.1 目标文件集校验和算法（tree-sha256-v1 src）

任务包 `target.tree_sha256` 与 `source-manifest.json.tree_sha256` 使用
`tree-sha256-v1`：单文件取文件字节摘要；目录递归枚举目标（排除
`__pycache__/`、`*.pyc`、符号链接），相对 POSIX 路径按 UTF-8 字节字典序
排序，拼接 `路径\x00校验和\n` 后取 SHA-256。证据索引的
`file_set_sha256` 是对其规范化 `files` 记录（含路径、大小与文件摘要）计算的
另一项绑定，必须独立回读验证，不得与 `tree_sha256` 比较相等。

## 6. 引擎行为（v2.1 升级）

### Schema 真实加载（R-AC-04）

引擎按"自包含优先"加载 `task-package.schema.json`、`result.schema.json`、`role-artifact.schema.json`：先看 `CORE_ROOT/references/`，回退 `SPEC_ROOT`。`validate-result-set` 与 `write-result` 必须对每个对象真实执行 JSON Schema 校验。

### write-result（R-AC-05）

参数：`--task-package`、`--role`、`--status`、`--attempt`、`--error`、`--receipt-file`、`--artifact-file`、`--outputs-file`/`--outputs-json`。

`COMPLETED` 时 `--receipt-file`、`--artifact-file`、非空 outputs 三者缺一即 `REJECTED`。回执/成果/输出文件必须已存在于磁盘，引擎回读重算 sha256。删除允许 `--outputs-json '[]'` 直接生成 `COMPLETED` 的旧路径。

### validate-result-set（R-AC-06）

真实 JSON Schema 校验每份结果；`expected_roles` 精确集合比较；`native_receipt.kind` ↔ `platform` 匹配；`native_agent_type` ↔ 映射表匹配；outputs/artifact/raw_record 摘要全部回读重算；`result_sha256` 重算；收集每个成果的 `semantic_status` / `conclusion_ceiling` 随结果一并输出。

### validate-execution-set

复用 `validate-result-set` 的全部结构、身份、路径、Schema、摘要、回执和集合检查，但把
`BLOCKED / INCOMPLETE / REJECT` 保留在 `semantic_failures` 中单独报告，不把它们冒充为
结构缺失。该入口只证明执行合同完整，不产生接受决定，也不允许 `finalize-run` 写成功式终态。

### finalize-run（R-AC-08，单调传播）

读取全部职责成果的语义状态，按最严格语义状态与最严格
`conclusion_ceiling` 汇总：

- 严重度序：`PASS_WITHIN_FROZEN_SCOPE < NEEDS_REVISION < {INCOMPLETE, BLOCKED, REJECT}`；
- 后三者任一出现即失败：退出非零，写 `machine-report.json`（`reason=SEMANTIC_FAILURE`、
  逐职责状态和语义摘要），**不写 `finalization.json`、不写成功式 `audit-report.md`**；
- 全部通过时：`run_verdict` = 最软允许结论（`PASS_WITHIN_FROZEN_SCOPE` 或 `NEEDS_REVISION`）。

## 7. 出错即停（失败关闭）清单

缺失、重复、额外输出、角色错配、摘要错配、Schema 失败、非零退出、超时、依赖序违反、语义状态越界、回执 kind 不匹配、native_agent_type 不匹配——出现任一即整体失败，不得降为警告或平均分。

## 8. 失败码登记表（v2.1）

| 失败码 | 触发条件 |
|---|---|
| MISSING_RECEIPT | COMPLETED 缺少 --receipt-file |
| MISSING_ARTIFACT | COMPLETED 缺少 --artifact-file |
| EMPTY_OUTPUTS | COMPLETED 时 outputs 为空 |
| OUTPUT_NOT_FOUND | outputs 引用不存在的文件 |
| WRONG_OUTPUT_DIGEST | outputs sha256 与文件实际不匹配 |
| DUPLICATE_OUTPUT_PATH | outputs 含重复路径 |
| WRONG_PLATFORM_RECEIPT | 回执 platform 与任务包不一致 |
| WRONG_RECEIPT_KIND | 回执 kind 与平台映射不匹配 |
| WRONG_NATIVE_AGENT_TYPE | 回执 native_agent_type 与映射表不一致 |
| RAW_RECORD_NOT_FOUND | 回执 raw_record.path 文件不存在 |
| RAW_RECORD_DIGEST_MISMATCH | 回执 raw_record.sha256 与文件不匹配 |
| RAW_RECORD_PATH_NOT_ALLOWED | raw_record.path 不在 allowed_write_paths |
| ARTIFACT_NOT_FOUND | artifact_file 不存在 |
| ARTIFACT_SCHEMA_INVALID | 成果不通过 Schema 校验 |
| ARTIFACT_DIGEST_MISMATCH | artifact_sha256 与重算不一致 |
| ARTIFACT_PATH_NOT_ALLOWED | 成果路径不在 allowed_write_paths |
| ARTIFACT_PLATFORM_MISMATCH | 成果 platform 与任务包不一致 |
| ARTIFACT_IDENTITY_MISMATCH | 成果身份字段不匹配 |
| DEPENDENCY_NOT_SATISFIED | 角色依赖未满足 |
| INNER_SEMANTIC_FAILURE | 职责成果语义状态为 BLOCKED/INCOMPLETE/REJECT |
| SEMANTIC_STATUS_EXCEEDS_CEILING | semantic_status 严格度超过 conclusion_ceiling |
| RESULT_SET_INCOMPLETE | validate-result-set 未全部通过 |
| SEMANTIC_FAILURE | finalize 时存在语义失败状态 |
| OUTPUT_PATH_NOT_ALLOWED | 输出文件路径不在 allowed_write_paths |
| OUTPUT_INVALID_SHAPE | 输出项不是 {path, sha256} |
| RECEIPT_SCHEMA_INVALID | 回执不通过 Schema 校验 |
| RECEIPT_ROLE_MISMATCH | 回执 role 与登记角色不一致 |
| RECEIPT_TASK_ID_MISMATCH | 回执 task_id 与任务包不一致 |

## 9. 职责隔离红线

- 主上下文不得接管缺失职责、不得手工创建或修改最终制品；
- 重新派发必须形成新尝试与新回执（attempt 递增）；
- 同一实现职责不得自审自收；
- 尝试只许新建、不许覆盖。

## 10. 适配器约束

平台适配器只做三件事：语义角色→原生语法映射、派发与等待、回执归一化。业务规则、FM 登记、Schema、状态枚举只维护一份（核心层）。平台版本升级时更新支持矩阵、能力探针与适配层，不改写核心。

# Claude Code 隔离编排

本文件规定独立 Skill 如何用 Claude Code 内置 Agent 类型加载 `prompts/` 中的
任务提示词。Claude Code 2.1.63 起的主工具名是 `Agent`；旧名称 `Task` 仍作为
兼容别名。`Plan`、`Explore` 和 `general-purpose` 是
`Agent` 的 `subagent_type`，不是需要创建的 Agent 定义。

本 Skill 的最低运行版本为 Claude Code 2.1.218，因为顶层 fork 依赖
`background: false` 等待结果。

## 固定职责

| 顺序 | 职责 | `subagent_type` | 提示词 |
|---|---|---|---|
| 1 | 范围与路由 | `Plan` | `scope-routing.md` |
| 2 | 静态审计 | `Explore` | `static-audit.md` |
| 2 | 运行期证据 | `Explore` | `runtime-evidence.md` |
| 2 | 评测完整性 | `general-purpose` | `evaluation-integrity.md` |
| 3 | 主动推翻 | `general-purpose` | `adversarial-challenge.md` |
| 4 | 结果复核 | `general-purpose` | `result-synthesis.md` |

`static` 不启动运行期证据职责，`runtime` 不启动静态职责，`combined` 两者都
启动。评测完整性、主动推翻和结果复核在三种模式中都必须执行。

## 调度硬门禁

必须先取得 `prepare-run` 输出的绝对 `task_package`、`task_package_sha256`、
`expected_roles` 和每项 `prompt_bindings`。状态不是
`READY_FOR_ISOLATED_TASKS` 时停止。

每个职责都以一次新的 `Agent` 调用启动。调用结构必须等价于：

```text
Agent({
  "description": "<职责的短描述>",
  "subagent_type": "<Plan|Explore|general-purpose>",
  "run_in_background": false,
  "prompt": "先完整读取 <提示词绝对路径> 和 <任务包绝对路径>。\
任务标识=<task_id>；提示词SHA-256=<prompt_sha256>；\
任务包SHA-256=<task_package_sha256>。严格执行提示词，只返回单个JSON对象。"
})
```

不得省略提示词路径、任务包路径或三个绑定值，不得把提示词内容靠摘要转述。
每次 Agent 调用都必须显式设置 `run_in_background: false`。缺失该字段、设置为
`true`、只取得调用标识但没有最终结果，均视为未完成并立即 `BLOCKED`。只有
Agent 调用返回最终结果、原始 trace 已保存且结果完成状态可回读后，才能登记
成果并推进依赖阶段；主流程不得在任何已派发职责仍运行时退出。
`Agent` 不可用、调用被拒绝、返回空值或返回值不是单个 JSON 对象时，立即返回
`BLOCKED`；不得由当前上下文接管该职责。

## 固定执行顺序

1. 启动新的 `Plan` 执行 `scope-routing`。职责 agent 完成工作后必须亲自
   调用引擎登记成果（引擎计算自摘要并绑定身份；禁止任何上下文手写结果
   JSON，主上下文不得代写）：

   ```bash
   python3 "$SKILL_ROOT/scripts/orchestration_engine.py" write-result \
     --task-package "<任务包绝对路径>" \
     --role "scope-routing" \
     --status COMPLETED \
     --outputs-file "<本职责成果 JSON 数组文件绝对路径>"
   ```

   引擎返回非 `WRITTEN` 即停止；失败职责以 `--status FAILED --error "<原因>"` 登记。
2. 按任务包的 `expected_roles` 启动第二阶段新上下文：静态审计和运行期证据
   使用 `Explore`，评测完整性使用 `general-purpose`。相互独立时可以在同一
   批次并行调用，但每项仍是独立 `Agent`；每个职责 agent 同样以
   `write-result` 登记成果。
3. 第二阶段全部有效后，启动新的 `general-purpose` 执行
   `adversarial-challenge`；任务输入同时给出全部初步结果的绝对路径。
4. 主动推翻结果有效后，启动另一个新的 `general-purpose` 执行
   `result-synthesis`；任务输入给出全部原始结果的绝对路径。
5. 不复用已完成 Agent 冒充新鲜复核，不允许一个职责修改另一个职责的输出。
   职责 agent 的工作文件（脚本、中间产物）只能写入 `<输出目录>/work/`；
   `agent-results/` 只存放 write-result 的结果文件，其他文件一律触发额外
   输出失败。
6. 结果文件齐全后运行：

   ```bash
   python3 "$SKILL_ROOT/scripts/orchestration_engine.py" \
     validate-result-set \
     --task-package "<任务包绝对路径>" \
     --results-dir "<输出目录>/agent-results"
   ```

   只有退出 0 且状态为 `COMPLETE` 才能进入最终结果合并。这个状态仍不代表
   外部接受。
7. 结果集通过后只运行：

   ```bash
   python3 "$SKILL_ROOT/scripts/orchestration_engine.py" \
     finalize-run \
     --task-package "<任务包绝对路径>" \
     --results-dir "<输出目录>/agent-results" \
     --output-root "<输出目录>"
   ```

   只有退出 0 且状态为 `FINALIZED` 才能返回
   `<输出目录>/audit-report.md`。不得手工生成、补写或修复证据索引、覆盖记录、
   机器结果或人读报告。

## 禁止退化

任何预期结果缺失、多余、重复、任务标识不一致、提示词校验和不一致、
`subagent_type` 错误、证据引用错误、覆盖声明不完整、非 `COMPLETED`、资源
不可读或工具退出非零，都必须停止
正常通过路径。禁止手工补写 Agent 结果、跳过脚本后直接审计、把失败写成
`NOT_HIT`，或生成一个形似报告的替代制品。

不得创建或读取 `.claude/agents/`、`~/.claude/agents/`、`.claude-plugin/`
或 Agent Team。`Plan` 和 `Explore` 不读取项目 `CLAUDE.md`，所以全部关键
约束必须来自任务包和提示词。

不同内置 Agent 上下文只证明上下文与职责分离；它们可能使用同源模型，不能
被称为外部独立审阅或最终接受方。

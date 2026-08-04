# Codex 隔离编排

本文件规定独立 Skill 如何用 Codex 原生协作工具派发职责并登记成果。
Codex 的协作原语是 `collaboration.spawn_agent` 与 `collaboration.wait_agent`；
**不得传入 Claude Agent 工具的角色类型参数**（如 Plan/Explore/general-purpose 等字面量）。

最低运行版本：codex-cli 0.145.0。

## 固定职责

| 顺序 | 职责 | 平台安全 task_name | 提示词 |
|---|---|---|---|
| 1 | 范围与路由 | `scope_routing` | `scope-routing.md` |
| 2 | 静态审计 | `static_audit` | `static-audit.md` |
| 2 | 运行期证据 | `runtime_evidence` | `runtime-evidence.md` |
| 2 | 评测完整性 | `evaluation_integrity` | `evaluation-integrity.md` |
| 3 | 主动推翻 | `adversarial_challenge` | `adversarial-challenge.md` |
| 4 | 结果复核 | `result_synthesis` | `result-synthesis.md` |

`static` 不启动运行期证据职责，`runtime` 不启动静态职责，`combined` 两者都启动。

## 原生派发语法

每个职责以 codex 原生协作语法派发：

```text
collaboration.spawn_agent({
  task_name: "<roleToTaskName 中的平台安全任务名>",
  message: "<完整提示词内容 + 任务包绑定值>",
  fork_turns: "none"
})
```

`task_name` 不是规范角色 ID，而是
`references/platform-adapter-mapping.json` 中 `roleToTaskName` 的可逆投影；职责
成果和任务包仍只写规范角色 ID。派发后反复调用
`collaboration.wait_agent({timeout_ms: 30000})`，直到收到该任务名对应的最终通知。
不得因一次等待超时、其他任务先完成或缺少新消息就提前退出主流程。
并发槽位约 4。`collaboration` 工具必须直接调用，不可嵌套 exec。

## 固定执行顺序

1. 取得 `prepare-run` 输出的 `task_package`、`expected_roles` 和 `prompt_bindings`。
   状态不是 `READY_FOR_ISOLATED_TASKS` 时停止。
2. 派发 `scope-routing`，等待完成，保存原始 rollout 到
   `<输出目录>/work/raw/scope-routing.jsonl`，构造回执，原样保存职责成果为
   `scope-routing-artifact.raw.json`，再用 `codex_artifact_normalizer.py` 只新建
   `scope-routing-artifact.json`，最后调用引擎 `write-result` 登记。
3. 按 `expected_roles` 派发第二阶段（static-audit / runtime-evidence /
   evaluation-integrity）。职责执行可以并行 spawn，但登记不得采用“谁先完成谁先
   登记”：先等待本阶段全部职责完成并保存各自 rollout、回执、原始成果，调用
   `codex_artifact_normalizer.py` 生成归一化成果，并保存 outputs；再按任务包映射
   的 `roleDependencies` 拓扑序调用引擎登记。`static` 模式必须先
   登记 `static-audit`、后登记 `evaluation-integrity`；`runtime` 模式必须先登记
   `runtime-evidence`、后登记 `evaluation-integrity`；`combined` 模式须先登记
   `static-audit` 与 `runtime-evidence`，二者都成功后才能登记
   `evaluation-integrity`。任一登记返回非 `WRITTEN` 即停止。
4. 第二阶段全部有效后，派发 `adversarial-challenge`。
5. 主动推翻有效后，派发 `result-synthesis`。
6. 运行 `validate-execution-set` 检查职责集合、文件、摘要、依赖和结构完整性。
   退出 0 且状态 `COMPLETE` 才能继续；职责给出负语义结论不属于结构缺失。
7. 结构完整后始终运行 `finalize-run`。正语义结果须退出 0、状态为
   `FINALIZED` 才交付 `audit-report.md`；`REJECT`、`BLOCKED` 或 `INCOMPLETE`
   等负语义结果须生成状态 `BLOCKED`、原因 `SEMANTIC_FAILURE` 的
   `machine-report.json`，即使命令非零退出也视为已正确完成负语义机器终态。
   缺少或不符合合同的机器报告才是编排失败。

## 坑位与红线

- **必须 `</dev/null`**：`codex exec` 否则等待 stdin 挂起。
- **隔离 CODEX_HOME**：rollout 仅落隔离目录；`~/.codex` 不得被改写。
- **websocket 瞬断**：可自愈（fallback HTTPS），以 exit=0 为准。
- 不复用已完成子智能体冒充新鲜复核；不复用主代理文本冒充子智能体回执。
- 职责 agent 的工作文件只能写入 `<输出目录>/work/`；`agent-results/` 只存放
  write-result 的结果文件。
- `artifact_sha256` 是确定性派生数据。子任务原始 JSON 必须保留；主上下文只能
  调用 `codex_artifact_normalizer.py` 生成新文件，不得手工修正或要求模型重算。

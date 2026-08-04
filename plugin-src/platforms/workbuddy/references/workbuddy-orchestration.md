# WorkBuddy/CodeBuddy 隔离编排

本文件规定独立 Skill 如何用 WorkBuddy 内嵌 codebuddy CLI 的 Agent 工具派发职责并登记成果。
WorkBuddy 命令面与 Claude Code 同构但为独立产品；`subagent_type` 值大小写敏感。

最低运行版本：WorkBuddy 应用壳 5.3.8，内嵌 codebuddy CLI 2.115.0。

## 固定职责

| 顺序 | 职责 | `subagent_type` | 提示词 |
|---|---|---|---|
| 1 | 范围与路由 | `Plan` | `scope-routing.md` |
| 2 | 静态审计 | `Explore` | `static-audit.md` |
| 2 | 运行期证据 | `Explore` | `runtime-evidence.md` |
| 2 | 评测完整性 | `general-purpose` | `evaluation-integrity.md` |
| 3 | 主动推翻 | `general-purpose` | `adversarial-challenge.md` |
| 4 | 结果复核 | `general-purpose` | `result-synthesis.md` |

`static` 不启动运行期证据职责，`runtime` 不启动静态职责，`combined` 两者都启动。

## 原生派发语法

```text
Agent({
  description: "<职责的短描述>",
  subagent_type: "<Plan|Explore|general-purpose>",
  prompt: "<完整提示词 + 任务包绑定>",
  run_in_background: false
})
```

`subagent_type` 值必须精确大小写匹配：`Plan`、`Explore`、`general-purpose`。
`run_in_background: false` 为确定性基线。

## 入口命令

```bash
HOME="<隔离HOME>" codebuddy -p "<驱动提示词>" -y
```

`-y` 非交互权限旁路（CI 自动化模式）；无 `-y` 时非交互授权提示不可用。

内嵌 CLI 路径：`/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy`
（PATH 中无命令）。

技能发现：`<隔离HOME>/.claude/skills/skill-failure-auditor/`——此为 WorkBuddy
自身 HOME 内的原生布局（WorkBuddy 兼容 Claude 风格技能发现路径），不是借用用户全局 Claude 目录。

## 固定执行顺序

1. 取得 `prepare-run` 输出的 `task_package`、`expected_roles` 和 `prompt_bindings`。
   状态不是 `READY_FOR_ISOLATED_TASKS` 时停止。
2. 派发 `scope-routing`（`Plan`），等待完成。保存 stream-json 到
   `<输出目录>/work/raw/scope-routing.jsonl`。构造回执与成果，
   调用引擎 `write-result` 登记。
3. 按 `expected_roles` 派发第二阶段（`Explore` / `general-purpose`），
   独立时可并行，每项各自保存 stream-json、构造回执与成果、调用引擎登记。
4. 第二阶段全部有效后，派发 `adversarial-challenge`（`general-purpose`）。
5. 主动推翻有效后，派发 `result-synthesis`（`general-purpose`）。
6. 运行 `validate-result-set`。退出 0 且状态 `COMPLETE` 才能继续。
7. 运行 `finalize-run`。退出 0 且状态 `FINALIZED` 才交付报告。

## 坑位与红线

- **必须 `-y`**：非交互模式下无 `-y` 则权限提示不可响应。
- **CLI 路径发现**：PATH 中无 `codebuddy` 命令；需使用应用壳内嵌完整路径或由 WorkBuddy 应用注入。
- 不复用已完成子智能体冒充新鲜复核。
- 职责 agent 的工作文件只能写入 `<输出目录>/work/`；`agent-results/` 只存放
  write-result 的结果文件。

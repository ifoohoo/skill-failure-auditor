# Kimi Code 隔离编排

本文件规定独立 Skill 如何用 Kimi Code 内置 Agent 类型派发职责并登记成果。
Kimi Code 的 `subagent_type` **小写强制**：`plan`、`explore`、`coder`。
大写 `Plan` 等形式非法（Subagent profile not found）。

最低运行版本：Kimi Code 0.31.0。

## 固定职责

| 顺序 | 职责 | `subagent_type` | 提示词 |
|---|---|---|---|
| 1 | 范围与路由 | `plan` | `scope-routing.md` |
| 2 | 静态审计 | `explore` | `static-audit.md` |
| 2 | 运行期证据 | `explore` | `runtime-evidence.md` |
| 2 | 评测完整性 | `coder` | `evaluation-integrity.md` |
| 3 | 主动推翻 | `coder` | `adversarial-challenge.md` |
| 4 | 结果复核 | `coder` | `result-synthesis.md` |

`static` 不启动运行期证据职责，`runtime` 不启动静态职责，`combined` 两者都启动。

## 原生派发语法

```text
Agent({
  description: "<职责的短描述>",
  subagent_type: "<plan|explore|coder>",
  prompt: "<完整提示词 + 任务包绑定>"
})
```

**小写强制**：`subagent_type` 值只允许 `plan`、`explore`、`coder`。
任何大写字母出现在值中即为非法。

`plan` 和 `explore` 子智能体只读（无 Bash/Write 工具）；
成果登记由有 Bash 的主 agent 统一调用引擎 write-result
（引擎仍是结果文件唯一写者）。

## 入口命令

```bash
kimi -p "<驱动提示词>" --output-format stream-json --skills-dir "<隔离技能目录>"
```

`-p` 内建自动批准；不得叠加 `--auto/--yolo`。
需要已登录；登录失效属 BLOCKED。

## 固定执行顺序

1. 取得 `prepare-run` 输出的 `task_package`、`expected_roles` 和 `prompt_bindings`。
   状态不是 `READY_FOR_ISOLATED_TASKS` 时停止。
2. 派发 `scope-routing`（`plan`），等待完成。保存 stream-json 到
   `<输出目录>/work/raw/scope-routing.jsonl`。由主 agent 构造回执与成果，
   调用引擎 `write-result` 登记。
3. 按 `expected_roles` 派发第二阶段（`explore` / `coder`），独立时可并行，
   每项各自保存 stream-json、由主 agent 构造回执与成果、调用引擎登记。
4. 第二阶段全部有效后，派发 `adversarial-challenge`（`coder`）。
5. 主动推翻有效后，派发 `result-synthesis`（`coder`）。
6. 运行 `validate-result-set`。退出 0 且状态 `COMPLETE` 才能继续。
7. 运行 `finalize-run`。退出 0 且状态 `FINALIZED` 才交付报告。

## 坑位与红线

- **prompt 必须作为 `-p` 参数**：管道 stdin 喂入会导致 `unknown command` 误报。
- **不得叠加 `--auto/--yolo`**：报错 Cannot combine --prompt with --auto。
- **plan/explore 只读**：不得允许其直接写 agent-results/；登记由主 agent 统一执行。
- 不复用已完成子智能体冒充新鲜复核。
- 职责 agent 的工作文件只能写入 `<输出目录>/work/`；`agent-results/` 只存放
  write-result 的结果文件。

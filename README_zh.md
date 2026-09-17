# skill-failure-auditor（技能失效审计）

审计 Skill、Prompt、Agent 指令、工作流及其已经产生的运行证据，识别“看似完成但真实目标未达成”的可靠性失效模式。Claude Code、Codex、Kimi Code 和 WorkBuddy/CodeBuddy 共用同一份审计核心。

SFA 是审计器，不是执行器。`static` 审阅静态定义，`runtime` 审阅已有日志和回执，`combined` 同时审阅两类材料。任何模式都不会启动目标 Skill、派发子智能体、等待或重试目标任务，也不会接入目标任务的实时控制过程。主动试验由独立评测器执行，SFA 只审阅其冻结证据。

任何执行或编排产品在 SFA 中都没有特殊地位。这类系统只能作为普通被审对象或外部消费者；SFA 不为它们提供专用接口，也不依赖它们完成开发、发布验证或运行。

## 同一入口的四种职能

`skill-failure-auditor` 按请求意图进入四个分支：

- 帮助：说明用途、输入、三种模式、副作用、使用前提和最短调用，说明后结束，不生成审计材料。
- 使用前提检查：只读检查当前 Python、`SFA_FOUNDATION_NODE` 绑定的无软链绝对实路径、pin 中的 Node.js 精确版本、必要材料和新输出目录。缺失或不匹配时说明继续条件，不安装或修改环境。
- 接入分析：说明当前项目已经满足什么、还缺什么，以及保持现状或采用有来源方案的影响，不创建目标脚手架。
- 可靠性失效诊断：通过原有适用性门禁后，进入规则选择、证据覆盖、结果校验和报告流程。

用户显式指定模式时保留该模式；材料不匹配时报告缺项，不自行换成另一模式。只有用户没有指定模式时，才按材料选择：只有静态定义时使用 `static`，只有已有运行材料时使用 `runtime`，两类材料都有时使用 `combined`。正式诊断还需要调用方指定一个尚不存在的输出目录。完整调用和交付边界见 [`plugin-src/core/references/usage-guide.md`](plugin-src/core/references/usage-guide.md)。

## 配套课程文档：LLM Academy

`docs/llm-academy/` 是与本技能配套的公开静态课程（17 个文件，按 CC BY 4.0 授权，见 NOTICE）。从 [index.html](docs/llm-academy/index.html) 进入，或按章节阅读：

| 章节 | 文件 |
|---|---|
| 全景矩阵 | [00-matrix.html](docs/llm-academy/00-matrix.html) |
| LLM 到底是怎么工作的 | [01-llm-basics.html](docs/llm-academy/01-llm-basics.html) |
| 正负面特性图谱 | [02-traits.html](docs/llm-academy/02-traits.html) |
| 提示词工程 | [03-prompt.html](docs/llm-academy/03-prompt.html) |
| 上下文工程 | [04-context.html](docs/llm-academy/04-context.html) |
| 运行框架工程 | [05-harness.html](docs/llm-academy/05-harness.html) |
| 假完成识别 | [06-fake-completion.html](docs/llm-academy/06-fake-completion.html) |
| 自评估陷阱 | [07-self-eval.html](docs/llm-academy/07-self-eval.html) |
| 验证体系防腐 | [08-verification.html](docs/llm-academy/08-verification.html) |
| 元治理 | [09-meta.html](docs/llm-academy/09-meta.html) |
| 失效模式手册（FM 锚点参考） | [10-fm-handbook.html](docs/llm-academy/10-fm-handbook.html) |
| 术语库 | [11-glossary.html](docs/llm-academy/11-glossary.html) |
| 实战装备包 | [12-recipes.html](docs/llm-academy/12-recipes.html) |
| 报文解剖 | [13-protocol.html](docs/llm-academy/13-protocol.html) |

### 本地预览

以下命令在本机启动静态文件服务器，供开发者预览课程页面：

```bash
cd packages/skill-failure-auditor/docs/llm-academy
python3 -m http.server 8000
# 打开 http://127.0.0.1:8000/
```

### GitHub Pages

公开仓 `ifoohoo/skill-failure-auditor` 创建并批准后，Pages 将由 `.github/workflows/deploy-pages.yml` 从本包 `docs/llm-academy/` 部署。当前状态：**待激活**（W13 外部批准点之前不宣称在线）。

## 平台与安装

四平台清单位于 `plugin-src/platforms/<platform>/`。支持状态以 `spec/platforms/support-matrix.json` 为唯一事实源。每个平台只保留安装清单、发现路径和客户端约束，不承载调度或目标执行能力。

平台验证从隔离安装调用 SFA，审阅同一份冻结样例。验证必须证明候选摘要、输入摘要、审计结果、人读报告和目标前后摘要；任何一项缺失，都不能宣称该平台已经验证。

## 安装

唯一的公开插件市场来源是 `ifoohoo/skill-family-hub`。通过平台的插件市场管理器添加该来源，再安装 `skill-failure-auditor@skill-family-hub`。本插件仓保留各平台的插件清单，但不再发布单独的市场索引。

- Claude Code：把 `platforms/claude-code/skill/` 复制（或软链）到技能目录（如项目 `.claude/skills/skill-failure-auditor`），然后调用 `/skill-failure-auditor <目标> <static|runtime|combined>`。
- WorkBuddy：把 `platforms/workbuddy/skill/` 复制到 `~/.workbuddy/skills/skill-failure-auditor/`。这是 WorkBuddy 应用默认的 `<CODEBUDDY_CONFIG_DIR>/skills` 发现根；不要把该投影安装到 `.claude/skills`。
- Kimi Code：使用 `kimi.plugin.json`（权威清单）；`.kimi-plugin/plugin.json` 为机械生成的 Hub 兼容投影（字段完全相等）。
- Codex / WorkBuddy（CodeBuddy）：安装对应 `platforms/<id>/` 投影；已验证运行时与诚实状态见 `spec/platforms/support-matrix.json`。

## 历史发布记录核对

治理场景如需只读核对已有发布记录，使用 release-skill 0.9.19 的 `verify-records`。调用方必须显式提供 plan、approval、target-run、每一份 source-run、期望 unit 和历史版本。该命令不搜索记录，不跟随记录里保存的路径，不联网，不运行 hooks，也不写文件。

`CONSISTENT`、`CONTRADICTED`、`INSUFFICIENT` 的退出码分别为 0、1、2；`historicalTerminalStatus` 是独立维度。记录一致只说明显式输入彼此相容，不能证明当前远端状态、安装效果或这些记录是全局最新。缺少必需记录时先向调用方索取，不能搜索或编造输入。

## 许可证

代码：Apache-2.0（见 [LICENSE](LICENSE)）。课程内容：CC BY 4.0（见 [NOTICE](NOTICE)）。

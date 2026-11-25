# skill-failure-auditor（技能失效审计）

面向大语言模型与智能体技能族的只读可靠性失效审计与跨仓报错归因工具。Claude Code、Codex、Kimi Code 与 WorkBuddy/CodeBuddy 四个宿主平台共享同一套规则核心与校验脚本。

本项目集中解决两类工程问题：

1. **识别“看似完成、实则未达标”的可靠性失效（`US-075`）**：审阅 Skill、Prompt、Agent 指令、工作流定义及其已经产生的运行日志和回执，排查把退出码 `0`、表面痕迹、同源职责自审或弱化断言当成验收通过的假完成问题。
2. **跨仓报错快速定责（`US-074`）**：当报错跨越宿主插件发现、`agent-method-registry`、`skill-family-foundation`、`artifact-graph`、`release-skill`、`skill-family-test`、外部领域技能插件（`domain-skill-plugin`）或具体业务技能时，通过只读 CLI `triage-symptom` 定位主责仓库并导出脱敏后的最小复现诊断包。

## 只读审计边界

SFA 是审计器，不是执行器。`static` 模式审阅静态定义，`runtime` 模式审阅已经产生的运行日志与回执，`combined` 模式联合审阅两类材料。任何模式都不会启动受检技能、派发子智能体、等待或重试目标任务，也不会介入目标任务的实时控制。主动运行试验由独立评测器先行完成并冻结证据，SFA 只把冻结后的材料当作普通数据审阅；任何执行或编排系统在 SFA 中都不享有专用接口或特殊豁免。

## 同一入口的四种服务分支

调用 `skill-failure-auditor` 时，入口按请求意图分流：

- **帮助说明（`HELP`）**：介绍用途、输入要求、三种审计模式、跨仓归因入口、只读边界与最短调用方式，答复后直接结束，不创建审计文件。
- **使用前提检查（`SETUP_CHECK`）**：只读核对当前 Python 3、`SFA_FOUNDATION_NODE` 绑定的无软链绝对实路径、与 `foundation-pin.json` 精确匹配的 Node.js 版本、输入材料可读性及尚不存在的新输出目录；如有缺失直接列出继续条件，不安装依赖或改动环境。
- **接入分析（`ADOPTION_ANALYSIS`）**：梳理当前工程已满足的条件、真实缺项及后续可选方案，不生成目标工程脚手架。
- **可靠性失效诊断（`AUDIT`）**：通过适用性门禁后，依次完成规则锁校验、28 条规则路由、证据分片全覆盖核验、机器结果校验与中文报告渲染。

用户显式指定的模式一律保留；若现有材料不满足该模式，直接说明缺项，不私自切换模式。仅当未指定模式时才按现有材料匹配（仅定义用 `static`、仅运行材料用 `runtime`、两类齐全用 `combined`）。详见 [`plugin-src/core/references/usage-guide.md`](plugin-src/core/references/usage-guide.md)。

## 跨仓故障归因只读诊断（`triage-symptom`）

遇到包含 `SFC*`/`SFH*`/`SFEK*`（`skill-family-foundation`）、`ACA*`/`AG_*`/`STALE_REVIEW`（`artifact-graph`）、`AMR*`（`agent-method-registry`）、`SFT*`（`skill-family-test`）、`RS_*`/`HOOK_SIDE_EFFECT`（`release-skill`）、`SFA*`（`skill-failure-auditor`）、`PYTHON_OVERRIDE_INVALID`（`domain-skill-plugin`，支持经 `SFA_CUSTOM_TRIAGE_PREFIXES` 扩展外部前缀）或宿主清单缺失/遮蔽的确定性故障时，直接运行只读归因脚本（详见 [`plugin-src/core/references/cross-repo-triage.md`](plugin-src/core/references/cross-repo-triage.md)）：

```bash
node scripts/triage-symptom.mjs \
  --symptom "prepare failed: STALE_REVIEW in traceability-version-lock.json" \
  --workspace /absolute/project/path \
  --output-bundle /absolute/path/to/new-triage-bundle
```

命令只读取运行时版本、插件清单、Git HEAD 提交号及内部依赖解析版本，默认打印结论前置的中文诊断单（加 `--json` 输出结构化 JSON），对目标工作区零写入。

## 配套课程文档：LLM Academy

`docs/llm-academy/` 是随包提供的公开静态课程（共 17 个文件，采用 CC BY 4.0 许可，详见 [NOTICE](NOTICE)）。可从首页 [index.html](docs/llm-academy/index.html) 进入，或按章节直接查阅：

| 章节主题 | 页面链接 |
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

本地预览静态课程：

```bash
cd docs/llm-academy && python3 -m http.server 8000
# 浏览器访问 http://127.0.0.1:8000/
```

公开仓库 `ifoohoo/skill-failure-auditor` 的 GitHub Pages 部署工作流定义在 `.github/workflows/deploy-pages.yml`，仅使用最小只读仓库权限上传 `docs/llm-academy/` 静态制品。

## 四平台安装说明

公开插件统一通过市场源 `ifoohoo/skill-family-hub` 分发：先在对应宿主中添加该市场来源，再安装 `skill-failure-auditor@skill-family-hub`。各宿主支持状态与最低客户端版本以 `spec/platforms/support-matrix.json` 为唯一事实源：

- **Claude Code**：将 `platforms/claude-code/skill/` 复制或软链到技能目录（如项目 `.claude/skills/skill-failure-auditor`），调用 `/skill-failure-auditor <目标路径> <static|runtime|combined>`。
- **WorkBuddy**：将 `platforms/workbuddy/skill/` 复制到 `~/.workbuddy/skills/skill-failure-auditor/`（对应 `<CODEBUDDY_CONFIG_DIR>/skills` 默认发现目录，勿放入 `.claude/skills`）。
- **Kimi Code**：以 `kimi.plugin.json` 为权威清单，`.kimi-plugin/plugin.json` 为字段完全一致的自动生成兼容投影。
- **Codex / WorkBuddy（CodeBuddy）**：安装对应的 `platforms/<id>/` 投影目录。

## 历史发布记录离线核对

如需在治理审阅中只读核对既有发布记录，使用 `release-skill` 的 `verify-records` 命令，并由调用方显式提供 `plan`、`approval`、`target-run`、全部 `source-run`、期望 `unit` 与历史版本号。该命令不搜索磁盘、不跟随记录内嵌路径、不联网、不运行钩子且不写文件；`CONSISTENT`、`CONTRADICTED`、`INSUFFICIENT` 的退出码分别为 `0`、`1`、`2`，`historicalTerminalStatus` 独立报告。记录一致仅说明所给记录内部相容，不代表远端实时状态或全局最新安装结果。

## 许可证

代码部分采用 Apache-2.0 许可（见 [LICENSE](LICENSE)）；`docs/llm-academy/` 课程内容采用 CC BY 4.0 许可（见 [NOTICE](NOTICE)）。

# skill-failure-auditor（技能失效审计）

审计 Skill、Prompt、Agent 指令、工作流及其真实运行证据中“看似完成但真实目标未达成”的可靠性失效模式。单一权威核心，四个平台投影：Claude Code、Codex、Kimi Code、WorkBuddy/CodeBuddy。

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

```bash
cd packages/skill-failure-auditor/docs/llm-academy
python3 -m http.server 8000
# 打开 http://127.0.0.1:8000/
```

### GitHub Pages

公开仓 `ifoohoo/skill-failure-auditor` 创建并批准后，Pages 将由 `.github/workflows/deploy-pages.yml` 从本包 `docs/llm-academy/` 部署。当前状态：**待激活**（W13 外部批准点之前不宣称在线）。

## 平台与安装

四平台清单位于 `plugin-src/platforms/<platform>/`；支持状态以 `spec/platforms/support-matrix.json` 为唯一事实源。任何平台在真实子智能体回执与消费者验证完成前不宣称稳定可用。

## 安装

- Claude Code：把 `platforms/claude-code/skill/` 复制（或软链）到技能目录（如项目 `.claude/skills/skill-failure-auditor`），然后调用 `/skill-failure-auditor <目标> <static|runtime|combined>`。
- WorkBuddy：把 `platforms/workbuddy/skill/` 复制到 `~/.workbuddy/skills/skill-failure-auditor/`。这是 WorkBuddy 应用默认的 `<CODEBUDDY_CONFIG_DIR>/skills` 发现根；不要把该投影安装到 `.claude/skills`。
- Kimi Code：使用 `kimi.plugin.json`（权威清单）；`.kimi-plugin/plugin.json` 为机械生成的 Hub 兼容投影（字段完全相等）。
- Codex / WorkBuddy（CodeBuddy）：安装对应 `platforms/<id>/` 投影；已验证运行时与诚实状态见 `spec/platforms/support-matrix.json`。

## 许可证

代码：Apache-2.0（见 [LICENSE](LICENSE)）。课程内容：CC BY 4.0（见 [NOTICE](NOTICE)）。

# 使用说明

`skill-failure-auditor`（SFA）专门审阅大语言模型或智能体的指令、能力定义、执行链以及已经产生的运行材料，识别假完成、自我验收、篡改验收标准、证据缺漏与职责混同等可靠性失效。普通源码审阅、技能编写、安装排错与目标功能实现不属于失效审计范围。

## 一、最短审计调用与模式规则

发起正式失效审计需要提供待审路径、审计模式以及一个尚不存在的新输出目录：

- `static`：只审阅指令、契约和能力定义，不验证运行表现。
- `runtime`：只审阅已经产生的日志、工具输出和回执，不启动目标补采证据。
- `combined`：同时审阅静态定义和已有运行材料。

调用方显式指定模式时，必须保留该模式；材料与所选模式不匹配时，直接报告缺失材料与继续条件，不私自切换成其他模式。仅当调用方未指定模式时才按现有材料自动匹配：只有一类材料就选对应模式，两类齐全就选 `combined`；材料本身不足以区分时，只追问这一项必要差异。

```text
使用 skill-failure-auditor，对 <待审路径> 做 <static|runtime|combined> 可靠性失效审计，只读目标，把产物写入 <新的输出目录>。
```

该调用会运行 SFA 自身的规则校验、分片索引和结果校验工具，并把产物写入调用方指定的新目录。它不会运行、修复或改写被审目标，不会派发子智能体，也不会接入目标任务的实时控制过程。

## 二、跨仓故障归因诊断（`triage-symptom`）

当用户遇到横跨宿主加载、`agent-method-registry` 方法解析、`Foundation` 契约/路径锁、`artifact-graph` 制品锁、`release-skill`、`skill-family-test`、外部领域技能插件（`domain-skill-plugin`）或具体业务技能的报错时，不应开启 28 条失效模式审计，而应直接运行只读归因脚本定位主责仓库（详见 [跨仓归因](cross-repo-triage.md)）：

```bash
python3 "$SKILL_ROOT/scripts/triage_symptom.py" \
  --symptom "<报错文本或错误JSON>" \
  --workspace /absolute/project/path \
  --output-bundle /absolute/path/to/new-bundle
```

该命令只读取环境版本、插件清单与依赖解析状态，输出自然中文诊断单与最小复现诊断包，对受检工作区零写入。

## 三、共同证明、精确复用和重新诊断

这三类请求语义独立，不能混为一谈：

- **读取已有共同证明**：只调用 `evaluation_tool.py professional-proof-read --proof-root <根> --proof <相对路径>`。它只解释本技能族已经写出的共同证明文件，不要求选择文件，不重查当前目标，不运行 `reuse-check`，也不创建新尝试。
- **精确复用**：只调用 `evaluation_tool.py reuse-check`。返回 `REUSE_IDENTICAL` 时沿用绑定的旧原生结果和旧报告，不生成新的共同证明，也不具备终审接受资格。
- **重新诊断**：按正式审计流程从头执行。缺陷修复由目标技能维护者负责；SFA 只在用户再次请求时审阅更新后的冻结材料。

若需从已通过原生校验的结果投影共同证明，执行：
`evaluation_tool.py professional-conclusion-create --result-root <根> --result <相对结果> --selection <相对选择> --conclusion-root <根> --conclusion-output <相对路径>`。
其中 `--selection` 是必填的原诊断选择文件（相对于 `--result-root` 严格受控读取），工具随后调用 `validate-result` 核对摘要与规则覆盖。由于原生结果只存 `selection_sha256` 而不存路径，工具绝不猜测同名文件，也不得用手填“通过”代替原生校验。公开领域码固定为 `FAILURE_DIAGNOSIS_CLEAR`、`FAILURE_DIAGNOSIS_FINDINGS`、`FAILURE_DIAGNOSIS_INCOMPLETE`、`FAILURE_DIAGNOSIS_UNAVAILABLE`；结构校验复用 Foundation 0.22.0 Bundle 内置的官方专业结论 Schema，读证返回 Python 首次解析的原始字典。

## 四、使用前提检查与接入分析

`SETUP_CHECK` 只回答当前环境能否运行 SFA，检查项包括：

- 可用的 Python 3 解释器；
- 已设置的环境变量 `SFA_FOUNDATION_NODE`（须为 Node.js 可执行文件的无软链绝对实路径），或 `PATH` 中可动态发现的合法 `node` 可执行文件；
- 该 Node.js `--version` 输出满足 `foundation/foundation-pin.json` 记录的 `nodeRange`（`>=22.22.2 <23`）；
- 与所选审计模式对应的可读输入材料，以及尚不存在的新输出目录。

若 `SFA_FOUNDATION_NODE` 含软链接、不是绝对实路径或版本超出 `nodeRange`（或未设置时 `PATH` 中找不到满足 `nodeRange` 的 Node.js），必须如实列为继续条件。`ADOPTION_ANALYSIS` 则回答当前工程已满足哪些前提、还缺什么以及保持现状或接入有来源方案的影响。两个分支在给出结论后立即结束，不安装依赖，不修改目标工程，其完成也不代表审计通过。

## 五、回复要求与证据适用边界

- 帮助、退出说明与读取已有共同证明采用简短直接回复，标明当前分支、原因与下一步（读取证明时标明 `professional-proof-read`，不把历史证明当作当前目标重验或外部接受）。
- 正式 `AUDIT` 按 [报告要求](report-contract.md) 生成完整中文报告，并在回复中提炼结论、冻结范围、核心发现、报告路径、未完成项与下一步。
- 诊断结论只对本轮冻结输入负责：静态模式未验证运行表现；运行观察仅适用于材料实际记录的模型、版本、配置、任务、时间和环境。材料未记录的事实保持未知，不能从模型 A 的材料推断模型 B，也不能把单次运行表现外推到后续运行。
- 若治理场景需要离线核对历史发布记录，使用 `release-skill` 的 `verify-records` 并由调用方显式传入 `plan`、`approval`、`target-run`、全部 `source-run`、期望 `unit` 与历史版本（`CONSISTENT`/`CONTRADICTED`/`INSUFFICIENT` 分别退出 `0`/`1`/`2`，`historicalTerminalStatus` 独立计算）；缺少输入时向调用方索取，不自行搜索或编造路径。

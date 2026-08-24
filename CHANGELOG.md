# Changelog

## 1.1.5 - 2026-08-24

- Foundation 三包精确升级到 0.9.0，并从冻结发布字节重建 Bundle 与四平台投影。
- Registry 的 28 条记录改为一次有序批量 Schema 校验；未知 Schema、畸形结果、顺序漂移或任一无效项都会停止处理。
- 审计结果复验复用同一次 selection/Registry 校验结果，不再重复启动 Foundation 机制；独立调用 coverage 时仍会自行校验。
- SFA 继续只审阅静态输入与既有运行记录，不新增执行器、常驻进程、缓存事实源或本地 Foundation 替代实现。

## 1.1.4 - 2026-08-23

- 明确 SFA 是纯审计器：`static`、`runtime`、`combined` 只表示输入范围，不启动或监督目标任务。
- 删除六角色提示词、编排引擎、任务包与角色产物 Schema、平台委派映射和外部 Loop 合同。
- 四个平台改为投影同一份审计核心，只保留安装清单、发现路径和客户端约束。
- Foundation Bundle 只校验 SFA 自有的五份审计 Schema，不再消费任何执行器交付结果。
- Foundation 依赖统一升级到已发布并验证的 0.8.3 三包，Bundle 从冻结发布字节机械重建。
- 四平台发布验证改为审阅同一冻结样例，并强制核对目标前后摘要不变。

## 1.1.3 - 未发布候选（2026-08-23）

- 将新审计编译为一个 Loop 交付任务，并用唯一 `delivery-task-result.json` 绑定全部职责成果。
- 升级到 Loop Agent 0.5.1 的公开工作流入口，分离外层宿主与内层工作流宿主绑定。
- 发布验收必须显式绑定当前候选摘要和全新的四平台黑盒证据根；旧证据不能继承为通过。
- 移除 preflight 对历史测试失败的基线豁免，任何测试失败均阻断候选生成。
- 快照分别核对公共内容清单与生成元数据清单，禁止候选清单混入公共内容。
- 真实平台运行只接受从发布树复制到隔离目录的磁盘副本，并在运行前核对副本与两份清单。

## 1.1.2 - 未发布候选（2026-08-23）

- 该候选完成冻结，但真实发布树合同校验失败；后续由 1.1.3 取代。

## 1.1.1 - 2026-08-22

- 采用 Foundation 0.8.1 的 Project Profile（项目配置档案），调用公开入口 `verifyProjectProfile()` 完成校验。
- 移除现役 `adoption-lock.json` 及其生成器，采用证明统一由 `profile.json` 提供。
- 增加 Foundation tarball 安装前校验，核对文件大小和 SHA-256 摘要后再安装。
- 重建 Claude Code、Codex、Kimi Code 与 WorkBuddy 四平台投影。
- 修复四个平台入口模板的资源闭包标记，让源码态豁免可审计、安装态引用仍指向真实资源。
- Codex 与 WorkBuddy 改为由调用方显式传入宿主配置根；缺失时停止，不再搜索用户主目录。
- 公开候选构建继续排除测试缓存，并新增源码标记与生成投影的一致性检查。

## 1.1.0 - 未发布候选（2026-08-22）

- 该候选仅在私有工作区冻结；release-skill 的资源闭包门禁发现平台入口问题后停止发布，后续由 1.1.1 取代。

## 1.0.0 - 2026-08-16

- 正式发布（1.0.0）：从 1.0.0-candidate.16 候选世代推进的单一版本额度发布。
- 全量候选世代经 1.0.0 preflight 证书周期（证书绑定候选摘要与 Release 快照）。
- Foundation 依赖升级 0.4.0（npm registry 字节物化，pin status VERIFIED）；batch E
  （publishFileExclusive/token-lock/HARNESS_ERROR_KINDS 本地等价实现）剥离并委托 Foundation 导出。
- 四平台投影（Claude Code / Codex / Kimi Code / WorkBuddy）随本版本重建。

## 1.0.0-candidate.16

- 收窄隐式触发策略，并把同一 description 与适用性门禁机械投影到四个平台。
- 修复 WorkBuddy 配置根/skills 发现路径与顶层 Agent 派发，移除 fork 死锁来源。
- 恢复结构化逐规则结果及精确集合、冻结元数据、证据和高严重度未检查门禁。

本产品的显著变更记录于此。格式参考 Keep a Changelog。

## [1.0.0-candidate] - 2026-08-01

### Added
- 单一权威核心（FM-01…FM-28 规则登记、证据/尝试/评测工具、报告合同）。
- 统一编排协议 v2.0：任务包/结果 Schema、六语义职责、static/runtime/combined 模式、引擎唯一写结果（write-result）与失败关闭。
- 四平台投影：Claude Code、Codex、Kimi Code、WorkBuddy/CodeBuddy（清单、调度映射、回执归一化）。
- 确定性构建器与统一门禁（结构/测试/构建漂移/制品链/泄漏/黑盒回执）。
- 配套静态课程 docs/llm-academy（17 文件，CC BY 4.0）。

### Status
- 候选状态 FORMAL_ACCEPTANCE_BLOCKED：三平台（Claude Code/Codex/WorkBuddy）真实黑盒通过；Kimi Code 待运行时鉴权验证；正式发布待独立验收（W12）与外部批准（W13）。

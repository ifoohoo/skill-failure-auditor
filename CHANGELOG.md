# Changelog

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

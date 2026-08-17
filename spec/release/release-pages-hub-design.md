# 发布、GitHub Pages 与 Hub 设计（W3 冻结）

**总结：** 公开仓 `ifoohoo/skill-failure-auditor` 由 Release Skill 从 `packages/skill-failure-auditor` 确定性生成；Pages 只从公开包内 `docs/` 部署；Hub 登记严格晚于公开发布与四平台远程消费验证。三个外部批准点 W13/W14/W15 逐项授权。

## 1. 发布链（W10→W13）

1. `prepare` 生成不可变生产计划（planPath/planDigest），不等于 `publish`；
2. 计划经用户审阅与 approval，再经 `confirm-production <planDigest>` 才 `publish`；
3. `PARTIAL` 用原 run 进入 reconcile，不从头重发；verify 返回 `VERIFIED` 才冻结 tag/40 位 SHA/Release；
4. tag 模板 `skill-failure-auditor-v{version}`；release 分支 `release/{tag}`；`previousPublicBaseline: {mode: none}`（首次公开，W13 前重验远程实际状态，若已存在则改 bound）。

## 2. GitHub Pages（W8/W13）

- 工作流 `.github/workflows/deploy-pages.yml` 使用官方静态制品上传/部署（actions/upload-pages-artifact + deploy-pages），不引入站点框架；
- 权限最小化：`pages: write`、`id-token: write`、`contents: read`；仅 `docs/llm-academy/` 为制品源；
- 触发限于公开仓默认分支；仓库创建前 Pages URL 只标“待激活”；
- 离线检查：内部链接、FM 锚点（`10-fm-handbook.html#fm-*`）、CSS/JS 资源、UTF-8、敏感信息扫描、本地静态服务器烟测。

## 3. Hub 登记（W14）

- 前置：W13 的不可变 tag/SHA 与四平台清单可远程读取；
- 登记字段：name、description、version、repo、category、author、四平台启用状态、manifestSources、release.version、release.tag、40 位 release.sha、tagPrefix、releaseUrl、publishedAt、verification；
- Kimi：远程检查同时验证 `kimi.plugin.json` 与 `.kimi-plugin/plugin.json` 相等；
- 新 14 位 projectVersion 快照，不覆盖旧快照；双 render 一致；`gate --check-remote`、`check-updates`、`release:check` 通过；不使用 force push；回滚用新快照与新提交。

## 4. 安装与回滚（W15）

- 版本化不可变安装目录；切换前后回读软链目标与树摘要（算法标识随附）；
- 保留旧版本；完成新→旧→新或用户批准的最小回滚往返；摘要或授权不匹配即失败关闭。

## 5. 顺序不变量

本地候选验收（W12）→ 公开发布（W13）→ Hub 登记（W14）→ 正式安装（W15）。发布前登记 Hub、缺少远程消费验证即宣称四平台可用，均为禁止路径。

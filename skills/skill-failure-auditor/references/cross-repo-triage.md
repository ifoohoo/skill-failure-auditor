# 跨仓故障归因只读诊断（`triage-symptom`）

当用户在多仓库协作的技能族环境中遇到报错时，问题往往横跨宿主插件发现、方法注册表、Foundation 底座、制品图谱、发布门禁或具体业务技能。直接进入 28 条可靠性失效模式审计既耗时也不对症；此时应优先使用 `triage_symptom.py`（或同目录 Node 入口 `triage-symptom.mjs`）执行只读归因，快速定位主责仓库并生成最小复现诊断包。

## 何时使用本工具

满足以下任一条件时直接运行 `triage-symptom`，不创建审计尝试（`ATTEMPT-*`），也不输出失效审计报告：

- 报错信息包含 `SFC*`、`SFH*`、`SFEK*`、`ACA*`、`AG_*`、`STALE_REVIEW`、`AMR*`、`SFT*`、`RS_*`、`HOOK_SIDE_EFFECT`、`SFA*`、`PYTHON_OVERRIDE_INVALID` 等生态或领域插件错误码；
- 技能在宿主中未被发现、疑似被本地旧安装目录遮蔽，或插件清单版本不一致；
- 发布或测试门禁因跨组件调用栈中断，需要先分清“底层根因仓”与“上层传播包装仓”。

## 生态错误码与归属矩阵

| 错误码前缀 / 典型标识 | 主责仓库 / 归属层 | 常见触发原因 |
|---|---|---|
| `HOST_*`、`PLATFORM_*`、清单缺失或旧安装遮蔽 | `host-configuration`（宿主配置层） | 插件清单缺失、客户端版本过低，或用户目录旧技能遮蔽当前候选 |
| `SFC*`、`SFH*`、`SFEK*`、`SPE*`、`FOUNDATION_*` | `skill-family-foundation` | 底座 Schema 拒绝、路径越界收容拦截、独占文件锁冲突或 Node 绑定不符 |
| `AMR*`、`HUMANIZE_RESOLUTION_FAILED`、`ENTRY_DISABLED` | `agent-method-registry` | 共享逻辑方法目录或项目 `agent-methods.yaml` 覆盖配置解析失败 |
| `HR*`、`HUMANIZE_*` | `humanize-rewrite` | 中文表达重写、风格规范与 `shuorenhua` / Qwen 分节门禁报错 |
| `ACA*`、`AG_*`、`STALE_REVIEW` | `artifact-graph` | 制品追溯边断链、评审状态过期或 `traceability-version-lock.json` 未刷新 |
| `RS_*`、`HOOK_SIDE_EFFECT`、快照集合漂移码 | `release-skill` | 公开文件映射漂移、快照摘要不符，或发布钩子在工作区留下越界脏文件 |
| `SFS*`、`STUDIO_*` | `skill-family-studio` | Studio 本地控制台运行态注册表、回环 API 或跨仓需求对齐巡检阻断 |
| `SFHUB*`、`HUB*` | `skill-family-hub` | Skill Family Hub 生命周期能力台账、提案核收或公开市场快照完整性阻断 |
| `SFT*` | `skill-family-test` | 跨技能族测试运行器或符合性测试套件前置条件未满足 |
| `SFA*`、`PREFLIGHT_CERTIFICATE_*`、`HIGH_SEVERITY_UNCHECKED` | `skill-failure-auditor` | 失效审计规则锁、分片覆盖、预检证书或自审/复用状态机阻断 |
| `PYTHON_OVERRIDE_INVALID`、`DOMAIN_PLUGIN_*`（或经 `SFA_CUSTOM_TRIAGE_PREFIXES` 注入的外部前缀） | `domain-skill-plugin` | 外部领域技能插件配置异常、自定义 Python 解释器覆盖路径无效或外部注册前缀报错 |
| 无生态前缀的业务脚本报错 | `business-skill` | 具体领域技能的参数或内部业务逻辑异常（信号不足时提示补充原始日志） |

当同一份日志同时出现多层错误码时，工具按因果根因顺序裁决主责仓库（宿主遮蔽 → Foundation 底座 → 方法注册表 → 制品图谱 → 发布/测试/审计门禁 → 领域技能；其中 `HOOK_SIDE_EFFECT` 作为发布钩子越界违约直接归属 `release-skill`），其余关联组件归入 `secondary_components` 并说明传播关系。

此外，当受检工作区 `package.json` 锁定的 Foundation（`< 0.39.0`）、`artifact-graph`（`< 1.2.0`）或 `agent-method-registry`（`< 0.3.0`）低于基线版本，且症状命中新宿主/清单、`skill-family-(contracts|harness-node|engineering-kit|kit)` 包名、CLI/契约校验漂移、主责归因为 `skill-family-foundation` 或双向血缘对齐信号时，报告会在 `versionUpgradeHint` 中按命中信号类型给出当前锁定版本、目标版本与对应因果说明（如新宿主适配支持或 `0.39.0` 四顶层 CLI 收敛与新版契约校验支持）。

## 命令行调用示例

在技能目录或工作区下只读执行：

```bash
# 传入报错文本并打印自然中文诊断单
python3 "$SKILL_ROOT/scripts/triage_symptom.py" \
  --symptom "prepare failed: STALE_REVIEW in traceability-version-lock.json" \
  --workspace /absolute/project/path

# 读取报错日志文件，输出机器 JSON 并导出脱敏后的最小复现诊断包
node "$SKILL_ROOT/scripts/triage-symptom.mjs" \
  --log-file /absolute/path/to/error.log \
  --workspace /absolute/project/path \
  --output-bundle /absolute/path/to/new-triage-bundle \
  --json
```

## 只读与隐私保护边界

- **工作区零副作用**：探测 Git HEAD 时强制设置 `GIT_OPTIONAL_LOCKS=0`，拒绝读取软链接日志文件，绝不在 `--workspace` 内写入临时文件或缓存。
- **自动脱敏**：输出的报告与诊断包（含 `triage-report.json`、`triage-report.md`、`environment-snapshot.json`、`symptom-excerpt.txt`）会自动将本机用户目录替换为 `<HOME>/`、外置挂载卷前缀替换为 `<VOLUME>/`、工作区前缀替换为 `<WORKSPACE>`、疑似访问令牌替换为 `<REDACTED_CREDENTIAL>`，便于跨仓交接排障。
- **不污染受检工作区且不覆盖既有证据**：`--output-bundle` 必须指向受检工作区（`--workspace`）目录树之外尚不存在的新目录或空目录；指向受检工作区内部或非空目录均会立即以退出码 `2` 报错退出。

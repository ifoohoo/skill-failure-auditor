# 结构化上下文交接

上下文阈值是风险信号，不是单独的失败条件。累计输入令牌也不等于当前活动上下文占用。

## 状态机

- `HEALTHY`：当前最小任务稳定产生可验证结果。
- `WARNING`：接近预算；减少无关读取，停止新增语义分支，准备交接。
- `HANDOFF_REQUIRED`：完成或封存当前最小任务后切换新鲜上下文。
- `SAFETY_STOPPED`：已经发生语义跑偏、越权、检查弱化或不可验证写入，立即封存。

## 交接包

交接包必须满足 `continuation-package.schema.json`，至少绑定：

- 当前目标和任务节点；
- 全部冻结输入的路径与校验和；
- 当前规则标识、修订和来源校验和；
- 已验收制品；
- 未完成的最小任务；
- 已证伪且禁止重复的路径；
- 开放问题；
- 唯一下一动作。

外部任务先冻结符合 `source-manifest.schema.json` 的来源全集；包内 `source_bindings` 必须与该清单逐项、顺序和校验和完全相等。`package_digest` 为删除该字段后，对规范 JSON（UTF-8、键排序、紧凑分隔符）计算的 SHA-256。

创建与回读都必须传入同一冻结清单，例如 `evaluation_tool.py continuation-create --template <模板> --output <包> --selection <选择文件> --registry <登记表> --source-manifest <冻结来源清单>`，验证时使用 `continuation-verify --input <包>` 和相同的后三项参数。删项、增项、重排、来源校验和变化、规则集合变化或包校验和不一致都必须失败。

新上下文只能依赖交接包和绑定的权威文件恢复。若仍需要旧会话隐含记忆，交接不合格。

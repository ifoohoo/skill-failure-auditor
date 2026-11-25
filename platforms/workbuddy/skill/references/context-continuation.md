# 结构化上下文交接

上下文接近窗口上限本身是风险预警信号，并非单独的审计失败结论；同时需区分“会话累计输入令牌总数”与“当前活跃窗口实际占用量”。

## 一、上下文水位状态机

- `HEALTHY`：当前最小任务稳定产出可验证中间结果，无需切换会话。
- `WARNING`：接近安全水位，停止展开无关文件读取与新分支，收敛手头最小任务并准备生成交接包。
- `HANDOFF_REQUIRED`：完成或封存当前最小单元后，立即导出交接包并切换到全新上下文继续。
- `SAFETY_STOPPED`：已出现指令遗忘、越界写入或检查放宽迹象，立即封存当前尝试并停止继续执行。

## 二、交接包生成与无损回读校验

跨会话交接包须严格符合 `continuation-package.schema.json`，明确记录：

- 当前审计目标与所处阶段节点（`current_node`）；
- 与冻结来源清单逐项等序的输入文件绑定（`source_bindings`）；
- 本轮已选规则的标识、修订号及规则条目摘要（`selected_rules`）；
- 已完成校验的阶段产物（`accepted_artifacts`）与剩余最小任务（`remaining_tasks`）；
- 已证伪且禁止重走的路径（`disproved_paths`）、未决问题（`open_questions`）及新会话的唯一下一步动作（`next_action`）。

生成交接包前，先冻结符合 `source-manifest.schema.json` 的来源全集清单；随后调用 `evaluation_tool.py` 创建与核验：

```bash
python3 "$SKILL_ROOT/scripts/evaluation_tool.py" continuation-create \
  --template /absolute/path/template.json \
  --output /absolute/path/continuation-package.json \
  --selection /absolute/path/selection.json \
  --registry "$SKILL_ROOT/references/failure-modes.jsonl" \
  --source-manifest /absolute/path/source-manifest.json

python3 "$SKILL_ROOT/scripts/evaluation_tool.py" continuation-verify \
  --input /absolute/path/continuation-package.json \
  --selection /absolute/path/selection.json \
  --registry "$SKILL_ROOT/references/failure-modes.jsonl" \
  --source-manifest /absolute/path/source-manifest.json
```

任何删减、增补、顺序变动、文件摘要变化或规则集合不一致都会在回读校验时直接报错。接续工作的新会话必须只凭交接包及其绑定的冻结文件即可恢复完整语境；凡仍依赖旧会话未落盘记忆的交接一律判定不合格。

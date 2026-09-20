# 领域说明

限售股减持受到证券批次来源、解禁条件、股东关系、敏感窗口与披露规则约束。关联账户可能需要合并计算数量，关系变更和监管规则也各有生效时间。计划、成交回报与公告流程应保留版本和累计依据，便于证券事务人员复核每次放行决定。

`reference/domain.json` 保存可公开的示例枚举与精度约定。正式业务记录应使用稳定标识，并区分业务发生时间、系统接收时间和记录版本。

## 核心语义

### 合并额度与关系时效

股东关系（`controller` / `family` / `employee_platform`）构成合并组（连通分量），按业务时间解析：仅在 `[effective_from, effective_to)` 内生效。成交回报记录时冻结当时归属（`attribution` = 成交时刻合并组成员名单）。

账户 A 在窗口内的合并消耗 = 所有 `attribution` 含 A 的未撤回成交数量之和。因此：

- 关系**生效前**的成交不并入新组（归属不含其他成员）；
- 关系**终止后**的成交不再并入；
- 关系存续期间的成交**永久保留当时归属**，即使关系后来终止，仍计入当时各成员的额度。

### 最严格原则

可减数量逐层计算并取最小值：

1. `plan_remaining`：计划数量 − 该计划已成交；
2. `lot_availability`：关联批次中已解禁批次的剩余量合计（解禁日期到达且声明的解禁条件全部满足）；
3. `rule_cap`：每条 `rolling_window_cap` 规则在滚动窗口内的剩余额度（合并消耗按上节口径）。

成交时间落入任何限制窗口（人工登记窗口或敏感事件窗口）时，该日整体禁止放行；存在逾期未确认公告或计划非 `active` 状态时同样拒绝。

### 送审保护

计划状态机：`draft → review → active → suspended → closed`（`suspended` 可恢复为 `active`）。`review` 状态的计划拒绝一切普通编辑（须先核准或待审结）；`draft`/`active` 状态的编辑要求 `expected_revision` 与当前版本一致（乐观并发），每次成功编辑版本号递增。

### 可重放的累计额度

所有状态变化写入追加式事件日志（`.runtime/events.jsonl`），重启后按序重放恢复。成交事件按 `(trade_time, seq)` 排序参与计算，与到达顺序无关，因此乱序回报得到一致的累计结果。`report_id` 是幂等键：同一回报重复提交不重复扣减；撤回是补偿事件，重复撤回为幂等空操作。派生数据（消耗、批次余量、计划已成交）不落地，始终由事件重放得出。

### 披露阈值与公告流程

`disclosure_threshold` 规则按**计划累计成交**核算：每跨越一次阈值（`累计 // 阈值` 的序号）生成一条公告，时限为 `deadline_days` 个自然日。扫描（每次决策与查询时惰性触发，也可显式调用）将超期未确认的公告标记为 `overdue` 并自动暂停对应计划；确认公告后可申请恢复（`resume`），恢复时若仍有逾期未确认公告则拒绝。

### 监管规则版本

规则按 `(rule_id, version, effective_from)` 登记，决策取业务时点已生效的最新版本。规则类型：

- `rolling_window_cap`：`{window_days, max_quantity? , max_pct_of_total_shares?}`，两者同时给出时取更严；比例换算需要 `POST /company` 登记总股本，未配置时比例上限按 0 处理（从严）。
- `sensitive_window`：`{event_types, applies_to_roles, days_before, days_after}`，与敏感事件（如 `earnings_forecast` 业绩预告公告日）共同生成 `[公告日 − days_before, 公告日 + days_after]` 的角色限制窗口。
- `disclosure_threshold`：`{threshold_quantity? , threshold_pct_of_total_shares?, deadline_days}`。

### 可解释结论

计划提交、变更、核准、执行、撤回、暂停、恢复、终止均产生决策记录，包含：放行结论与理由、逐层可减数量（标注约束层）、冲突窗口、关联账户贡献、待披露事项、以及每条规则的版本、参数与核算结果（规则证据）。`GET /plans/{id}` 汇总任一计划的全部上述信息。

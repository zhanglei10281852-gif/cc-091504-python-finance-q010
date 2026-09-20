# 限售股减持合规服务

面向限售证券、关联持有人和交易窗口管理的 Python 后端服务。证券事务负责人在提交、变更、
执行与终止股东减持计划时，系统给出**可解释的放行结论**：逐层可减数量、冲突窗口、
关联账户贡献、待披露事项与每次决定采用的规则证据。

服务仅依赖 Python 标准库，以**追加事件日志**为唯一事实来源，读取模型由事件重放得到。

## 核心语义

- **合并计算**：实际控制人、员工持股平台、亲属等关联账户按有生效区间的关系合并额度；
  关系变化只影响生效后的合并范围，历史成交保留当时归属。
- **最严格结果**：计划额度、已解禁持仓、实控人连续 90 日集中竞价 1%/大宗 2%、
  董监高自然年 25% 等多层同时限制时，取最小余量。
- **送审锁**：`draft → review → active → suspended/closed`；送审中的计划拒绝普通编辑，
  变更须走变更送审，核准时挂起版本才提升为现行版本。
- **成交可重放**：成交回报可乱序到达、撤回或替换；按业务发生日重排累计，同一
  `report_id` 不重复扣减。
- **披露闸门**：预披露与阶梯减持（每 1% 总股本）自动创建有时限的公告流程，
  逾期未确认则暂停后续放行，确认后恢复。
- **规则版本**：监管规则按生效日选取，历史计算保留当时版本。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

默认监听 `8000`，事件日志写入 `.runtime/events.jsonl`（可用 `COMPLIANCE_STORE` 覆盖）。
执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以 `docker compose up --build`。

## API

所有请求/响应均为 JSON（`Content-Type: application/json`）。

### 基础数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/shareholders` | 登记股东（可标记实际控制人/董事） |
| POST | `/relationships` | 声明关联关系（含 `effective_from`/`effective_to`） |
| POST | `/relationships/end` | 关系到期（只影响之后的合并范围） |
| POST | `/batches` | 登记证券批次（限售来源、总量、发行人锁定期） |
| POST | `/unlocks` | 登记解禁条件（到期日/业绩考核、批次数量、是否满足） |
| POST | `/windows` | 登记窗口期（如业绩预告敏感期，`blocking`） |
| POST | `/rules` | 发布监管/内部规则版本（按生效日选取） |

### 计划生命周期

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans` | 创建草稿计划 |
| POST | `/plans/{id}/edit` | 草稿普通编辑（送审中返回 409） |
| POST | `/plans/{id}/submit` | 提交送审 |
| POST | `/plans/{id}/approve` | 核准生效（达预披露阈值自动建公告） |
| POST | `/plans/{id}/reject` `/withdraw` | 驳回 / 撤回到草稿 |
| POST | `/plans/{id}/changes` | active 计划提出变更送审（现行版本继续执行） |
| POST | `/plans/{id}/changes/approve` `/reject` | 变更核准 / 驳回 |
| POST | `/plans/{id}/suspend` `/resume` `/terminate` | 暂停 / 恢复 / 终止 |

### 执行与披露

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans/{id}/trades` | 成交回报（乱序安全、重复拒绝、先过放行闸门） |
| POST | `/trades/withdraw` | 撤回回报（额度回补） |
| POST | `/trades/replace` | 更正回报（原回报失效，替换回报参与重放） |
| POST | `/plans/{id}/evaluate` | 不落库试算 |
| POST | `/plans/{id}/announcements/{aid}/confirm` | 公告确认（解除逾期阻断） |
| GET | `/plans` `/plans/{id}?as_of=YYYY-MM-DD` | 计划列表 / 可解释详情 |

### 计划详情包含的可解释内容

`GET /plans/{id}` 返回：

- `available_qty.layers[]`：每个数量层的 `limit/used/remaining`、是否适用、
  命中的规则版本与条款、参与计算的批次/成交/关系；`binding_layer` 为最严层。
- `conflict_windows`：与计划区间重叠或覆盖判定日的窗口。
- `contributions`：各关联账户（含关系终止后的历史成员）的成交贡献。
- `pending_disclosures` / `gating_blockers`：待披露事项与逾期阻断。
- `trade_replay`：成交重放序列，逐笔标注是否计入及排除原因
  （撤回/替换、成交当日不在合并范围）。
- `decisions`：提交、核准、成交申报/拒绝等每次决定的规则证据快照。

## 示例

```bash
curl -X POST localhost:8000/plans/P1/trades -d '{
  "report_id": "T-20260510-01",
  "shareholder_id": "C-001",
  "batch_id": "B-IPO-01",
  "channel": "secondary",
  "trade_date": "2026-05-10",
  "qty": 800000
}'
```

拒绝时响应体给出全部命中原因，例如：

```json
{
  "result": "denied",
  "reasons": ["当日处于禁止交易窗口：半年度业绩预告敏感期（2026-07-10~2026-07-20）"],
  "binding_layer": "controller_secondary",
  "allowed_qty": 200000
}
```

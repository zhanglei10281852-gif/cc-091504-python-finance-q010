# 限售股减持合规服务

面向限售证券、关联持有人和交易窗口管理的 Python 后端服务。系统维护股东关系、证券批次、限售来源、解禁条件、已披露计划、窗口期、成交回报与监管规则版本，并在计划提交、变更、执行与终止时给出可解释的放行结论。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，访问 `GET /health` 可确认进程状态。执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。

## 持久化

所有状态变化以追加式事件写入 `.runtime/events.jsonl`（目录可用 `RUNTIME_DIR` 环境变量覆盖），重启后重放恢复；累计额度等派生数据始终由事件重放计算，保证乱序回报可重放、同一回报不重复扣减。

## 接口概览

所有接口收发 JSON，时间使用 ISO 8601；决策类接口接受 `as_of`（查询参数或请求体）指定业务时点，缺省为系统当前时间。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/enums` | 公开枚举（限售来源、关系类型、计划状态） |
| POST | `/company` | 登记总股本（比例规则换算依据） |
| POST | `/accounts` · GET `/accounts` | 股东账户（类型、角色） |
| POST | `/relationships` · POST `/relationships/{id}/end` | 股东关系及生效区间 |
| POST | `/lots` · POST `/lots/{id}/satisfy` | 证券批次（限售来源、解禁日期与条件） |
| POST | `/rules` · GET `/rules` | 监管规则版本（滚动上限、敏感窗口、披露阈值） |
| POST | `/sensitive-events` | 敏感事件（如业绩预告公告日、适用角色） |
| POST | `/windows` | 人工登记限制窗口 |
| POST | `/plans` · GET `/plans` | 创建减持计划（draft） |
| POST | `/plans/{id}/submit` `/approve` `/edit` `/suspend` `/resume` `/close` | 计划生命周期；`review` 状态拒绝普通编辑，编辑需 `expected_revision` |
| POST | `/plans/{id}/check` | 提交前试算：不记账，返回可解释放行结论 |
| GET | `/plans/{id}` | 负责人视图：逐层可减数量、冲突窗口、关联账户贡献、待披露事项、决策与规则证据 |
| GET | `/plans/{id}/decisions` | 计划的全部决策记录 |
| POST | `/executions` · POST `/executions/{report_id}/revoke` | 成交回报（`report_id` 幂等）与撤回 |
| GET | `/announcements` · POST `/announcements/{id}/confirm` | 披露公告列表与确认 |
| POST | `/admin/sweep` | 显式逾期扫描（平时随决策惰性触发） |

领域语义（合并口径、最严格原则、送审保护、重放与公告流程）见 `docs/domain.md`。

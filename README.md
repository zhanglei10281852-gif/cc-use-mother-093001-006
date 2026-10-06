# 分区水量平衡与漏损核算平台

面向供水集团的**独立、可追溯**分区（DMA）水量平衡与漏损核算系统。
解决"迟到抄表、换表归零、估算修正反复覆盖月报、无法回答当时为何检修"
的问题：每一份月报都是不可变的版本快照，真实读数迟到只产生**调整单**，
绝不改写旧账。

## 设计原则

| 需求 | 实现 |
| --- | --- |
| 有效期管理 | 总表/用户表、调水、消防、合法未计量、估算规则均带 `valid_from/valid_to`，按结算窗口裁剪 |
| 迟到抄表 / 估算修正 | 估算读数照常出账；真实读数到达后登记新读数并生成**调整单（delta）**，旧版本指纹不变 |
| 换表归零 | `MeterReplacement` 提供旧表末次底数与新表起算底数锚点，分段按天分摊，自然衔接 |
| 跨分区调水成对 | 一条 `Transfer` 是同一笔业务事实，调入/调出两侧分量同时入账，`verify` 全局核对 |
| 重复导入幂等 | 所有事实按自然键 `key` 去重；同键内容一致忽略，冲突报 `IdempotencyConflict`，不静默覆盖 |
| 统一结算窗口 | 月度窗口 `[月初, 下月初)`，月末收盘读数视同窗口结算点 |
| 可追溯版本 | 每版本含全部分量、逐项**输入摘要**、内容指纹（sha256 截断）与状态流转历史 |
| 状态/权限约束 | `draft → under_review → issued`；签发冻结；仅 `admin` 可重开且必须填原因，重开派生新 `revision` |
| 历史重算与核对 | CLI 与 HTTP API 均可重算任意历史月份、核对总量恒等式、追踪每项修正对漏损指标的影响 |

漏损口径：

```
净输入   = 总表供水 + 调入 − 调出
合法用水 = 用户用水 + 消防用水 + 其它合法未计量
漏损水量 = 净输入 − 合法用水
漏损率   = 漏损水量 / 净输入
```

## 代码结构

```
src/water_balance/
  contracts.py   结算窗口、分录、分量定义与漏损方向系数
  models.py      表计/读数/换表/调水/事件/估算规则/调整单（均带有效期与自然键）
  store.py       幂等导入、有效期查询、JSON 持久化
  engine.py      区间按天分摊、换表衔接、缺数估算、调整单汇总、指纹、恒等式/调水核对
  snapshots.py   版本快照、状态机、角色权限、流转历史
  ledger.py      修订号管理、签发冻结、重开、迟到读数->调整单、影响追踪
  cli.py         命令行
  api.py         标准库 HTTP API（无第三方依赖）
```

## 命令行

```bash
# 端到端演示（换表、估算、迟到实抄、签发、重开、追踪）
python3 run_cli.py --db db.json --versions ver.json demo

python3 run_cli.py import data.json
python3 run_cli.py recompute DMA-7 2026-09 --actor 张工
python3 run_cli.py submit   DMA-7 2026-09 --actor 张工 --note 初稿
python3 run_cli.py issue    DMA-7 2026-09 --actor 李复核 --role reviewer
python3 run_cli.py late-reading DMA-7 2026-09 --key C1:real --meter C1 \
    --date 2026-10-08 --value 9060 --actor 张工 --reason 实抄到达
python3 run_cli.py reopen   DMA-7 2026-09 --actor 赵管理 --role admin --reason 实抄修正
python3 run_cli.py show     DMA-7 2026-09 --revision 1
python3 run_cli.py verify   2026-09          # 总量恒等式 + 调水成对
python3 run_cli.py trace    DMA-7 2026-09    # 逐项修正对漏损率的影响
python3 run_cli.py serve --port 8080
```

## HTTP API

```
POST /import
POST /zones/{zone}/months/{YYYY-MM}/recompute
GET  /zones/{zone}/months/{YYYY-MM}/versions[/{revision}]
POST /zones/{zone}/months/{YYYY-MM}/transition   # actor + role + to_status
POST /zones/{zone}/months/{YYYY-MM}/reopen       # 仅 admin，需 reason
POST /zones/{zone}/months/{YYYY-MM}/late-reading
GET  /zones/{zone}/months/{YYYY-MM}/trace
GET  /months/{YYYY-MM}/verify
```

## 测试

```bash
python3 -m unittest discover -s tests -v   # 19 个用例
python3 -m compileall -q src tests run_cli.py
```

## 追溯语义示例

r1 按估算用户用水 9440 签发（漏损 3410，率 26.23%）；真实读数 760 取代
估算 800，生成调整单 `customer_consumption −40`。管理员重开后得到 r2：
旧账 r1 指纹与漏损率不变，r2 漏损 3450、率 +0.31pp，`trace` 明确标注
该调整单对漏损的方向性影响为 **+40 m³**（用户用水下调，漏损上升）。

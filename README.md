# 分区水量平衡与漏损核算平台

面向供水集团的独立分区（DMA）水量平衡台账。目标是让**每一个月度漏损
数字都能回答「当时依据什么算出来、后来为何改变」**：

- 总表、用户表、消防用水、合法未计量用水、估算规则全部带**有效期**；
- 按统一月度结算窗口生成**不可变的平衡版本**，重算产生新修订号而非覆盖；
- 迟到抄表/换表归零/估算修正到达后生成**调整单**，旧账只追加不改写；
- 跨分区调水**成对入账**，批量导入按内容哈希**幂等**；
- 版本在 复核 → 签发 → 重开 之间有角色权限与状态约束；
- 签发版本保留**计算输入摘要（SHA-256）**与**逐项差异原因**；
- CLI 与 HTTP API 均可重算任意历史月份、核对总量恒等式并追踪每项
  修正对漏损指标（NRW 水量与漏损率）的影响。

## 水量平衡恒等式

```
总表供入 + 跨分区调入 = 用户表实计 + 规则估算 + 消防用水
                      + 合法未计量用水 + 跨分区调出 + 漏损(NRW)
```

漏损率 = NRW ÷（总表供入 + 调入）× 100%。每次签发前强制核对恒等式
残差为 0；`verify` 另做跨分区调入/调出全局守恒核对。

## 项目结构

```
src/water_balance/
  contracts.py   枚举、异常、窗口/分录/结果结构（保留原有基础契约）
  engine.py      纯函数核算引擎：读数区间按天均摊、换表归零、缺数估算、摘要
  storage.py     JSON 仓储：原子写入 + 文件锁 + 批次内容哈希
  service.py     应用服务：注册、导入、调水、状态机、调整单与差异追踪
  api.py         标准库 HTTP API（X-User-Id 鉴权）
  cli.py         命令行
tests/           引擎/服务/API 共 43 项 unittest
```

## 运行

```bash
# 测试 / 编译 / 冒烟
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
python3 run_cli.py

# 一键演示（两个分区、总表、用户、消防、估算规则、调水）
python3 -m water_balance.cli --db demo.json bootstrap --as init
python3 -m water_balance.cli --db demo.json preview DMA-7 2026-09
python3 -m water_balance.cli --db demo.json recompute DMA-7 2026-09 --as op1
python3 -m water_balance.cli --db demo.json review  VB-000001 --as rev1
python3 -m water_balance.cli --db demo.json issue   VB-000001 --as iss1
python3 -m water_balance.cli --db demo.json verify 2026-09

# HTTP API
python3 -m water_balance.cli --db demo.json serve --port 8080
```

## 关键业务规则

### 有效期与换表归零
- 供水点、表计均为 `[valid_from, valid_to)` 半开区间，同一供水点表计
  有效期不允许重叠；换表用 `replace-meter`：旧表当日停用、新表同角色启用。
- 读数表征「自上一读数以来」用量，跨窗口/有效期边界按天均摊；
  新读数小于旧读数视为归零，窗口内新装表的首读以安装日起度 0 为前驱。

### 缺数估算
- 只有 `active` 且经 issuer/admin `approve` 的规则在其有效期内可用。
- 按窗口内该供水点**实际日均用量 × 系数 × 缺口天数**估算；实际覆盖率
  低于规则下限（如 50%）或总表缺数时拒绝核算并报缺数明细，不静默编造。

### 只读历史账与调整单
- 读数同键同值提交幂等，同键异值直接拒绝（真实修正走调整流程）。
- 已签发版本不可重算；须 admin `reopen`（强制填原因）后才能生成新修订。
- 新修订对每个变化的平衡行项目生成调整单，记录 `before/after/delta`
  与 `nrw_impact_m3`；签发时逐项确认差异原因（可 JSON 文件覆盖自动原因）。
- 重开后输入摘要未变化则拒绝生成空修订；旧签发版本在新版本签发时
  转为 `superseded` 永久留痕。

### 调水与导入
- 调水单在同一事务写入调出/调入两条腿，自调与非正水量被拒绝。
- 导入按 `source_ref + 排序后记录` 的 SHA-256 去重：重发（即使行序不同）
  返回 `duplicate`；与既有行完全一致计为跳过；任何非法记录整批拒绝。

### 权限
operator 录入/重算，reviewer 复核，issuer 签发（含估算规则审批），
admin 重开与用户管理；admin 可执行所有操作。

## 常用命令一览

```bash
# 主数据
... user|zone|sp|sp-close|meter|replace-meter|reading|usage|rule|approve-rule|transfer
# 批量导入（records 为 JSON 数组，type ∈ reading|usage|transfer）
... import BILL-2026-09 records.json --as op1
# 版本与核对
... preview ZONE MONTH
... recompute ZONE MONTH --as op1 [--note ...]
... review|issue|reopen VERSION ...
... versions [--zone Z] [--month YYYY-MM]
... version VERSION          # 完整快照：输入摘要/差异/调整单/原因
... adjustments [--zone Z] [--month M] [--status open|applied]
... verify MONTH [--zones A,B]
```

## HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/users` `/zones` `/service-points` `/meters` `/readings` `/usages` `/rules` `/transfers` `/imports` | 录入 |
| POST | `/service-points/close` `/meters/replace` `/rules/{id}/approve` | 有效期/换表/审批 |
| POST | `/recompute` `/versions/{id}/review` `/versions/{id}/issue` `/versions/{id}/reopen` | 状态机 |
| GET | `/zones/{id}/balance?month=YYYY-MM` | 只读试算 |
| GET | `/versions` `/versions/{id}` `/adjustments` | 台账查询 |
| GET | `/verify?month=YYYY-MM[&zones=A,B]` | 恒等式核对 |

写请求携带 `X-User-Id` 头标识操作人；错误返回统一的
`{error, code, message, details}`，状态码 403/404/409/422 语义化区分。

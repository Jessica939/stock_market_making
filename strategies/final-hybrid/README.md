# final-hybrid

`final_hybrid_v1`：A 使用 baseline-refine 的做市报价，B 使用 stale_quote_sniping 的
周期 fair value 与主动 IOC。**只有 A 做市，B 不挂做市单。**

## 默认行为

| 项目 | 行为 |
| --- | --- |
| A 做市 | 扣除自身挂单后改善外部最优价；新增报价最多 100 股，减仓侧覆盖剩余仓位；窄价差避免双边相交 |
| B 定价 | `FV_B = A_mid - predicted_basis`；固定 180 秒周期，启动使用 sniper 的绝对时间相位先验，180 秒会话历史后因果拟合 |
| B 入场 | 至少 3 ticks edge；基础 2 股，edge 每增加 2 ticks 加 10 股，最大 50 股；整笔 VWAP 必须支持该档位 |
| B 执行 | 等待至少 50ms，再刷新盘口并用冻结模型复核；入场与退出均 IOC，无 B 做市补仓通道 |
| B 退出 | 可执行 VWAP 达到入场复核时的 FV 后，用决策后的新盘口再确认；目标消失则取消止盈；5 秒超时或风险停止独立退出 |
| 轮询 | 目标每 50ms 一轮（约 20Hz），每轮处理 B 再更新 A；按本轮已耗时补足睡眠 |
| 学习与录制 | 每秒最多一个新 A/B 样本，重复盘口不增加样本；每 5 秒拟合；行情录制每 0.5 秒 |
| 额度 | A、B 各自实际仓位 ±100，计入同向挂单；每品种挂单总量 200；同一个发送器共享 200 次/秒更新预算 |

复核时如果 fair value 已变化，入场使用复核后的新 FV 作为退出目标。
例如原做空决策 FV 为 113.8，而发送前可卖价已跌到 112.2，旧模型不能给出足够正 edge 时
直接放弃，不会挂出 B 做市单再沿用旧目标退出。

启动允许 A/B 已有仓位，但 B 不能有遗留挂单。A 纳入做市管理；B 记为启动基准，
只管理本次 IOC 产生的增量，不替用户清空原来的 B。B 账户仓位与“基准＋已确认成交”
不符时停止，运行期间不能让其他程序或人工同时交易 B。

退出时间和风险阈值沿用 sniper：B 策略损失 1250 或高点回撤 1500 时停止新增交易；
这些指标仅属于本次 B 增量，不包含 A 做市收益。A/B 报价超过 1 秒陈旧或不同步时停止 B 入场。
宽 A 盘口只保留减仓报价，失效 A 盘口撤掉报价。

## 运行

在当前工作区父目录执行；默认只检查，不连接交易所：

```sh
python3 -B stock_market_making/strategies/final-hybrid/run.py --check
```

在装有 Optibook SDK 的部署环境中启动：

```sh
python3 stock_market_making/strategies/final-hybrid/run.py --live
```

支持 `--config`、`--duration`、`--log-dir`、`--state-file`。
默认交易 1800 秒，最后 30 秒停止入场、撤 A 报价并退出 B 策略仓位。
Ctrl+C 也先撤 A，再最多用 5 秒处理已确认的 B 残量；A 残仓保留。
日志写入 `data/runs/final-hybrid`，行情写入共享 `data/market`，状态默认保存到
`state/default/final-hybrid.json`。部署时同步整个 `stock_market_making`，包括新共享文件
`order_sides.py`、`quote_helpers.py`、`recording` 和现有 `strategies/common`、`strategies/hybrid/state.py`。

私人成交由一个 `FillStream` 消费，标准化 SDK side 枚举、去重、核对成交方向/限价/数量，
同时提供执行确认和 `fill` 日志。`maker_quote`、`stale_signal`、`stale_entry_pending`、
`stale_entry_confirmed`、`stale_exit_cancelled`、`ioc_confirmed` 和 `session_end` 记录完整决策链。

沿用 sniper 的保守成交确认：live API 没有最终 IOC 部分成交数量证明，只有全量私人成交
与账户变化相符时确认完成。部分或零成交在 0.6 秒内无法证明最终状态时，停止 A/B 后续新增单，
取消挂单并留下不可自动重启状态；空轮询或看不到挂单不等于“确定未成交”。
模拟测试才允许注入最终成交量证明。同步 RPC、限速和成交确认会让实际轮询间隔超过 50ms。

## 本轮修复与验证

- baseline-refine / baseline-refine-hybird / final-hybrid 的订单读取统一把 Cap'n Proto
  `_DynamicEnum` 转成 `bid`/`ask` 字符串，消除 `'reduce_' + side` 异常。
- 两份旧 Notebook 的执行异常继续撤单，但保留模型并记录 traceback；真正的行情中断仍由
  模型自身重置。修复 baseline-refine 启动打印的引号错误，`--check` 增加全部策略单元语法检查。
- 原 hybird 的周期方向增仓必须由周期层批准；不再绕过冻结目标价、建仓窗口和信号约束。
- 21 项 final-hybrid 测试覆盖枚举挂单、A 报价等价、只 A 做市、B IOC 复核/止盈/超时、
  部分成交、共享限速、仓位保护、单一成交流、持续学习、重复盘口和真实 runner 的模拟生命周期。

2026-09-11 验证：final-hybrid 21 项、baseline-refine 57 项、baseline-refine-hybird 82 项
策略测试通过；共享 quote protection 8 项测试通过；三个策略的 `--check` 均通过。

```sh
python3 -B -m unittest discover -s stock_market_making/strategies/final-hybrid/tests -p 'test_*.py' -q
python3 -B -m unittest discover -s stock_market_making/strategies/baseline-refine/tests -p 'test_*.py' -q
python3 -B -m unittest discover -s stock_market_making/strategies/baseline-refine-hybird/tests -p 'test_*.py' -q
```

本轮验证均离线，没有连接交易所，也没有验证收益。180 秒和 epoch 相位沿用 sniper 的配置，
不是从那段反复重置、到期预测样本为零的日志重新证明的结论。sniper 的拟合 R² 是样本内指标，
也不等同于 baseline-refine 的 45 秒到期预测检验。

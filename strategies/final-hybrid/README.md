# final-hybrid

`final_hybrid_v1_1`：A 使用 baseline-refine 的做市报价，B 使用 stale_quote_sniping 的
周期 fair value 与主动 IOC。**只有 A 做市，B 不挂做市单。**

## 默认行为

| 项目 | 行为 |
| --- | --- |
| A 做市 | 扣除自身挂单后改善外部最优价；新增报价最多 100 股，减仓侧覆盖剩余仓位；窄价差避免双边相交 |
| B 定价 | `FV_B = A_mid - predicted_basis`；固定 180 秒周期，启动使用 sniper 的绝对时间相位先验，180 秒会话历史后因果拟合 |
| B 入场 | 至少 3 ticks edge；基础 2 股，edge 每增加 2 ticks 加 10 股，最大 50 股；整笔 VWAP 必须支持该档位 |
| B 执行 | 等待至少 50ms，再刷新盘口并用冻结模型复核；入场与退出均 IOC，无 B 做市补仓通道 |
| B 退出 | 可执行 VWAP 达到入场复核时的 FV 后，用决策后的新盘口再确认；目标消失则取消止盈；5 秒超时或风险停止独立退出 |
| 轮询 | B 目标每 50ms 一轮；A 报价核对默认每 250ms；实际间隔受共享请求预算和同步 RPC 耗时限制 |
| 学习与录制 | 每秒最多一个新 A/B 样本，重复盘口不增加样本；每 5 秒拟合；行情录制每 0.5 秒 |
| 额度 | A、B 各自实际仓位 ±100，计入同向挂单；每品种挂单总量 200；数据查询、成交轮询、报单、撤单共享 200 次/秒上限；实际采用 190 次/1.05 秒，保留余量 |

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

## 限速修复与断线恢复（v1.1）

原 v1 只限制订单更新，没有把账户/挂单查询等调用纳入预算。50ms 主循环里，A 的报价核对
会产生多次读取。本地 API Reference 没有说明部署端每个 getter 是远程请求还是本地缓存，
因此本版本在最内层 SDK 边界保守地计入所有 `get_*`、`poll_*`、报单、改单、撤单与连接调用；
`is_connected` 和 `disconnect` 是单独的连接状态/关闭操作。
行情记录器、私人成交流、A 做市、B IOC、退出清理共用同一个预算，不缓存风控仓位来减少计数。

交易所上限仍为 **200/秒**，不是 25。`max_requests_per_second=200`，实际预算保留 5% 余量，
按 190 次/1.05 秒控制边界和 SDK 内部消息。`max_updates_per_second` 保留兼容，若改小，
两者取较小值作为全局预算。代码记录真实 API 调用时间，失败请求也计数；下单前先等预算，
再读取新盘口/仓位。超时的 A 报价会取消而非照旧发送，B 入场继续复核 edge 和有效期。
A/B 独立 ±100 已保证净值不超过 ±200，因此去掉 A 每次核对时对 B 的重复净额查询，
仍保留逐品种发送前的最坏仓位检查。

`request_budget` 日志包含按方法累计的调用数、等待时间、循环实际耗时与目标间隔，
可用于核对部署 SDK 的实际负载。190 的调用预算不代表能在做市同时每秒提交 190 张新单。
此方案在缺少部署 SDK 源码时保守计数，也可能把本地缓存读取计入；实际频率以日志为准。

若上一次因断线留下 `safe_to_start=false`，先同步**整个 final-hybrid 目录**，包括新的
`request_budget.py` 和 `reconcile.py`，保留原状态与原日志，然后执行：

```sh
python3 stock_market_making/strategies/final-hybrid/run.py --check
python3 stock_market_making/strategies/final-hybrid/run.py --reconcile
```

`--check` 输出版本应为 `final_hybrid_v1_1`。`--reconcile` 获取状态锁，重新连接，
确认 A/B 无挂单、实际仓位合法且 B 等于上次原始基准后才解锁；它不发送、撤销或平仓任何订单，
也不把当前残仓偷偷改成新基准。成功后再运行 `--live`。

旧 v1 在断线收尾时可能把 B 基准从状态文件中覆盖掉；恢复入口会读取状态中 `run_id`
指定的那份 `events.jsonl` 的 `inventory_baseline` / `settings`，不会使用其他会话的基准。
若日志被搬走，使用相同的 `--log-dir` 指定原运行日志父目录；自定义状态路径需继续使用
相同 `--state-file`。缺失/冲突基准、仍有挂单、B 残量不为零或风险停止均保持锁定并打印原因。
不要直接删除状态文件来启动。

新版断线日志保留捕获到的服务端强制断开原因，同时保存后续 API 异常；状态保留原始 B 基准、
最后已确认的策略仓位和未完成 IOC。最后已确认值不冒充断线时的实时账户仓位。

## 验证

```sh
python3 -B -m unittest discover -s stock_market_making/strategies/final-hybrid/tests -p 'test_*.py' -q
python3 -B stock_market_making/strategies/final-hybrid/run.py --check
```

v1.1 离线测试覆盖 A 报价等价、只 A 做市、B IOC 复核/止盈/超时、部分成交、枚举兼容、
持续学习、重复盘口、真实 runner 的模拟生命周期、混合查询/写请求滚动限速、
旧版缺失基准恢复、残仓/挂单拒绝解锁以及断线保留原始错误与基准。
2026-09-11：35 项测试及 `--check` 通过，尚未在部署交易所重跑。

本轮没有连接交易所，也没有验证收益。180 秒和 epoch 相位沿用 sniper 的配置，
不是从那段反复重置、到期预测样本为零的日志重新证明的结论。sniper 的拟合 R² 是样本内指标，
也不等同于 baseline-refine 的 45 秒到期预测检验。

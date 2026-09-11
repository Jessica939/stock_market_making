# final-hybrid

`final_hybrid_v1_2`：A 使用 baseline-refine 的做市报价，B 同时评估 stale_quote_sniping
抢价和 baseline-refine 周期目标价，统一通过 IOC 执行。**只有 A 做市，B 不挂做市单。**

## 默认行为

| 项目 | 行为 |
| --- | --- |
| A 做市 | 扣除自身挂单后改善外部最优价；新增报价最多 100 股，减仓侧覆盖剩余仓位；窄价差避免双边相交 |
| B 抢价定价 | `FV_B = A_mid - predicted_basis`；固定 180 秒周期，启动使用 sniper 的绝对时间相位先验，180 秒会话历史后因果拟合 |
| B 抢价入场 | 至少 3 ticks edge；基础 2 股，edge 每增加 2 ticks 加 10 股，最大 50 股；整笔 VWAP 必须支持该档位 |
| B 抢价执行 | 等待至少 50ms，再刷新盘口并用冻结模型复核；入场与退出均 IOC，无 B 做市补仓通道 |
| B 抢价退出 | 可执行 VWAP 达到入场复核时的 FV 后，用决策后的新盘口再确认；目标消失则取消止盈；5 秒超时或风险停止独立退出 |
| 轮询 | B 目标每 50ms 一轮；A 报价核对默认每 250ms；实际间隔受共享请求预算和同步 RPC 耗时限制 |
| 学习与录制 | 每秒最多一个新 A/B 样本，重复盘口不增加样本；每 5 秒拟合；行情录制每 0.5 秒 |
| 额度 | A、B 各自实际仓位 ±100，计入同向挂单；每品种挂单总量 200；数据查询、成交轮询、报单、撤单共享 200 次/秒上限；实际采用 190 次/1.05 秒，保留余量 |

抢价通道复核时如果 fair value 已变化，入场使用复核后的新 FV 作为退出目标。
例如原做空决策 FV 为 113.8，而发送前可卖价已跌到 112.2，旧模型不能给出足够正 edge 时
直接放弃，不会挂出 B 做市单再沿用旧目标退出。

启动允许 A/B 已有仓位，但 B 不能有遗留挂单。A 纳入做市管理；B 记为启动基准，
只管理本次 IOC 产生的增量，不替用户清空原来的 B。B 账户仓位与“基准＋已确认成交”
不符时停止，运行期间不能让其他程序或人工同时交易 B。

抢价退出时间沿用 sniper；两种 B 计划共享风险阈值：B 策略损失 1250 或高点回撤 1500 时停止新增交易；
这些指标仅属于本次 B 增量，不包含 A 做市收益。A/B 报价超过 1 秒陈旧或不同步时停止 B 入场。
宽 A 盘口只保留减仓报价，失效 A 盘口撤掉报价。

## B 周期目标价（v1.2）

默认配置 `b_cycle.enabled=true`。B 空闲时先判断原有抢价信号；没有抢价计划时，
再启动周期目标价计划。一个周期计划从创建到退出或无成交过期期间独占 B，
不会同时建立两个方向或让抢价通道改写周期目标。设为 `false` 可只运行原有抢价策略。
旧配置未提供 `b_cycle` 时保持关闭。

直接复用 `baseline-refine/cycle_signal.py`、`cycle_history.py`、`cycle_position.py`：

- 周期预测默认 45 秒；保留在线拟合、周期/窗口调整、验证权重与近期历史初始化。
  持仓期间继续学习，宽价差仍可学习但禁止入场。
- 预测变动为 `predicted_B_change × fit_weight`。目标从计划创建时 B 中点出发，
  取预测变动绝对值的 80%，扣除 2 ticks，再向更容易成交的 tick 方向取整。
  方向和目标在计划创建时冻结，复核、补仓和模型更新均不移动目标。
- 默认目标 100 股，10 秒建仓窗口内请求剩余目标量，实际按账户 ±100、
  可执行深度和价格空间裁剪。入场等待至少配置的 50ms 延迟，再检查新盘口、
  信号方向和有效期；每个扫到的价位距离冻结目标都须超过 1 tick 加双程手续费。
- 45 秒仅是预测和持仓参考，周期计划不受抢价通道的 5 秒超时控制。
  多仓 best_bid、空仓 best_ask 达到目标后锁定退出，部分成交或价格回落不会撤销。
- 相对计划中点逆向 60 ticks、持续观察 3 秒触发止损；恢复会清空计时，
  相邻观察超过 1.5 秒则重新计时。A 盘口失效仍可用新鲜 B 盘口触发退出。
- 正常周期退出后冷却 2 秒；建仓窗口无成交结束后 1 秒重新评估周期计划。
  会话停止、风险停止与 Ctrl+C 仍退出本次已确认 B 增量，保留启动基准仓位。

执行适配：这里使用 final-hybrid 的 IOC，不移植 baseline-refine 的改善价挂单及
“2 秒后跨盘”的限价退出流程。达到目标后立即用有界深度 IOC 请求全部残量；
深度不足时后续继续处理剩余量。零成交或部分成交的最终状态无法证明时，仍按原规则停止，
不会猜测成交或自动补单。仅在成交终态已确认后才能继续补仓/退出。

`b_cycle.signal` 和 `b_cycle.position` 分别配置预测与目标持仓参数；周期的退出执行参数
`exit_cross_after_seconds`、`exit_sweep_ticks` 不控制这里的 IOC，深度限制沿用顶层
`max_sweep_ticks`、`depth_reserve_lots`。新增日志为 `cycle_bootstrap`、`cycle_signal`、
`cycle_position`、`cycle_entry_pending`、`cycle_entry_confirmed`；退出原因记录在
共用的 `stale_exit_intent` 中。两种通道合计的 B 收益参与会话损失和回撤判断。

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
`order_sides.py`、`quote_helpers.py`、`recording`、`strategies/baseline-refine` 和现有
`strategies/common`、`strategies/hybrid/state.py`。

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

`--check` 输出版本应为 `final_hybrid_v1_3`。`--reconcile` 获取状态锁并重新连接，
自动逐笔取消 PHILIPS_A/B 的全部挂单并确认清空，然后读取实际仓位。当前实际 B 直接成为
新运行基准；旧基准和差额写入 `previous_baseline_B`、`baseline_change_B` 供追溯。
它不自动平仓或发送新订单。成功后再运行 `--live`。

默认 `run_forever=true`：正常运行不会因 session 时长、IOC 私人成交回报延迟、账户仓位与
本地 B 状态不一致、断线或单次 API 异常自行退出。断线会持续重连；IOC 在结算等待结束后以
交易所账户仓位为准记录成交，并用限价估算未出现在私人成交流中的成交额。发现 B 不一致时，
策略会清空 B 的本地周期状态、以当前账户仓位重建基准，然后继续交易。按 Ctrl+C 才会进入
撤销 A 挂单和关闭 B 周期仓位的收尾流程。

旧 v1 在断线收尾时可能把 B 基准从状态文件中覆盖掉；恢复入口会读取状态中 `run_id`
指定的那份 `events.jsonl` 的 `inventory_baseline` / `settings`，不会使用其他会话的基准。
若日志被搬走，使用相同的 `--log-dir` 指定原运行日志父目录；自定义状态路径需继续使用
相同 `--state-file`。无法确认撤单、仓位快照无效、超出 ±100 或风险停止时保持锁定并打印原因。

新版断线日志保留捕获到的服务端强制断开原因，同时保存后续 API 异常；状态保留原始 B 基准、
最后已确认的策略仓位和未完成 IOC。最后已确认值不冒充断线时的实时账户仓位。

## 验证

```sh
python3 -B -m unittest discover -s stock_market_making/strategies/final-hybrid/tests -p 'test_*.py' -q
python3 -B stock_market_making/strategies/final-hybrid/run.py --check
```

离线测试新增周期目标公式等价、长短仓、冻结目标、部分补仓、持仓参考、持续止损、
锁定止盈退出、抢价优先、基准仓位恢复和真实历史拟合。原 v1.1 测试覆盖 A 报价等价、只 A 做市、B IOC 复核/止盈/超时、部分成交、枚举兼容、
持续学习、重复盘口、真实 runner 的模拟生命周期、混合查询/写请求滚动限速、
旧版缺失基准恢复、自动撤销 A/B 挂单、按最新账户重建 B 基准，以及断线保留原始错误与基准。
2026-09-11：54 项测试及 `--check` 通过，尚未在部署交易所重跑。

本轮没有连接交易所，也没有验证收益。180 秒和 epoch 相位沿用 sniper 的配置，
不是从那段反复重置、到期预测样本为零的日志重新证明的结论。sniper 的拟合 R² 是样本内指标，
也不等同于 baseline-refine 的 45 秒到期预测检验。

# B stale-quote sniping v1

这是独立的 B 主动吃单策略。A 只作为定价参考，不会向 A 发单。不要与 baseline、pair、hybrid 或 b_cycle 在同一账户同时运行。

## 交易规则

- 用决策帧之前的历史拟合固定 180 秒周期的 `A_mid - B_mid`。
- 当前 fair value 为 `FV_B = A_mid - predicted_basis`。当前 B 盘口在产生本次信号以后才进入模型，因此不会污染当次 FV。
- 2 股可执行 ask 低于 FV 至少 3 ticks 时准备买 B；可执行 bid 高于 FV 至少 3 ticks 时准备卖 B。
- 等待至少 250ms 后刷新 A/B 盘口，使用决策时冻结的模型重新计算 FV 和 edge。edge 消失就放弃，不发送订单。
- 入场和退出均使用 IOC。同一时间最多一笔 B 仓位，最多 2 股；A 必须始终为零。
- B 的可执行价格达到入场 FV 后准备退出；最长持仓 5 秒。退出也等待新的可执行盘口。
- 盘口最多 1 秒陈旧，A/B 时间差最多 750ms；每侧先预留 200 股，最多扫 10 ticks。
- 累计损失 50 或从权益高点回撤 60 时停止入场并尝试平仓。阈值触发不能保证最终损失不超过阈值。

这些是两日历史回放得到的实验参数，不是盈利保证。历史盘口包含原策略行为，无法重建新策略参与后的反事实队列和成交。

## 检查与回放

从项目根目录运行。默认只检查配置，不连接交易所：

```powershell
python -B strategies/stale_quote_sniping/run.py --check
```

使用一份完整的全深度录制回放：

```powershell
python -B strategies/stale_quote_sniping/run.py --replay "D:\Jessica\optiver\data\market\philips_20260910T085700_968660Z_ad33f82a\orderbooks_00001.jsonl.gz" --duration 800
```

回放不会伪造文件末尾的平仓盘口。如果录制结束时仍有仓位，程序返回 2 并在 `session_end` 保留实际尾仓。

## Live

只有下面的命令会连接交易所并发送 IOC：

```powershell
python strategies/stale_quote_sniping/run.py --live
```

启动要求整个账户空仓且所有品种都无挂单。状态文件默认是 `state/default/stale_quote_sniping.json`。运行开始前先写入不可自动重启标记；只有正常结束、确认空仓、无执行故障且日志正常时，才允许下次自动启动。未知 IOC、部分成交无法证明终态、残余仓位或风险停止都要求人工核对账户。

主要日志事件包括 `stale_signal`、`stale_entry_pending`、`stale_entry_blocked`、`stale_entry_confirmed`、`stale_holding`、`stale_exit_intent`、`stale_exit_confirmed` 和 `session_end`。入场日志同时保存决策 FV、延迟后 FV、可执行 VWAP、edge 和冻结模型。

# B stale-quote sniping v2

这是独立的 B 主动吃单策略。A 只作为定价参考，不会向 A 发单。账户可以带着已有 A/B 仓位启动；程序把启动时的 B 仓位记为基准，只管理自己相对该基准产生的 B 增量。

## 交易规则

- 用决策帧之前的历史拟合固定 180 秒周期的 `A_mid - B_mid`。
- 当前 fair value 为 `FV_B = A_mid - predicted_basis`。当前 B 盘口在产生本次信号以后才进入模型，因此不会污染当次 FV。
- 默认在 2 股可执行 ask 低于 FV 至少 2 ticks 时准备买 B；可执行 bid 高于 FV 至少 2 ticks 时准备卖 B。
- 等待至少 250ms 后刷新 A/B 盘口，使用决策时冻结的模型重新计算 FV 和 edge。edge 消失就放弃，不发送订单。
- 入场和退出均使用 IOC。同一时间最多一笔 B 仓位，最多 2 股；A 必须始终为零。
- B 的可执行价格达到入场 FV 后准备退出；最长持仓 5 秒。退出也等待新的可执行盘口。
- live 模式直接使用显示深度，不做无意义的 queue reserve，因为本策略是主动 IOC taker。历史回放仍预留 200 股，降低旧策略自身订单污染。盘口最多 1 秒陈旧，A/B 时间差最多 750ms，最多扫 10 ticks。
- 累计损失 50 或从权益高点回撤 60 时停止入场并尝试平仓。阈值触发不能保证最终损失不超过阈值。

默认 2 股、5 秒持仓和 180 秒周期来自两日历史回放。2-tick 门槛也在参数网格中保持正收益，但交易更多、单笔质量低于 3 ticks；这不是盈利保证。历史盘口包含原策略行为，无法重建新策略参与后的反事实队列和成交。

## 仓位所有权

假设启动时账户是 `A=-18, B=+9`，策略记录 `baseline_B=+9`。买入 2 股以后，账户 B 是 `+11`，但 sniper 自有仓位只是 `+2`；退出目标是回到 `+9`，不会替用户清空原来的 B。A 的任何已有仓位都会被忽略。

运行期间不能有其他程序或人工操作同时交易 B，否则账户 B 的变化无法可靠归属。A 可以由其他策略交易，但这笔 A 风险不计入 sniper 的损益和止损。启动时只要求 B 没有遗留挂单；其他品种的仓位和挂单不阻止启动。

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

启动允许已有 A/B 仓位，只要求 B 没有遗留挂单。状态文件默认是 `state/default/stale_quote_sniping.json`。运行开始前先写入不可自动重启标记；只有正常结束、确认 sniper 的 B 增量回到零、无执行故障且日志正常时，才允许下次自动启动。未知 IOC、无法确认的部分成交、未恢复到 B 基准或风险停止都要求人工核对账户。

主要日志事件包括 `stale_signal`、`stale_entry_pending`、`stale_entry_blocked`、`stale_entry_confirmed`、`stale_holding`、`stale_exit_intent`、`stale_exit_confirmed` 和 `session_end`。入场日志同时保存决策 FV、延迟后 FV、可执行 VWAP、edge 和冻结模型。

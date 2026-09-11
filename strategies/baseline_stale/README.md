# baseline-stale v1

单进程合并 `baseline-refine` 被动做市与 `stale_quote_sniping` 的 B 主动 IOC。

## 执行与仓位

- 只有一个交易所连接、一个下单入口和一个私有成交消费者。
- 内部把 `mm` 作为 baseline-refine owner，把 `pair` 作为 stale owner；账户实际仓位必须始终等于两者之和。
- 启动时已有 A/B 仓位归 baseline-refine，stale 从零仓开始。启动时不允许存在任何遗留挂单。
- stale 出现 pending、持仓或退出状态时，先撤销并确认 B 的 baseline 挂单；A 的 baseline 做市继续运行。
- stale 平仓目标只是其自有 B 仓位归零，不会平掉 baseline 的 B 仓位。
- B 的 baseline 配额默认 50 股，stale 配额最多 50 股；最终发送仍按账户实际逐品种 ±100、A+B 净仓及全部同向挂单统一检查。
- 正常停止时取消 baseline 挂单、尝试平掉 stale 仓位；baseline 已成交库存沿用原策略语义保留。

如果进程在 stale IOC 或持仓期间异常终止，不要直接重启并把残仓自动归给 baseline；应先根据日志人工核对并处理 B。v1 尚未持久化跨进程的 owner 归属。

## 运行

从项目根目录检查配置，不连接交易所：

```powershell
python -B strategies/baseline_stale/run.py --check
```

实盘：

```powershell
python strategies/baseline_stale/run.py --live
```

不要同时运行 `baseline-refine`、`stale_quote_sniping`、旧 `hybrid` 或其他会交易 A/B 的进程。

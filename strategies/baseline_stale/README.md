# baseline-stale v1

单进程合并 `baseline-refine` 被动做市与 `stale_quote_sniping` 的 B 主动 IOC。
这让两类机会可以共存，但不会保证收益相加；B 报价在主动成交窗口内会短暂停止，主动吃单也会增加手续费、冲击和库存风险。

## 执行与仓位

- 只有一个交易所连接、一个下单入口和一个私有成交消费者。
- 内部把 `mm` 作为 baseline-refine owner，把 `pair` 作为 stale owner；账户实际仓位必须始终等于两者之和。
- 启动时已有 A/B 仓位归 baseline-refine，stale 从零仓开始。启动时不允许存在任何遗留挂单。
- A/B 以外的非零账户仓位会阻止启动，避免未管理风险混入总限额。
- stale 出现 pending、持仓或退出状态时，先撤销并确认 B 的 baseline 挂单；A 的 baseline 做市继续运行。
- stale 平仓目标只是其自有 B 仓位归零，不会平掉 baseline 的 B 仓位。
- B 的 baseline 配额默认 50 股，stale 配额最多 50 股；最终发送仍按账户实际逐品种 ±100、A+B 净仓及全部同向挂单统一检查。
- 正常停止时取消 baseline 挂单、尝试平掉 stale 仓位；baseline 已成交库存沿用原策略语义保留。

默认状态文件是 `state/default/baseline_stale.json`，`--account NAME` 可选择隔离的本地状态空间。正常停止会在撤单并确认 stale 归零后保存 baseline 库存。每个交易步骤前先把状态标记为未确认；如果进程异常终止，下次启动会拒绝交易，要求先根据日志和账户手工核对，而不会把可能的 stale 残仓静默归给 baseline。状态文件还有进程锁，不能由两个实例共用。

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

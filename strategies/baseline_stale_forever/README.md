# baseline-stale-forever

这是 `baseline_stale` 的独立容错版本。它复制相同的 baseline-refine 做市与
B stale-quote sniping 组合，但不会修改或复用原策略的状态文件。

主要差异：

- 报价容量因并发成交发生变化时，撤销对应品种的 baseline 挂单并在下一轮重报。
- 单轮可恢复异常发生后，先撤销全部 baseline 挂单并核对实际仓位，然后继续运行。
- 收尾阶段不会因为 stale 仓位已经归零而提前结束；进程保持到完整会话截止时间。
- 断线、未知下单结果、无法归属的成交或仓位不一致仍属于硬故障，不会继续盲目下单。

日志默认写入 `data/runs/baseline_stale_forever`，状态默认写入
`state/ACCOUNT/baseline_stale_forever.json`，因此不会覆盖原 `baseline_stale`。

检查配置：

```bash
python -B strategies/baseline_stale_forever/run.py --check
```

首次明确采用当前账户仓位：

```bash
python strategies/baseline_stale_forever/run.py --live --adopt-current
```

后续正常启动：

```bash
python strategies/baseline_stale_forever/run.py --live
```

不要同时运行本策略、`baseline_stale` 或其他会交易 PHILIPS_A/B 的策略。

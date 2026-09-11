# Volatility-adaptive market maker

这是从 baseline 独立出来的实验策略。基础 fair value、inventory skew 和 B 的周期风险保护保持不变；新增的波动率层对 A、B 分别维护因果 EWMA。

核心定义是：

```text
variance_rate = EWMA((mid[t] - mid[t-1])^2 / dt)
sigma         = sqrt(variance_rate * risk_horizon_seconds)
full_spread   = base_full_spread + sigma_multiplier * sigma
```

最终 half-spread 取上述结果、盘口 half-spread 和最小 half-spread 的最大值。默认参数使用 10 秒半衰期、2 秒风险窗口、`2 * sigma` 风险补偿；完整报价最多扩至 12 ticks。达到 2 sigma-ticks 后逐渐缩小增加库存侧的挂单，达到 5 sigma-ticks 后暂停该侧。已有库存的减仓侧不因高波动被关闭。B 原有的周期保护会在波动率层之后继续扩宽或抑制不利方向。

这些参数只是保守的实验起点，尚未用历史数据校准，也不代表盈利保证。先离线检查：

```powershell
python strategies/volatility_adaptive/run.py --check
```

连接模拟交易所并真正发送订单必须显式指定：

```powershell
python strategies/volatility_adaptive/run.py --live
```

不要与 baseline 或其他策略在同一账户同时运行。日志写入 `data/runs/volatility_adaptive/`，市场数据继续使用共享的 `data/market/`。


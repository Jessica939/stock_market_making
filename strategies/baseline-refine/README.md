# baseline-refine

从 2026-09-11 四策略历史对照中，按总模拟盈亏最高选出的 Baseline 改进版。
保留 A 独立做市和 B 周期持仓；没有改成只交易 B。

12 段完整录制的理想化回放：总盈亏 **+2529.2**（包括残仓估值），B 已平仓股数加权持仓
中位数 **43.1 秒**，B 峰值仓位 **20 股**。限价触价全额成交、忽略排队竞争等假设见
[原始比较报告](../../analysis/cycle_revision_20260911/README.md)，这些数字不保证未来收益。

## 参数与行为

- 固定 180 秒周期，在线预测未来 45 秒；不加载历史相位，不使用未来数据。
- B 目标最多 20 股，每单最多 5 股，前 10 秒分批建立，新计划至少间隔 45 秒。
- 首次报价时冻结方向、目标价格和退出期限；加仓与重新拟合不会延后期限。
- B 达到目标或建仓窗口结束后撤掉普通双边报价并持仓；到期、不利移动 20 ticks
  或异常仓位时持续减仓，直到空仓。行情和限价成交条件可能让退出晚于计划。
- A 继续独立做市；停止程序仍取消挂单并保留残仓。

参数位于本目录 Notebook 的参数单元。周期模型、报价保护、持仓控制、订单管理均在本目录，
不依赖旧 `strategies/baseline` 的策略实现，也不需要修改共用 `order_execution.py`。
通用报价辅助与录制组件仍复用仓库原有模块。日志版本为 `baseline_refine_v1`，
默认日志目录为 `data/runs/baseline-refine`，与旧 Baseline 区分。

## 运行

从 `stock_market_making` 目录运行离线参数检查（不连接交易所）：

```sh
python -B strategies/baseline-refine/run.py --check
```

部署好原有 Optibook SDK 和工作区 `common/trade_logger.py` 后，显式启动交易：

```sh
python strategies/baseline-refine/run.py --live
```

`--check` 只检查策略定义和参数，不验证 SDK/日志依赖部署。本地仍缺外部日志 helper，
没有运行 `--live`。目录名中的连字符是命名要求；启动脚本与 Notebook 使用相对包导入，
无需将目录改成下划线名称。

## 验证与回退范围

从仓库父目录运行：

```sh
rtk proxy python3 -B -m unittest discover -s stock_market_making/strategies/baseline-refine/tests -p 'test_*.py' -q
rtk proxy python3 -B stock_market_making/analysis/cycle_revision_20260911/evaluate.py
```

第一条执行本目录 7 项持仓/订单控制测试。第二条比较原始 Baseline 与本目录版本，
并逐段断言本目录的完整回放结果等于隔离前获选版本，包括下单记录哈希、盈亏、仓位和持仓时间。
截断与未来价格扰动检查也会重新执行。结果写入
[retained](../../analysis/cycle_revision_20260911/retained/)，不会覆盖原始四策略比较。

原 Baseline、Pair、B 单腿、Hybrid 的本轮策略调整，以及共用订单/配对执行器中的本轮改动
已回退。用户另行进行的 `common` 目录迁移及 B 引擎格式化/迁移导入被保留。

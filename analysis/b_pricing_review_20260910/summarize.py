"""Generate a reproducible Chinese research note with independent ledger checks."""
import json
from pathlib import Path

OUT=Path(__file__).resolve().parent


def main():
    m=json.loads((OUT/'metrics.json').read_text(encoding='utf-8'))
    rows=m['scenarios']
    current=m['runs'][-1]['run']
    keys=('run','queue','latency','depth_removal')
    names=dict(depth_guard='原深度估值 + 减仓保护',clipped_guard='限制估值在买卖盘内 + 减仓保护',
               mid_guard='中间价估值 + 减仓保护',wider_entry_guard='原估值 + 减仓保护 + 增仓远 1 tick')
    def control(row):
        return next(r for r in rows if r['policy']=='depth_guard' and all(r[k]==row[k] for k in keys))
    counts={p:sum(r['terminal_equity']>control(r)['terminal_equity'] for r in rows if r['policy']==p)
            for p in names if p!='depth_guard'}
    mainrows=[r for r in rows if r['run']==current and r['queue']=='displayed' and r['latency']==.05 and r['depth_removal']==200]
    table=[]
    for r in mainrows:
        table.append(f"| {names[r['policy']]} | {r['terminal_equity']:.1f} | {r['stats'].get('volume',0)} | {r['final_position']:+d} | {r['peak_abs_position']} | {r['observed_mid_drawdown']:.1f} |")
    ranges=[]
    for meta in m['runs']:
        run=meta['run']
        label='前一段约 14.66 分钟' if run!=current else '当前约 6.14 分钟'
        for p in names:
            z=[r for r in rows if r['run']==run and r['policy']==p]
            ranges.append(f"| {label} | {names[p]} | {min(r['terminal_equity'] for r in z):.1f} 至 {max(r['terminal_equity'] for r in z):.1f} | {sum(r['terminal_equity']>control(r)['terminal_equity'] for r in z)} / 8 |")
    forecasts=[]
    for meta in m['runs']:
        label='前一段' if meta['run']!=current else '当前段'
        for h in (1,3,5):
            z={r['mode']:r for r in meta['pricing_diagnostic']['forecasts'] if r['horizon']==h}
            forecasts.append(f"| {label} | {h} 秒 | {z['depth']['n']} | {z['depth']['mae']:.3f} | {z['clipped']['mae']:.3f} | {z['mid']['mae']:.3f} |")
    diag=[]
    for meta in m['runs']:
        run=meta['run']
        r=next(r for r in rows if r['run']==run and r['policy']=='depth_guard' and 'fill_diagnostic' in r)
        d=r['fill_diagnostic']
        label='前一段' if run!=current else '当前段'
        diag.append(f"| {label} | {d['increase_1s']['mark_per_share']:+.3f} | {d['reduce_1s']['mark_per_share']:+.3f} | {d['increase_5s']['mark_per_share']:+.3f}（{d['increase_5s']['volume']} 股） | {d['reduce_5s']['mark_per_share']:+.3f}（{d['reduce_5s']['volume']} 股） |")
    # Independently audit all 8 main-case traces, including open residuals.
    audits=[]
    for r in rows:
        if 'fill_diagnostic' not in r:
            continue
        prefix=r['run']+'_'+r['policy']
        fills=[json.loads(l) for l in (OUT/(prefix+'_fills.jsonl')).read_text().splitlines()]
        position=0;cash=0.;peak=0;volume=0
        for f in fills:
            assert position==f['before']
            sign=1 if f['side']=='bid' else -1
            position+=sign*f['volume']
            cash-=sign*f['volume']*f['price']
            volume+=f['volume']
            peak=max(peak,abs(position))
            assert position==f['after']
            if f.get('context'):
                assert f['context']['decision_t']<=f['context']['placed_t']<f['t']
        assert position==r['final_position'] and abs(cash-r['cash'])<1e-7
        assert peak==r['peak_abs_position'] and volume==r['stats']['volume']
        if position==0:
            episodes=[json.loads(l) for l in (OUT/(prefix+'_episodes.jsonl')).read_text().splitlines()]
            assert abs(sum(e['pnl'] for e in episodes)-cash)<1e-7
        audits.append(prefix)
    summary=dict(scenarios=len(rows),negative_scenarios=sum(r['terminal_equity']<0 for r in rows),
                 better_than_depth_guard=counts,independently_audited_traces=audits)
    text=f'''# B 估值与增仓距离：机制对照实验

结论：目前不能把剩余亏损归因于“原估值偶尔超出买一卖一”，也不能凭直觉替换成中间价。本轮固定的两种估值替换在全部 16 个对应比较中都更差。增仓额外远 1 tick 在 14 / 16 个对应比较中少亏，但当前主情景更差，且没有产生稳定的盈利。64 个情景全部为负。

这是上一轮动态回放的延续：所有候选都保留小仓位减仓 3 tick 价格保护，不设固定持仓超时。分别只改估值或增仓报价距离，以定位机制；没有搜索最优 tick、库存或超时参数。

## 1. 当前窗口的主情景结果

主情景与上一轮相同：50 毫秒执行延迟，同价成交消耗前方显示队列后才匹配；主动盘口每侧先扣 200 股，再只使用剩余数量的一半。

| 规则 | 模拟终点权益变化，费用前 | 买卖总量 | 终点持仓 | 最大绝对持仓 | 观察到的权益回撤 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(table)}

中间价估值情景终点残留 −1 股，因此 −128.0 包含最后有效中间价估值，不能称为全部已实现亏损。其他三个情景为空仓，终点权益变化等于模拟现金流。各情景仍是固定历史需求下的模型结果，不是修改后一定会实现的利润。

增仓加宽后，交易量从 500 股降到 366 股，最大持仓从 8 股降到 6 股，但亏损从 −53.0 增至 −57.1。减少交易和仓位本身不等于改善盈利。这也说明不能只看删掉了多少不利成交，需要重新计算剩余全部交易的路径。

## 2. 两段行情和八组执行假设

继续使用两种被动成交规则、50 / 250 毫秒延迟、主动深度先扣 0 / 200 股的八组组合。同样的组合内与“原深度估值 + 减仓保护”比较：

| 窗口 | 规则 | 费用前终点权益变化范围 | 优于对应原估值保护情景 |
|---|---|---:|---:|
{chr(10).join(ranges)}

两种估值替换都在 16 / 16 个比较中更差，增仓加宽在前一段 8 / 8、当前段 6 / 8 中更好。比较同一组假设的结果，不将区间端点相减。范围不是统计置信区间，两个相邻历史窗口也不是独立新验证集。

## 3. 估值超出买卖盘，不等于已经找到错误

原估值取买卖两侧按距离衰减的深度加权均价，再取平均；大量挂单会影响它。当前窗口有 181 / 655 次（27.6%）基础估值在当时外部买一卖一范围之外，前一段有 393 / 1541 次（25.5%）。估值相对中间价的绝对偏移中位数分别约 0.084、0.077，最大约 0.820、0.858。

本次估值对照精确定义如下：

- 原深度估值：保留历史基础 fair_value；v1 的历史周期价格偏移先扣除，正式回放统一使用当前 v2 规则。
- 限制范围：将基础估值裁剪到当时外部买一价与卖一价之间。之后仍按模拟持仓计算库存偏移、报价距离与风险保护。
- 中间价：基础估值改为当时外部买一卖一的算术平均；其他规则相同。
- 增仓加宽：原基础估值不变，只将增加绝对库存的一侧再向外移 1 tick；空仓两侧都向外移。单量和减仓保护不变。

再用每个报价时刻的三种估值预测未来外部买卖中间价，计算同一批可用样本的平均绝对误差：

| 窗口 | 预测间隔 | 同批有效报价数 | 原深度估值 MAE | 限制范围 MAE | 中间价 MAE |
|---|---:|---:|---:|---:|---:|
{chr(10).join(forecasts)}

未来价格取首次符合要求的后续报价中的外部中间价：该盘口交易所时间不得早于目标时刻，观察时刻不得晚于目标 1 秒；三种估值共享样本，不用缺失值填零。MAE 衡量点预测误差，不表示可交易收益；这里原估值稍好，差异没有做独立样本显著性检验。

这些结果反对“把超出买卖盘的估值一律当作错误”的解释，不证明原估值已经足够准确。机器人原始订单大于 200 的用户分类规则仍保留；本次没有把聚合价位数量直接当成机器人身份，也没有从空白 buyer/seller 列编造身份。

## 4. 剩余亏损仍与退出有关，但入场也不稳定

在主情景“原深度估值 + 减仓保护”的模拟成交中，按成交瞬间库存拆分增加与减少绝对持仓。跨零成交拆分数量，未来外部中间价的覆盖规则与上一节一致：

| 窗口 | 增仓 1 秒每股价格表现 | 减仓 1 秒每股价格表现 | 增仓 5 秒每股表现与覆盖量 | 减仓 5 秒每股表现与覆盖量 |
|---|---:|---:|---:|---:|
{chr(10).join(diag)}

买入的每股价格表现为“未来中间价减成交价”，卖出为“成交价减未来中间价”。这些是模拟成交的后续价格诊断，不是历史真实成交的重新归因，也不能把两组均值直接相加当作收益。

当前段增仓表现为正、减仓仍为负；前一段增仓也为负。现阶段证据既没有证明简单换估值可以修复，也没有证明单纯扩大入场距离可以在不同时间稳定盈利。

另检查了从生成该订单的报价决策到成交的年龄，以 0.5 秒分组。在当前段，超过 0.5 秒的增仓成交 5 秒后表现反而较好；减仓则较差。前一段减仓的年龄关系也不相同，未出现“旧单总是更差”的一致关系。保留的同价订单可能经过后续报价重新确认，因此订单年龄不能直接当作估值失效年龄。本次没有据此加一个机械撤单计时器。

## 5. 已核验与仍然缺失的证据

12 项执行和定价语义测试通过；2196 次原报价重建通过。新增代码后的 16 个原估值保护对照情景与上一轮结果完全一致，核对现金、尾仓、峰值仓位和权益。64 个新情景均做现金及持仓守恒检查，8 份落盘主情景明细另行独立重算，并核对生成报价时间、订单生效时间、成交时间的先后。

执行模型限制沿用[动态回放报告](../b_dynamic_exit_20260910/REPORT.md)：周期信号、公允价原始市场输入及报价时刻仍固定于历史；没有真实订单级队列、市场冲击、实际私有回报延迟和 A/B 共用请求限速的完整仿真。主动深度压力扣除并不精确识别自身订单。本实验的尾仓按最后有效中间价估值，当前窗口尾部盘口年龄约 0.218 秒，前一段约 1.561 秒；数据截断信息保留在 metrics.json。

所有情景都是费用前结果，metrics.json 附带每交易股 0、0.01、0.05 的假设费用敏感性，不声称这些是交易所真实费率。全部行情已用于前面的探索，未把重复回放包装成样本外验证。

## 研究结论

当前支持保留的最小候选仍是小仓位减仓价格保护。没有证据支持默认加 1 秒强制退出、用中间价替换深度估值，或把“再远 1 tick”认定为稳定盈利改动。保护之后的亏损依然存在，需要能解释不同时间入场质量变化、以及退出后的不利价格表现的机制；继续在同一小段行情里堆参数无法代替新数据验证。

本次只修改独立分析代码，交易策略及执行器运行前后哈希一致，未连接交易所。完整参数和来源哈希在 metrics.json；summary.json 为比较结论，逐成交、报价和回合记录可追溯。

```text
python -m unittest discover -s analysis/b_dynamic_exit_20260910 -p test_replay.py
python analysis/b_pricing_review_20260910/study.py
python analysis/b_pricing_review_20260910/summarize.py
```
'''
    (OUT/'REPORT.md').write_text(text,encoding='utf-8')
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    main()

"""Static diagnostics from the completed, reconciled offline analysis."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

OUT=Path(__file__).resolve().parent
m=json.loads((OUT/'metrics.json').read_text(encoding='utf-8'))
r=next(r for r in m['runs'] if r['version']==m['current_version'])
f=pd.read_json(OUT/'fills.jsonl',lines=True)
f=f[f.run==r['run']].sort_values(['t','trade_id'])
e=pd.read_json(OUT/'excursions.jsonl',lines=True)
e=e[e.run==r['run']].sort_values('end')
origin=pd.Timestamp(r['start']).timestamp()
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})
fig,axs=plt.subplots(3,1,figsize=(10,9),layout='constrained')
axs[0].plot((e.end-origin)/60,e.pnl.cumsum(),color='#b42318',lw=1.6)
axs[0].axhline(0,color='#888',lw=.5)
axs[0].set(title='B current v2: 148 completed inventory excursions, total PnL -147.6',
           xlabel='Minutes from recording start',ylabel='Realized excursion PnL')
axs[1].step((f.t-origin)/60,f.position_after,where='post',color='#2563eb',lw=1)
axs[1].axhline(0,color='#888',lw=.5)
axs[1].set(title='Actual inventory remained between -6 and +6 shares; rule limit is +/-100',
           xlabel='Minutes from recording start',ylabel='Shares',ylim=(-7,7))
xs=np.arange(4)
for i,role in enumerate(('increase','reduce')):
    g=next(g for g in m['groups'] if g['run']==r['run'] and g['dimension']=='role' and g['labels']['role']==role)
    vals=[g['horizons'][str(h)]['mark_per_share'] for h in (1,3,5,15)]
    axs[2].bar(xs+(i-.5)*.32,vals,.32,color=('#2563eb','#b42318')[i],label=('Increase inventory','Reduce inventory')[i])
axs[2].set_xticks(xs,['1 second','3 seconds','5 seconds','15 seconds'])
axs[2].axhline(0,color='#888',lw=.5)
axs[2].set(title='Subsequent mid-price valuation: entry and exit fills behave differently',ylabel='Markout per share')
axs[2].legend()
fig.savefig(OUT/'b_loss_diagnostics.png',dpi=150)
print(OUT/'b_loss_diagnostics.png')

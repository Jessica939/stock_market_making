"""Offline-only candidate. Not imported by the live trading strategy."""
import math

SETTINGS=dict(tick=.1,min_distance_ticks=3,small_position_limit=10,
              require_active_cycle=True)


def apply(quote, settings=SETTINGS):
    out=dict(quote)
    if quote.get('instrument')!='PHILIPS_B': return out
    position=quote['position']
    if not 0<abs(position)<=settings['small_position_limit']: return out
    if settings['require_active_cycle'] and not quote.get('cycle',{}).get('active'): return out
    if quote.get('reduce_only'): return out
    tick=settings['tick']
    if position>0:
        if quote['sell_volume']<=0: return out
        raw=max(quote['ask_price'],quote['center']+settings['min_distance_ticks']*tick,quote['best_bid']+tick)
        out['ask_price']=round(math.ceil(raw/tick-1e-9)*tick,10)
    else:
        if quote['buy_volume']<=0: return out
        raw=min(quote['bid_price'],quote['center']-settings['min_distance_ticks']*tick,quote['best_ask']-tick)
        if raw<=0: return out
        out['bid_price']=round(math.floor(raw/tick+1e-9)*tick,10)
    return out

"""Distinguish order repricing from rejection of an observed execution price."""


def fill_impact(side, old_limit, new_limit, execution_price, position_before, volume):
    sign = 1 if side == 'bid' else -1
    repriced = abs(new_limit - old_limit) > 1e-7
    blocked = (new_limit < execution_price - 1e-7 if side == 'bid'
               else new_limit > execution_price + 1e-7)
    reducing = position_before * sign < 0
    fully_reducing = reducing and abs(position_before) >= volume
    return dict(repriced=repriced, original_execution_blocked=blocked,
                fully_reducing=fully_reducing,
                reducing_volume=min(abs(position_before), volume) if reducing else 0,
                changed=repriced and blocked and fully_reducing)

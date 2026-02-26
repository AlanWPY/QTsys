"""内置策略 - 均值回归"""

MEAN_REVERSION_CODE = '''
def initialize(context):
    """初始化: 均值回归参数"""
    context.lookback = 20
    context.entry_z = -1.0
    context.exit_z = 0.0

def handle_data(context):
    """Z-score偏离均值反向交易"""
    import numpy as np

    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.lookback + 1, "close")
        if len(closes) < context.lookback:
            continue

        recent = closes.tail(context.lookback).values
        mean = np.mean(recent)
        std = np.std(recent, ddof=1)

        if std == 0:
            continue

        current_price = closes.iloc[-1]
        z_score = (current_price - mean) / std

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        # Z-score低于阈值, 价格偏低, 买入
        if z_score < context.entry_z and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} 均值回归买入 Z={z_score:.2f} 价格={current_price:.2f} 均值={mean:.2f}")

        # Z-score回归到0附近, 卖出
        elif z_score > context.exit_z and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 均值回归卖出 Z={z_score:.2f} 价格={current_price:.2f} 均值={mean:.2f}")
'''

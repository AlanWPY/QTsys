"""内置策略 - 布林带突破"""

BOLLINGER_BREAKOUT_CODE = '''
def initialize(context):
    """初始化: 布林带参数"""
    context.bb_period = 20
    context.bb_std = 2.0

def handle_data(context):
    """突破上轨买入, 跌破中轨卖出"""
    import numpy as np

    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.bb_period + 1, "close")
        if len(closes) < context.bb_period:
            continue

        recent = closes.tail(context.bb_period).values
        mid = np.mean(recent)
        std = np.std(recent, ddof=1)
        upper = mid + context.bb_std * std

        current_price = closes.iloc[-1]

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        # 突破上轨买入
        if current_price > upper and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} 突破上轨买入 价格={current_price:.2f} 上轨={upper:.2f}")

        # 跌破中轨卖出
        elif current_price < mid and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 跌破中轨卖出 价格={current_price:.2f} 中轨={mid:.2f}")
'''

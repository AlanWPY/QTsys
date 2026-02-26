"""内置策略 - MACD信号"""

MACD_SIGNAL_CODE = '''
def initialize(context):
    """初始化: MACD参数"""
    context.fast_period = 12
    context.slow_period = 26
    context.signal_period = 9

def handle_data(context):
    """MACD金叉买入, 死叉卖出"""
    import numpy as np

    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.slow_period + context.signal_period + 10, "close")
        if len(closes) < context.slow_period + context.signal_period:
            continue

        prices = closes.values.astype(float)

        # 计算EMA
        def ema(data, period):
            result = np.empty_like(data)
            result[0] = data[0]
            k = 2.0 / (period + 1)
            for i in range(1, len(data)):
                result[i] = data[i] * k + result[i - 1] * (1 - k)
            return result

        ema_fast = ema(prices, context.fast_period)
        ema_slow = ema(prices, context.slow_period)
        dif = ema_fast - ema_slow
        dea = ema(dif, context.signal_period)

        macd_cur = dif[-1] - dea[-1]
        macd_prev = dif[-2] - dea[-2]

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        # 金叉: MACD柱由负转正
        if macd_cur > 0 and macd_prev <= 0 and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} MACD金叉买入 DIF={dif[-1]:.3f} DEA={dea[-1]:.3f}")

        # 死叉: MACD柱由正转负
        elif macd_cur < 0 and macd_prev >= 0 and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} MACD死叉卖出 DIF={dif[-1]:.3f} DEA={dea[-1]:.3f}")
'''

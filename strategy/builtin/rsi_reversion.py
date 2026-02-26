"""内置策略 - RSI超买超卖"""

RSI_REVERSION_CODE = '''
def initialize(context):
    """初始化: RSI参数"""
    context.rsi_period = 14
    context.oversold = 35
    context.overbought = 65

def handle_data(context):
    """RSI超买超卖策略: RSI低于阈值买入, 高于阈值卖出"""
    import numpy as np

    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.rsi_period + 2, "close")
        if len(closes) < context.rsi_period + 1:
            continue

        # 计算RSI (Wilder平滑)
        deltas = np.diff(closes.values)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # 初始平均
        avg_gain = np.mean(gains[:context.rsi_period])
        avg_loss = np.mean(losses[:context.rsi_period])

        # Wilder平滑后续值
        for i in range(context.rsi_period, len(gains)):
            avg_gain = (avg_gain * (context.rsi_period - 1) + gains[i]) / context.rsi_period
            avg_loss = (avg_loss * (context.rsi_period - 1) + losses[i]) / context.rsi_period

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        if rsi < context.oversold and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} RSI={rsi:.1f} 超卖买入")

        elif rsi > context.overbought and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} RSI={rsi:.1f} 超买卖出")
'''

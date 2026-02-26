"""内置策略 - 动量策略"""

MOMENTUM_CODE = '''
def initialize(context):
    """初始化: 动量参数"""
    context.lookback = 20

def handle_data(context):
    """N日收益率为正买入, 为负卖出"""
    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.lookback + 1, "close")
        if len(closes) < context.lookback + 1:
            continue

        # N日动量(收益率)
        momentum = (closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0]

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        if momentum > 0 and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} 动量为正买入 {context.lookback}日收益率={momentum:.2%}")

        elif momentum < 0 and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 动量为负卖出 {context.lookback}日收益率={momentum:.2%}")
'''

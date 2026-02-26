"""内置策略 - 量价策略"""

VOLUME_PRICE_CODE = '''
def initialize(context):
    """初始化: 量价参数"""
    context.ma_period = 20
    context.vol_ratio = 1.2

def handle_data(context):
    """放量突破均线买入, 跌破均线卖出"""
    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.ma_period + 1, "close")
        volumes = context.get_history(ts_code, context.ma_period + 1, "vol")

        if len(closes) < context.ma_period or len(volumes) < context.ma_period:
            continue

        ma = closes.tail(context.ma_period).mean()
        avg_vol = volumes.iloc[:-1].tail(context.ma_period - 1).mean()
        current_price = closes.iloc[-1]
        prev_price = closes.iloc[-2]
        current_vol = volumes.iloc[-1]

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        # 放量突破均线买入(当日站上均线 + 量比达标)
        is_breakout = current_price > ma and prev_price <= ma
        is_volume_surge = avg_vol > 0 and current_vol > avg_vol * context.vol_ratio

        if is_breakout and is_volume_surge and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} 放量突破买入 价格={current_price:.2f} MA={ma:.2f} 量比={current_vol/avg_vol:.1f}")

        elif current_price < ma and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 跌破均线卖出 价格={current_price:.2f} MA={ma:.2f}")
'''

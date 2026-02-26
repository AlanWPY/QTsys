"""内置示例策略 - 双均线交叉"""

MA_CROSS_CODE = '''
def initialize(context):
    """初始化: 设置参数"""
    context.short_period = 5
    context.long_period = 20

def handle_data(context):
    """每日执行: 双均线交叉策略"""
    for ts_code in context.universe:
        # 获取历史收盘价
        closes = context.get_history(ts_code, context.long_period + 1, "close")
        if len(closes) < context.long_period:
            continue

        # 计算均线
        ma_short = closes.tail(context.short_period).mean()
        ma_long = closes.tail(context.long_period).mean()

        # 前一日均线
        closes_prev = closes.iloc[:-1]
        ma_short_prev = closes_prev.tail(context.short_period).mean()
        ma_long_prev = closes_prev.tail(context.long_period).mean()

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        # 金叉买入
        if ma_short > ma_long and ma_short_prev <= ma_long_prev and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} 金叉买入 MA{context.short_period}={ma_short:.2f} MA{context.long_period}={ma_long:.2f}")

        # 死叉卖出
        elif ma_short < ma_long and ma_short_prev >= ma_long_prev and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 死叉卖出 MA{context.short_period}={ma_short:.2f} MA{context.long_period}={ma_long:.2f}")
'''

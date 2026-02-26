"""内置策略 - 海龟交易"""

TURTLE_TRADING_CODE = '''
def initialize(context):
    """初始化: 海龟交易参数"""
    context.entry_period = 20
    context.exit_period = 10
    context.atr_period = 14

def handle_data(context):
    """唐奇安通道突破 + ATR仓位管理"""
    import numpy as np

    for ts_code in context.universe:
        # 统一请求足够长度的数据,保证日期对齐
        need = max(context.entry_period, context.atr_period) + 2
        highs = context.get_history(ts_code, need, "high")
        lows = context.get_history(ts_code, need, "low")
        closes = context.get_history(ts_code, need, "close")

        if len(highs) < context.entry_period or len(closes) < context.atr_period + 1:
            continue

        # 唐奇安通道(不含当日)
        upper = highs.iloc[:-1].tail(context.entry_period).max()
        lower = lows.iloc[:-1].tail(context.exit_period).min()
        current_price = closes.iloc[-1]

        # ATR计算(日期已对齐)
        h = highs.values
        l = lows.values
        c = closes.values
        tr_list = []
        for i in range(1, len(c)):
            tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
            tr_list.append(tr)
        atr = np.mean(tr_list[-context.atr_period:]) if tr_list else 1.0

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        if current_price > upper and not has_position:
            # ATR仓位: 2%资金风险 / ATR
            risk_amount = context.portfolio_value * 0.02
            unit_size = int(risk_amount / atr) if atr > 0 else 0
            unit_size = (unit_size // 100) * 100
            if unit_size < 100:
                unit_size = 100
            context.order(ts_code, unit_size)
            context.log(f"{ts_code} 突破上轨买入 价格={current_price:.2f} ATR={atr:.2f} 数量={unit_size}")

        elif current_price < lower and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 跌破下轨卖出 价格={current_price:.2f} 下轨={lower:.2f}")
'''

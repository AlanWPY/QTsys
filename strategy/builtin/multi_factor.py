"""内置策略 - 多因子"""

MULTI_FACTOR_CODE = '''
def initialize(context):
    """初始化: 多因子参数"""
    context.lookback = 20
    context.buy_threshold = 0.55
    context.sell_threshold = 0.45

def handle_data(context):
    """动量+波动率+成交量综合评分"""
    import numpy as np

    for ts_code in context.universe:
        closes = context.get_history(ts_code, context.lookback + 1, "close")
        volumes = context.get_history(ts_code, context.lookback + 1, "vol")

        if len(closes) < context.lookback + 1 or len(volumes) < context.lookback:
            continue

        prices = closes.values.astype(float)

        # 因子1: 动量 (N日收益率, 归一化到0-1)
        momentum = (prices[-1] - prices[0]) / prices[0]
        mom_score = 1.0 / (1.0 + np.exp(-momentum * 20))

        # 因子2: 波动率 (低波动得高分)
        returns = np.diff(prices) / prices[:-1]
        vol = np.std(returns)
        vol_score = 1.0 / (1.0 + vol * 20)

        # 因子3: 成交量趋势 (近期放量得高分)
        vol_arr = volumes.values.astype(float)
        if len(vol_arr) >= 10:
            recent_vol = np.mean(vol_arr[-5:])
            past_vol = np.mean(vol_arr[-10:-5])
            vol_trend = recent_vol / past_vol if past_vol > 0 else 1.0
            vol_trend_score = min(vol_trend / 1.5, 1.0)
        else:
            vol_trend_score = 0.5

        # 综合评分 (动量权重更高)
        score = mom_score * 0.5 + vol_score * 0.2 + vol_trend_score * 0.3

        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0

        if score > context.buy_threshold and not has_position:
            context.order_value(ts_code, context.portfolio_value * 0.3)
            context.log(f"{ts_code} 多因子买入 评分={score:.2f} 动量={mom_score:.2f} 波动={vol_score:.2f} 量能={vol_trend_score:.2f}")

        elif score < context.sell_threshold and has_position:
            context.order(ts_code, -pos.amount)
            context.log(f"{ts_code} 多因子卖出 评分={score:.2f}")
'''

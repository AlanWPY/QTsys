"""内置因子库"""
import numpy as np
import pandas as pd


def _safe_div(a, b):
    return np.where(b != 0, a / b, 0.0)


BUILTIN_FACTORS = {}


def register(name, category, desc):
    def decorator(func):
        BUILTIN_FACTORS[name] = {
            "func": func,
            "category": category,
            "description": desc,
            "expression": f"builtin:{name}",
        }
        return func
    return decorator


# ===== 动量类 =====

@register("momentum_5", "动量", "5日动量: 5日收益率")
def momentum_5(closes, highs, lows, volumes, **kw):
    return closes.pct_change(5)


@register("momentum_20", "动量", "20日动量: 20日收益率")
def momentum_20(closes, highs, lows, volumes, **kw):
    return closes.pct_change(20)


@register("momentum_60", "动量", "60日动量: 60日收益率")
def momentum_60(closes, highs, lows, volumes, **kw):
    return closes.pct_change(60)


# ===== 反转类 =====

@register("reversal_5", "反转", "5日反转: -5日收益率")
def reversal_5(closes, highs, lows, volumes, **kw):
    return -closes.pct_change(5)


@register("reversal_20", "反转", "20日反转: -20日收益率")
def reversal_20(closes, highs, lows, volumes, **kw):
    return -closes.pct_change(20)


# ===== 波动率类 =====

@register("volatility_20", "波动率", "20日波动率: 日收益率标准差")
def volatility_20(closes, highs, lows, volumes, **kw):
    return closes.pct_change().rolling(20).std()


@register("atr_14", "波动率", "14日ATR: 平均真实波幅/收盘价")
def atr_14(closes, highs, lows, volumes, **kw):
    tr1 = highs - lows
    tr2 = (highs - closes.shift(1)).abs()
    tr3 = (lows - closes.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(14).mean() / closes


# ===== 成交量类 =====

@register("volume_ratio_5", "成交量", "5日量比: 当日成交量/5日均量")
def volume_ratio_5(closes, highs, lows, volumes, **kw):
    avg = volumes.rolling(5).mean()
    return volumes / avg.replace(0, np.nan)


@register("volume_momentum", "成交量", "成交量动量: 5日均量/20日均量")
def volume_momentum(closes, highs, lows, volumes, **kw):
    v5 = volumes.rolling(5).mean()
    v20 = volumes.rolling(20).mean()
    return v5 / v20.replace(0, np.nan)


# ===== 技术指标类 =====

@register("rsi_14", "技术指标", "14日RSI")
def rsi_14(closes, highs, lows, volumes, **kw):
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


@register("bias_20", "技术指标", "20日乖离率: (价格-MA)/MA")
def bias_20(closes, highs, lows, volumes, **kw):
    ma = closes.rolling(20).mean()
    return (closes - ma) / ma.replace(0, np.nan)


@register("ma_position", "技术指标", "均线位置: 价格在5/10/20/60日均线上方的比例")
def ma_position(closes, highs, lows, volumes, **kw):
    score = pd.Series(0.0, index=closes.index)
    for p in [5, 10, 20, 60]:
        ma = closes.rolling(p).mean()
        score = score + (closes > ma).astype(float)
    return score / 4.0


# ===== 价格形态类 =====

@register("high_low_range", "价格形态", "振幅: (最高-最低)/收盘价的20日均值")
def high_low_range(closes, highs, lows, volumes, **kw):
    daily_range = (highs - lows) / closes.replace(0, np.nan)
    return daily_range.rolling(20).mean()


@register("upper_shadow", "价格形态", "上影线比例: 20日平均上影线/实体")
def upper_shadow(closes, highs, lows, volumes, **kw):
    opens = closes.shift(1)
    body = (closes - opens).abs()
    upper = highs - pd.concat([closes, opens], axis=1).max(axis=1)
    ratio = upper / body.replace(0, np.nan)
    return ratio.rolling(20).mean()

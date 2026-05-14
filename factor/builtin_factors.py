"""系统预置因子库。

分类口径兼容聚宽常见因子分类：成长、质量、情绪、风险、每股指标、
风格、技术、动量、基础/估值。因子实现只使用真实行情与 Tushare
daily_basic 可取得字段；缺少所需数据时返回 NaN 序列，不生成模拟数据。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


FACTOR_CATEGORIES = {
    "basics": {
        "label": "基础/估值",
        "jq_key": "basics",
        "description": "市盈率、市净率、市销率、市值等基础估值类因子。",
        "accent": "#38bdf8",
    },
    "growth": {
        "label": "成长",
        "jq_key": "growth",
        "description": "价格成长、趋势延续和量价扩张等成长代理因子。",
        "accent": "#22c55e",
    },
    "quality": {
        "label": "质量",
        "jq_key": "quality",
        "description": "趋势质量、下行风险、价格稳定性等质量代理因子。",
        "accent": "#a7f3d0",
    },
    "emotion": {
        "label": "情绪",
        "jq_key": "emotion",
        "description": "换手、放量、影线、振幅等市场情绪与拥挤度因子。",
        "accent": "#f59e0b",
    },
    "risk": {
        "label": "风险",
        "jq_key": "risk",
        "description": "波动率、回撤、ATR、尾部波动等风险暴露因子。",
        "accent": "#fb7185",
    },
    "pershare": {
        "label": "每股指标",
        "jq_key": "pershare",
        "description": "基于市值、价格与换手构造的股本/每股口径代理因子。",
        "accent": "#c084fc",
    },
    "style": {
        "label": "风格",
        "jq_key": "style",
        "description": "规模、价值、低波、流动性和复合风格暴露。",
        "accent": "#818cf8",
    },
    "technical": {
        "label": "技术",
        "jq_key": "technical",
        "description": "RSI、BIAS、MACD、布林、VWAP 乖离等技术面因子。",
        "accent": "#60a5fa",
    },
    "momentum": {
        "label": "动量/反转",
        "jq_key": "momentum",
        "description": "短中长期动量、反转、突破与趋势强度因子。",
        "accent": "#f97316",
    },
    "custom": {
        "label": "自定义",
        "jq_key": "custom",
        "description": "用户自定义、AI 生成或工作流生成因子。",
        "accent": "#94a3b8",
    },
}

CATEGORY_ALIASES = {
    "动量": "momentum",
    "反转": "momentum",
    "波动率": "risk",
    "成交量": "emotion",
    "价格形态": "technical",
    "技术指标": "technical",
    "GP挖掘": "custom",
    "LLM挖掘": "custom",
    "因子挖掘": "custom",
    "Alpha191": "technical",
    "workflow": "custom",
    "builtin": "custom",
    "value": "basics",
    "liquidity": "style",
    "size": "style",
}

BUILTIN_FACTORS: dict[str, dict] = {}


def normalize_category(category: str | None) -> str:
    """将历史中文分类或本地扩展分类映射到标准分类键。"""
    key = str(category or "custom").strip()
    return key if key in FACTOR_CATEGORIES else CATEGORY_ALIASES.get(key, "custom")


def category_label(category: str | None) -> str:
    key = normalize_category(category)
    return FACTOR_CATEGORIES.get(key, FACTOR_CATEGORIES["custom"])["label"]


def register(name: str, category: str, desc: str, *, display_name: str | None = None):
    """注册内置因子。name 是稳定计算键，display_name 用于 UI 展示。"""

    def decorator(func):
        category_key = normalize_category(category)
        BUILTIN_FACTORS[name] = {
            "func": func,
            "category": category_key,
            "category_label": category_label(category_key),
            "description": desc,
            "display_name": display_name or name,
            "expression": f"builtin:{name}",
        }
        return func

    return decorator


def _nan_like(base: pd.Series) -> pd.Series:
    return pd.Series(np.nan, index=base.index, dtype=float)


def _num(s, index=None) -> pd.Series:
    if isinstance(s, pd.Series):
        return pd.to_numeric(s, errors="coerce").astype(float)
    return pd.Series(s, index=index, dtype=float)


def _safe_div(a, b) -> pd.Series:
    if isinstance(a, pd.Series):
        left = _num(a)
        right = _num(b, index=left.index)
    elif isinstance(b, pd.Series):
        right = _num(b)
        left = pd.Series(float(a), index=right.index, dtype=float)
    else:
        left = _num(a)
        right = _num(b, index=left.index)
    return left.divide(right.replace(0, np.nan))


def _basic(kw, name: str, base: pd.Series) -> pd.Series:
    data = kw.get("basic_data") or {}
    value = data.get(name)
    if value is None:
        return _nan_like(base)
    return _num(value).reindex(base.index)


def _amount(closes, volumes, kw) -> pd.Series:
    value = kw.get("amounts")
    if value is not None:
        return _num(value).reindex(closes.index)
    return _num(closes) * _num(volumes)


def _opens(closes, kw) -> pd.Series:
    value = kw.get("opens")
    if value is not None:
        return _num(value).reindex(closes.index)
    return closes.shift(1)


def _ret(closes) -> pd.Series:
    return _num(closes).pct_change()


def _log_positive(s) -> pd.Series:
    values = _num(s)
    return np.log(values.where(values > 0))


def _rolling_max_drawdown(closes, window: int) -> pd.Series:
    price = _num(closes)
    peak = price.rolling(window).max()
    return price.divide(peak.replace(0, np.nan)) - 1.0


def _trend_quality(closes, window: int) -> pd.Series:
    returns = _ret(closes)
    total_return = _num(closes).pct_change(window).abs()
    noise = returns.rolling(window).std() * np.sqrt(window)
    return _safe_div(total_return, noise)


def _rsi(closes, window: int) -> pd.Series:
    delta = _num(closes).diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = _safe_div(gain, loss)
    return 100 - 100 / (1 + rs)


def _true_range(closes, highs, lows) -> pd.Series:
    close = _num(closes)
    high = _num(highs)
    low = _num(lows)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


# ===== 基础/估值 =====


@register("valuation_ep_ttm", "basics", "盈利收益率：1 / PE_TTM，PE 越低因子值越高。", display_name="盈利收益率_EP")
def valuation_ep_ttm(closes, highs, lows, volumes, **kw):
    pe = _basic(kw, "pe_ttm", closes)
    if pe.isna().all():
        pe = _basic(kw, "pe", closes)
    return _safe_div(1.0, pe)


@register("valuation_bp", "basics", "账面市值比：1 / PB，偏向低估值资产。", display_name="账面市值比_BP")
def valuation_bp(closes, highs, lows, volumes, **kw):
    return _safe_div(1.0, _basic(kw, "pb", closes))


@register("valuation_sp_ttm", "basics", "销售收益率：1 / PS_TTM，衡量收入相对市值的便宜程度。", display_name="销售收益率_SP")
def valuation_sp_ttm(closes, highs, lows, volumes, **kw):
    ps = _basic(kw, "ps_ttm", closes)
    if ps.isna().all():
        ps = _basic(kw, "ps", closes)
    return _safe_div(1.0, ps)


@register("valuation_low_circ_mv", "basics", "低流通市值：-log(流通市值)，用于捕捉小盘估值风格。", display_name="低流通市值")
def valuation_low_circ_mv(closes, highs, lows, volumes, **kw):
    return -_log_positive(_basic(kw, "circ_mv", closes))


@register("valuation_low_total_mv", "basics", "低总市值：-log(总市值)，偏向规模较小标的。", display_name="低总市值")
def valuation_low_total_mv(closes, highs, lows, volumes, **kw):
    return -_log_positive(_basic(kw, "total_mv", closes))


# ===== 成长 =====


@register("growth_price_60", "growth", "60日价格成长：中期收益率，衡量趋势成长。", display_name="60日价格成长")
def growth_price_60(closes, highs, lows, volumes, **kw):
    return _num(closes).pct_change(60)


@register("growth_vwap_20", "growth", "20日 VWAP 成长：成交均价趋势，降低收盘价噪声。", display_name="20日VWAP成长")
def growth_vwap_20(closes, highs, lows, volumes, **kw):
    vwap = _safe_div(_amount(closes, volumes, kw), volumes)
    return vwap.pct_change(20)


@register("growth_momentum_accel", "growth", "动量加速度：20日收益 - 60日前20日收益，衡量趋势改善。", display_name="动量加速度")
def growth_momentum_accel(closes, highs, lows, volumes, **kw):
    close = _num(closes)
    return close.pct_change(20) - close.shift(20).pct_change(40)


@register("growth_volume_price_confirm", "growth", "量价确认成长：20日涨幅 × 5/20日量比。", display_name="量价确认成长")
def growth_volume_price_confirm(closes, highs, lows, volumes, **kw):
    volume_ratio = _safe_div(_num(volumes).rolling(5).mean(), _num(volumes).rolling(20).mean())
    return _num(closes).pct_change(20) * volume_ratio


# ===== 质量 =====


@register("quality_trend_smooth_60", "quality", "趋势质量：60日绝对收益 / 60日波动，偏向平滑趋势。", display_name="趋势质量_60日")
def quality_trend_smooth_60(closes, highs, lows, volumes, **kw):
    return _trend_quality(closes, 60)


@register("quality_price_stability_60", "quality", "价格稳定性：-60日收益波动率。", display_name="价格稳定性_60日")
def quality_price_stability_60(closes, highs, lows, volumes, **kw):
    return -_ret(closes).rolling(60).std()


@register("quality_downside_control_60", "quality", "下行控制：-60日负收益波动率。", display_name="下行控制_60日")
def quality_downside_control_60(closes, highs, lows, volumes, **kw):
    downside = _ret(closes).where(_ret(closes) < 0, 0.0)
    return -downside.rolling(60).std()


@register("quality_drawdown_control_60", "quality", "回撤控制：60日最大回撤越小因子值越高。", display_name="回撤控制_60日")
def quality_drawdown_control_60(closes, highs, lows, volumes, **kw):
    return -_rolling_max_drawdown(closes, 60).abs()


# ===== 情绪 =====


@register("emotion_turnover_rate", "emotion", "换手率：使用 Tushare daily_basic.turnover_rate。", display_name="换手率")
def emotion_turnover_rate(closes, highs, lows, volumes, **kw):
    return _basic(kw, "turnover_rate", closes)


@register("emotion_turnover_surge_5_20", "emotion", "换手突增：5日换手均值 / 20日换手均值。", display_name="换手突增_5_20")
def emotion_turnover_surge_5_20(closes, highs, lows, volumes, **kw):
    turnover = _basic(kw, "turnover_rate", closes)
    return _safe_div(turnover.rolling(5).mean(), turnover.rolling(20).mean())


@register("emotion_volume_ratio_5_20", "emotion", "量比：5日成交量均值 / 20日成交量均值。", display_name="量比_5_20")
def emotion_volume_ratio_5_20(closes, highs, lows, volumes, **kw):
    vol = _num(volumes)
    return _safe_div(vol.rolling(5).mean(), vol.rolling(20).mean())


@register("emotion_upper_shadow_20", "emotion", "上影线拥挤度：20日平均上影线占实体比例。", display_name="上影线拥挤度")
def emotion_upper_shadow_20(closes, highs, lows, volumes, **kw):
    open_ = _opens(closes, kw)
    close = _num(closes)
    upper = _num(highs) - pd.concat([close, open_], axis=1).max(axis=1)
    body = (close - open_).abs()
    return _safe_div(upper, body).rolling(20).mean()


# ===== 风险 =====


@register("risk_volatility_20", "risk", "20日收益波动率。", display_name="20日波动率")
def risk_volatility_20(closes, highs, lows, volumes, **kw):
    return _ret(closes).rolling(20).std()


@register("risk_volatility_60", "risk", "60日收益波动率。", display_name="60日波动率")
def risk_volatility_60(closes, highs, lows, volumes, **kw):
    return _ret(closes).rolling(60).std()


@register("risk_atr_14", "risk", "14日 ATR / 收盘价，衡量真实波动。", display_name="ATR风险_14日")
def risk_atr_14(closes, highs, lows, volumes, **kw):
    return _safe_div(_true_range(closes, highs, lows).rolling(14).mean(), closes)


@register("risk_max_drawdown_60", "risk", "60日滚动最大回撤。", display_name="最大回撤_60日")
def risk_max_drawdown_60(closes, highs, lows, volumes, **kw):
    return _rolling_max_drawdown(closes, 60)


# ===== 每股指标 =====


@register("pershare_float_share_proxy", "pershare", "流通股本代理：流通市值 / 收盘价，用于衡量流通盘大小。", display_name="流通股本代理")
def pershare_float_share_proxy(closes, highs, lows, volumes, **kw):
    return _safe_div(_basic(kw, "circ_mv", closes), closes)


@register("pershare_total_share_proxy", "pershare", "总股本代理：总市值 / 收盘价。", display_name="总股本代理")
def pershare_total_share_proxy(closes, highs, lows, volumes, **kw):
    return _safe_div(_basic(kw, "total_mv", closes), closes)


@register("pershare_turnover_per_mv", "pershare", "市值换手强度：换手率 / log(流通市值)。", display_name="市值换手强度")
def pershare_turnover_per_mv(closes, highs, lows, volumes, **kw):
    return _safe_div(_basic(kw, "turnover_rate", closes), _log_positive(_basic(kw, "circ_mv", closes)))


# ===== 风格 =====


@register("style_small_value", "style", "小盘价值：低流通市值 + 低 PB 的复合风格。", display_name="小盘价值")
def style_small_value(closes, highs, lows, volumes, **kw):
    return valuation_low_circ_mv(closes, highs, lows, volumes, **kw) + valuation_bp(closes, highs, lows, volumes, **kw)


@register("style_low_vol_value", "style", "低波价值：低波动 + 低 PB 的复合风格。", display_name="低波价值")
def style_low_vol_value(closes, highs, lows, volumes, **kw):
    return -risk_volatility_60(closes, highs, lows, volumes, **kw) + valuation_bp(closes, highs, lows, volumes, **kw)


@register("style_momentum_low_vol", "style", "动量低波：60日动量 - 60日波动。", display_name="动量低波")
def style_momentum_low_vol(closes, highs, lows, volumes, **kw):
    return _num(closes).pct_change(60) - risk_volatility_60(closes, highs, lows, volumes, **kw)


@register("style_liquidity_compensation", "style", "低流动性补偿：-20日成交额均值的对数。", display_name="低流动性补偿")
def style_liquidity_compensation(closes, highs, lows, volumes, **kw):
    amt = _amount(closes, volumes, kw).rolling(20).mean()
    return -_log_positive(amt)


# ===== 技术 =====


@register("technical_rsi_14", "technical", "14日 RSI，衡量短期超买超卖。", display_name="RSI_14")
def technical_rsi_14(closes, highs, lows, volumes, **kw):
    return _rsi(closes, 14)


@register("technical_bias_20", "technical", "20日乖离率：(收盘价 - MA20) / MA20。", display_name="BIAS_20")
def technical_bias_20(closes, highs, lows, volumes, **kw):
    ma = _num(closes).rolling(20).mean()
    return _safe_div(_num(closes) - ma, ma)


@register("technical_ma_position", "technical", "均线位置：站上 5/10/20/60 日均线的比例。", display_name="均线多头排列")
def technical_ma_position(closes, highs, lows, volumes, **kw):
    close = _num(closes)
    score = pd.Series(0.0, index=close.index)
    for window in [5, 10, 20, 60]:
        score = score + (close > close.rolling(window).mean()).astype(float)
    return score / 4.0


@register("technical_macd_diff", "technical", "MACD DIF：EMA12 - EMA26。", display_name="MACD_DIF")
def technical_macd_diff(closes, highs, lows, volumes, **kw):
    close = _num(closes)
    return close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()


@register("technical_boll_z_20", "technical", "布林 Z 值：(收盘价 - MA20) / STD20。", display_name="布林Z值_20")
def technical_boll_z_20(closes, highs, lows, volumes, **kw):
    close = _num(closes)
    return _safe_div(close - close.rolling(20).mean(), close.rolling(20).std())


@register("technical_vwap_deviation_20", "technical", "VWAP 乖离：收盘价相对 20日 VWAP 均值偏离。", display_name="VWAP乖离_20")
def technical_vwap_deviation_20(closes, highs, lows, volumes, **kw):
    vwap = _safe_div(_amount(closes, volumes, kw), volumes)
    vwap_ma = vwap.rolling(20).mean()
    return _safe_div(_num(closes) - vwap_ma, vwap_ma)


# ===== 动量/反转 =====


@register("momentum_5", "momentum", "5日动量：短期收益率。", display_name="5日动量")
def momentum_5(closes, highs, lows, volumes, **kw):
    return _num(closes).pct_change(5)


@register("momentum_20", "momentum", "20日动量：中短期收益率。", display_name="20日动量")
def momentum_20(closes, highs, lows, volumes, **kw):
    return _num(closes).pct_change(20)


@register("momentum_60", "momentum", "60日动量：中期收益率。", display_name="60日动量")
def momentum_60(closes, highs, lows, volumes, **kw):
    return _num(closes).pct_change(60)


@register("momentum_120", "momentum", "120日动量：长期收益率。", display_name="120日动量")
def momentum_120(closes, highs, lows, volumes, **kw):
    return _num(closes).pct_change(120)


@register("reversal_5", "momentum", "5日反转：短期过度反应修正，取 5日收益率相反数。", display_name="5日反转")
def reversal_5(closes, highs, lows, volumes, **kw):
    return -_num(closes).pct_change(5)


@register("reversal_20", "momentum", "20日反转：中短期反向信号。", display_name="20日反转")
def reversal_20(closes, highs, lows, volumes, **kw):
    return -_num(closes).pct_change(20)


@register("momentum_breakout_60", "momentum", "60日突破强度：收盘价相对 60日最高价的位置。", display_name="60日突破强度")
def momentum_breakout_60(closes, highs, lows, volumes, **kw):
    high_60 = _num(highs).rolling(60).max()
    low_60 = _num(lows).rolling(60).min()
    return _safe_div(_num(closes) - low_60, high_60 - low_60)


# ===== 历史内置因子兼容别名 =====


@register("volatility_20", "risk", "兼容旧版：20日收益波动率。", display_name="20日波动率_旧版")
def volatility_20(closes, highs, lows, volumes, **kw):
    return risk_volatility_20(closes, highs, lows, volumes, **kw)


@register("atr_14", "risk", "兼容旧版：14日 ATR / 收盘价。", display_name="ATR_14_旧版")
def atr_14(closes, highs, lows, volumes, **kw):
    return risk_atr_14(closes, highs, lows, volumes, **kw)


@register("volume_ratio_5", "emotion", "兼容旧版：当日成交量 / 5日均量。", display_name="5日量比_旧版")
def volume_ratio_5(closes, highs, lows, volumes, **kw):
    return _safe_div(_num(volumes), _num(volumes).rolling(5).mean())


@register("volume_momentum", "emotion", "兼容旧版：5日均量 / 20日均量。", display_name="成交量动量_旧版")
def volume_momentum(closes, highs, lows, volumes, **kw):
    vol = _num(volumes)
    return _safe_div(vol.rolling(5).mean(), vol.rolling(20).mean())


@register("rsi_14", "technical", "兼容旧版：14日 RSI。", display_name="RSI_14_旧版")
def rsi_14(closes, highs, lows, volumes, **kw):
    return technical_rsi_14(closes, highs, lows, volumes, **kw)


@register("bias_20", "technical", "兼容旧版：20日乖离率。", display_name="BIAS_20_旧版")
def bias_20(closes, highs, lows, volumes, **kw):
    return technical_bias_20(closes, highs, lows, volumes, **kw)


@register("ma_position", "technical", "兼容旧版：均线位置。", display_name="均线位置_旧版")
def ma_position(closes, highs, lows, volumes, **kw):
    return technical_ma_position(closes, highs, lows, volumes, **kw)


@register("high_low_range", "technical", "兼容旧版：20日平均振幅。", display_name="振幅_旧版")
def high_low_range(closes, highs, lows, volumes, **kw):
    return _safe_div(_num(highs) - _num(lows), closes).rolling(20).mean()


@register("upper_shadow", "emotion", "兼容旧版：20日平均上影线比例。", display_name="上影线_旧版")
def upper_shadow(closes, highs, lows, volumes, **kw):
    return emotion_upper_shadow_20(closes, highs, lows, volumes, **kw)

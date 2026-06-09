"""因子服务。"""
import asyncio
import inspect
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from database.models import Factor, FactorResult, Strategy
from factor.factor_engine import FactorEngine
from factor.genetic import run_gp
from services.backtest_service import resolve_backtest_universe
from services.settings_service import get_or_create_settings


def build_factor_compute_code(factor: Factor) -> str:
    """生成可复制的因子计算函数代码。"""
    expression = str(factor.expression or "")
    factor_name = str(factor.name or f"factor_{factor.id}")
    builtin_name = expression[8:] if expression.startswith("builtin:") else ""
    builtin_source = ""
    if builtin_name:
        try:
            from factor.builtin_factors import BUILTIN_FACTORS

            func = BUILTIN_FACTORS.get(builtin_name, {}).get("func")
            if func:
                builtin_source = inspect.getsource(func).strip()
        except Exception:
            builtin_source = ""

    builtin_note = ""
    if builtin_source:
        builtin_note = f'''

# 内置因子原始实现，供阅读和复核：
{builtin_source}
'''

    return f'''"""
QTsys 因子计算代码
因子ID: {factor.id}
因子名称: {factor_name}
因子表达式: {expression}

说明：
1. calculate_factor(cache, ts_code, start_date, end_date) 与系统因子引擎一致，适合在 QTsys 项目内直接复用。
2. calculate_factor_from_daily_frame(df, daily_basic=None) 适合已拿到单只股票行情 DataFrame 时直接计算。
3. df 至少需要包含 trade_date/open/high/low/close/vol/amount 字段；daily_basic 可选，用于 pe/pb/ps/市值/换手率类因子。
"""
import numpy as np
import pandas as pd

from factor.factor_engine import FactorEngine
from factor.builtin_factors import BUILTIN_FACTORS


FACTOR_ID = {factor.id!r}
FACTOR_NAME = {factor_name!r}
FACTOR_EXPRESSION = {expression!r}


def calculate_factor(cache, ts_code, start_date, end_date):
    """使用 QTsys DataCache + FactorEngine 计算单只股票的完整因子时间序列。"""
    engine = FactorEngine(cache)
    return engine.compute_factor_values(FACTOR_EXPRESSION, ts_code, start_date, end_date)


def _daily_basic_to_dict(daily_basic, trade_index):
    """将 Tushare daily_basic 结果对齐为 FactorEngine 需要的字段字典。"""
    if daily_basic is None or daily_basic.empty:
        return {{}}
    basic = daily_basic.copy()
    basic["trade_date"] = pd.to_datetime(basic["trade_date"], errors="coerce")
    basic = basic.dropna(subset=["trade_date"]).set_index("trade_date").sort_index()
    aligned = basic.reindex(trade_index).ffill()
    result = {{}}
    for col in ["pe", "pe_ttm", "pb", "ps", "ps_ttm", "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f"]:
        if col in aligned.columns:
            result[col] = pd.to_numeric(aligned[col], errors="coerce")
    return result


def calculate_factor_from_daily_frame(df, daily_basic=None):
    """基于已加载行情 DataFrame 计算因子值，返回 pandas.Series，索引为交易日。"""
    if df is None or df.empty:
        return pd.Series(dtype=float)

    data = df.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce")
    data = data.dropna(subset=["trade_date"]).sort_values("trade_date").set_index("trade_date")

    closes = pd.to_numeric(data["close"], errors="coerce")
    highs = pd.to_numeric(data["high"], errors="coerce")
    lows = pd.to_numeric(data["low"], errors="coerce")
    volumes = pd.to_numeric(data["vol"], errors="coerce")
    opens = pd.to_numeric(data["open"], errors="coerce") if "open" in data.columns else closes.shift(1)
    amounts = pd.to_numeric(data["amount"], errors="coerce") if "amount" in data.columns else volumes * closes

    if FACTOR_EXPRESSION.startswith("builtin:"):
        factor_key = FACTOR_EXPRESSION[8:]
        info = BUILTIN_FACTORS.get(factor_key)
        if not info:
            raise ValueError(f"unknown builtin factor: {{factor_key}}")
        basic_data = _daily_basic_to_dict(daily_basic, data.index)
        return info["func"](
            closes,
            highs,
            lows,
            volumes,
            opens=opens,
            amounts=amounts,
            basic_data=basic_data,
        )

    basic_data = _daily_basic_to_dict(daily_basic, data.index)
    engine = FactorEngine(cache=None)
    result = engine._eval_expression(
        FACTOR_EXPRESSION,
        closes,
        highs,
        lows,
        volumes,
        opens,
        basic_data,
        amounts,
    )
    return result if result is not None else pd.Series(dtype=float)
{builtin_note}'''


def build_joinquant_backtest_code(
    factor: Factor,
    *,
    index_symbol: str = "000300.XSHG",
    benchmark: str = "000300.XSHG",
    start_date: str = "",
    end_date: str = "",
    top_n: int = 10,
    rebalance_days: int = 5,
    lookback: int = 260,
    high_is_better: bool = True,
    target_exposure: float = 0.95,
    max_position_pct: float = 0.12,
    min_trade_lot: int = 100,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    include_star_market: bool = False,
    exclude_star_market: bool | None = None,
    exclude_st: bool = True,
    slippage: float = 0.002,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    min_commission: float = 5.0,
    universe_codes: list[str] | None = None,
    universe_as_of_date: str = "",
) -> str:
    """生成可直接复制到聚宽的因子截面选股回测代码。"""
    expression = str(factor.expression or "")
    factor_name = str(factor.name or f"factor_{factor.id}")
    top_n = max(1, min(int(top_n or 10), 200))
    rebalance_days = max(1, min(int(rebalance_days or 5), 60))
    lookback = max(60, min(int(lookback or 260), 1200))
    target_exposure = max(0.0, min(float(target_exposure or 0.95), 1.0))
    max_position_pct = max(0.0, min(float(max_position_pct or 0.12), 1.0))
    min_trade_lot = max(100, int(min_trade_lot or 100))
    stop_loss_pct = max(0.0, min(float(stop_loss_pct or 0.0), 0.95))
    take_profit_pct = max(0.0, min(float(take_profit_pct or 0.0), 10.0))
    if exclude_star_market is not None:
        include_star_market = not bool(exclude_star_market)
    include_star_market = bool(include_star_market)
    exclude_st = bool(exclude_st)
    slippage = max(0.0, min(float(slippage or 0.0), 0.05))
    commission_rate = max(0.0, min(float(commission_rate or 0.0), 0.05))
    stamp_tax_rate = max(0.0, min(float(stamp_tax_rate or 0.0), 0.05))
    min_commission = max(0.0, min(float(min_commission or 0.0), 100.0))
    index_symbol = str(index_symbol or "000300.XSHG").strip()
    benchmark = str(benchmark or index_symbol or "000300.XSHG").strip()
    start_date = str(start_date or "").strip()
    end_date = str(end_date or "").strip()
    universe_as_of_date = str(universe_as_of_date or start_date or "").strip()

    def _to_jq_code(code: str) -> str:
        text = str(code or "").strip().upper()
        if text.endswith(".SH"):
            return text[:6] + ".XSHG"
        if text.endswith(".SZ"):
            return text[:6] + ".XSHE"
        return text

    fixed_universe = []
    for code in universe_codes or []:
        jq_code = _to_jq_code(code)
        if re.match(r"^\d{6}\.XS(HG|HE)$", jq_code) and jq_code not in fixed_universe:
            fixed_universe.append(jq_code)
    template = r'''
# -*- coding: utf-8 -*-
"""
QTsys -> JoinQuant 因子回测模板
因子名称: __FACTOR_NAME__
因子表达式: __FACTOR_EXPRESSION__

使用方法：
1. 将本文件完整复制到聚宽策略编辑器。
2. 默认只做多 A 股，所有开仓和平仓都按 100 股整数倍处理，避免小于一手的订单失败。
3. 如需反向验证，将 g.high_is_better 改为 False。
4. 本模板只使用调仓日前已存在的历史数据；调仓在 run_daily(open) 中执行，避免使用未来函数。
"""
try:
    from jqdata import *
except Exception:
    pass

import numpy as np
import pandas as pd


FACTOR_ID = __FACTOR_ID__
FACTOR_NAME = __FACTOR_NAME_REPR__
FACTOR_EXPRESSION = __FACTOR_EXPRESSION_REPR__


def initialize(context):
    set_benchmark(__BENCHMARK_REPR__)
    set_option('use_real_price', True)
    set_slippage(FixedSlippage(__SLIPPAGE__))
    try:
        log.set_level('order', 'error')
    except Exception:
        pass

    set_order_cost(OrderCost(
        open_tax=0,
        close_tax=__STAMP_TAX_RATE__,
        open_commission=__COMMISSION_RATE__,
        close_commission=__COMMISSION_RATE__,
        close_today_commission=0,
        min_commission=__MIN_COMMISSION__
    ), type='stock')

    g.index_symbol = __INDEX_SYMBOL_REPR__  # 000300.XSHG 沪深300；000016.XSHG 上证50；000905.XSHG 中证500
    g.top_n = __TOP_N__                     # 持仓股票数量
    g.lookback = __LOOKBACK__               # 单股历史数据窗口，复杂长窗口因子可调大
    g.rebalance_days = __REBALANCE_DAYS__   # 调仓间隔
    g.high_is_better = __HIGH_IS_BETTER__   # False 表示因子值越小越好
    g.max_position_pct = __MAX_POSITION_PCT__  # 单股最大权重
    g.target_exposure = __TARGET_EXPOSURE__    # 总股票仓位
    g.min_trade_lot = __MIN_TRADE_LOT__        # A股一手=100股，低于一手不下单
    g.stop_loss_pct = __STOP_LOSS_PCT__        # 0 表示关闭止损；如 0.08 表示亏损8%止损
    g.take_profit_pct = __TAKE_PROFIT_PCT__    # 0 表示关闭止盈；如 0.20 表示盈利20%止盈
    g.include_star_market = __INCLUDE_STAR_MARKET__  # 默认过滤科创板，避免市价单保护限价问题
    g.fixed_universe = __FIXED_UNIVERSE_REPR__
    g.universe_as_of_date = __UNIVERSE_AS_OF_DATE_REPR__
    g.day_count = 0

    run_daily(rebalance, time='open')


def rebalance(context):
    apply_risk_controls(context)

    g.day_count += 1
    if g.day_count % g.rebalance_days != 1:
        return

    stocks = get_strategy_universe()
    stocks = filter_tradable_stocks(stocks)
    scores = score_universe(stocks, context)
    if len(scores) == 0:
        log.info('本次调仓无有效因子值')
        return

    scores = sorted(scores, key=lambda item: item[1], reverse=g.high_is_better)
    selected = [stock for stock, value in scores[:max(1, int(g.top_n))]]
    target_weight = min(g.max_position_pct, g.target_exposure / max(len(selected), 1))
    target_value = context.portfolio.total_value * target_weight

    current_positions = list(context.portfolio.positions.keys())
    for stock in current_positions:
        if stock not in selected:
            safe_order_target_value(context, stock, 0)

    for stock in selected:
        safe_order_target_value(context, stock, target_value)

    log.info('调仓完成：%s' % ','.join(selected))


def get_strategy_universe():
    if getattr(g, 'fixed_universe', None):
        return list(g.fixed_universe)
    try:
        if getattr(g, 'universe_as_of_date', ''):
            return get_index_stocks(g.index_symbol, date=g.universe_as_of_date)
    except Exception:
        pass
    return get_index_stocks(g.index_symbol)


def apply_risk_controls(context):
    """可选止盈止损；只做多，不开空。"""
    if g.stop_loss_pct <= 0 and g.take_profit_pct <= 0:
        return
    for stock, position in list(context.portfolio.positions.items()):
        amount = int(getattr(position, 'total_amount', 0) or 0)
        if amount < g.min_trade_lot:
            continue
        avg_cost = float(getattr(position, 'avg_cost', 0) or 0)
        price = float(getattr(position, 'price', 0) or 0)
        if avg_cost <= 0 or price <= 0:
            continue
        ret = price / avg_cost - 1.0
        if g.stop_loss_pct > 0 and ret <= -g.stop_loss_pct:
            safe_order_target_value(context, stock, 0)
            log.info('%s 触发止损 %.2f%%' % (stock, ret * 100))
        elif g.take_profit_pct > 0 and ret >= g.take_profit_pct:
            safe_order_target_value(context, stock, 0)
            log.info('%s 触发止盈 %.2f%%' % (stock, ret * 100))


def safe_order_target_value(context, stock, target_value):
    """A股安全下单：避免开仓/平仓数量小于100导致聚宽报错。"""
    current_data = get_current_data()
    try:
        price = float(current_data[stock].last_price)
    except Exception:
        price = 0.0
    if price <= 0 or np.isnan(price):
        try:
            price = float(attribute_history(stock, 1, '1d', ['close'], skip_paused=True, df=True, fq='pre')['close'].iloc[-1])
        except Exception:
            return
    if price <= 0 or np.isnan(price):
        return

    if stock in context.portfolio.positions:
        position = context.portfolio.positions[stock]
        current_amount = int(getattr(position, 'total_amount', 0) or 0)
    else:
        position = None
        current_amount = 0
    target_amount = int(float(target_value) / price / g.min_trade_lot) * g.min_trade_lot
    target_amount = max(0, target_amount)
    delta_amount = target_amount - current_amount

    if target_amount == 0:
        if current_amount >= g.min_trade_lot:
            order_target(stock, 0)
        return

    if abs(delta_amount) < g.min_trade_lot:
        return

    if is_star_market(stock):
        limit_price = round(price * 1.02, 2) if target_amount > current_amount else round(price * 0.98, 2)
        order_target(stock, target_amount, style=LimitOrderStyle(limit_price))
    else:
        order_target(stock, target_amount)


def filter_tradable_stocks(stocks):
    current_data = get_current_data()
    result = []
    for stock in stocks:
        try:
            if (not g.include_star_market) and is_star_market(stock):
                continue
            data = current_data[stock]
            name = getattr(data, 'name', '') or ''
            if data.paused or data.is_st or ('ST' in name) or ('*' in name) or ('退' in name):
                continue
            result.append(stock)
        except Exception:
            continue
    return result


def is_star_market(stock):
    """科创板证券代码通常以 688 开头。"""
    return str(stock).startswith('688')


def score_universe(stocks, context):
    rows = []
    for stock in stocks:
        value = calculate_latest_factor_value(stock, context)
        if value is None:
            continue
        try:
            value = float(value)
        except Exception:
            continue
        if np.isfinite(value):
            rows.append((stock, value))
    return rows


def calculate_latest_factor_value(stock, context):
    end_date = getattr(context, 'previous_date', None)
    df = None
    try:
        df = get_price(
            stock,
            end_date=end_date,
            count=int(g.lookback),
            frequency='daily',
            fields=['open', 'high', 'low', 'close', 'volume', 'money'],
            skip_paused=True,
            fq='pre',
            panel=False,
        )
    except Exception:
        df = None
    if df is None or df.empty:
        df = attribute_history(
            stock,
            count=int(g.lookback),
            unit='1d',
            fields=['open', 'high', 'low', 'close', 'volume', 'money'],
            skip_paused=True,
            df=True,
            fq='pre'
        )
    if df is None or df.empty or len(df) < 30:
        return None

    daily = df.copy()
    daily['trade_date'] = pd.to_datetime(daily.index)
    daily = daily.rename(columns={'volume': 'vol', 'money': 'amount'})
    valuation_data = load_valuation_history(stock, daily['trade_date'])
    series = calculate_factor_from_daily_frame(daily, valuation_data)
    if series is None or len(series.dropna()) == 0:
        return None
    return series.dropna().iloc[-1]


def load_valuation_history(stock, trade_dates):
    """尽量拉取聚宽估值时序；失败时返回空字典，纯量价因子不受影响。"""
    result = {}
    if trade_dates is None or len(trade_dates) == 0:
        return result
    end_date = pd.to_datetime(trade_dates.iloc[-1]).date()
    count = len(trade_dates)
    field_map = {
        'pe': 'pe_ratio',
        'pe_ttm': 'pe_ratio',
        'pb': 'pb_ratio',
        'ps': 'ps_ratio',
        'ps_ttm': 'ps_ratio',
        'total_mv': 'market_cap',
        'circ_mv': 'circulating_market_cap',
        'turnover_rate': 'turnover_ratio',
        'turnover_rate_f': 'turnover_ratio',
    }
    raw = None
    try:
        q = query(
            valuation.code,
            valuation.pe_ratio,
            valuation.pb_ratio,
            valuation.ps_ratio,
            valuation.market_cap,
            valuation.circulating_market_cap,
            valuation.turnover_ratio
        ).filter(valuation.code == stock)
        raw = get_fundamentals_continuously(q, end_date=end_date, count=count, panel=False)
    except Exception:
        raw = None

    if raw is not None and not raw.empty:
        frame = raw.copy()
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.reset_index()
        date_col = 'day' if 'day' in frame.columns else ('date' if 'date' in frame.columns else None)
        if date_col is not None:
            frame[date_col] = pd.to_datetime(frame[date_col], errors='coerce')
            frame = frame.dropna(subset=[date_col]).set_index(date_col).sort_index()
            aligned_index = pd.to_datetime(trade_dates)
            for target, source in field_map.items():
                if source in frame.columns:
                    result[target] = pd.to_numeric(frame[source], errors='coerce').reindex(aligned_index).ffill()
            return result

    try:
        q = query(
            valuation.code,
            valuation.pe_ratio,
            valuation.pb_ratio,
            valuation.ps_ratio,
            valuation.market_cap,
            valuation.circulating_market_cap,
            valuation.turnover_ratio
        ).filter(valuation.code == stock)
        snapshot = get_fundamentals(q, date=end_date)
        if snapshot is not None and not snapshot.empty:
            idx = pd.to_datetime(trade_dates)
            row = snapshot.iloc[0]
            for target, source in field_map.items():
                if source in snapshot.columns:
                    result[target] = pd.Series(float(row[source]) if pd.notna(row[source]) else np.nan, index=idx)
    except Exception:
        pass
    return result


def calculate_factor_from_daily_frame(df, valuation_data=None):
    data = df.copy()
    data['trade_date'] = pd.to_datetime(data['trade_date'], errors='coerce')
    data = data.dropna(subset=['trade_date']).sort_values('trade_date').set_index('trade_date')

    close = pd.to_numeric(data['close'], errors='coerce')
    high = pd.to_numeric(data['high'], errors='coerce')
    low = pd.to_numeric(data['low'], errors='coerce')
    open_ = pd.to_numeric(data['open'], errors='coerce') if 'open' in data.columns else close.shift(1)
    volume = pd.to_numeric(data['vol'], errors='coerce') if 'vol' in data.columns else pd.to_numeric(data['volume'], errors='coerce')
    amount = pd.to_numeric(data['amount'], errors='coerce') if 'amount' in data.columns else volume * close

    if FACTOR_EXPRESSION.startswith('builtin:'):
        return calc_builtin_factor(FACTOR_EXPRESSION[8:], close, high, low, volume)

    return eval_expression(FACTOR_EXPRESSION, close, high, low, open_, volume, amount, valuation_data or {})


def calc_builtin_factor(name, close, high, low, volume):
    if name == 'momentum_5':
        return close.pct_change(5)
    if name == 'momentum_20':
        return close.pct_change(20)
    if name == 'momentum_60':
        return close.pct_change(60)
    if name == 'reversal_5':
        return -close.pct_change(5)
    if name == 'reversal_20':
        return -close.pct_change(20)
    if name == 'volatility_20':
        return close.pct_change().rolling(20).std()
    if name == 'atr_14':
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(14).mean() / close.replace(0, np.nan)
    if name == 'volume_ratio_5':
        return volume / volume.rolling(5).mean().replace(0, np.nan)
    if name == 'volume_momentum':
        return volume.rolling(5).mean() / volume.rolling(20).mean().replace(0, np.nan)
    if name == 'rsi_14':
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - 100 / (1 + rs)
    if name == 'bias_20':
        ma = close.rolling(20).mean()
        return (close - ma) / ma.replace(0, np.nan)
    if name == 'ma_position':
        score = pd.Series(0.0, index=close.index)
        for period in [5, 10, 20, 60]:
            score += (close > close.rolling(period).mean()).astype(float)
        return score / 4.0
    if name == 'high_low_range':
        return ((high - low) / close.replace(0, np.nan)).rolling(20).mean()
    if name == 'upper_shadow':
        open_proxy = close.shift(1)
        body = (close - open_proxy).abs()
        upper = high - pd.concat([close, open_proxy], axis=1).max(axis=1)
        return (upper / body.replace(0, np.nan)).rolling(20).mean()
    raise ValueError('unknown builtin factor: %s' % name)


def eval_expression(expr, close, high, low, open_, volume, amount, valuation_data):
    empty = pd.Series(np.nan, index=close.index)
    pe = valuation_data.get('pe_ttm', valuation_data.get('pe', empty))
    pb = valuation_data.get('pb', empty)
    ps = valuation_data.get('ps_ttm', valuation_data.get('ps', empty))
    total_mv = valuation_data.get('total_mv', empty)
    circ_mv = valuation_data.get('circ_mv', empty)
    turnover_rate = valuation_data.get('turnover_rate', empty)

    def _ts_rank_func(x):
        n = len(x)
        if n == 0 or np.isnan(x[-1]):
            return np.nan
        return np.sum(x <= x[-1]) / float(n)

    def _expanding_rank_pct(s):
        result = []
        values = []
        for value in pd.Series(s).values:
            values.append(value)
            valid = [item for item in values if pd.notna(item)]
            if not valid or pd.isna(value):
                result.append(np.nan)
            else:
                result.append(float(np.sum(np.array(valid) <= value) / len(valid)))
        return pd.Series(result, index=s.index)

    def _expanding_zscore(s):
        mean = s.expanding(min_periods=2).mean()
        std = s.expanding(min_periods=2).std()
        return ((s - mean) / std.replace(0, np.nan)).fillna(0.0)

    def _wma(s, window):
        weights = np.arange(1, int(window) + 1, dtype=float)
        return s.rolling(int(window)).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

    def _decaylinear(s, window):
        weights = np.arange(int(window), 0, -1, dtype=float)
        return s.rolling(int(window)).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

    def _sma(s, window, weight):
        result = s.copy()
        alpha = float(weight) / float(window)
        for i in range(1, len(s)):
            if pd.notna(s.iloc[i]) and pd.notna(result.iloc[i - 1]):
                result.iloc[i] = alpha * s.iloc[i] + (1 - alpha) * result.iloc[i - 1]
        return result

    def _ema(s, span):
        return s.ewm(span=int(span), adjust=False).mean()

    def _macd_dif(s, fast=12, slow=26):
        return _ema(s, int(fast)) - _ema(s, int(slow))

    def _macd_dea(s, fast=12, slow=26, signal=9):
        return _ema(_macd_dif(s, int(fast), int(slow)), int(signal))

    def _macd_hist(s, fast=12, slow=26, signal=9):
        dif = _macd_dif(s, int(fast), int(slow))
        dea = _ema(dif, int(signal))
        return dif - dea

    def _rsi(s, window=14):
        delta = s.diff()
        gain = delta.clip(lower=0).rolling(int(window)).mean()
        loss = (-delta.clip(upper=0)).rolling(int(window)).mean()
        return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    def _cross_up(left, right):
        return ((left > right) & (left.shift(1) <= right.shift(1))).astype(float)

    def _cross_down(left, right):
        return ((left < right) & (left.shift(1) >= right.shift(1))).astype(float)

    def _bars_since(event):
        values = []
        last_seen = None
        for idx, flag in enumerate(pd.Series(event, index=close.index).fillna(0).astype(float).values):
            if flag > 0:
                last_seen = idx
                values.append(0.0)
            elif last_seen is None:
                values.append(np.nan)
            else:
                values.append(float(idx - last_seen))
        return pd.Series(values, index=close.index)

    def _count_true(event, window):
        return (pd.Series(event, index=close.index).fillna(0).astype(float) > 0).rolling(int(window)).sum()

    def _zscore(s, window):
        mean = s.rolling(int(window)).mean()
        std = s.rolling(int(window)).std()
        return (s - mean) / std.replace(0, np.nan)

    def _vol_zscore(window):
        return _zscore(volume.astype(float), int(window))

    def _gap_pct():
        return open_ / close.shift(1).replace(0, np.nan) - 1

    def _breakout(s, window):
        prev_high = s.rolling(int(window)).max().shift(1)
        return ((s > prev_high) & prev_high.notna()).astype(float)

    def _drawdown_from_high(s, window):
        peak = s.rolling(int(window)).max()
        return s / peak.replace(0, np.nan) - 1

    def _skew(s, window):
        return s.rolling(int(window)).skew()

    def _kurt(s, window):
        return s.rolling(int(window)).kurt()

    def _downside_std(s, window):
        downside = s.where(s < 0, 0.0)
        return downside.rolling(int(window)).std()

    def _upside_std(s, window):
        upside = s.where(s > 0, 0.0)
        return upside.rolling(int(window)).std()

    def _efficiency_ratio(s, window):
        window = int(window)
        direction = (s - s.shift(window)).abs()
        path = s.diff().abs().rolling(window).sum()
        return direction / path.replace(0, np.nan)

    def _regbeta(x, y, window):
        values = []
        window = int(window)
        for i in range(len(x)):
            if i < window - 1:
                values.append(np.nan)
                continue
            xa = x.iloc[i - window + 1:i + 1].values
            ya = y.iloc[i - window + 1:i + 1].values
            var = np.nanvar(xa)
            values.append(np.nan if var <= 0 else np.nan_to_num(np.cov(xa, ya)[0, 1] / var))
        return pd.Series(values, index=x.index)

    def _regresi(x, y, window):
        beta = _regbeta(x, y, window)
        alpha = y.rolling(int(window)).mean() - beta * x.rolling(int(window)).mean()
        return y - (alpha + beta * x)

    vwap = amount / volume.replace(0, np.nan)
    returns = close.pct_change()
    dtm = pd.Series(np.where(open_ <= open_.shift(1), 0, np.maximum(high - open_, open_ - open_.shift(1))), index=open_.index)
    dbm = pd.Series(np.where(open_ >= open_.shift(1), 0, np.maximum(open_ - low, open_ - open_.shift(1))), index=open_.index)
    tr = pd.Series(np.maximum(np.maximum(high - low, np.abs(high - close.shift(1))), np.abs(low - close.shift(1))), index=close.index)
    hd = high - high.shift(1)
    ld = low.shift(1) - low

    safe_ns = {
        'np': np, 'pd': pd,
        'close': close, 'high': high, 'low': low, 'open': open_,
        'vol': volume, 'volume': volume, 'amount': amount, 'vwap': vwap,
        'returns': returns, 'dtm': dtm, 'dbm': dbm, 'tr': tr, 'hd': hd, 'ld': ld,
        'pe': pe, 'pb': pb, 'ps': ps, 'total_mv': total_mv, 'circ_mv': circ_mv, 'turnover_rate': turnover_rate,
        'abs': np.abs, 'log': np.log, 'sqrt': np.sqrt, 'sign': np.sign,
        'floor': np.floor, 'ceil': np.ceil, 'round_val': np.round,
        'power': lambda s, exp: np.power(s, exp),
        'signedpower': lambda s, exp: np.sign(s) * np.power(np.abs(s), exp),
        'neg': lambda s: -s,
        'max': np.maximum, 'min': np.minimum,
        'clip': lambda s, lower, upper: s.clip(lower=lower, upper=upper),
        'mean': lambda s, n: s.rolling(int(n)).mean(),
        'std': lambda s, n: s.rolling(int(n)).std(),
        'sum': lambda s, n: s.rolling(int(n)).sum(),
        'rank': _expanding_rank_pct,
        'delay': lambda s, n: s.shift(int(n)),
        'delta': lambda s, n: s.diff(int(n)),
        'pctchange': lambda s, n: s.pct_change(int(n)),
        'corr': lambda a, b, n: a.rolling(int(n)).corr(b),
        'cov': lambda a, b, n: a.rolling(int(n)).cov(b),
        'ts_max': lambda s, n: s.rolling(int(n)).max(),
        'ts_min': lambda s, n: s.rolling(int(n)).min(),
        'ts_rank': lambda s, n: s.rolling(int(n)).apply(_ts_rank_func, raw=True),
        'ema': _ema,
        'macd_dif': _macd_dif,
        'macd_dea': _macd_dea,
        'macd_hist': _macd_hist,
        'rsi': _rsi,
        'wma': _wma, 'decaylinear': _decaylinear, 'sma': _sma,
        'regbeta': _regbeta, 'regresi': _regresi,
        'scale': lambda s: s / np.abs(s).expanding(min_periods=1).sum().replace(0, np.nan),
        'cs_rank': _expanding_rank_pct,
        'cs_zscore': _expanding_zscore,
        'cs_percentile': _expanding_rank_pct,
        'cs_demean': lambda s: s - s.expanding(min_periods=1).mean(),
        'indneutralize': lambda s: s - s.expanding(min_periods=1).mean(),
        'advm': lambda s, n: s.rolling(int(n)).mean(),
        'where': lambda cond, t, f: pd.Series(np.where(cond, t, f), index=close.index),
        'ternary': lambda cond, t, f: pd.Series(np.where(cond, t, f), index=close.index),
        'cross_up': _cross_up,
        'cross_down': _cross_down,
        'bars_since': _bars_since,
        'count_true': _count_true,
        'zscore': _zscore,
        'vol_zscore': _vol_zscore,
        'gap_pct': _gap_pct,
        'breakout': _breakout,
        'drawdown_from_high': _drawdown_from_high,
        'skew': _skew,
        'kurt': _kurt,
        'downside_std': _downside_std,
        'upside_std': _upside_std,
        'efficiency_ratio': _efficiency_ratio,
        'ts_argmax': lambda s, n: s.rolling(int(n)).apply(lambda x: x.argmax(), raw=True),
        'ts_argmin': lambda s, n: s.rolling(int(n)).apply(lambda x: x.argmin(), raw=True),
        'ts_product': lambda s, n: s.rolling(int(n)).apply(lambda x: np.prod(x), raw=True),
        'highday': lambda s, n: s.rolling(int(n)).apply(lambda x: int(n) - 1 - x.argmax(), raw=True),
        'lowday': lambda s, n: s.rolling(int(n)).apply(lambda x: int(n) - 1 - x.argmin(), raw=True),
        'sequence': lambda length: pd.Series(range(1, int(length) + 1)),
        'sumif': lambda s, condition, n: (s * condition).rolling(int(n)).sum(),
    }
    try:
        result = eval(expr, {'__builtins__': {}}, safe_ns)
    except Exception as exc:
        log.info('因子表达式计算失败: %s' % exc)
        return pd.Series(dtype=float)
    if isinstance(result, pd.Series):
        return result
    return pd.Series(result, index=close.index)
'''
    return (
        template
        .replace("__FACTOR_ID__", repr(factor.id))
        .replace("__FACTOR_NAME__", factor_name)
        .replace("__FACTOR_NAME_REPR__", repr(factor_name))
        .replace("__FACTOR_EXPRESSION__", expression)
        .replace("__FACTOR_EXPRESSION_REPR__", repr(expression))
        .replace("__INDEX_SYMBOL_REPR__", repr(index_symbol))
        .replace("__BENCHMARK_REPR__", repr(benchmark))
        .replace("__TOP_N__", repr(top_n))
        .replace("__LOOKBACK__", repr(lookback))
        .replace("__REBALANCE_DAYS__", repr(rebalance_days))
        .replace("__HIGH_IS_BETTER__", "True" if high_is_better else "False")
        .replace("__MAX_POSITION_PCT__", repr(round(max_position_pct, 6)))
        .replace("__TARGET_EXPOSURE__", repr(round(target_exposure, 6)))
        .replace("__MIN_TRADE_LOT__", repr(min_trade_lot))
        .replace("__STOP_LOSS_PCT__", repr(round(stop_loss_pct, 6)))
        .replace("__TAKE_PROFIT_PCT__", repr(round(take_profit_pct, 6)))
        .replace("__INCLUDE_STAR_MARKET__", "True" if include_star_market else "False")
        .replace("__SLIPPAGE__", repr(round(slippage, 6)))
        .replace("__COMMISSION_RATE__", repr(round(commission_rate, 6)))
        .replace("__STAMP_TAX_RATE__", repr(round(stamp_tax_rate, 6)))
        .replace("__MIN_COMMISSION__", repr(round(min_commission, 4)))
        .replace("__START_DATE_REPR__", repr(start_date))
        .replace("__END_DATE_REPR__", repr(end_date))
        .replace("__EXCLUDE_ST__", "True" if exclude_st else "False")
        .replace("__FIXED_UNIVERSE_REPR__", repr(fixed_universe))
        .replace("__UNIVERSE_AS_OF_DATE_REPR__", repr(universe_as_of_date))
    )


async def evaluate_factor_workflow(
    db: AsyncSession,
    *,
    factor_id: int,
    universe: list[str],
    start_date: str,
    end_date: str,
    groups: int,
    forward_days: int,
):
    factor_result = await db.execute(select(Factor).where(Factor.id == factor_id))
    factor = factor_result.scalar_one_or_none()
    if not factor:
        raise LookupError("因子不存在")

    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    eval_result = await asyncio.to_thread(
        engine.evaluate,
        factor.expression,
        universe,
        start_date,
        end_date,
        groups,
        forward_days,
    )
    if "error" in eval_result:
        raise ValueError(eval_result["error"])

    result = FactorResult(
        factor_id=factor.id,
        factor_name=factor.name,
        universe=",".join(universe),
        start_date=start_date,
        end_date=end_date,
        metrics=eval_result["metrics"],
        ic_series=eval_result["ic_series"],
        group_returns=eval_result["group_returns"],
        turnover_series=eval_result["turnover_series"],
        long_short_curve=eval_result["long_short_curve"],
    )
    db.add(result)
    await db.commit()
    return eval_result


async def mine_gp_workflow(
    db: AsyncSession,
    *,
    universe: list[str],
    start_date: str,
    end_date: str,
    pop_size: int,
    generations: int,
):
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    results = await asyncio.to_thread(
        run_gp,
        engine,
        universe,
        start_date,
        end_date,
        pop_size,
        generations,
    )

    saved = []
    for index, item in enumerate(results, start=1):
        name = f"GP因子_{index}"
        factor = Factor(
            name=name,
            description=f"遗传算法挖掘 (适应度 {item['fitness']})",
            expression=item["expression"],
            category="GP挖掘",
            source="gp",
        )
        db.add(factor)
        saved.append({"name": name, **item})
    await db.commit()
    return {"factors": saved}


def _compact_date(value: str) -> str:
    return str(value or "").replace("-", "").strip()[:8]


def _lookback_start(end_date: str, days: int = 420) -> str:
    try:
        dt = datetime.strptime(_compact_date(end_date), "%Y%m%d")
    except Exception:
        dt = datetime.now()
    return (dt - timedelta(days=days)).strftime("%Y%m%d")


def build_factor_strategy_code(
    factor_id: int,
    factor_name: str,
    direction: str = "top",
    *,
    select_pct: float = 0.1,
    max_positions: int = 0,
    rebalance_days: int = 5,
    target_exposure: float = 0.95,
    max_position_pct: float = 0.12,
) -> str:
    safe_name = str(factor_name or f"Factor {factor_id}").replace('"""', "")
    reverse = "True" if direction != "bottom" else "False"
    select_pct = max(0.001, min(float(select_pct or 0.1), 1.0))
    max_positions = max(0, int(max_positions or 0))
    rebalance_days = max(1, min(int(rebalance_days or 5), 60))
    target_exposure = max(0.0, min(float(target_exposure or 0.95), 1.0))
    max_position_pct = max(0.0, min(float(max_position_pct or 0.12), 1.0))
    return f'''def initialize(context):
    """基于因子库因子自动生成的选股策略：{safe_name}"""
    context.factor_ref = "id:{factor_id}"
    context.max_positions = {max_positions}
    context.select_pct = {select_pct!r}
    context.rebalance_days = {rebalance_days}
    context.factor_direction_high_is_better = {reverse}
    context.target_exposure = {target_exposure!r}
    context.max_position_pct = {max_position_pct!r}
    context.day_count = 0

def handle_data(context):
    context.day_count += 1
    if context.day_count % context.rebalance_days != 1:
        return

    scores = []
    for ts_code in context.universe:
        value = context.get_factor(context.factor_ref, ts_code, default=None)
        if value is None:
            continue
        try:
            value = float(value)
        except Exception:
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            scores.append((ts_code, value))

    if not scores:
        return

    scores.sort(key=lambda item: item[1], reverse=context.factor_direction_high_is_better)
    if context.max_positions and context.max_positions > 0:
        select_count = max(1, int(context.max_positions))
    else:
        select_count = max(1, int(len(scores) * float(context.select_pct)))
    selected = [code for code, _ in scores[:select_count]]
    target_weight = min(float(context.target_exposure) / max(len(selected), 1), float(context.max_position_pct))

    for ts_code in list(context.positions.keys()):
        if ts_code not in selected:
            context.order_target_percent(ts_code, 0)

    for ts_code in selected:
        context.order_target_percent(ts_code, target_weight)
'''


async def create_strategy_from_factor_workflow(
    db: AsyncSession,
    *,
    factor_id: int,
    direction: str = "top",
) -> dict:
    factor_result = await db.execute(select(Factor).where(Factor.id == factor_id))
    factor = factor_result.scalar_one_or_none()
    if not factor:
        raise LookupError("因子不存在")

    mining_meta = {}
    if isinstance(factor.graph_json, dict):
        mining_meta = factor.graph_json.get("mining") or {}
    mined_direction = str(mining_meta.get("direction") or "").strip().lower()
    requested_direction = str(direction or "").strip().lower()
    if mined_direction in {"top", "bottom"}:
        effective_direction = mined_direction
    elif requested_direction in {"top", "bottom"}:
        effective_direction = requested_direction
    else:
        effective_direction = "top"
    select_pct = float(mining_meta.get("select_pct") or 0.1)
    rebalance_days = int(mining_meta.get("rebalance_days") or 5)
    target_exposure = float(mining_meta.get("target_exposure") or 0.95)
    max_position_pct = float(mining_meta.get("max_position_pct") or 0.12)
    benchmark = str(mining_meta.get("benchmark") or mining_meta.get("benchmark_code") or "000300.SH")
    start_date = str(mining_meta.get("session_start_date") or mining_meta.get("display_start") or mining_meta.get("test_start") or "")
    end_date = str(mining_meta.get("session_end_date") or mining_meta.get("display_end") or mining_meta.get("test_end") or "")
    universe_type = str(mining_meta.get("universe_type") or "")
    universe_code = str(mining_meta.get("universe_code") or "")
    universe_name = str(mining_meta.get("universe_name") or "")
    custom_pool_id = mining_meta.get("custom_pool_id")
    name = f"因子策略_{factor.id}_{factor.name}"[:100]
    code = build_factor_strategy_code(
        factor.id,
        factor.name,
        direction=effective_direction,
        select_pct=select_pct,
        rebalance_days=rebalance_days,
        target_exposure=target_exposure,
        max_position_pct=max_position_pct,
    )
    direction_text = "低值组占优" if effective_direction == "bottom" else "高值组占优"
    description = (
        f"由因子库自动生成，使用因子 {factor.name} ({factor.expression}) 进行截面排序选股。"
        f"方向：{direction_text}；选股比例：{select_pct:.4f}；调仓周期：{rebalance_days}日。"
    )
    existing_result = await db.execute(select(Strategy).where(Strategy.name == name))
    strategy = existing_result.scalar_one_or_none()
    if strategy:
        strategy.description = description
        strategy.code = code
        strategy.updated_at = datetime.utcnow()
    else:
        strategy = Strategy(
            name=name,
            description=description,
            code=code,
        )
        db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return {
        "id": strategy.id,
        "name": strategy.name,
        "factor_id": factor.id,
        "factor_name": factor.name,
        "direction": effective_direction,
        "select_pct": select_pct,
        "rebalance_days": rebalance_days,
        "target_exposure": target_exposure,
        "max_position_pct": max_position_pct,
        "backtest_context": {
            "source": "mined_factor_strategy",
            "source_label": "因子挖掘策略",
            "factor_id": factor.id,
            "factor_name": factor.name,
            "direction": effective_direction,
            "select_pct": select_pct,
            "rebalance_days": rebalance_days,
            "target_exposure": target_exposure,
            "max_position_pct": max_position_pct,
            "benchmark": benchmark,
            "universe_type": universe_type,
            "universe_code": universe_code,
            "universe_name": universe_name,
            "custom_pool_id": custom_pool_id,
            "start_date": start_date,
            "end_date": end_date,
            "stock_items": [],
        },
    }


async def rank_factor_cross_section_workflow(
    db: AsyncSession,
    *,
    factor_id: int,
    trade_date: str,
    universe_type: str,
    universe_code: str,
    custom_pool_id: int | None,
    n: int,
) -> dict:
    factor_result = await db.execute(select(Factor).where(Factor.id == factor_id))
    factor = factor_result.scalar_one_or_none()
    if not factor:
        raise LookupError("因子不存在")

    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    resolved = await resolve_backtest_universe(
        db,
        settings,
        universe_type=universe_type or "system",
        universe_code=universe_code or "000300.SH",
        custom_pool_id=custom_pool_id,
        as_of_date=trade_date,
    )
    codes = list(resolved.get("codes") or [])[:800]
    if not codes:
        raise ValueError("股票池为空")

    date_text = _compact_date(trade_date) or datetime.now().strftime("%Y%m%d")
    start_date = _lookback_start(date_text)
    n = max(1, min(int(n or 10), 100))

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    def _compute():
        name_map = {}
        try:
            stock_basic = client.get_stock_basic()
            if stock_basic is not None and not stock_basic.empty:
                for row in stock_basic.to_dict("records"):
                    name_map[str(row.get("ts_code") or "").upper()] = str(row.get("name") or "")
        except Exception:
            name_map = {}

        rows = []
        for code in codes:
            try:
                series = engine.compute_factor_values(factor.expression, code, start_date, date_text)
            except Exception:
                continue
            if series is None or series.dropna().empty:
                continue
            valid = series.dropna()
            valid = valid[valid.index <= datetime.strptime(date_text, "%Y%m%d")]
            if valid.empty:
                continue
            value = float(valid.iloc[-1])
            if value != value:
                continue
            rows.append({
                "ts_code": code,
                "name": name_map.get(str(code).upper(), ""),
                "factor_value": round(value, 8),
                "factor_date": valid.index[-1].strftime("%Y%m%d") if hasattr(valid.index[-1], "strftime") else str(valid.index[-1]),
            })
        rows.sort(key=lambda item: item["factor_value"], reverse=True)
        return rows

    rows = await asyncio.to_thread(_compute)
    return {
        "factor": {
            "id": factor.id,
            "name": factor.name,
            "expression": factor.expression,
        },
        "trade_date": date_text,
        "universe": resolved,
        "total_count": len(rows),
        "top": rows[:n],
        "bottom": list(reversed(rows[-n:])) if rows else [],
    }

"""Factor mining workflow.

The mining path is deliberately bounded: it generates executable factor
expressions, evaluates them on real cached market data, runs a next-bar
selection backtest, and then tests simple ensembles of the strongest factors.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from database.connection import async_session
from database.models import (
    Factor,
    FactorCorrelationCluster,
    FactorMiningCandidate,
    FactorMiningSession,
    FactorMiningTrialLog,
    StockPool,
)
from factor.factor_engine import FactorEngine
from factor.genetic import run_gp
from factor.graph_compiler import NODE_REGISTRY
from services.backtest_service import resolve_backtest_universe
from services.factor_board_service import get_system_universes
from services.settings_service import get_or_create_settings


STRICT_MINING_PROTOCOL_VERSION = "institutional_factor_lab_v4"
DEFAULT_TARGET_EXPOSURE = 0.95
DEFAULT_MAX_POSITION_PCT = 0.12
DEFAULT_VOLUME_LIMIT_PCT = 0.10
DEFAULT_LIMIT_UP_PCT = 0.095
DEFAULT_LIMIT_DOWN_PCT = 0.095
DEFAULT_WALK_FORWARD_WINDOWS = 3
DEFAULT_NEUTRALIZE_MODE = "rank_zscore"
DEFAULT_EMBARGO_DAYS = 5
DEFAULT_MIN_DSR = -0.25


SYSTEM_UNIVERSES = [
    {"code": "000016.SH", "name": "上证50", "benchmark_code": "000016.SH"},
    {"code": "000300.SH", "name": "沪深300", "benchmark_code": "000300.SH"},
    {"code": "000905.SH", "name": "中证500", "benchmark_code": "000905.SH"},
    {"code": "000001.SH", "name": "上证指数", "benchmark_code": "000001.SH"},
]


BASE_TEMPLATES = [
    {
        "name": "20日反转",
        "expression": "-(pctchange(close, 20))",
        "description": "过去20日涨幅越低，因子值越高，捕捉短期均值回归。",
    },
    {
        "name": "20日动量",
        "expression": "pctchange(close, 20)",
        "description": "过去20日涨幅越高，因子值越高，捕捉趋势延续。",
    },
    {
        "name": "低波动",
        "expression": "-std(returns, 20)",
        "description": "近20日收益波动越低，因子值越高，偏向稳健资产。",
    },
    {
        "name": "量能扩张",
        "expression": "mean(vol, 5) / (mean(vol, 20) + 0.0001)",
        "description": "短期成交量相对中期成交量放大，刻画资金关注度变化。",
    },
    {
        "name": "布林位置",
        "expression": "(close - mean(close, 20)) / (std(close, 20) + 0.0001)",
        "description": "价格相对20日均线的标准化偏离，适合识别突破或过热。",
    },
    {
        "name": "区间强弱",
        "expression": "(close - ts_min(low, 20)) / (ts_max(high, 20) - ts_min(low, 20) + 0.0001)",
        "description": "收盘价在近20日高低区间中的位置，衡量阶段强势程度。",
    },
    {
        "name": "价量相关",
        "expression": "corr(close, vol, 10)",
        "description": "近10日价量相关性，衡量上涨是否获得成交确认。",
    },
    {
        "name": "流动性反转",
        "expression": "-rank(mean(vol, 20))",
        "description": "对高成交拥挤度做反向处理，用于降低热门拥挤风险。",
    },
]


@dataclass
class Candidate:
    name: str
    expression: str
    description: str
    source: str = "template"
    theme: str = ""
    hypothesis: str = ""


def _date_text(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    return str(value)[:10].replace("-", "")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default


def _safe_pct_return(start_value: float, end_value: float) -> float:
    start_value = _safe_float(start_value)
    end_value = _safe_float(end_value)
    if start_value <= 0 or end_value <= 0:
        return 0.0
    return (end_value / start_value - 1.0) * 100.0


def _annualized_return(total_return_pct: float, point_count: int) -> float:
    if point_count <= 1:
        return 0.0
    ratio = 1.0 + _safe_float(total_return_pct) / 100.0
    if ratio <= 0:
        return 0.0
    return (math.pow(ratio, 252 / max(point_count, 1)) - 1.0) * 100.0


def _max_drawdown_pct(curve: list[dict]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for point in curve or []:
        val = _safe_float(point.get("value"))
        peak = max(peak, val)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - val) / peak)
    return max_drawdown * 100.0


def _curve_value_map(curve: list[dict]) -> dict[str, float]:
    return {str(item.get("date")): _safe_float(item.get("value")) for item in curve or []}


def _build_excess_curve(strategy_curve: list[dict], benchmark_curve: list[dict]) -> list[dict]:
    strategy = _curve_value_map(_normalize_curve(strategy_curve))
    benchmark = _curve_value_map(_normalize_curve(benchmark_curve))
    result = []
    for date in sorted(set(strategy) & set(benchmark)):
        result.append({"date": date, "value": round(1.0 + strategy[date] - benchmark[date], 6)})
    return result


def _curve_metrics(curve: list[dict], initial_cash: float) -> dict:
    if not curve:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0}
    final_value = _safe_float(curve[-1].get("value"), initial_cash)
    total_return = _safe_pct_return(initial_cash, final_value)
    return {
        "total_return": round(total_return, 2),
        "annual_return": round(_annualized_return(total_return, len(curve)), 2),
        "max_drawdown": round(_max_drawdown_pct(curve), 2),
    }


def _normalize_curve(equity_curve: list[dict]) -> list[dict]:
    if not equity_curve:
        return []
    base = _safe_float(equity_curve[0].get("value"), 0.0)
    if base <= 0:
        return []
    return [
        {"date": item["date"], "value": round(_safe_float(item.get("value")) / base, 6)}
        for item in equity_curve
    ]


async def get_factor_mining_options(db: AsyncSession) -> dict:
    result = await db.execute(select(StockPool).order_by(StockPool.updated_at.desc(), StockPool.id.desc()))
    custom_pools = []
    for pool in result.scalars().all():
        items = pool.stock_items or []
        custom_pools.append(
            {
                "id": pool.id,
                "name": pool.name,
                "description": pool.description or "",
                "stock_count": len(items),
                "stock_items": items,
            }
        )
    system_map = {item["code"]: item for item in get_system_universes()}
    for item in SYSTEM_UNIVERSES:
        system_map[item["code"]] = item
    return {
        "system_universes": list(system_map.values()),
        "custom_pools": custom_pools,
        "factor_themes": [
            {
                "name": theme["name"],
                "source": theme["source"],
                "hypothesis": theme["hypothesis"],
                "template_count": len(theme["templates"]),
            }
            for theme in PROFESSIONAL_FACTOR_THEMES
        ],
        "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
    }


async def resolve_mining_universe(
    db: AsyncSession,
    settings,
    *,
    universe_type: str,
    universe_code: str,
    custom_pool_id: Optional[int],
    max_stocks: int,
    as_of_date: Optional[str] = None,
) -> dict:
    universe_type = (universe_type or "system").lower()
    universe_code = (universe_code or "000016.SH").upper()
    max_stocks = max(5, min(int(max_stocks or 50), 500))

    if universe_type == "custom":
        resolved = await resolve_backtest_universe(
            db,
            settings,
            universe_type="custom",
            custom_pool_id=custom_pool_id,
            as_of_date=as_of_date,
        )
        resolved["codes"] = resolved["codes"][:max_stocks]
        return resolved

    if universe_code == "000001.SH":
        client = TushareClient(settings.tushare_token)
        stock_basic = await asyncio.to_thread(client.get_stock_basic)
        if stock_basic is None or stock_basic.empty:
            raise ValueError("上证指数股票池暂时无法获取成分股")
        frame = stock_basic.copy()
        frame["ts_code"] = frame["ts_code"].astype(str).str.upper()
        codes = frame[frame["ts_code"].str.endswith(".SH")]["ts_code"].head(max_stocks).tolist()
        if len(codes) < 5:
            raise ValueError("上证指数股票池有效股票不足")
        return {
            "codes": codes,
            "universe_type": "system",
            "universe_code": universe_code,
            "universe_name": "上证指数",
            "universe_label": f"上证指数 ({len(codes)}只)",
        }

    meta = next((item for item in SYSTEM_UNIVERSES if item["code"] == universe_code), None)
    resolved = await resolve_backtest_universe(
        db,
        settings,
        universe_type="system",
        universe_code=universe_code,
        universe_name=(meta or {}).get("name", universe_code),
        as_of_date=as_of_date,
    )
    resolved["codes"] = resolved["codes"][:max_stocks]
    resolved["universe_label"] = f"{resolved.get('universe_name') or universe_code} ({len(resolved['codes'])}只)"
    return resolved


def _generate_template_candidates(limit: int) -> list[Candidate]:
    candidates = [Candidate(**item) for item in BASE_TEMPLATES]
    for theme in PROFESSIONAL_FACTOR_THEMES:
        for item in theme["templates"]:
            candidates.append(
                Candidate(
                    name=item["name"],
                    expression=item["expression"],
                    description=item["description"],
                    source=theme["source"],
                    theme=theme["name"],
                    hypothesis=theme["hypothesis"],
                )
            )
    windows = [2, 3, 5, 8, 10, 11, 15, 20, 30, 60, 120]
    generated: list[Candidate] = []
    for window in windows:
        generated.extend(
            [
                Candidate(
                    f"{window}d_reversal",
                    f"-(pctchange(close, {window}))",
                    f"Short-term reversal over {window} trading days.",
                    "param_scan",
                    "short_reversal",
                    "Short-term overreaction tends to mean-revert.",
                ),
                Candidate(
                    f"{window}d_momentum",
                    f"pctchange(close, {window})",
                    f"Trend continuation over {window} trading days.",
                    "param_scan",
                    "medium_momentum",
                    "Persistent trend continuation can carry expected return.",
                ),
                Candidate(
                    f"{window}d_low_vol",
                    f"-std(returns, {window})",
                    f"Lower volatility over {window} days can be rewarded.",
                    "param_scan",
                    "low_volatility",
                    "Low-volatility exposure may improve risk-adjusted returns.",
                ),
                Candidate(
                    f"{window}d_volume_price_corr",
                    f"-corr(pctchange(close, 1), pctchange(vol, 1), {window})",
                    f"Negative price-volume correlation over {window} days.",
                    "param_scan",
                    "volume_price_divergence",
                    "Price rallies without volume confirmation often fade.",
                ),
            ]
        )
    seen = set()
    result: list[Candidate] = []
    for item in candidates + generated:
        if item.expression in seen:
            continue
        seen.add(item.expression)
        result.append(item)
        if len(result) >= limit:
            break
    return result


PROFESSIONAL_FACTOR_THEMES = [
    {
        "name": "short_reversal",
        "source": "research_seed",
        "hypothesis": "短期过度反应在交易成本和行为偏差下容易出现均值回归。",
        "templates": [
            {"name": "short_reversal_price", "expression": "-pctchange(close, 5)", "description": "五日短期反转信号，捕捉急涨急跌后的修正。"},
            {"name": "short_reversal_intraday", "expression": "-(close / (open + 0.0001) - 1)", "description": "当日收开盘反向信号，适合捕捉日内过冲。"},
        ],
    },
    {
        "name": "medium_momentum",
        "source": "research_seed",
        "hypothesis": "中期趋势延续与资金定价滞后有关，常在机构资金主导阶段表现更稳定。",
        "templates": [
            {"name": "medium_momentum_20", "expression": "pctchange(close, 20)", "description": "二十日动量因子。"},
            {"name": "medium_momentum_60", "expression": "pctchange(close, 60)", "description": "六十日中期趋势信号。"},
        ],
    },
    {
        "name": "low_volatility",
        "source": "research_seed",
        "hypothesis": "低波动股票在风险约束资金中更容易获得配置溢价。",
        "templates": [
            {"name": "low_vol_20", "expression": "-std(returns, 20)", "description": "二十日波动率反向暴露。"},
            {"name": "low_range_20", "expression": "-(ts_max(high, 20) - ts_min(low, 20))", "description": "高低振幅越小，得分越高。"},
        ],
    },
    {
        "name": "volume_price_divergence",
        "source": "research_seed",
        "hypothesis": "价量背离代表趋势动能不足，容易在后续收益中表现弱化。",
        "templates": [
            {"name": "vp_corr_20", "expression": "-corr(pctchange(close, 1), pctchange(vol, 1), 20)", "description": "价格与成交量变动负相关。"},
            {"name": "vp_breakout_20", "expression": "(close - mean(close, 20)) / (std(close, 20) + 0.0001)", "description": "价格突破均值后偏离程度。"},
        ],
    },
    {
        "name": "liquidity_compression",
        "source": "research_seed",
        "hypothesis": "流动性收缩后若伴随价格变化，可能代表资金介入的早期阶段。",
        "templates": [
            {"name": "liquidity_compression_20", "expression": "-std(vol, 20) / (mean(vol, 20) + 0.0001)", "description": "成交量压缩信号。"},
            {"name": "turnover_spike_20", "expression": "mean(vol, 5) / (mean(vol, 20) + 0.0001)", "description": "短期成交量放大。"},
        ],
    },
    {
        "name": "fundamental_value",
        "source": "research_seed",
        "hypothesis": "估值与质量因子体现横截面风险补偿，但稳定性依赖中性化和财务披露时点。",
        "templates": [
            {"name": "value_pe", "expression": "-rank(pe)", "description": "低市盈率优选。"},
            {"name": "value_pb", "expression": "-rank(pb)", "description": "低市净率优选。"},
            {"name": "quality_turnover", "expression": "-rank(turnover_rate)", "description": "低换手拥挤度优选。"},
        ],
    },
]


def _walk_forward_splits(
    dates: list,
    windows: int = DEFAULT_WALK_FORWARD_WINDOWS,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
) -> list[dict[str, set]]:
    if not dates:
        return []
    dates = list(dates)
    windows = max(2, int(windows or 2))
    folds = []
    train_min = max(20, len(dates) // (windows + 1))
    for idx in range(windows):
        train_end = min(len(dates) - 3, train_min + idx * max(3, len(dates) // (windows + 2)))
        embargo = max(0, int(embargo_days or 0))
        valid_start = min(len(dates), train_end + embargo)
        valid_end = min(len(dates) - 2, valid_start + max(3, len(dates) // (windows + 3)))
        test_start = min(len(dates), valid_end + embargo)
        test_end = min(len(dates), test_start + max(3, len(dates) // (windows + 4)))
        if valid_end <= valid_start or test_end <= test_start:
            continue
        folds.append(
            {
                "train": set(dates[:train_end]),
                "valid": set(dates[valid_start:valid_end]),
                "test": set(dates[test_start:test_end]),
                "all": set(dates[:test_end]),
                "embargo_days": embargo,
            }
        )
    if folds:
        return folds
    midpoint = max(1, len(dates) // 2)
    second = max(midpoint + 1, len(dates) * 3 // 4)
    return [{
        "train": set(dates[:midpoint]),
        "valid": set(dates[midpoint:second]),
        "test": set(dates[second:]),
        "all": set(dates),
    }]


def _safe_p_value_from_t(t_stat: float) -> float:
    t = abs(_safe_float(t_stat))
    return float(math.erfc(t / math.sqrt(2.0)))


def _factor_preprocess_single(series: pd.Series, method: str = "rank_zscore") -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if values.dropna().empty:
        return values
    q1 = values.quantile(0.01)
    q99 = values.quantile(0.99)
    values = values.clip(lower=q1, upper=q99)
    if method in {"rank", "rank_zscore"}:
        values = values.rank(pct=True)
    if method in {"zscore", "rank_zscore"}:
        std = values.std(ddof=0)
        if std > 0:
            values = (values - values.mean()) / std
    return values


def _format_research_hypothesis(theme: str, description: str) -> str:
    return f"[{theme}] {description}".strip()
def _score_metrics(metrics: dict) -> float:
    ic_mean = abs(_safe_float(metrics.get("ic_mean")))
    ic_ir = abs(_safe_float(metrics.get("ic_ir")))
    ls_ret = _safe_float(metrics.get("long_short_return"))
    mono = _safe_float(metrics.get("monotonicity"))
    turnover = max(_safe_float(metrics.get("avg_turnover")), 0.0)
    turnover_penalty = min(turnover / 200.0, 0.5)
    return round(ic_mean * 3.0 + min(ic_ir, 2.0) * 0.35 + max(ls_ret, 0.0) / 100.0 + mono * 0.25 - turnover_penalty, 6)


def _preload_market(cache: DataCache, universe: list[str], start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
    market = {}
    for code in universe:
        df = cache.get_daily(code, start_date, end_date)
        if df is None or df.empty or len(df) < 40:
            continue
        market[code] = df.sort_values("trade_date").reset_index(drop=True)
    return market


def _run_factor_selection_backtest(
    cache: DataCache,
    engine: FactorEngine,
    expression: str,
    universe: list[str],
    start_date: str,
    end_date: str,
    *,
    select_pct: float,
    rebalance_days: int,
    initial_cash: float,
    direction: str,
    market: Optional[dict[str, pd.DataFrame]] = None,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage: float = 0.001,
) -> dict:
    market = market or _preload_market(cache, universe, start_date, end_date)
    factors = {}
    closes = {}
    opens = {}
    for code, df in market.items():
        series = engine.compute_factor_values(expression, code, start_date, end_date)
        if series is None or series.dropna().empty:
            continue
        indexed = df.set_index("trade_date")
        factors[code] = series
        closes[code] = indexed["close"]
        opens[code] = indexed["open"] if "open" in indexed.columns else indexed["close"]

    if len(factors) < 5:
        return {"error": "有效股票不足，无法回测"}

    all_dates = sorted(set().union(*[set(item.index) for item in closes.values()]))
    if len(all_dates) <= rebalance_days + 2:
        return {"error": "交易日不足，无法回测"}

    cash = float(initial_cash)
    holdings: dict[str, int] = {}
    equity_curve = []
    daily_returns = []
    trades = []
    prev_value = initial_cash
    rebalance_set = set(range(0, len(all_dates) - 1, max(1, rebalance_days)))
    reverse = direction != "bottom"
    commission_rate = max(0.0, float(commission_rate or 0.0))
    stamp_tax_rate = max(0.0, float(stamp_tax_rate or 0.0))
    slippage = max(0.0, float(slippage or 0.0))

    for idx, dt in enumerate(all_dates):
        if idx > 0 and (idx - 1) in rebalance_set:
            signal_date = all_dates[idx - 1]
            exec_date = dt
            scores = {}
            for code, series in factors.items():
                if signal_date in series.index:
                    value = series.get(signal_date)
                    if pd.notna(value):
                        scores[code] = float(value)
            if len(scores) >= 5:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=reverse)
                n_select = max(1, int(len(ranked) * select_pct))
                selected = [item[0] for item in ranked[:n_select]]

                for code in list(holdings):
                    if code in selected:
                        continue
                    price = opens.get(code, pd.Series(dtype=float)).get(exec_date)
                    shares = holdings.get(code, 0)
                    if shares > 0 and price and price > 0:
                        sell_price = float(price) * (1 - slippage)
                        cash += shares * sell_price * (1 - commission_rate - stamp_tax_rate)
                        trades.append({"date": _date_text(exec_date), "code": code, "action": "sell", "price": round(sell_price, 4), "shares": shares})
                    holdings.pop(code, None)

                portfolio_value = cash
                for code, shares in holdings.items():
                    price = opens.get(code, pd.Series(dtype=float)).get(exec_date)
                    if price and price > 0:
                        portfolio_value += shares * float(price)
                target_value = portfolio_value / max(len(selected), 1)

                for code in selected:
                    price = opens.get(code, pd.Series(dtype=float)).get(exec_date)
                    if not price or price <= 0:
                        continue
                    buy_price = float(price) * (1 + slippage)
                    sell_price = float(price) * (1 - slippage)
                    target_shares = int(target_value / buy_price / 100) * 100
                    current = holdings.get(code, 0)
                    diff = target_shares - current
                    if diff > 0:
                        cost = diff * buy_price * (1 + commission_rate)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = target_shares
                            trades.append({"date": _date_text(exec_date), "code": code, "action": "buy", "price": round(buy_price, 4), "shares": diff})
                    elif diff < 0:
                        cash += abs(diff) * sell_price * (1 - commission_rate - stamp_tax_rate)
                        holdings[code] = target_shares
                        trades.append({"date": _date_text(exec_date), "code": code, "action": "sell", "price": round(sell_price, 4), "shares": abs(diff)})

        value = cash
        for code, shares in holdings.items():
            price = closes.get(code, pd.Series(dtype=float)).get(dt)
            if price and price > 0:
                value += shares * float(price)
        daily_returns.append((value - prev_value) / prev_value if prev_value > 0 else 0.0)
        equity_curve.append({"date": _date_text(dt), "value": round(value, 2)})
        prev_value = value

    final_value = equity_curve[-1]["value"] if equity_curve else initial_cash
    total_return = (final_value / initial_cash - 1) * 100 if initial_cash > 0 else 0.0
    annual_return = 0.0
    if initial_cash > 0 and final_value > 0 and len(equity_curve) > 1:
        annual_return = (math.pow(final_value / initial_cash, 252 / max(len(equity_curve), 1)) - 1) * 100
    max_drawdown = 0.0
    peak = initial_cash
    for point in equity_curve:
        val = _safe_float(point.get("value"))
        peak = max(peak, val)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - val) / peak)
    daily_arr = np.array(daily_returns, dtype=float)
    sharpe = float(np.mean(daily_arr) / np.std(daily_arr, ddof=1) * math.sqrt(252)) if len(daily_arr) > 2 and np.std(daily_arr, ddof=1) > 0 else 0.0
    return {
        "metrics": {
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return, 2),
            "max_drawdown": round(max_drawdown * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "trade_count": len(trades),
        },
        "equity_curve": equity_curve,
        "normalized_curve": _normalize_curve(equity_curve),
        "trades": trades[-200:],
    }


def _build_combinations(top_items: list[dict], limit: int = 4) -> list[Candidate]:
    result = []
    for idx in range(min(len(top_items), 4)):
        for jdx in range(idx + 1, min(len(top_items), 5)):
            left = top_items[idx]["expression"]
            right = top_items[jdx]["expression"]
            result.append(
                Candidate(
                    name=f"组合因子 {idx + 1}-{jdx + 1}",
                    expression=f"(rank({left}) + rank({right})) / 2",
                    description="由两个入选因子等权线性组合，目标是提升稳定性。",
                    source="ensemble",
                )
            )
            if len(result) >= limit:
                return result
    return result


def _mine_sync(
    cache: DataCache,
    engine: FactorEngine,
    universe: list[str],
    start_date: str,
    end_date: str,
    *,
    candidate_count: int,
    gp_generations: int,
    gp_population: int,
    select_pct: float,
    rebalance_days: int,
    initial_cash: float,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage: float = 0.001,
) -> dict:
    random.seed(42)
    market = _preload_market(cache, universe, start_date, end_date)
    valid_universe = list(market.keys())
    if len(valid_universe) < 5:
        raise ValueError("可用行情股票不足，请检查股票池和日期区间")

    candidates = _generate_template_candidates(max(candidate_count, 6))
    if gp_generations > 0 and gp_population > 0:
        gp_results = run_gp(engine, valid_universe, start_date, end_date, pop_size=gp_population, generations=gp_generations, top_n=min(6, candidate_count))
        for idx, item in enumerate(gp_results, start=1):
            candidates.append(
                Candidate(
                    name=f"遗传挖掘 {idx}",
                    expression=item["expression"],
                    description=f"遗传搜索生成，适应度 {item.get('fitness')}",
                    source="gp",
                )
            )

    seen = set()
    unique_candidates = []
    for item in candidates:
        if item.expression in seen:
            continue
        seen.add(item.expression)
        unique_candidates.append(item)

    evaluated = []
    for candidate in unique_candidates[: max(candidate_count + 6, candidate_count)]:
        evaluation = engine.evaluate(candidate.expression, valid_universe, start_date, end_date, groups=5, forward_days=5)
        if "error" in evaluation:
            continue
        metrics = evaluation.get("metrics", {})
        direction = "bottom" if _safe_float(metrics.get("ic_mean")) < 0 or _safe_float(metrics.get("long_short_return")) < 0 else "top"
        backtest = _run_factor_selection_backtest(
            cache,
            engine,
            candidate.expression,
            valid_universe,
            start_date,
            end_date,
            select_pct=select_pct,
            rebalance_days=rebalance_days,
            initial_cash=initial_cash,
            direction=direction,
            market=market,
            commission_rate=commission_rate,
            stamp_tax_rate=stamp_tax_rate,
            slippage=slippage,
        )
        if "error" in backtest:
            continue
        evaluated.append(
            {
                "name": candidate.name,
                "description": candidate.description,
                "expression": candidate.expression,
                "source": candidate.source,
                "direction": direction,
                "score": _score_metrics(metrics) + max(_safe_float(backtest["metrics"].get("total_return")), 0) / 100.0,
                "metrics": metrics,
                "backtest_metrics": backtest["metrics"],
                "long_short_curve": evaluation.get("long_short_curve", []),
                "equity_curve": backtest["equity_curve"],
                "normalized_curve": backtest["normalized_curve"],
            }
        )

    evaluated.sort(key=lambda item: item["score"], reverse=True)
    combo_candidates = _build_combinations(evaluated[:6], limit=4)
    for candidate in combo_candidates:
        evaluation = engine.evaluate(candidate.expression, valid_universe, start_date, end_date, groups=5, forward_days=5)
        if "error" in evaluation:
            continue
        metrics = evaluation.get("metrics", {})
        direction = "bottom" if _safe_float(metrics.get("ic_mean")) < 0 or _safe_float(metrics.get("long_short_return")) < 0 else "top"
        backtest = _run_factor_selection_backtest(
            cache,
            engine,
            candidate.expression,
            valid_universe,
            start_date,
            end_date,
            select_pct=select_pct,
            rebalance_days=rebalance_days,
            initial_cash=initial_cash,
            direction=direction,
            market=market,
            commission_rate=commission_rate,
            stamp_tax_rate=stamp_tax_rate,
            slippage=slippage,
        )
        if "error" in backtest:
            continue
        evaluated.append(
            {
                "name": candidate.name,
                "description": candidate.description,
                "expression": candidate.expression,
                "source": candidate.source,
                "direction": direction,
                "score": _score_metrics(metrics) + max(_safe_float(backtest["metrics"].get("total_return")), 0) / 100.0,
                "metrics": metrics,
                "backtest_metrics": backtest["metrics"],
                "long_short_curve": evaluation.get("long_short_curve", []),
                "equity_curve": backtest["equity_curve"],
                "normalized_curve": backtest["normalized_curve"],
            }
        )

    evaluated.sort(key=lambda item: item["score"], reverse=True)
    return {
        "results": evaluated[: max(1, candidate_count)],
        "candidate_total": len(unique_candidates),
        "valid_universe_count": len(valid_universe),
    }


async def run_factor_mining_workflow(
    db: AsyncSession,
    *,
    universe_type: str,
    universe_code: str,
    custom_pool_id: Optional[int],
    start_date: str,
    end_date: str,
    max_stocks: int,
    candidate_count: int,
    gp_generations: int,
    gp_population: int,
    select_pct: float,
    rebalance_days: int,
) -> dict:
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先在系统设置中配置 Tushare Token")

    resolved = await resolve_mining_universe(
        db,
        settings,
        universe_type=universe_type,
        universe_code=universe_code,
        custom_pool_id=custom_pool_id,
        max_stocks=max_stocks,
        as_of_date=start_date,
    )
    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)
    result = await asyncio.to_thread(
        _mine_sync,
        cache,
        engine,
        resolved["codes"],
        start_date,
        end_date,
        candidate_count=max(3, min(int(candidate_count or 12), 40)),
        gp_generations=max(0, min(int(gp_generations or 0), 8)),
        gp_population=max(0, min(int(gp_population or 0), 40)),
        select_pct=max(0.02, min(float(select_pct or 0.1), 0.5)),
        rebalance_days=max(1, min(int(rebalance_days or 5), 60)),
        initial_cash=float(settings.default_cash or 1_000_000),
        commission_rate=float(settings.commission_rate or 0.0003),
        stamp_tax_rate=float(settings.stamp_tax_rate or 0.001),
        slippage=float(settings.slippage or 0.001),
    )
    result.update(
        {
            "universe": resolved,
            "start_date": start_date,
            "end_date": end_date,
            "method": "template_gp_ensemble",
            "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
            "research_mode": "professional",
        }
    )
    return result


async def save_mined_factor(db: AsyncSession, payload: dict) -> dict:
    expression = str(payload.get("expression") or "").strip()
    if not expression:
        raise ValueError("因子表达式不能为空")
    name = str(payload.get("name") or "挖掘因子").strip()[:100]
    description = str(payload.get("description") or "").strip()
    candidate_id = payload.get("candidate_id")
    if candidate_id:
        result = await db.execute(select(FactorMiningCandidate).where(FactorMiningCandidate.id == int(candidate_id)))
        candidate = result.scalar_one_or_none()
        if candidate:
            name = name or candidate.name
            expression = candidate.expression
            description = description or candidate.description
            description = (
                f"{description}\n\n"
                f"挖掘会话: {candidate.session_id}\n"
                f"综合评分: {_safe_float(candidate.score):.4f}\n"
                f"验证指标: {candidate.valid_metrics or {}}\n"
                f"测试指标: {candidate.test_metrics or {}}"
            ).strip()
            description = (
                f"{description}\n"
                f"protocol_version: {(candidate.backtest_metrics or {}).get('protocol_version', 'legacy_unverified')}\n"
                f"strict_note: only {STRICT_MINING_PROTOCOL_VERSION} candidates should be treated as current out-of-sample results."
            ).strip()
    factor = Factor(
        name=name,
        description=description,
        expression=expression,
        category="因子挖掘",
        source="mining_session" if candidate_id else "mining",
        factor_type="technical",
    )
    db.add(factor)
    await db.commit()
    await db.refresh(factor)
    return {"id": factor.id, "name": factor.name}


WINDOW_SPACE = [2, 3, 5, 8, 10, 11, 15, 20, 30, 60, 120]
TERMINAL_SPACE = [
    "close", "open", "high", "low", "vol", "returns", "vwap", "amount",
    "pe", "pb", "ps", "turnover_rate", "circ_mv",
]
UNARY_TEMPLATES = [
    "rank({})", "cs_rank({})", "abs({})", "neg({})",
    "log(abs({}) + 1)", "sqrt(abs({}) + 0.0001)",
    "signedpower({}, 2)", "clip({}, -3, 3)",
]
BINARY_TEMPLATES = [
    "({} + {})", "({} - {})", "({} * {})", "({} / (abs({}) + 0.0001))",
    "max({}, {})", "min({}, {})",
]
ROLLING_TEMPLATES = [
    "mean({}, {w})", "std({}, {w})", "sum({}, {w})", "ts_max({}, {w})",
    "ts_min({}, {w})", "ts_rank({}, {w})", "delta({}, {w})", "delay({}, {w})",
    "pctchange({}, {w})", "ts_decay({}, {w})", "ewm({}, {w})",
    "ts_argmax({}, {w})", "ts_argmin({}, {w})", "wma({}, {w})",
    "decaylinear({}, {w})",
]
PAIR_ROLLING_TEMPLATES = [
    "corr({}, {}, {w})", "cov({}, {}, {w})", "regbeta({}, {}, {w})",
    "regresi({}, {}, {w})",
]
CONDITION_TEMPLATES = [
    "where(({} > {}), {}, {})",
    "where(({} < {}), {}, {})",
    "ternary(({} > {}), {}, {})",
]


@dataclass
class StreamingMiningConfig:
    session_id: str
    settings: SimpleNamespace
    resolved: dict
    universe_type: str
    universe_code: str
    custom_pool_id: Optional[int]
    start_date: str
    end_date: str
    max_stocks: int
    candidate_count: int
    gp_generations: int
    gp_population: int
    select_pct: float
    rebalance_days: int
    max_depth: int = 4
    max_expression_length: int = 600
    auto_stop_candidates: int = 0
    protocol_version: str = STRICT_MINING_PROTOCOL_VERSION
    research_mode: str = "professional"
    factor_themes: tuple[str, ...] = ()
    neutralize: str = DEFAULT_NEUTRALIZE_MODE
    walk_forward_windows: int = DEFAULT_WALK_FORWARD_WINDOWS
    embargo_days: int = DEFAULT_EMBARGO_DAYS
    max_trials: int = 0
    capacity_limit_pct: float = DEFAULT_VOLUME_LIMIT_PCT
    min_dsr: float = DEFAULT_MIN_DSR


class MiningDataContext:
    def __init__(
        self,
        cache: DataCache,
        engine: FactorEngine,
        universe: list[str],
        start_date: str,
        end_date: str,
        benchmark_code: str = "",
    ):
        self.cache = cache
        self.engine = engine
        self.universe = universe
        self.start_date = start_date
        self.end_date = end_date
        self.benchmark_code = str(benchmark_code or "").upper()
        self.stock_data: dict[str, dict] = {}
        self.all_dates: list = []
        self.benchmark_close = pd.Series(dtype=float)

    def preload(self, settings: SimpleNamespace, stop_event: threading.Event, progress_callback=None) -> None:
        completed = 0
        failed = 0
        total = len(self.universe)
        max_workers = min(max((os.cpu_count() or 4) // 2, 2), 4)

        def load_one(code: str):
            client = TushareClient(settings.tushare_token)
            worker_cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
            try:
                daily = worker_cache.get_daily(code, self.start_date, self.end_date, adj="qfq")
                basic = worker_cache.get_daily_basic(code, self.start_date, self.end_date)
                return code, daily, basic
            finally:
                if worker_cache.mysql:
                    try:
                        worker_cache.mysql.close()
                    except Exception:
                        pass

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(load_one, code): code for code in self.universe}
            for future in as_completed(futures):
                if stop_event.is_set():
                    break
                code = futures[future]
                try:
                    _, daily, basic = future.result()
                    item = self._prepare_stock_data(daily, basic)
                    if item and len(item["close"]) >= 45:
                        self.stock_data[code] = item
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, len(self.stock_data), failed)

        all_dates = set()
        for item in self.stock_data.values():
            all_dates.update(item["close"].dropna().index.tolist())
        self.all_dates = sorted(all_dates)
        self._prepare_benchmark()

    def _prepare_benchmark(self) -> None:
        if self.benchmark_code:
            try:
                df = self.cache.get_index_daily(self.benchmark_code, self.start_date, self.end_date)
                if df is not None and not df.empty and "close" in df.columns:
                    frame = df.copy()
                    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                    frame = frame.dropna(subset=["trade_date"]).sort_values("trade_date")
                    series = pd.to_numeric(frame.set_index("trade_date")["close"], errors="coerce").dropna()
                    if not series.empty:
                        self.benchmark_close = series
                        return
            except Exception:
                self.benchmark_close = pd.Series(dtype=float)

        values = []
        previous = 1.0
        last_prices: dict[str, float] = {}
        for dt in self.all_dates:
            returns = []
            for code, data in self.stock_data.items():
                close = data.get("close", pd.Series(dtype=float)).get(dt)
                if pd.isna(close) or close <= 0:
                    continue
                if code in last_prices and last_prices[code] > 0:
                    returns.append(float(close) / last_prices[code] - 1.0)
                last_prices[code] = float(close)
            if returns:
                previous *= 1.0 + float(np.nanmean(returns))
            values.append((dt, previous))
        if values:
            self.benchmark_close = pd.Series([x[1] for x in values], index=[x[0] for x in values], dtype=float)

    def _prepare_stock_data(self, daily: pd.DataFrame, basic: pd.DataFrame) -> Optional[dict]:
        if daily is None or daily.empty:
            return None
        df = daily.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df = df.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
        if df.empty or "close" not in df.columns:
            return None
        indexed = df.set_index("trade_date")
        closes = indexed["close"].astype(float)
        highs = indexed["high"].astype(float)
        lows = indexed["low"].astype(float)
        volumes = indexed["vol"].astype(float)
        opens = indexed["open"].astype(float) if "open" in indexed.columns else closes.shift(1)
        amounts = indexed["amount"].astype(float) if "amount" in indexed.columns else volumes * closes
        basic_data = {}
        if basic is not None and not basic.empty:
            bdf = basic.copy()
            bdf["trade_date"] = pd.to_datetime(bdf["trade_date"], errors="coerce")
            bdf = bdf.dropna(subset=["trade_date"]).sort_values("trade_date")
            aligned = bdf.set_index("trade_date").reindex(indexed.index).ffill()
            for col in ["pe", "pe_ttm", "pb", "ps", "ps_ttm", "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f"]:
                if col in aligned.columns:
                    basic_data[col] = aligned[col].astype(float)
        return {
            "close": closes, "high": highs, "low": lows, "vol": volumes,
            "open": opens, "amount": amounts, "basic": basic_data,
            "prev_close": closes.shift(1),
            "forward_return": closes.pct_change(5).shift(-5),
        }

    def compute_expression(self, expression: str) -> dict[str, pd.Series]:
        result = {}
        for code, data in self.stock_data.items():
            values = self.engine._eval_expression(
                expression,
                data["close"], data["high"], data["low"], data["vol"],
                data["open"], data["basic"], data["amount"],
            )
            if values is None or not isinstance(values, pd.Series):
                continue
            cleaned = values.replace([np.inf, -np.inf], np.nan)
            if cleaned.dropna().empty:
                continue
            result[code] = cleaned
        return result


class StreamingMiningManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.active_session_id: Optional[str] = None
        self.stop_events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.elites: dict[str, list[dict]] = {}

    def can_start(self) -> Optional[str]:
        with self.lock:
            sid = self.active_session_id
            if not sid:
                return None
            event = self.stop_events.get(sid)
            thread = self.threads.get(sid)
            if event and thread and thread.is_alive() and not event.is_set():
                return sid
            self.active_session_id = None
            return None

    def snapshot(self) -> dict:
        with self.lock:
            active_id = self.active_session_id
            thread = self.threads.get(active_id) if active_id else None
            event = self.stop_events.get(active_id) if active_id else None
            return {
                "active_session_id": active_id or "",
                "running": bool(active_id and thread and thread.is_alive() and not (event and event.is_set())),
                "stopping": bool(active_id and event and event.is_set()),
                "tracked_threads": len(self.threads),
            }

    def start(self, config: StreamingMiningConfig):
        stop_event = threading.Event()
        thread = threading.Thread(target=_run_streaming_mining_thread, args=(config, stop_event), daemon=True)
        with self.lock:
            self.active_session_id = config.session_id
            self.stop_events[config.session_id] = stop_event
            self.threads[config.session_id] = thread
            self.elites[config.session_id] = []
        thread.start()

    def stop(self, session_id: str) -> bool:
        with self.lock:
            event = self.stop_events.get(session_id)
            if not event:
                return False
            event.set()
            return True

    def finish(self, session_id: str):
        with self.lock:
            if self.active_session_id == session_id:
                self.active_session_id = None


MINING_MANAGER = StreamingMiningManager()


def _expression_hash(expression: str) -> str:
    normalized = re.sub(r"\s+", "", str(expression or ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _expression_complexity(expression: str) -> int:
    return len(re.findall(r"[A-Za-z_]+\(", expression or "")) + len(re.findall(r"[+\-*/><=]", expression or ""))


def _downsample_curve(curve: list[dict], max_points: int = 260) -> list[dict]:
    if len(curve) <= max_points:
        return curve
    step = max(1, math.ceil(len(curve) / max_points))
    sampled = curve[::step]
    if sampled[-1].get("date") != curve[-1].get("date"):
        sampled.append(curve[-1])
    return sampled


def _build_settings_snapshot(settings) -> SimpleNamespace:
    return SimpleNamespace(
        tushare_token=settings.tushare_token,
        default_cash=float(settings.default_cash or 1_000_000),
        commission_rate=float(settings.commission_rate or 0.0),
        stamp_tax_rate=float(settings.stamp_tax_rate or 0.0),
        slippage=float(settings.slippage or 0.0),
        use_mysql=getattr(settings, "use_mysql", 0),
        mysql_host=getattr(settings, "mysql_host", ""),
        mysql_port=getattr(settings, "mysql_port", 3306),
        mysql_user=getattr(settings, "mysql_user", ""),
        mysql_password=getattr(settings, "mysql_password", ""),
        mysql_database=getattr(settings, "mysql_database", "qtsys"),
    )


def _date_splits(dates: list) -> dict[str, set]:
    if not dates:
        return {"train": set(), "valid": set(), "test": set(), "all": set()}
    first = int(len(dates) * 0.50)
    second = int(len(dates) * 0.75)
    return {
        "train": set(dates[:max(first, 1)]),
        "valid": set(dates[max(first, 1):max(second, first + 1)]),
        "test": set(dates[max(second, first + 1):]),
        "all": set(dates),
    }


def _preprocess_factor_panel(
    factors: dict[str, pd.Series],
    ctx: MiningDataContext,
    method: str = DEFAULT_NEUTRALIZE_MODE,
) -> dict[str, pd.Series]:
    if not factors:
        return {}
    dates = sorted(set().union(*[set(series.index) for series in factors.values()]))
    by_code = {code: pd.Series(index=series.index, dtype=float) for code, series in factors.items()}
    for dt in dates:
        row = {}
        for code, series in factors.items():
            if dt in series.index and pd.notna(series.get(dt)):
                row[code] = float(series.get(dt))
        if len(row) < 5:
            continue
        processed = _factor_preprocess_single(pd.Series(row), method)
        for code, value in processed.items():
            by_code[code].loc[dt] = value
    return {code: series.dropna() for code, series in by_code.items() if not series.dropna().empty}


def _rank_ic(values: list[float], returns: list[float]) -> float:
    if len(values) < 4 or len(returns) < 4:
        return np.nan
    left = pd.Series(values).rank().values
    right = pd.Series(returns).rank().values
    if np.nanstd(left) <= 0 or np.nanstd(right) <= 0:
        return np.nan
    return float(np.corrcoef(left, right)[0, 1])


def _segment_metrics(factors: dict[str, pd.Series], ctx: MiningDataContext, dates: set) -> dict:
    ic_values = []
    long_short = []
    valid_points = 0
    group_spreads = []
    for dt in sorted(dates):
        row = []
        for code, series in factors.items():
            data = ctx.stock_data.get(code)
            if data is None or dt not in series.index or dt not in data["forward_return"].index:
                continue
            fv = series.get(dt)
            fr = data["forward_return"].get(dt)
            if pd.notna(fv) and pd.notna(fr):
                row.append((code, float(fv), float(fr)))
        if len(row) < 5:
            continue
        valid_points += len(row)
        ic = _rank_ic([x[1] for x in row], [x[2] for x in row])
        if pd.notna(ic):
            ic_values.append(ic)
        ranked = sorted(row, key=lambda x: x[1])
        group = max(1, len(ranked) // 5)
        bottom = np.mean([x[2] for x in ranked[:group]])
        top = np.mean([x[2] for x in ranked[-group:]])
        if pd.notna(top) and pd.notna(bottom):
            spread = float(top - bottom)
            long_short.append(spread)
            group_spreads.append(spread)
    if not ic_values:
        return {
            "ic_mean": 0.0,
            "rank_ic_mean": 0.0,
            "ic_ir": 0.0,
            "ic_t_stat": 0.0,
            "ic_p_value": 1.0,
            "ic_win_rate": 0.0,
            "ic_positive_ratio": 0.0,
            "ic_count": 0,
            "long_short_return": 0.0,
            "long_short_win_rate": 0.0,
            "valid_points": valid_points,
            "monotonicity": 0.0,
        }
    ic_arr = np.array(ic_values, dtype=float)
    ic_std = float(np.std(ic_arr, ddof=1)) if len(ic_arr) > 1 else 0.0
    ls = float(np.nanmean(long_short) * 100) if long_short else 0.0
    ls_arr = np.array(long_short, dtype=float) if long_short else np.array([], dtype=float)
    ic_direction = 1 if float(np.nanmean(ic_arr)) >= 0 else -1
    ic_mean = float(np.nanmean(ic_arr))
    ic_t = ic_mean / (ic_std / math.sqrt(len(ic_arr))) if ic_std > 0 and len(ic_arr) > 1 else 0.0
    group_monotonicity = float(np.mean(np.array(group_spreads, dtype=float) > 0) * 100) if group_spreads else 0.0
    return {
        "ic_mean": round(ic_mean, 6),
        "rank_ic_mean": round(ic_mean, 6),
        "ic_ir": round(ic_mean / ic_std, 6) if ic_std > 0 else 0.0,
        "ic_t_stat": round(ic_t, 6),
        "ic_p_value": round(_safe_p_value_from_t(ic_t), 6),
        "ic_win_rate": round(float(np.mean(ic_arr * ic_direction > 0) * 100), 2),
        "ic_positive_ratio": round(float(np.mean(ic_arr > 0) * 100), 2),
        "ic_count": int(len(ic_values)),
        "long_short_return": round(ls, 4),
        "long_short_win_rate": round(float(np.mean(ls_arr > 0) * 100), 2) if len(ls_arr) else 0.0,
        "valid_points": int(valid_points),
        "monotonicity": round(1.0 if ls > 0 else -1.0 if ls < 0 else 0.0, 4),
        "monotonicity_score": round(group_monotonicity, 2),
    }


def _backtest_from_factors(
    factors: dict[str, pd.Series],
    ctx: MiningDataContext,
    settings: SimpleNamespace,
    *,
    select_pct: float,
    rebalance_days: int,
    direction: str,
    dates: Optional[set] = None,
) -> dict:
    usable_dates = [dt for dt in ctx.all_dates if not dates or dt in dates]
    if len(usable_dates) <= rebalance_days + 2:
        return {"error": "交易日不足"}
    cash = float(settings.default_cash or 1_000_000)
    holdings: dict[str, int] = {}
    equity_curve = []
    daily_returns = []
    prev_value = cash
    reverse = direction != "bottom"
    commission = float(settings.commission_rate or 0)
    stamp_tax = float(settings.stamp_tax_rate or 0)
    slippage = float(settings.slippage or 0)

    rebalance_set = set(range(0, len(usable_dates) - 1, max(1, rebalance_days)))

    for idx, dt in enumerate(usable_dates):
        if idx > 0 and (idx - 1) in rebalance_set:
            signal_date = usable_dates[idx - 1]
            exec_date = dt
            scores = {}
            for code, series in factors.items():
                if signal_date in series.index and pd.notna(series.get(signal_date)):
                    scores[code] = float(series.get(signal_date))
            if len(scores) >= 5:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=reverse)
                selected = [x[0] for x in ranked[:max(1, int(len(ranked) * select_pct))]]
                for code in list(holdings):
                    if code in selected:
                        continue
                    price = ctx.stock_data.get(code, {}).get("open", pd.Series(dtype=float)).get(exec_date)
                    shares = holdings.get(code, 0)
                    if shares > 0 and pd.notna(price) and price > 0:
                        sell_price = float(price) * max(0.0, 1 - slippage)
                        cash += shares * sell_price * (1 - commission - stamp_tax)
                    holdings.pop(code, None)

                portfolio_value = cash
                for code, shares in holdings.items():
                    price = ctx.stock_data.get(code, {}).get("open", pd.Series(dtype=float)).get(exec_date)
                    if pd.notna(price) and price > 0:
                        portfolio_value += shares * float(price)
                target_value = portfolio_value / max(len(selected), 1)
                for code in selected:
                    price = ctx.stock_data.get(code, {}).get("open", pd.Series(dtype=float)).get(exec_date)
                    if pd.isna(price) or price <= 0:
                        continue
                    buy_price = float(price) * (1 + slippage)
                    target_shares = int(target_value / buy_price / 100) * 100
                    current = holdings.get(code, 0)
                    diff = target_shares - current
                    if diff > 0:
                        cost = diff * buy_price * (1 + commission)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = target_shares
                    elif diff < 0:
                        cash += abs(diff) * float(price) * max(0.0, 1 - slippage) * (1 - commission - stamp_tax)
                        holdings[code] = target_shares

        value = cash
        for code, shares in holdings.items():
            price = ctx.stock_data.get(code, {}).get("close", pd.Series(dtype=float)).get(dt)
            if pd.notna(price) and price > 0:
                value += shares * float(price)
        daily_returns.append((value - prev_value) / prev_value if prev_value > 0 else 0.0)
        equity_curve.append({"date": _date_text(dt), "value": round(value, 2)})
        prev_value = value

    final_value = equity_curve[-1]["value"] if equity_curve else float(settings.default_cash or 1_000_000)
    initial_cash = float(settings.default_cash or 1_000_000)
    annual_return = 0.0
    if initial_cash > 0 and final_value > 0 and len(equity_curve) > 1:
        annual_return = (math.pow(final_value / initial_cash, 252 / max(len(equity_curve), 1)) - 1) * 100
    peak = initial_cash
    max_drawdown = 0.0
    for point in equity_curve:
        val = _safe_float(point.get("value"))
        peak = max(peak, val)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - val) / peak)
    returns = np.array(daily_returns, dtype=float)
    vol = float(np.std(returns, ddof=1)) if len(returns) > 2 else 0.0
    sharpe = float(np.mean(returns) / vol * math.sqrt(252)) if vol > 0 else 0.0
    return {
        "metrics": {
            "total_return": round((final_value / initial_cash - 1) * 100, 2),
            "annual_return": round(annual_return, 2),
            "max_drawdown": round(max_drawdown * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "trade_count": 0,
        },
        "equity_curve": _downsample_curve(equity_curve),
        "normalized_curve": _downsample_curve(_normalize_curve(equity_curve)),
    }


def _score_candidate(valid_metrics: dict, test_metrics: dict, backtest_metrics: dict, complexity: int) -> float:
    valid_ic = abs(_safe_float(valid_metrics.get("ic_mean")))
    valid_ir = min(abs(_safe_float(valid_metrics.get("ic_ir"))), 3.0)
    valid_ls = max(_safe_float(valid_metrics.get("long_short_return")), 0.0)
    test_ic = abs(_safe_float(test_metrics.get("ic_mean")))
    total_return = _safe_float(backtest_metrics.get("total_return"))
    max_drawdown = max(_safe_float(backtest_metrics.get("max_drawdown")), 0.0)
    complexity_penalty = min(max(complexity, 0) / 120.0, 0.35)
    return round(
        valid_ic * 4.0
        + valid_ir * 0.35
        + valid_ls / 80.0
        + test_ic * 1.5
        + max(total_return, 0.0) / 120.0
        - max_drawdown / 300.0
        - complexity_penalty,
        6,
    )


def _evaluate_candidate(candidate: Candidate, ctx: MiningDataContext, config: StreamingMiningConfig) -> Optional[dict]:
    expression = str(candidate.expression or "").strip()
    if not expression or len(expression) > config.max_expression_length:
        return None
    factors = ctx.compute_expression(expression)
    if len(factors) < 5:
        return None
    non_null = sum(int(series.notna().sum()) for series in factors.values())
    total = sum(len(series) for series in factors.values())
    if total <= 0 or non_null / total < 0.45:
        return None
    flat = pd.concat([series.dropna() for series in factors.values()], ignore_index=True)
    if flat.empty or float(flat.std()) <= 1e-12:
        return None
    quality_metrics = {
        "valid_stock_count": int(len(factors)),
        "observation_count": int(total),
        "non_null_count": int(non_null),
        "coverage_ratio": round(float(non_null / total * 100), 2),
        "value_std": round(float(flat.std()), 6),
        "value_mean": round(float(flat.mean()), 6),
    }

    splits = _date_splits(ctx.all_dates)
    train_metrics = _segment_metrics(factors, ctx, splits["train"])
    valid_metrics = _segment_metrics(factors, ctx, splits["valid"])
    test_metrics = _segment_metrics(factors, ctx, splits["test"] or splits["valid"])
    valid_metrics = {**valid_metrics, **quality_metrics}
    direction = "bottom" if _safe_float(valid_metrics.get("ic_mean")) < 0 or _safe_float(valid_metrics.get("long_short_return")) < 0 else "top"
    backtest = _backtest_from_factors(
        factors,
        ctx,
        config.settings,
        select_pct=config.select_pct,
        rebalance_days=config.rebalance_days,
        direction=direction,
        dates=splits["valid"] | splits["test"] if splits["test"] else splits["all"],
    )
    if "error" in backtest or not backtest.get("normalized_curve"):
        return None
    complexity = _expression_complexity(expression)
    score = _score_candidate(valid_metrics, test_metrics, backtest["metrics"], complexity)
    if score < -0.15 and abs(_safe_float(valid_metrics.get("ic_mean"))) < 0.015:
        return None
    return {
        "name": candidate.name,
        "description": candidate.description,
        "expression": expression,
        "expression_hash": _expression_hash(expression),
        "source": candidate.source,
        "direction": direction,
        "complexity": complexity,
        "score": score,
        "metrics": valid_metrics,
        "train_metrics": train_metrics,
        "valid_metrics": valid_metrics,
        "test_metrics": test_metrics,
        "backtest_metrics": backtest["metrics"],
        "equity_curve": backtest["equity_curve"],
        "normalized_curve": backtest["normalized_curve"],
    }


def _price_at(ctx: MiningDataContext, code: str, field: str, dt) -> float:
    data = ctx.stock_data.get(code, {})
    return _safe_float(data.get(field, pd.Series(dtype=float)).get(dt), 0.0)


def _is_star_market(code: str) -> bool:
    return str(code or "").upper().startswith("688")


def _can_trade(ctx: MiningDataContext, code: str, dt, side: str, rejection_counts: dict[str, int]) -> bool:
    if _is_star_market(code):
        rejection_counts["star_market_filtered"] = rejection_counts.get("star_market_filtered", 0) + 1
        return False
    price = _price_at(ctx, code, "open", dt)
    close = _price_at(ctx, code, "close", dt)
    volume = _price_at(ctx, code, "vol", dt)
    if price <= 0 or close <= 0 or volume <= 0:
        rejection_counts["paused_or_missing_price"] = rejection_counts.get("paused_or_missing_price", 0) + 1
        return False
    prev_close = _price_at(ctx, code, "prev_close", dt)
    if prev_close > 0:
        if side == "buy" and price >= prev_close * (1.0 + DEFAULT_LIMIT_UP_PCT):
            rejection_counts["limit_up_buy_blocked"] = rejection_counts.get("limit_up_buy_blocked", 0) + 1
            return False
        if side == "sell" and price <= prev_close * (1.0 - DEFAULT_LIMIT_DOWN_PCT):
            rejection_counts["limit_down_sell_blocked"] = rejection_counts.get("limit_down_sell_blocked", 0) + 1
            return False
    return True


def _volume_limited_shares(ctx: MiningDataContext, code: str, dt, desired_shares: int) -> int:
    desired_shares = int(desired_shares / 100) * 100
    volume_hands = _price_at(ctx, code, "vol", dt)
    if volume_hands <= 0:
        return 0
    max_shares = int(volume_hands * 100 * DEFAULT_VOLUME_LIMIT_PCT / 100) * 100
    return max(0, min(desired_shares, max_shares))


def _portfolio_value_at(ctx: MiningDataContext, cash: float, holdings: dict[str, int], dt, price_field: str = "close") -> float:
    value = float(cash)
    for code, shares in holdings.items():
        price = _price_at(ctx, code, price_field, dt)
        if price > 0:
            value += int(shares) * price
    return value


def _benchmark_curve_for_dates(ctx: MiningDataContext, dates: list, initial_cash: float) -> list[dict]:
    if not dates:
        return []
    series = ctx.benchmark_close
    if series is None or series.empty:
        return [{"date": _date_text(dt), "value": round(float(initial_cash), 2)} for dt in dates]
    aligned = series.reindex(pd.to_datetime(dates)).ffill().bfill()
    base = _safe_float(aligned.iloc[0], 0.0) if len(aligned) else 0.0
    if base <= 0:
        return [{"date": _date_text(dt), "value": round(float(initial_cash), 2)} for dt in dates]
    return [
        {"date": _date_text(dt), "value": round(float(initial_cash) * _safe_float(aligned.iloc[idx], base) / base, 2)}
        for idx, dt in enumerate(dates)
    ]


def _backtest_from_factors(
    factors: dict[str, pd.Series],
    ctx: MiningDataContext,
    settings: SimpleNamespace,
    *,
    select_pct: float,
    rebalance_days: int,
    direction: str,
    dates: Optional[set] = None,
) -> dict:
    all_dates = list(ctx.all_dates)
    segment_dates = [dt for dt in all_dates if not dates or dt in dates]
    if len(all_dates) <= rebalance_days + 2 or len(segment_dates) <= 2:
        return {"error": "insufficient trading dates"}

    initial_cash = float(settings.default_cash or 1_000_000)
    cash = initial_cash
    holdings: dict[str, int] = {}
    equity_curve = []
    daily_returns = []
    trades = []
    rejection_counts: dict[str, int] = {}
    turnover_values = []
    prev_value = initial_cash
    reverse = direction != "bottom"
    commission = max(0.0, float(settings.commission_rate or 0))
    stamp_tax = max(0.0, float(settings.stamp_tax_rate or 0))
    slippage = max(0.0, float(settings.slippage or 0))
    segment_set = set(segment_dates)
    rebalance_set = set(range(0, len(all_dates) - 1, max(1, rebalance_days)))

    for idx, dt in enumerate(all_dates):
        in_segment = dt in segment_set
        traded_value = 0.0
        if idx > 0 and in_segment and (idx - 1) in rebalance_set:
            signal_date = all_dates[idx - 1]
            exec_date = dt
            scores = {}
            for code, series in factors.items():
                value = series.get(signal_date) if signal_date in series.index else np.nan
                if pd.notna(value):
                    scores[code] = float(value)
            if len(scores) >= 5:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=reverse)
                selected = [x[0] for x in ranked[:max(1, int(len(ranked) * select_pct))]]
                tradable_selected = [code for code in selected if _can_trade(ctx, code, exec_date, "buy", rejection_counts)]

                for code in list(holdings):
                    if code in tradable_selected:
                        continue
                    shares = holdings.get(code, 0)
                    price = _price_at(ctx, code, "open", exec_date)
                    if shares > 0 and _can_trade(ctx, code, exec_date, "sell", rejection_counts):
                        sell_price = price * max(0.0, 1 - slippage)
                        proceeds = shares * sell_price * (1 - commission - stamp_tax)
                        cash += proceeds
                        traded_value += shares * sell_price
                        trades.append({"date": _date_text(exec_date), "code": code, "action": "sell", "price": round(sell_price, 4), "shares": shares})
                        holdings.pop(code, None)

                portfolio_value = _portfolio_value_at(ctx, cash, holdings, exec_date, price_field="open")
                target_gross = portfolio_value * DEFAULT_TARGET_EXPOSURE
                target_value = min(target_gross / max(len(tradable_selected), 1), portfolio_value * DEFAULT_MAX_POSITION_PCT)

                for code in tradable_selected:
                    price = _price_at(ctx, code, "open", exec_date)
                    if price <= 0:
                        continue
                    buy_price = price * (1 + slippage)
                    target_shares = int(target_value / buy_price / 100) * 100
                    current = holdings.get(code, 0)
                    diff = target_shares - current
                    if diff > 0:
                        diff = _volume_limited_shares(ctx, code, exec_date, diff)
                        if diff < 100:
                            rejection_counts["below_lot_or_volume_cap"] = rejection_counts.get("below_lot_or_volume_cap", 0) + 1
                            continue
                        cost = diff * buy_price * (1 + commission)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = current + diff
                            traded_value += diff * buy_price
                            trades.append({"date": _date_text(exec_date), "code": code, "action": "buy", "price": round(buy_price, 4), "shares": diff})
                        else:
                            rejection_counts["insufficient_cash"] = rejection_counts.get("insufficient_cash", 0) + 1
                    elif diff < 0:
                        sell_shares = _volume_limited_shares(ctx, code, exec_date, abs(diff))
                        if sell_shares < 100 or not _can_trade(ctx, code, exec_date, "sell", rejection_counts):
                            continue
                        sell_price = price * max(0.0, 1 - slippage)
                        cash += sell_shares * sell_price * (1 - commission - stamp_tax)
                        holdings[code] = max(0, current - sell_shares)
                        if holdings[code] <= 0:
                            holdings.pop(code, None)
                        traded_value += sell_shares * sell_price
                        trades.append({"date": _date_text(exec_date), "code": code, "action": "sell", "price": round(sell_price, 4), "shares": sell_shares})

        if in_segment:
            value = _portfolio_value_at(ctx, cash, holdings, dt, price_field="close")
            daily_returns.append((value - prev_value) / prev_value if prev_value > 0 else 0.0)
            if value > 0:
                turnover_values.append(traded_value / value * 100.0)
            equity_curve.append({"date": _date_text(dt), "value": round(value, 2)})
            prev_value = value

    if not equity_curve:
        return {"error": "empty equity curve"}

    final_value = equity_curve[-1]["value"]
    total_return = _safe_pct_return(initial_cash, final_value)
    returns = np.array(daily_returns, dtype=float)
    vol = float(np.std(returns, ddof=1)) if len(returns) > 2 else 0.0
    sharpe = float(np.mean(returns) / vol * math.sqrt(252)) if vol > 0 else 0.0
    benchmark_curve = _benchmark_curve_for_dates(ctx, segment_dates, initial_cash)
    benchmark_metrics = _curve_metrics(benchmark_curve, initial_cash)
    excess_return = total_return - _safe_float(benchmark_metrics.get("total_return"))
    excess_curve = _build_excess_curve(equity_curve, benchmark_curve)

    return {
        "metrics": {
            "total_return": round(total_return, 2),
            "annual_return": round(_annualized_return(total_return, len(equity_curve)), 2),
            "max_drawdown": round(_max_drawdown_pct(equity_curve), 2),
            "sharpe_ratio": round(sharpe, 3),
            "trade_count": len(trades),
            "turnover": round(float(np.nanmean(turnover_values)), 2) if turnover_values else 0.0,
            "benchmark_return": round(_safe_float(benchmark_metrics.get("total_return")), 2),
            "excess_return": round(excess_return, 2),
            "benchmark_source": ctx.benchmark_code or "universe_equal_weight",
            "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
        },
        "benchmark_metrics": benchmark_metrics,
        "equity_curve": _downsample_curve(equity_curve),
        "normalized_curve": _downsample_curve(_normalize_curve(equity_curve)),
        "benchmark_curve": _downsample_curve(benchmark_curve),
        "benchmark_normalized_curve": _downsample_curve(_normalize_curve(benchmark_curve)),
        "excess_curve": _downsample_curve(excess_curve),
        "trades": trades[-200:],
        "rejection_reasons": rejection_counts,
    }


def _score_candidate(valid_metrics: dict, valid_backtest_metrics: dict, complexity: int, tested_count: int = 0) -> float:
    valid_ic = abs(_safe_float(valid_metrics.get("ic_mean")))
    valid_ir = min(abs(_safe_float(valid_metrics.get("ic_ir"))), 3.0)
    valid_ls = max(_safe_float(valid_metrics.get("long_short_return")), 0.0)
    valid_excess = max(_safe_float(valid_backtest_metrics.get("excess_return")), -20.0)
    max_drawdown = max(_safe_float(valid_backtest_metrics.get("max_drawdown")), 0.0)
    complexity_penalty = min(max(complexity, 0) / 120.0, 0.35)
    multiple_test_penalty = min(math.log1p(max(tested_count, 0)) / 18.0, 0.45)
    return round(
        valid_ic * 4.0
        + valid_ir * 0.35
        + valid_ls / 80.0
        + max(valid_excess, 0.0) / 100.0
        - max_drawdown / 300.0
        - complexity_penalty
        - multiple_test_penalty,
        6,
    )


def _final_oos_score(test_metrics: dict, test_backtest_metrics: dict, complexity: int) -> float:
    test_ic = abs(_safe_float(test_metrics.get("ic_mean")))
    test_ir = min(abs(_safe_float(test_metrics.get("ic_ir"))), 3.0)
    excess_return = _safe_float(test_backtest_metrics.get("excess_return"))
    max_drawdown = max(_safe_float(test_backtest_metrics.get("max_drawdown")), 0.0)
    turnover = max(_safe_float(test_backtest_metrics.get("turnover")), 0.0)
    complexity_penalty = min(max(complexity, 0) / 160.0, 0.25)
    return round(
        max(excess_return, -30.0) / 60.0
        + test_ic * 3.0
        + test_ir * 0.25
        - max_drawdown / 250.0
        - min(turnover / 600.0, 0.25)
        - complexity_penalty,
        6,
    )


def _deflated_sharpe(backtest_metrics: dict, tested_count: int) -> float:
    sharpe = _safe_float(backtest_metrics.get("sharpe_ratio"))
    penalty = math.sqrt(max(math.log(max(tested_count, 2)), 0.0)) * 0.15
    return round(sharpe - penalty, 6)


def _pbo_risk(valid_metrics: dict, test_metrics: dict, valid_backtest: dict, test_backtest: dict) -> str:
    valid_ic = abs(_safe_float(valid_metrics.get("ic_mean")))
    test_ic = abs(_safe_float(test_metrics.get("ic_mean")))
    valid_excess = _safe_float(valid_backtest.get("excess_return"))
    test_excess = _safe_float(test_backtest.get("excess_return"))
    if test_excess <= 0 or test_ic <= 0:
        return "high"
    if valid_excess > 0 and test_excess < valid_excess * 0.25:
        return "high"
    if valid_ic > 0 and test_ic < valid_ic * 0.35:
        return "medium"
    return "low"


def _candidate_significance(test_metrics: dict, test_backtest_metrics: dict, tested_count: int) -> dict:
    p_value = _safe_float(test_metrics.get("ic_p_value"), 1.0)
    bonferroni_p = min(1.0, p_value * max(int(tested_count or 1), 1))
    return {
        "rank_ic": _safe_float(test_metrics.get("rank_ic_mean")),
        "ic_t_stat": _safe_float(test_metrics.get("ic_t_stat")),
        "p_value": round(p_value, 6),
        "bonferroni_p": round(bonferroni_p, 6),
        "dsr": _deflated_sharpe(test_backtest_metrics, tested_count),
        "tested_count": int(tested_count or 0),
    }


def _capacity_summary(backtest_metrics: dict, select_pct: float) -> dict:
    turnover = max(_safe_float(backtest_metrics.get("turnover")), 0.0)
    capacity_score = max(0.0, 100.0 - min(turnover, 500.0) / 5.0)
    return {
        "turnover": round(turnover, 2),
        "capacity_score": round(capacity_score, 2),
        "select_pct": round(float(select_pct or 0.0), 4),
        "volume_limit_pct": DEFAULT_VOLUME_LIMIT_PCT,
        "target_exposure": DEFAULT_TARGET_EXPOSURE,
        "max_position_pct": DEFAULT_MAX_POSITION_PCT,
    }


def _factor_fingerprint(expression: str) -> dict:
    text = str(expression or "")
    operators = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
    variables = sorted(set(re.findall(r"\b(close|open|high|low|vol|volume|amount|vwap|returns|pe|pb|ps|turnover_rate|circ_mv|total_mv)\b", text)))
    windows = sorted({int(x) for x in re.findall(r",\s*(\d+)\)", text) if str(x).isdigit()})
    if any(var in variables for var in ("pe", "pb", "ps", "turnover_rate", "circ_mv", "total_mv")):
        family = "fundamental_or_liquidity"
    elif "vol" in variables or "volume" in variables or "amount" in variables:
        family = "volume_price"
    elif "returns" in variables or "pctchange" in operators:
        family = "return_based"
    else:
        family = "price_technical"
    return {
        "family": family,
        "operators": sorted(set(operators)),
        "operator_count": len(operators),
        "variables": variables,
        "windows": windows,
        "max_window": max(windows) if windows else 0,
        "expression_length": len(text),
    }


def _robustness_summary(
    fold_checks: list[dict],
    valid_metrics: dict,
    test_metrics: dict,
    valid_backtest_metrics: dict,
    test_backtest_metrics: dict,
    direction: str,
) -> dict:
    if not fold_checks:
        pass_ratio = 0.0
        median_test_ic = 0.0
        worst_test_ic = 0.0
    else:
        signed_tests = []
        for item in fold_checks:
            test_ic = _safe_float(item.get("test_ic"))
            signed = test_ic if direction != "bottom" else -test_ic
            signed_tests.append(signed)
        pass_ratio = float(np.mean(np.array(signed_tests) > 0)) if signed_tests else 0.0
        median_test_ic = float(np.nanmedian(signed_tests)) if signed_tests else 0.0
        worst_test_ic = float(np.nanmin(signed_tests)) if signed_tests else 0.0
    valid_ic = abs(_safe_float(valid_metrics.get("ic_mean")))
    test_ic = abs(_safe_float(test_metrics.get("ic_mean")))
    valid_excess = max(_safe_float(valid_backtest_metrics.get("excess_return")), 0.0)
    test_excess = max(_safe_float(test_backtest_metrics.get("excess_return")), 0.0)
    ic_decay = 1.0 - min(test_ic / valid_ic, 1.0) if valid_ic > 0 else 1.0
    excess_decay = 1.0 - min(test_excess / valid_excess, 1.0) if valid_excess > 0 else 1.0
    score = (
        pass_ratio * 45.0
        + max(min(median_test_ic, 0.05), -0.05) * 600.0
        - max(ic_decay, 0.0) * 15.0
        - max(excess_decay, 0.0) * 15.0
        + max(min(worst_test_ic, 0.03), -0.03) * 300.0
    )
    return {
        "walk_forward_pass_ratio": round(pass_ratio, 4),
        "median_directional_test_ic": round(median_test_ic, 6),
        "worst_directional_test_ic": round(worst_test_ic, 6),
        "ic_decay": round(ic_decay, 4),
        "excess_decay": round(excess_decay, 4),
        "robustness_score": round(max(0.0, min(score, 100.0)), 2),
        "fold_count": len(fold_checks),
    }


def _institutional_final_score(
    test_metrics: dict,
    test_backtest_metrics: dict,
    significance: dict,
    overfit_risk: dict,
    capacity: dict,
    robustness: dict,
    complexity: int,
) -> float:
    base = _final_oos_score(test_metrics, test_backtest_metrics, complexity)
    dsr = _safe_float(significance.get("dsr"))
    p_value = min(_safe_float(significance.get("bonferroni_p"), 1.0), 1.0)
    pbo_penalty = {"low": 0.0, "medium": 0.25, "high": 0.65}.get(str(overfit_risk.get("pbo_risk")), 0.35)
    return round(
        base
        + dsr * 0.20
        + _safe_float(robustness.get("robustness_score")) / 120.0
        + _safe_float(capacity.get("capacity_score")) / 350.0
        - p_value * 0.30
        - pbo_penalty,
        6,
    )


def _directional_ic(metrics: dict, direction: str) -> float:
    value = _safe_float(metrics.get("ic_mean"))
    return value if direction != "bottom" else -value


def _strict_acceptance_reasons(
    valid_metrics: dict,
    test_metrics: dict,
    valid_backtest_metrics: dict,
    test_backtest_metrics: dict,
    direction: str,
) -> list[str]:
    reasons = []
    if _safe_float(valid_metrics.get("coverage_ratio")) < 45:
        reasons.append("coverage_below_45pct")
    if _safe_float(valid_metrics.get("valid_stock_count")) < 5:
        reasons.append("valid_stock_count_below_5")
    if _safe_float(valid_metrics.get("ic_count")) < 3:
        reasons.append("valid_ic_count_below_3")
    if _safe_float(test_metrics.get("ic_count")) < 3:
        reasons.append("test_ic_count_below_3")
    if _directional_ic(valid_metrics, direction) < 0.005:
        reasons.append("validation_directional_ic_too_weak")
    if _directional_ic(test_metrics, direction) < 0.0:
        reasons.append("test_directional_ic_negative")
    if _safe_float(test_metrics.get("ic_p_value"), 1.0) > 0.35:
        reasons.append("test_ic_statistically_weak")
    if _safe_float(valid_backtest_metrics.get("excess_return")) < -2.0:
        reasons.append("validation_excess_return_too_low")
    if _safe_float(test_backtest_metrics.get("excess_return")) <= 0.0:
        reasons.append("test_excess_return_not_positive")
    if _safe_float(test_backtest_metrics.get("trade_count")) <= 0:
        reasons.append("no_real_trades")
    if _safe_float(test_backtest_metrics.get("max_drawdown")) > 35.0:
        reasons.append("test_max_drawdown_above_35pct")
    if _safe_float(test_backtest_metrics.get("turnover")) > 350.0:
        reasons.append("turnover_too_high")
    return reasons


def _evaluate_candidate(candidate: Candidate, ctx: MiningDataContext, config: StreamingMiningConfig, tested_count: int = 0) -> Optional[dict]:
    expression = str(candidate.expression or "").strip()
    if not expression or len(expression) > config.max_expression_length:
        return None
    factors = ctx.compute_expression(expression)
    if len(factors) < 5:
        return None
    factors = _preprocess_factor_panel(factors, ctx, config.neutralize or DEFAULT_NEUTRALIZE_MODE)
    if len(factors) < 5:
        return None
    non_null = sum(int(series.notna().sum()) for series in factors.values())
    total = sum(len(series) for series in factors.values())
    if total <= 0 or non_null / total < 0.45:
        return None
    flat = pd.concat([series.dropna() for series in factors.values()], ignore_index=True)
    if flat.empty or float(flat.std()) <= 1e-12:
        return None
    quality_metrics = {
        "valid_stock_count": int(len(factors)),
        "observation_count": int(total),
        "non_null_count": int(non_null),
        "coverage_ratio": round(float(non_null / total * 100), 2),
        "value_std": round(float(flat.std()), 6),
        "value_mean": round(float(flat.mean()), 6),
        "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
        "neutralization": config.neutralize or DEFAULT_NEUTRALIZE_MODE,
    }

    folds = _walk_forward_splits(ctx.all_dates, config.walk_forward_windows, config.embargo_days)
    splits = folds[-1] if folds else _date_splits(ctx.all_dates)
    if len(splits["valid"]) < 3 or len(splits["test"]) < 3:
        return None
    train_metrics = _segment_metrics(factors, ctx, splits["train"])
    valid_metrics = {**_segment_metrics(factors, ctx, splits["valid"]), **quality_metrics}
    test_metrics = _segment_metrics(factors, ctx, splits["test"])
    direction = "bottom" if _safe_float(valid_metrics.get("ic_mean")) < 0 or _safe_float(valid_metrics.get("long_short_return")) < 0 else "top"

    fold_checks = []
    for fold in folds:
        fold_valid = _segment_metrics(factors, ctx, fold["valid"])
        fold_test = _segment_metrics(factors, ctx, fold["test"])
        fold_checks.append(
            {
                "valid_ic": _safe_float(fold_valid.get("ic_mean")),
                "test_ic": _safe_float(fold_test.get("ic_mean")),
                "valid_count": int(fold_valid.get("ic_count") or 0),
                "test_count": int(fold_test.get("ic_count") or 0),
            }
        )
    directional_folds = [
        1
        for fold in fold_checks
        if (_safe_float(fold.get("valid_ic")) >= 0 and direction == "top")
        or (_safe_float(fold.get("valid_ic")) < 0 and direction == "bottom")
    ]

    valid_backtest = _backtest_from_factors(
        factors,
        ctx,
        config.settings,
        select_pct=config.select_pct,
        rebalance_days=config.rebalance_days,
        direction=direction,
        dates=splits["valid"],
    )
    if "error" in valid_backtest or not valid_backtest.get("normalized_curve"):
        return None
    test_backtest = _backtest_from_factors(
        factors,
        ctx,
        config.settings,
        select_pct=config.select_pct,
        rebalance_days=config.rebalance_days,
        direction=direction,
        dates=splits["test"],
    )
    if "error" in test_backtest or not test_backtest.get("normalized_curve"):
        return None

    complexity = _expression_complexity(expression)
    discovery_score = _score_candidate(valid_metrics, valid_backtest["metrics"], complexity, tested_count)
    rejection_reasons = _strict_acceptance_reasons(
        valid_metrics,
        test_metrics,
        valid_backtest["metrics"],
        test_backtest["metrics"],
        direction,
    )
    if rejection_reasons:
        return None
    final_score = _final_oos_score(test_metrics, test_backtest["metrics"], complexity)
    significance = _candidate_significance(test_metrics, test_backtest["metrics"], tested_count)
    overfit_risk = {
        "pbo_risk": _pbo_risk(valid_metrics, test_metrics, valid_backtest["metrics"], test_backtest["metrics"]),
        "walk_forward_pass_ratio": round(len(directional_folds) / max(len(fold_checks), 1), 4),
        "folds": fold_checks[-5:],
        "embargo_days": config.embargo_days,
    }
    capacity = _capacity_summary(test_backtest["metrics"], config.select_pct)
    robustness = _robustness_summary(fold_checks, valid_metrics, test_metrics, valid_backtest["metrics"], test_backtest["metrics"], direction)
    fingerprint = _factor_fingerprint(expression)
    if significance["dsr"] < config.min_dsr:
        return None
    if overfit_risk["pbo_risk"] == "high" and robustness["robustness_score"] < 35:
        return None
    if robustness["walk_forward_pass_ratio"] < 0.34:
        return None
    final_score = _institutional_final_score(test_metrics, test_backtest["metrics"], significance, overfit_risk, capacity, robustness, complexity)

    backtest_metrics = {
        **test_backtest["metrics"],
        "discovery_score": discovery_score,
        "test_excess_return": _safe_float(test_backtest["metrics"].get("excess_return")),
        "validation_excess_return": _safe_float(valid_backtest["metrics"].get("excess_return")),
        "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
        "rank_ic": significance["rank_ic"],
        "ic_t_stat": significance["ic_t_stat"],
        "p_value": significance["p_value"],
        "dsr": significance["dsr"],
        "pbo_risk": overfit_risk["pbo_risk"],
        "monotonicity_score": _safe_float(test_metrics.get("monotonicity_score")),
        "capacity_score": capacity["capacity_score"],
        "robustness_score": robustness["robustness_score"],
        "walk_forward_pass_ratio": robustness["walk_forward_pass_ratio"],
        "neutralization": config.neutralize or DEFAULT_NEUTRALIZE_MODE,
    }
    return {
        "name": candidate.name,
        "description": candidate.description,
        "expression": expression,
        "expression_hash": _expression_hash(expression),
        "source": candidate.source,
        "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
        "theme": candidate.theme or "general_formula",
        "hypothesis": candidate.hypothesis or _format_research_hypothesis(candidate.theme or "general_formula", candidate.description),
        "preprocessing": {"method": config.neutralize or DEFAULT_NEUTRALIZE_MODE, "winsorize": "1%-99%", "cross_sectional": True},
        "significance": significance,
        "overfit_risk": overfit_risk,
        "capacity": capacity,
        "robustness": robustness,
        "fingerprint": fingerprint,
        "correlation_cluster": "",
        "revalidation_status": "strict_passed",
        "direction": direction,
        "complexity": complexity,
        "score": final_score,
        "metrics": {**valid_metrics, "score_basis": "validation_only", "protocol_version": STRICT_MINING_PROTOCOL_VERSION},
        "train_metrics": {**train_metrics, "protocol_version": STRICT_MINING_PROTOCOL_VERSION},
        "valid_metrics": valid_metrics,
        "test_metrics": {**test_metrics, "protocol_version": STRICT_MINING_PROTOCOL_VERSION},
        "backtest_metrics": {
            **backtest_metrics,
            "benchmark_metrics": test_backtest.get("benchmark_metrics", {}),
            "benchmark_curve": test_backtest.get("benchmark_curve", []),
            "benchmark_normalized_curve": test_backtest.get("benchmark_normalized_curve", []),
            "excess_curve": test_backtest.get("excess_curve", []),
            "trades": test_backtest.get("trades", []),
            "rejection_reasons": test_backtest.get("rejection_reasons", {}),
        },
        "equity_curve": test_backtest["equity_curve"],
        "normalized_curve": test_backtest["normalized_curve"],
    }


def _mine_sync(
    cache: DataCache,
    engine: FactorEngine,
    universe: list[str],
    start_date: str,
    end_date: str,
    *,
    candidate_count: int,
    gp_generations: int,
    gp_population: int,
    select_pct: float,
    rebalance_days: int,
    initial_cash: float,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage: float = 0.001,
) -> dict:
    random.seed(42)
    market = _preload_market(cache, universe, start_date, end_date)
    valid_universe = list(market.keys())
    if len(valid_universe) < 5:
        raise ValueError("available market data is insufficient for mining")

    ctx = MiningDataContext(cache, engine, valid_universe, start_date, end_date)
    for code, frame in market.items():
        prepared = ctx._prepare_stock_data(frame, pd.DataFrame())
        if prepared and len(prepared["close"]) >= 45:
            ctx.stock_data[code] = prepared
    all_dates = set()
    for item in ctx.stock_data.values():
        all_dates.update(item["close"].dropna().index.tolist())
    ctx.all_dates = sorted(all_dates)
    ctx._prepare_benchmark()
    if len(ctx.stock_data) < 5:
        raise ValueError("available prepared data is insufficient for mining")

    settings = SimpleNamespace(
        default_cash=float(initial_cash or 1_000_000),
        commission_rate=float(commission_rate or 0.0),
        stamp_tax_rate=float(stamp_tax_rate or 0.0),
        slippage=float(slippage or 0.0),
    )
    config = StreamingMiningConfig(
        session_id="one_shot",
        settings=settings,
        resolved={"codes": valid_universe},
        universe_type="system",
        universe_code="",
        custom_pool_id=None,
        start_date=start_date,
        end_date=end_date,
        max_stocks=len(valid_universe),
        candidate_count=candidate_count,
        gp_generations=gp_generations,
        gp_population=gp_population,
        select_pct=select_pct,
        rebalance_days=rebalance_days,
    )

    candidates = _generate_template_candidates(max(candidate_count, 6))
    if gp_generations > 0 and gp_population > 0:
        gp_results = run_gp(engine, valid_universe, start_date, end_date, pop_size=gp_population, generations=gp_generations, top_n=min(6, candidate_count))
        for idx, item in enumerate(gp_results, start=1):
            candidates.append(Candidate(name=f"GP factor {idx}", expression=item["expression"], description=f"Generated by genetic search; fitness={item.get('fitness')}", source="gp"))

    seen = set()
    evaluated = []
    tested = 0
    for candidate in candidates[: max(candidate_count + 6, candidate_count)]:
        if candidate.expression in seen:
            continue
        seen.add(candidate.expression)
        tested += 1
        payload = _evaluate_candidate(candidate, ctx, config, tested_count=tested)
        if payload:
            evaluated.append(payload)

    evaluated.sort(key=lambda item: item["score"], reverse=True)
    return {
        "results": evaluated[: max(1, candidate_count)],
        "candidate_total": len(seen),
        "valid_universe_count": len(valid_universe),
        "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
    }


def _operator_pool_available() -> set[str]:
    available = set()
    for item in NODE_REGISTRY.values():
        template = str(item.get("template") or "")
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", template)
        if match:
            available.add(match.group(1))
    return available


def _random_expr(depth: int = 0, max_depth: int = 4) -> str:
    if depth >= max_depth or random.random() < 0.22:
        return random.choice(TERMINAL_SPACE)
    roll = random.random()
    if roll < 0.25:
        return random.choice(UNARY_TEMPLATES).format(_random_expr(depth + 1, max_depth))
    if roll < 0.48:
        template = random.choice(BINARY_TEMPLATES)
        left = _random_expr(depth + 1, max_depth)
        right = _random_expr(depth + 1, max_depth)
        if template.count("{}") == 3:
            return template.format(left, right, right)
        return template.format(left, right)
    if roll < 0.76:
        return random.choice(ROLLING_TEMPLATES).format(_random_expr(depth + 1, max_depth), w=random.choice(WINDOW_SPACE))
    if roll < 0.92:
        return random.choice(PAIR_ROLLING_TEMPLATES).format(
            _random_expr(depth + 1, max_depth),
            _random_expr(depth + 1, max_depth),
            w=random.choice(WINDOW_SPACE),
        )
    return random.choice(CONDITION_TEMPLATES).format(
        _random_expr(depth + 1, max_depth),
        _random_expr(depth + 1, max_depth),
        _random_expr(depth + 1, max_depth),
        _random_expr(depth + 1, max_depth),
    )


def _mutate_expression(expression: str) -> str:
    text = str(expression or "")
    windows = [int(x) for x in re.findall(r",\s*(\d+)\)", text)]
    if windows:
        old = random.choice(windows)
        new = random.choice([w for w in WINDOW_SPACE if w != old])
        return re.sub(rf",\s*{old}\)", f", {new})", text, count=1)
    terminal = random.choice(TERMINAL_SPACE)
    replacement = random.choice([x for x in TERMINAL_SPACE if x != terminal])
    if terminal in text:
        return text.replace(terminal, replacement, 1)
    return f"rank({text})"


def _combine_expressions(left: str, right: str) -> str:
    mode = random.choice(["rank_mean", "spread", "weighted"])
    if mode == "spread":
        return f"(rank({left}) - rank({right}))"
    if mode == "weighted":
        weight = random.choice([0.35, 0.5, 0.65])
        return f"({weight} * rank({left}) + {round(1 - weight, 2)} * rank({right}))"
    return f"(rank({left}) + rank({right})) / 2"


def _candidate_stream(config: StreamingMiningConfig, elite_provider) -> Candidate:
    random.seed(int(time.time()) % 10_000_000)
    for item in _generate_template_candidates(80):
        if config.factor_themes and item.theme and item.theme not in config.factor_themes:
            continue
        yield item
        for mutated, window in _parameter_neighbors(item.expression):
            yield Candidate(
                _parameterized_factor_name(item.name, window),
                mutated,
                f"Parameter-neighborhood validation using a {window}-day window.",
                "param_scan",
                item.theme,
                item.hypothesis,
            )
    index = 1
    _operator_pool_available()
    while True:
        elites = elite_provider()
        if len(elites) >= 2 and random.random() < 0.35:
            left, right = random.sample(elites[: min(len(elites), 20)], 2)
            yield Candidate(
                f"elite_ensemble_{index}",
                _combine_expressions(left["expression"], right["expression"]),
                "Non-negative rank ensemble of validated elite factors.",
                "ensemble",
                "ensemble_factor",
                "Low-correlation validated factors may diversify idiosyncratic noise.",
            )
        elif elites and random.random() < 0.35:
            base = random.choice(elites[: min(len(elites), 20)])
            yield Candidate(
                f"elite_mutation_{index}",
                _mutate_expression(base["expression"]),
                "Neighborhood mutation of a validated elite factor.",
                "elite_mutation",
                base.get("theme") or "elite_mutation",
                base.get("hypothesis") or "Elite mutation tests whether nearby formulas preserve economic signal.",
            )
        else:
            yield Candidate(
                f"typed_grammar_{index}",
                _random_expr(0, config.max_depth),
                "Typed grammar expression generated from the factor operator space.",
                "grammar",
                "grammar_search",
                "Machine search explores economically plausible formula neighborhoods, then relies on strict out-of-sample validation.",
            )
        index += 1


def _parameterized_factor_name(base_name: str, window: int) -> str:
    clean = re.sub(r"^\d+\s*日", "", str(base_name or "").strip())
    clean = clean.strip("_- ") or str(base_name or "因子").strip() or "因子"
    return f"{clean}_{int(window)}日"


def _parameter_neighbors(expression: str) -> list[tuple[str, int]]:
    result = []
    for old_text in sorted(set(re.findall(r",\s*(\d+)\)", expression))):
        old = int(old_text)
        for new in WINDOW_SPACE:
            if new == old:
                continue
            result.append((re.sub(rf",\s*{old}\)", f", {new})", expression, count=1), int(new)))
            if len(result) >= 8:
                return result
    return result


def _parameterized_factor_name(base_name: str, window: int) -> str:
    clean = re.sub(r"^\d+\s*[dD日]?", "", str(base_name or "").strip())
    clean = re.sub(r"_[0-9]+[dD日]?$", "", clean)
    clean = clean.strip("_- ") or "factor"
    return f"{clean}_{int(window)}d"


async def _update_mining_session(session_id: str, **patch):
    async with async_session() as db:
        patch["updated_at"] = datetime.utcnow()
        await db.execute(update(FactorMiningSession).where(FactorMiningSession.session_id == session_id).values(**patch))
        await db.commit()


async def _insert_candidate(session_id: str, payload: dict) -> Optional[int]:
    async with async_session() as db:
        existing = await db.execute(
            select(FactorMiningCandidate).where(
                FactorMiningCandidate.session_id == session_id,
                FactorMiningCandidate.expression_hash == payload["expression_hash"],
            )
        )
        if existing.scalar_one_or_none():
            return None
        candidate = FactorMiningCandidate(session_id=session_id, **payload)
        db.add(candidate)
        await db.commit()
        await db.refresh(candidate)
        return candidate.id


async def _insert_trial_log(session_id: str, candidate: Candidate, stage: str, reasons: list[str], score: float = 0.0, metrics: Optional[dict] = None):
    async with async_session() as db:
        expr_hash = _expression_hash(candidate.expression)
        existing = await db.execute(
            select(FactorMiningTrialLog).where(
                FactorMiningTrialLog.session_id == session_id,
                FactorMiningTrialLog.expression_hash == expr_hash,
                FactorMiningTrialLog.stage == stage,
            )
        )
        if existing.scalar_one_or_none():
            return
        db.add(
            FactorMiningTrialLog(
                session_id=session_id,
                expression_hash=expr_hash,
                name=candidate.name,
                source=candidate.source,
                theme=candidate.theme,
                expression=candidate.expression,
                stage=stage,
                score=float(score or 0.0),
                metrics=metrics or {},
                rejection_reasons=reasons,
            )
        )
        await db.commit()


async def _upsert_correlation_cluster(session_id: str, payload: dict, candidate_id: int, elites: list[dict]) -> str:
    expression = payload.get("expression", "")
    curve = _curve_value_map(payload.get("normalized_curve") or [])
    best_cluster = ""
    best_corr = 0.0
    for item in elites:
        other_curve = _curve_value_map(item.get("normalized_curve") or [])
        dates = sorted(set(curve) & set(other_curve))
        if len(dates) < 5:
            continue
        left = np.array([curve[d] for d in dates], dtype=float)
        right = np.array([other_curve[d] for d in dates], dtype=float)
        if np.nanstd(left) <= 0 or np.nanstd(right) <= 0:
            continue
        corr = abs(float(np.corrcoef(left, right)[0, 1]))
        if corr > best_corr:
            best_corr = corr
            best_cluster = str(item.get("correlation_cluster") or f"cluster_{item.get('id') or candidate_id}")
    cluster_key = best_cluster if best_corr >= 0.85 else f"cluster_{candidate_id}"
    payload["correlation_cluster"] = cluster_key
    async with async_session() as db:
        existing = await db.execute(
            select(FactorCorrelationCluster).where(
                FactorCorrelationCluster.session_id == session_id,
                FactorCorrelationCluster.cluster_key == cluster_key,
            )
        )
        row = existing.scalar_one_or_none()
        member = {
            "candidate_id": candidate_id,
            "name": payload.get("name"),
            "expression_hash": payload.get("expression_hash") or _expression_hash(expression),
            "score": payload.get("score", 0.0),
        }
        if row:
            members = list(row.members or [])
            if not any(item.get("candidate_id") == candidate_id for item in members):
                members.append(member)
            row.members = members[-100:]
            row.max_abs_corr = max(float(row.max_abs_corr or 0.0), best_corr)
            row.updated_at = datetime.utcnow()
        else:
            db.add(
                FactorCorrelationCluster(
                    session_id=session_id,
                    cluster_key=cluster_key,
                    representative_candidate_id=candidate_id,
                    members=[member],
                    max_abs_corr=best_corr,
                )
            )
        await db.commit()
    return cluster_key


def _run_streaming_mining_thread(config: StreamingMiningConfig, stop_event: threading.Event):
    session_id = config.session_id
    tested = 0
    accepted = 0
    best_score = -999.0
    seen_hashes: set[str] = set()
    elites: list[dict] = []
    client = TushareClient(config.settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(config.settings))
    engine = FactorEngine(cache)
    ctx = MiningDataContext(
        cache,
        engine,
        config.resolved["codes"],
        config.start_date,
        config.end_date,
        benchmark_code=config.resolved.get("benchmark_code") or config.universe_code,
    )

    def run_async(coro):
        return asyncio.run(coro)

    try:
        run_async(_update_mining_session(session_id, status="running", phase="preload", message="正在准备股票池行情与财务数据"))

        last_progress_at = {"time": 0.0, "done": 0}

        def progress(done, total, valid, failed):
            now = time.time()
            if done < total and done - last_progress_at["done"] < 5 and now - last_progress_at["time"] < 2.5:
                return
            last_progress_at["time"] = now
            last_progress_at["done"] = done
            run_async(_update_mining_session(
                session_id,
                phase="preload",
                message=f"行情准备 {done}/{total}，有效 {valid}，失败 {failed}",
                stock_count=valid,
            ))

        ctx.preload(config.settings, stop_event, progress)
        if stop_event.is_set():
            run_async(_update_mining_session(session_id, status="cancelled", phase="cancelled", message="用户已停止挖掘", stopped_at=datetime.utcnow()))
            return
        if len(ctx.stock_data) < 5:
            raise ValueError("可用行情股票不足，无法启动因子挖掘")

        run_async(_update_mining_session(session_id, phase="mining", message="行情准备完成，开始持续挖掘候选因子", stock_count=len(ctx.stock_data)))

        def elite_provider():
            return list(elites)

        for candidate in _candidate_stream(config, elite_provider):
            if stop_event.is_set():
                break
            expression_hash = _expression_hash(candidate.expression)
            if expression_hash in seen_hashes:
                continue
            seen_hashes.add(expression_hash)
            tested += 1
            if tested % 5 == 1:
                run_async(_update_mining_session(
                    session_id,
                    phase="screening",
                    message=f"正在筛选第 {tested} 个候选因子，已入选 {accepted} 个",
                    tested_count=tested,
                    accepted_count=accepted,
                    best_score=max(best_score, 0.0),
                ))
            try:
                payload = _evaluate_candidate(candidate, ctx, config, tested_count=tested)
            except Exception:
                payload = None
            if not payload:
                if tested <= 200 or tested % 25 == 0:
                    run_async(_insert_trial_log(
                        session_id,
                        candidate,
                        "screen_or_strict_reject",
                        ["failed_fast_screen_or_v4_institutional_validation"],
                    ))
                if config.max_trials and tested >= config.max_trials:
                    break
                continue
            inserted_id = run_async(_insert_candidate(session_id, payload))
            if inserted_id is None:
                continue
            cluster_key = run_async(_upsert_correlation_cluster(session_id, payload, inserted_id, elites))
            if cluster_key:
                async def _set_cluster():
                    async with async_session() as db:
                        await db.execute(
                            update(FactorMiningCandidate)
                            .where(FactorMiningCandidate.id == inserted_id)
                            .values(correlation_cluster=cluster_key)
                        )
                        await db.commit()
                run_async(_set_cluster())
            accepted += 1
            payload["id"] = inserted_id
            payload["correlation_cluster"] = cluster_key
            elites.append(payload)
            elites.sort(key=lambda item: item["score"], reverse=True)
            elites[:] = elites[:200]
            best_score = max(best_score, _safe_float(payload.get("score")))
            run_async(_update_mining_session(
                session_id,
                phase="mining",
                message=f"发现有效因子：{payload['name']}，评分 {payload['score']:.4f}",
                tested_count=tested,
                accepted_count=accepted,
                best_score=max(best_score, 0.0),
            ))
            if config.auto_stop_candidates and accepted >= config.auto_stop_candidates:
                break
            if config.max_trials and tested >= config.max_trials:
                break

        final_status = "cancelled" if stop_event.is_set() else "completed"
        final_message = "用户已停止挖掘" if stop_event.is_set() else "挖掘任务已完成"
        run_async(_update_mining_session(
            session_id,
            status=final_status,
            phase=final_status,
            message=final_message,
            tested_count=tested,
            accepted_count=accepted,
            best_score=max(best_score, 0.0),
            stopped_at=datetime.utcnow(),
        ))
    except Exception as exc:
        run_async(_update_mining_session(
            session_id,
            status="failed",
            phase="failed",
            message=f"挖掘失败: {exc}",
            error=str(exc),
            tested_count=tested,
            accepted_count=accepted,
            stopped_at=datetime.utcnow(),
        ))
    finally:
        if cache.mysql:
            try:
                cache.mysql.close()
            except Exception:
                pass
        MINING_MANAGER.finish(session_id)


async def start_streaming_mining_session(db: AsyncSession, payload: dict) -> dict:
    active_id = MINING_MANAGER.can_start()
    if active_id:
        return {"session_id": active_id, "already_running": True}

    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先在系统设置中配置 Tushare Token")
    start_date = str(payload.get("start_date") or "").replace("-", "")[:8]
    end_date = str(payload.get("end_date") or "").replace("-", "")[:8]
    if len(start_date) != 8 or len(end_date) != 8 or start_date >= end_date:
        raise ValueError("请提供有效的开始和结束日期")

    max_stocks = max(5, min(int(payload.get("max_stocks") or 50), 500))
    resolved = await resolve_mining_universe(
        db,
        settings,
        universe_type=payload.get("universe_type") or "system",
        universe_code=payload.get("universe_code") or "000016.SH",
        custom_pool_id=payload.get("custom_pool_id"),
        max_stocks=max_stocks,
        as_of_date=start_date,
    )
    session_id = uuid4().hex[:16]
    config = StreamingMiningConfig(
        session_id=session_id,
        settings=_build_settings_snapshot(settings),
        resolved=resolved,
        universe_type=payload.get("universe_type") or "system",
        universe_code=payload.get("universe_code") or "",
        custom_pool_id=payload.get("custom_pool_id"),
        start_date=start_date,
        end_date=end_date,
        max_stocks=max_stocks,
        candidate_count=max(3, min(int(payload.get("candidate_count") or 12), 200)),
        gp_generations=max(0, min(int(payload.get("gp_generations") or 0), 8)),
        gp_population=max(0, min(int(payload.get("gp_population") or 0), 40)),
        select_pct=max(0.02, min(float(payload.get("select_pct") or 0.1), 0.5)),
        rebalance_days=max(1, min(int(payload.get("rebalance_days") or 5), 60)),
        max_depth=max(2, min(int(payload.get("max_depth") or 4), 6)),
        max_expression_length=max(120, min(int(payload.get("max_expression_length") or 600), 1200)),
        auto_stop_candidates=max(0, min(int(payload.get("auto_stop_candidates") or 0), 500)),
        protocol_version=STRICT_MINING_PROTOCOL_VERSION,
        research_mode=str(payload.get("research_mode") or "professional")[:40],
        factor_themes=tuple(str(x) for x in (payload.get("factor_themes") or []) if str(x).strip()),
        neutralize=str(payload.get("neutralize") or DEFAULT_NEUTRALIZE_MODE)[:40],
        walk_forward_windows=max(2, min(int(payload.get("walk_forward_windows") or DEFAULT_WALK_FORWARD_WINDOWS), 8)),
        embargo_days=max(0, min(int(payload.get("embargo_days") or DEFAULT_EMBARGO_DAYS), 30)),
        max_trials=max(0, min(int(payload.get("max_trials") or 0), 100000)),
        capacity_limit_pct=max(0.01, min(float(payload.get("capacity_limit_pct") or DEFAULT_VOLUME_LIMIT_PCT), 0.50)),
        min_dsr=max(-5.0, min(float(payload.get("min_dsr") if payload.get("min_dsr") is not None else DEFAULT_MIN_DSR), 5.0)),
    )
    row = FactorMiningSession(
        session_id=session_id,
        status="pending",
        phase="queued",
        message="挖掘任务已提交",
        universe_type=config.universe_type,
        universe_code=config.universe_code or resolved.get("universe_code", ""),
        universe_name=resolved.get("universe_name") or resolved.get("universe_label") or "",
        custom_pool_id=config.custom_pool_id,
        stock_count=len(resolved.get("codes") or []),
        start_date=start_date,
        end_date=end_date,
        params={
            "max_stocks": config.max_stocks,
            "select_pct": config.select_pct,
            "rebalance_days": config.rebalance_days,
            "max_depth": config.max_depth,
            "max_expression_length": config.max_expression_length,
            "auto_stop_candidates": config.auto_stop_candidates,
            "protocol_version": config.protocol_version,
            "research_mode": config.research_mode,
            "factor_themes": list(config.factor_themes),
            "neutralize": config.neutralize,
            "walk_forward_windows": config.walk_forward_windows,
            "embargo_days": config.embargo_days,
            "max_trials": config.max_trials,
            "capacity_limit_pct": config.capacity_limit_pct,
            "min_dsr": config.min_dsr,
        },
        protocol_version=config.protocol_version,
        research_mode=config.research_mode,
        factor_themes=list(config.factor_themes),
        neutralize=config.neutralize,
        walk_forward_windows=config.walk_forward_windows,
        embargo_days=config.embargo_days,
        max_trials=config.max_trials,
        capacity_limit_pct=config.capacity_limit_pct,
        min_dsr=config.min_dsr,
    )
    db.add(row)
    await db.commit()
    MINING_MANAGER.start(config)
    return {"session_id": session_id, "already_running": False}


async def get_streaming_mining_status(db: AsyncSession, session_id: str) -> dict:
    result = await db.execute(select(FactorMiningSession).where(FactorMiningSession.session_id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise ValueError("挖掘会话不存在")
    running = session.status in {"pending", "running"} and MINING_MANAGER.can_start() == session_id
    return {
        "session_id": session.session_id,
        "status": session.status,
        "running": running,
        "phase": session.phase,
        "message": session.message,
        "universe_type": session.universe_type,
        "universe_code": session.universe_code,
        "universe_name": session.universe_name,
        "stock_count": session.stock_count,
        "start_date": session.start_date,
        "end_date": session.end_date,
        "tested_count": session.tested_count,
        "accepted_count": session.accepted_count,
        "best_score": session.best_score,
        "error": session.error,
        "protocol_version": session.protocol_version or "legacy_unverified",
        "research_mode": session.research_mode or "professional",
        "factor_themes": session.factor_themes or [],
        "neutralize": session.neutralize or DEFAULT_NEUTRALIZE_MODE,
        "walk_forward_windows": session.walk_forward_windows or DEFAULT_WALK_FORWARD_WINDOWS,
        "embargo_days": session.embargo_days or DEFAULT_EMBARGO_DAYS,
        "max_trials": session.max_trials or 0,
        "capacity_limit_pct": session.capacity_limit_pct or DEFAULT_VOLUME_LIMIT_PCT,
        "min_dsr": session.min_dsr if session.min_dsr is not None else DEFAULT_MIN_DSR,
        "started_at": session.started_at.isoformat() if session.started_at else "",
        "updated_at": session.updated_at.isoformat() if session.updated_at else "",
        "stopped_at": session.stopped_at.isoformat() if session.stopped_at else "",
        "params": session.params or {},
    }


def _serialize_candidate(item: FactorMiningCandidate) -> dict:
    backtest_metrics = item.backtest_metrics or {}
    return {
        "id": item.id,
        "session_id": item.session_id,
        "name": item.name,
        "description": item.description,
        "expression": item.expression,
        "source": item.source,
        "theme": item.theme or "",
        "hypothesis": item.hypothesis or "",
        "preprocessing": item.preprocessing or {},
        "significance": item.significance or {},
        "overfit_risk": item.overfit_risk or {},
        "capacity": item.capacity or {},
        "robustness": item.robustness or {},
        "fingerprint": item.fingerprint or {},
        "correlation_cluster": item.correlation_cluster or "",
        "revalidation_status": item.revalidation_status or "",
        "direction": item.direction,
        "complexity": item.complexity,
        "score": item.score,
        "metrics": item.metrics or {},
        "train_metrics": item.train_metrics or {},
        "valid_metrics": item.valid_metrics or {},
        "test_metrics": item.test_metrics or {},
        "backtest_metrics": backtest_metrics,
        "equity_curve": item.equity_curve or [],
        "normalized_curve": item.normalized_curve or [],
        "protocol_version": item.protocol_version or backtest_metrics.get("protocol_version") or (item.metrics or {}).get("protocol_version") or "legacy_unverified",
        "benchmark_curve": backtest_metrics.get("benchmark_curve", []),
        "benchmark_normalized_curve": backtest_metrics.get("benchmark_normalized_curve", []),
        "excess_curve": backtest_metrics.get("excess_curve", []),
        "benchmark_metrics": backtest_metrics.get("benchmark_metrics", {}),
        "test_excess_return": backtest_metrics.get("test_excess_return", backtest_metrics.get("excess_return", 0.0)),
        "rank_ic": backtest_metrics.get("rank_ic", (item.test_metrics or {}).get("rank_ic_mean", 0.0)),
        "ic_t_stat": backtest_metrics.get("ic_t_stat", (item.test_metrics or {}).get("ic_t_stat", 0.0)),
        "p_value": backtest_metrics.get("p_value", (item.test_metrics or {}).get("ic_p_value", 1.0)),
        "dsr": backtest_metrics.get("dsr", (item.significance or {}).get("dsr", 0.0)),
        "pbo_risk": backtest_metrics.get("pbo_risk", (item.overfit_risk or {}).get("pbo_risk", "unknown")),
        "monotonicity_score": backtest_metrics.get("monotonicity_score", (item.test_metrics or {}).get("monotonicity_score", 0.0)),
        "capacity_score": backtest_metrics.get("capacity_score", (item.capacity or {}).get("capacity_score", 0.0)),
        "robustness_score": backtest_metrics.get("robustness_score", (item.robustness or {}).get("robustness_score", 0.0)),
        "walk_forward_pass_ratio": backtest_metrics.get("walk_forward_pass_ratio", (item.robustness or {}).get("walk_forward_pass_ratio", 0.0)),
        "neutralization": backtest_metrics.get("neutralization", (item.preprocessing or {}).get("method", "")),
        "turnover": backtest_metrics.get("turnover", 0.0),
        "trade_count": backtest_metrics.get("trade_count", 0),
        "rejection_reasons": backtest_metrics.get("rejection_reasons", {}),
        "legacy_warning": (backtest_metrics.get("protocol_version") or (item.metrics or {}).get("protocol_version")) != STRICT_MINING_PROTOCOL_VERSION,
        "created_at": item.created_at.isoformat() if item.created_at else "",
    }


async def get_streaming_mining_results(db: AsyncSession, session_id: str, after_id: int = 0, limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    result = await db.execute(
        select(FactorMiningCandidate)
        .where(FactorMiningCandidate.session_id == session_id, FactorMiningCandidate.id > int(after_id or 0))
        .order_by(FactorMiningCandidate.id.asc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return {
        "items": [_serialize_candidate(item) for item in rows],
        "last_id": rows[-1].id if rows else int(after_id or 0),
    }


async def get_mining_research_report(db: AsyncSession, candidate_id: int) -> dict:
    result = await db.execute(select(FactorMiningCandidate).where(FactorMiningCandidate.id == int(candidate_id)))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise ValueError("mining candidate not found")
    session_result = await db.execute(select(FactorMiningSession).where(FactorMiningSession.session_id == candidate.session_id))
    session = session_result.scalar_one_or_none()
    item = _serialize_candidate(candidate)
    settings_summary = {
        "universe": session.universe_name if session else "",
        "universe_code": session.universe_code if session else "",
        "start_date": session.start_date if session else "",
        "end_date": session.end_date if session else "",
        "neutralize": item.get("neutralization") or (session.neutralize if session else DEFAULT_NEUTRALIZE_MODE),
        "protocol_version": item.get("protocol_version"),
    }
    jq_notes = {
        "rebalance_days": (session.params or {}).get("rebalance_days", 5) if session else 5,
        "select_pct": (session.params or {}).get("select_pct", 0.1) if session else 0.1,
        "trade_constraints": "A-share long-only, next-open execution, 100-share lots, fees, stamp tax, slippage, ST/STAR/limit/volume filters.",
        "retest_warning": "JoinQuant must use the same universe, dates, rebalance frequency, costs and filters as this report.",
    }
    return {
        "candidate": item,
        "research_card": {
            "title": candidate.name,
            "theme": candidate.theme or "general_formula",
            "hypothesis": candidate.hypothesis or _format_research_hypothesis(candidate.theme or "general_formula", candidate.description or ""),
            "formula": candidate.expression,
            "source": candidate.source,
            "protocol": STRICT_MINING_PROTOCOL_VERSION,
            "preprocessing": candidate.preprocessing or {},
            "significance": candidate.significance or {},
            "overfit_risk": candidate.overfit_risk or {},
            "capacity": candidate.capacity or {},
            "robustness": candidate.robustness or {},
            "fingerprint": candidate.fingerprint or {},
            "metrics": {
                "train": candidate.train_metrics or {},
                "valid": candidate.valid_metrics or {},
                "test": candidate.test_metrics or {},
                "backtest": candidate.backtest_metrics or {},
            },
            "curves": {
                "equity": candidate.equity_curve or [],
                "normalized": candidate.normalized_curve or [],
                "benchmark": (candidate.backtest_metrics or {}).get("benchmark_normalized_curve", []),
                "excess": (candidate.backtest_metrics or {}).get("excess_curve", []),
            },
            "joinquant": jq_notes,
            "settings": settings_summary,
        },
    }


async def revalidate_mining_candidate(db: AsyncSession, candidate_id: int) -> dict:
    result = await db.execute(select(FactorMiningCandidate).where(FactorMiningCandidate.id == int(candidate_id)))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise ValueError("mining candidate not found")
    session_result = await db.execute(select(FactorMiningSession).where(FactorMiningSession.session_id == candidate.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise ValueError("mining session not found")
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("please configure Tushare token first")

    resolved = await resolve_mining_universe(
        db,
        settings,
        universe_type=session.universe_type or "system",
        universe_code=session.universe_code or "000016.SH",
        custom_pool_id=session.custom_pool_id,
        max_stocks=int((session.params or {}).get("max_stocks") or session.stock_count or 50),
        as_of_date=session.start_date,
    )
    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    try:
        engine = FactorEngine(cache)
        ctx = MiningDataContext(
            cache,
            engine,
            resolved["codes"],
            session.start_date,
            session.end_date,
            benchmark_code=resolved.get("benchmark_code") or session.universe_code,
        )
        stop_event = threading.Event()
        ctx.preload(_build_settings_snapshot(settings), stop_event)
        config = StreamingMiningConfig(
            session_id=session.session_id,
            settings=_build_settings_snapshot(settings),
            resolved=resolved,
            universe_type=session.universe_type,
            universe_code=session.universe_code,
            custom_pool_id=session.custom_pool_id,
            start_date=session.start_date,
            end_date=session.end_date,
            max_stocks=len(resolved.get("codes") or []),
            candidate_count=1,
            gp_generations=0,
            gp_population=0,
            select_pct=float((session.params or {}).get("select_pct") or 0.1),
            rebalance_days=int((session.params or {}).get("rebalance_days") or 5),
            protocol_version=STRICT_MINING_PROTOCOL_VERSION,
            research_mode=session.research_mode or "professional",
            factor_themes=tuple(session.factor_themes or []),
            neutralize=session.neutralize or (session.params or {}).get("neutralize") or DEFAULT_NEUTRALIZE_MODE,
            walk_forward_windows=int(session.walk_forward_windows or (session.params or {}).get("walk_forward_windows") or DEFAULT_WALK_FORWARD_WINDOWS),
            embargo_days=int(session.embargo_days or (session.params or {}).get("embargo_days") or DEFAULT_EMBARGO_DAYS),
            max_trials=int(session.max_trials or 0),
            capacity_limit_pct=float(session.capacity_limit_pct or DEFAULT_VOLUME_LIMIT_PCT),
            min_dsr=float(session.min_dsr if session.min_dsr is not None else DEFAULT_MIN_DSR),
        )
        payload = _evaluate_candidate(
            Candidate(candidate.name, candidate.expression, candidate.description or "", candidate.source or "revalidate", candidate.theme or "", candidate.hypothesis or ""),
            ctx,
            config,
            tested_count=max(int(session.tested_count or 0), 1),
        )
        if not payload:
            candidate.backtest_metrics = {
                **(candidate.backtest_metrics or {}),
                "protocol_version": STRICT_MINING_PROTOCOL_VERSION,
                "revalidated": False,
                "legacy_warning": True,
                "revalidation_message": "candidate failed strict out-of-sample validation",
            }
            candidate.protocol_version = STRICT_MINING_PROTOCOL_VERSION
            candidate.revalidation_status = "strict_failed"
            await db.commit()
            return {"success": False, "message": "candidate failed strict out-of-sample validation", "protocol_version": STRICT_MINING_PROTOCOL_VERSION}

        candidate.name = payload["name"]
        candidate.description = payload["description"]
        candidate.direction = payload["direction"]
        candidate.complexity = payload["complexity"]
        candidate.score = payload["score"]
        candidate.metrics = payload["metrics"]
        candidate.train_metrics = payload["train_metrics"]
        candidate.valid_metrics = payload["valid_metrics"]
        candidate.test_metrics = payload["test_metrics"]
        candidate.backtest_metrics = {**payload["backtest_metrics"], "revalidated": True}
        candidate.equity_curve = payload["equity_curve"]
        candidate.normalized_curve = payload["normalized_curve"]
        candidate.protocol_version = payload["protocol_version"]
        candidate.theme = payload["theme"]
        candidate.hypothesis = payload["hypothesis"]
        candidate.preprocessing = payload["preprocessing"]
        candidate.significance = payload["significance"]
        candidate.overfit_risk = payload["overfit_risk"]
        candidate.capacity = payload["capacity"]
        candidate.robustness = payload["robustness"]
        candidate.fingerprint = payload["fingerprint"]
        candidate.correlation_cluster = payload.get("correlation_cluster") or candidate.correlation_cluster
        candidate.revalidation_status = "strict_passed"
        await db.commit()
        await db.refresh(candidate)
        return {"success": True, "candidate": _serialize_candidate(candidate)}
    finally:
        if cache.mysql:
            try:
                cache.mysql.close()
            except Exception:
                pass


async def stop_streaming_mining_session(db: AsyncSession, session_id: str) -> dict:
    ok = MINING_MANAGER.stop(session_id)
    if ok:
        await db.execute(
            update(FactorMiningSession)
            .where(FactorMiningSession.session_id == session_id)
            .values(status="running", phase="stopping", message="已收到停止请求，正在安全停止", updated_at=datetime.utcnow())
        )
        await db.commit()
        return {"success": True, "message": "已发送停止请求"}
    result = await db.execute(select(FactorMiningSession).where(FactorMiningSession.session_id == session_id))
    if not result.scalar_one_or_none():
        raise ValueError("挖掘会话不存在")
    return {"success": True, "message": "当前会话没有运行中的后台任务"}

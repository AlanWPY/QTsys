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
from database.models import Factor, FactorMiningCandidate, FactorMiningSession, StockPool
from factor.factor_engine import FactorEngine
from factor.genetic import run_gp
from factor.graph_compiler import NODE_REGISTRY
from services.backtest_service import resolve_backtest_universe
from services.factor_board_service import get_system_universes
from services.settings_service import get_or_create_settings


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
    return {"system_universes": list(system_map.values()), "custom_pools": custom_pools}


async def resolve_mining_universe(
    db: AsyncSession,
    settings,
    *,
    universe_type: str,
    universe_code: str,
    custom_pool_id: Optional[int],
    max_stocks: int,
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
    )
    resolved["codes"] = resolved["codes"][:max_stocks]
    resolved["universe_label"] = f"{resolved.get('universe_name') or universe_code} ({len(resolved['codes'])}只)"
    return resolved


def _generate_template_candidates(limit: int) -> list[Candidate]:
    candidates = [Candidate(**item) for item in BASE_TEMPLATES]
    windows = [5, 10, 20, 60]
    generated: list[Candidate] = []
    for window in windows:
        generated.extend(
            [
                Candidate(f"{window}日收益反转", f"-(pctchange(close, {window}))", f"{window}日收益率的反向信号。"),
                Candidate(f"{window}日均线偏离", f"(close / (mean(close, {window}) + 0.0001)) - 1", f"价格相对{window}日均线偏离。"),
                Candidate(f"{window}日波动反转", f"-std(returns, {window})", f"{window}日低波动偏好。"),
                Candidate(f"{window}日量价背离", f"corr(pctchange(close, 1), pctchange(vol, 1), {window})", f"{window}日收益与成交变化相关性。"),
            ]
        )
    seen = set()
    result = []
    for item in candidates + generated:
        if item.expression in seen:
            continue
        seen.add(item.expression)
        result.append(item)
        if len(result) >= limit:
            break
    return result


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
) -> dict:
    market = market or _preload_market(cache, universe, start_date, end_date)
    factors = {}
    closes = {}
    for code, df in market.items():
        series = engine.compute_factor_values(expression, code, start_date, end_date)
        if series is None or series.dropna().empty:
            continue
        indexed = df.set_index("trade_date")
        factors[code] = series
        closes[code] = indexed["close"]

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

    for idx, dt in enumerate(all_dates):
        if idx in rebalance_set and idx + 1 < len(all_dates):
            signal_date = dt
            exec_date = all_dates[idx + 1]
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
                    price = closes.get(code, pd.Series(dtype=float)).get(exec_date)
                    shares = holdings.get(code, 0)
                    if shares > 0 and price and price > 0:
                        cash += shares * float(price)
                        trades.append({"date": _date_text(exec_date), "code": code, "action": "sell", "price": round(float(price), 4), "shares": shares})
                    holdings.pop(code, None)

                portfolio_value = cash
                for code, shares in holdings.items():
                    price = closes.get(code, pd.Series(dtype=float)).get(exec_date)
                    if price and price > 0:
                        portfolio_value += shares * float(price)
                target_value = portfolio_value / max(len(selected), 1)

                for code in selected:
                    price = closes.get(code, pd.Series(dtype=float)).get(exec_date)
                    if not price or price <= 0:
                        continue
                    target_shares = int(target_value / float(price) / 100) * 100
                    current = holdings.get(code, 0)
                    diff = target_shares - current
                    if diff > 0:
                        cost = diff * float(price)
                        if cost <= cash:
                            cash -= cost
                            holdings[code] = target_shares
                            trades.append({"date": _date_text(exec_date), "code": code, "action": "buy", "price": round(float(price), 4), "shares": diff})
                    elif diff < 0:
                        cash += abs(diff) * float(price)
                        holdings[code] = target_shares
                        trades.append({"date": _date_text(exec_date), "code": code, "action": "sell", "price": round(float(price), 4), "shares": abs(diff)})

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
    )
    result.update(
        {
            "universe": resolved,
            "start_date": start_date,
            "end_date": end_date,
            "method": "template_gp_ensemble",
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


class MiningDataContext:
    def __init__(self, cache: DataCache, engine: FactorEngine, universe: list[str], start_date: str, end_date: str):
        self.cache = cache
        self.engine = engine
        self.universe = universe
        self.start_date = start_date
        self.end_date = end_date
        self.stock_data: dict[str, dict] = {}
        self.all_dates: list = []

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
            long_short.append(float(top - bottom))
    if not ic_values:
        return {
            "ic_mean": 0.0,
            "ic_ir": 0.0,
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
    return {
        "ic_mean": round(float(np.nanmean(ic_arr)), 6),
        "ic_ir": round(float(np.nanmean(ic_arr) / ic_std), 6) if ic_std > 0 else 0.0,
        "ic_win_rate": round(float(np.mean(ic_arr * ic_direction > 0) * 100), 2),
        "ic_positive_ratio": round(float(np.mean(ic_arr > 0) * 100), 2),
        "ic_count": int(len(ic_values)),
        "long_short_return": round(ls, 4),
        "long_short_win_rate": round(float(np.mean(ls_arr > 0) * 100), 2) if len(ls_arr) else 0.0,
        "valid_points": int(valid_points),
        "monotonicity": round(1.0 if ls > 0 else -1.0 if ls < 0 else 0.0, 4),
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

    for idx, dt in enumerate(usable_dates):
        if idx % max(1, rebalance_days) == 0 and idx + 1 < len(usable_dates):
            exec_date = usable_dates[idx + 1]
            scores = {}
            for code, series in factors.items():
                if dt in series.index and pd.notna(series.get(dt)):
                    scores[code] = float(series.get(dt))
            if len(scores) >= 5:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=reverse)
                selected = [x[0] for x in ranked[:max(1, int(len(ranked) * select_pct))]]
                for code in list(holdings):
                    if code in selected:
                        continue
                    price = ctx.stock_data.get(code, {}).get("close", pd.Series(dtype=float)).get(exec_date)
                    shares = holdings.get(code, 0)
                    if shares > 0 and pd.notna(price) and price > 0:
                        sell_price = float(price) * max(0.0, 1 - slippage)
                        cash += shares * sell_price * (1 - commission - stamp_tax)
                    holdings.pop(code, None)

                portfolio_value = cash
                for code, shares in holdings.items():
                    price = ctx.stock_data.get(code, {}).get("close", pd.Series(dtype=float)).get(exec_date)
                    if pd.notna(price) and price > 0:
                        portfolio_value += shares * float(price)
                target_value = portfolio_value / max(len(selected), 1)
                for code in selected:
                    price = ctx.stock_data.get(code, {}).get("close", pd.Series(dtype=float)).get(exec_date)
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
        yield item
        for mutated, window in _parameter_neighbors(item.expression):
            yield Candidate(_parameterized_factor_name(item.name, window), mutated, f"将原始窗口替换为 {window} 日后重新验证。", "param_scan")
    index = 1
    _operator_pool_available()
    while True:
        elites = elite_provider()
        if len(elites) >= 2 and random.random() < 0.35:
            left, right = random.sample(elites[: min(len(elites), 20)], 2)
            yield Candidate(
                f"精英组合 {index}",
                _combine_expressions(left["expression"], right["expression"]),
                "由已验证精英因子做 Rank 组合或价差信号组合。",
                "ensemble",
            )
        elif elites and random.random() < 0.35:
            base = random.choice(elites[: min(len(elites), 20)])
            yield Candidate(
                f"精英突变 {index}",
                _mutate_expression(base["expression"]),
                "对已验证精英因子做窗口、变量或标准化扰动。",
                "elite_mutation",
            )
        else:
            yield Candidate(
                f"语法树搜索 {index}",
                _random_expr(0, config.max_depth),
                "从因子工作流算子空间随机生成的可执行表达式。",
                "grammar",
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
    ctx = MiningDataContext(cache, engine, config.resolved["codes"], config.start_date, config.end_date)

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
                payload = _evaluate_candidate(candidate, ctx, config)
            except Exception:
                payload = None
            if not payload:
                continue
            inserted_id = run_async(_insert_candidate(session_id, payload))
            if inserted_id is None:
                continue
            accepted += 1
            payload["id"] = inserted_id
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
        },
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
        "started_at": session.started_at.isoformat() if session.started_at else "",
        "updated_at": session.updated_at.isoformat() if session.updated_at else "",
        "stopped_at": session.stopped_at.isoformat() if session.stopped_at else "",
        "params": session.params or {},
    }


def _serialize_candidate(item: FactorMiningCandidate) -> dict:
    return {
        "id": item.id,
        "session_id": item.session_id,
        "name": item.name,
        "description": item.description,
        "expression": item.expression,
        "source": item.source,
        "direction": item.direction,
        "complexity": item.complexity,
        "score": item.score,
        "metrics": item.metrics or {},
        "train_metrics": item.train_metrics or {},
        "valid_metrics": item.valid_metrics or {},
        "test_metrics": item.test_metrics or {},
        "backtest_metrics": item.backtest_metrics or {},
        "equity_curve": item.equity_curve or [],
        "normalized_curve": item.normalized_curve or [],
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

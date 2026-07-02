"""Factor mining workflow.

The mining path is deliberately bounded: it generates executable factor
expressions, evaluates them on real cached market data, runs a next-bar
selection backtest, and then tests simple ensembles of the strongest factors.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
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
from engine.execution_simulator import CanonicalExecutionSimulator, ExecutionSettings, PanelMarketData
from factor.factor_engine import FactorEngine
from factor.genetic import run_gp
from factor.graph_compiler import NODE_REGISTRY
from services.backtest_service import resolve_backtest_universe
from services.factor_board_service import get_system_universes
from services.llm_gateway import chat_complete_text
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


def _warmup_start_date(start_date: str, days: int = 540) -> str:
    try:
        return (datetime.strptime(str(start_date), "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    except Exception:
        return start_date


def _is_sqlite_lock_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "sqlite_busy" in text


async def _sleep_for_db_retry(attempt: int) -> None:
    await asyncio.sleep(min(0.15 * (attempt + 1), 1.5))


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

RESEARCH_FORMULA_FAMILIES = [
    {
        "theme": "volatility_adjusted_momentum",
        "source": "research_formula",
        "hypothesis": "动量信号需要用波动、成交拥挤度和回撤质量约束，否则容易选择高噪声反弹。",
        "templates": [
            ("波动调整动量_{w}日", "(pctchange(close, {w}) / (std(returns, {w}) + 0.0001))", "收益除以波动，衡量单位风险趋势强度。"),
            ("低回撤动量_{w}日", "(pctchange(close, {w}) - abs((close / (ts_max(high, {w}) + 0.0001) - 1)))", "趋势收益扣除距离阶段高点的回撤惩罚。"),
            ("量能确认动量_{w}日", "(pctchange(close, {w}) * (mean(vol, 5) / (mean(vol, {w}) + 0.0001)))", "价格趋势需要短期成交量确认。"),
        ],
    },
    {
        "theme": "behavioral_reversal",
        "source": "research_formula",
        "hypothesis": "A股短周期交易者占比较高，极端收益、上影线和放量后更容易出现短期反转。",
        "templates": [
            ("放量反转_{w}日", "(-pctchange(close, {w}) * (mean(vol, 5) / (mean(vol, {w}) + 0.0001)))", "放量急涨急跌后的反向修正。"),
            ("影线反转_{w}日", "(-((high - max(open, close)) / (abs(close - open) + 0.0001)))", "长上影线反映冲高回落后的供给压力。"),
            ("VWAP偏离反转_{w}日", "(-((close - vwap) / (std(close, {w}) + 0.0001)))", "收盘价偏离成交均价后的均值回归。"),
        ],
    },
    {
        "theme": "liquidity_and_crowding",
        "source": "research_formula",
        "hypothesis": "拥挤交易和流动性冲击会降低未来收益，低拥挤且容量可交易的信号更稳健。",
        "templates": [
            ("Amihud低冲击_{w}日", "(-mean(abs(returns) / (amount + 1.0), {w}))", "单位成交额价格冲击越小，交易容量越好。"),
            ("成交额稳定_{w}日", "(-(std(amount, {w}) / (mean(amount, {w}) + 1.0)))", "成交额稳定性高，降低流动性跳变风险。"),
            ("换手拥挤反转_{w}日", "(-turnover_rate * abs(pctchange(close, {w})))", "高换手叠加大涨跌更可能是拥挤交易。"),
        ],
    },
    {
        "theme": "range_breakout_quality",
        "source": "research_formula",
        "hypothesis": "突破类因子只有在波动压缩、振幅可控且收盘位置强时更可能延续。",
        "templates": [
            ("压缩突破_{w}日", "((close - ts_min(low, {w})) / (ts_max(high, {w}) - ts_min(low, {w}) + 0.0001) - std(returns, {w}))", "阶段位置强且波动较低的突破。"),
            ("真实波幅压缩_{w}日", "(-(mean(tr, {w}) / (mean(close, {w}) + 0.0001)))", "真实波幅占价格比例越低，代表蓄势压缩。"),
            ("收盘强度_{w}日", "mean((close - low) / (high - low + 0.0001), {w})", "每日收盘接近高点的持续性。"),
        ],
    },
    {
        "theme": "valuation_quality_proxy",
        "source": "research_formula",
        "hypothesis": "估值因子需要结合风险和成交约束，单纯低估值可能陷入价值陷阱。",
        "templates": [
            ("低PB低波_{w}日", "(-pb - std(returns, {w}))", "低市净率叠加低波动的防守型价值暴露。"),
            ("盈利收益低拥挤_{w}日", "(-pe - turnover_rate)", "低估值同时规避高换手拥挤。"),
            ("小盘质量_{w}日", "(-log(circ_mv + 1) - std(returns, {w}))", "小盘暴露需用波动控制质量。"),
        ],
    },
    {
        "theme": "price_volume_structure",
        "source": "research_formula",
        "hypothesis": "价量关系的方向、滞后和协方差可刻画资金推动是否真实有效。",
        "templates": [
            ("价量背离_{w}日", "(-corr(pctchange(close, 1), pctchange(vol, 1), {w}))", "价格上涨但成交不确认时后续收益更弱。"),
            ("量先价后_{w}日", "corr(delay(pctchange(vol, 1), 1), pctchange(close, 1), {w})", "成交量变化领先价格变化的资金推动。"),
            ("成交额趋势确认_{w}日", "(pctchange(close, {w}) * pctchange(mean(amount, 5), {w}))", "价格与成交额趋势同向确认。"),
        ],
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
        result.append({"date": date, "value": round((strategy[date] - benchmark[date]) * 100.0, 4)})
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
    research_generated: list[Candidate] = []
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
                Candidate(
                    f"{window}d_breakout_quality",
                    f"(close - ts_min(low, {window})) / (ts_max(high, {window}) - ts_min(low, {window}) + 0.0001)",
                    f"Range-position breakout quality over {window} days.",
                    "param_scan",
                    "breakout_structure",
                    "Closing near the top of a recent range may capture controlled trend continuation.",
                ),
                Candidate(
                    f"{window}d_turnover_crowding",
                    f"-(mean(vol, {window}) / (mean(vol, {max(20, window * 2)}) + 0.0001))",
                    f"Low crowding turnover signal over {window} days.",
                    "param_scan",
                    "liquidity_crowding",
                    "Lower crowding and less extreme turnover can reduce reversal pressure.",
                ),
                Candidate(
                    f"{window}d_vwap_reversion",
                    f"-((close - vwap) / (std(close, {window}) + 0.0001))",
                    f"Close-to-VWAP mean reversion over {window} days.",
                    "param_scan",
                    "intraday_reversion",
                    "Large deviations from VWAP may partly mean-revert when not backed by persistent flow.",
                ),
            ]
        )
    for family in RESEARCH_FORMULA_FAMILIES:
        for window in windows:
            for name_template, expression_template, description in family["templates"]:
                research_generated.append(
                    Candidate(
                        name_template.format(w=window),
                        expression_template.format(w=window),
                        description,
                        family["source"],
                        family["theme"],
                        family["hypothesis"],
                    )
                )
    pair_windows = [(5, 20), (10, 30), (20, 60), (30, 120)]
    for short, long in pair_windows:
        research_generated.extend(
            [
                Candidate(
                    f"长短动量斜率_{short}_{long}日",
                    f"(pctchange(close, {short}) - pctchange(close, {long}))",
                    "比较短期和长期趋势强度，识别动量改善或衰减。",
                    "research_formula",
                    "trend_slope",
                    "趋势斜率比单窗口动量更能刻画资金边际变化。",
                ),
                Candidate(
                    f"长短波动收敛_{short}_{long}日",
                    f"(-(std(returns, {short}) / (std(returns, {long}) + 0.0001)))",
                    "短期波动相对长期波动收敛，代表风险释放后的稳定状态。",
                    "research_formula",
                    "volatility_term_structure",
                    "波动期限结构可用于过滤噪声和拥挤风险。",
                ),
                Candidate(
                    f"长短成交额确认_{short}_{long}日",
                    f"(mean(amount, {short}) / (mean(amount, {long}) + 1.0) * pctchange(close, {short}))",
                    "短期成交额相对长期成交额扩张并确认价格趋势。",
                    "research_formula",
                    "flow_confirmation",
                    "成交额比成交量更接近真实资金强度。",
                ),
            ]
        )
    seen = set()
    result: list[Candidate] = []
    for item in candidates + research_generated + generated:
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
        "name": "macd_volume_confirmation",
        "source": "technical_event",
        "hypothesis": "MACD and moving-average crosses are weak alone; repeated crosses plus volume expansion can proxy repaired trend participation.",
        "templates": [
            {"name": "macd_second_cross_volume_20", "expression": "where((count_true(cross_up(macd_dif(close, 12, 26), macd_dea(close, 12, 26, 9)), 40) >= 2) & (vol_zscore(20) > 1.0), macd_hist(close, 12, 26, 9) + pctchange(close, 5), -std(returns, 20))", "description": "Second MACD golden-cross with volume confirmation."},
            {"name": "ma_second_cross_volume_20", "expression": "where((count_true(cross_up(ema(close, 5), ema(close, 20)), 40) >= 2) & (vol_zscore(20) > 1.0), pctchange(close, 5) - drawdown_from_high(close, 40), -std(returns, 20))", "description": "Second moving-average golden-cross with volume expansion."},
        ],
    },
    {
        "name": "rsi_mean_reversion_repair",
        "source": "technical_event",
        "hypothesis": "RSI recovery from oversold conditions is more useful when price stabilizes and volume confirms real demand.",
        "templates": [
            {"name": "rsi_repair_volume_20", "expression": "where((rsi(close, 14) > 35) & (delay(rsi(close, 14), 5) < 35) & (vol_zscore(20) > 0.5), (50 - abs(rsi(close, 14) - 50)) / 50 + pctchange(close, 5), -abs(drawdown_from_high(close, 40)))", "description": "RSI repair with moderate volume confirmation."},
        ],
    },
    {
        "name": "compression_breakout",
        "source": "technical_event",
        "hypothesis": "Breakouts after volatility compression and volume confirmation are more tradable than already-crowded high-volatility breakouts.",
        "templates": [
            {"name": "breakout_compression_volume_20", "expression": "where((breakout(close, 40) > 0) & (std(returns, 20) < std(returns, 40)) & (vol_zscore(20) > 0.8), pctchange(close, 20) / (std(returns, 20) + 0.0001), -abs(pctchange(close, 5)))", "description": "Low-volatility compression followed by volume-confirmed breakout."},
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
    {
        "name": "short_term_reversal",
        "source": "research_seed",
        "hypothesis": "Short-term price reversals are robust in A-shares due to retail overreaction.",
        "templates": [
            {"name": "reversal_5d", "expression": "-(pctchange(close, 5))", "description": "5-day reversal factor."},
            {"name": "reversal_10d_vol_adj", "expression": "-(pctchange(close, 10)) / (std(returns, 20) + 0.0001)", "description": "Vol-adjusted 10-day reversal."},
            {"name": "momentum_drawdown_adj", "expression": "pctchange(close, 40) / (abs(drawdown_from_high(close, 40)) + 0.01)", "description": "Drawdown-adjusted 40-day momentum."},
        ],
    },
    {
        "name": "low_volatility",
        "source": "research_seed",
        "hypothesis": "Low-volatility anomaly: low-vol stocks earn risk-adjusted premium in A-shares.",
        "templates": [
            {"name": "neg_realized_vol_20", "expression": "-(std(returns, 20))", "description": "Negative realized vol (low-vol factor)."},
            {"name": "neg_downside_vol_30", "expression": "-(downside_std(returns, 30))", "description": "Negative downside volatility."},
            {"name": "smooth_momentum_20", "expression": "pctchange(close, 20) * efficiency_ratio(close, 20)", "description": "Return weighted by efficiency ratio."},
        ],
    },
    {
        "name": "value_quality",
        "source": "research_seed",
        "hypothesis": "Combined value+quality: low PE/PB with low volatility outperforms in China.",
        "templates": [
            {"name": "ep_ratio", "expression": "1 / (pe + 0.1)", "description": "Earnings yield (1/PE)."},
            {"name": "bp_ratio", "expression": "1 / (pb + 0.1)", "description": "Book-to-price (1/PB)."},
            {"name": "value_quality_combo", "expression": "(1 / (pe + 0.1)) * (1 / (std(returns, 30) + 0.001))", "description": "Value filtered by low-vol quality."},
            {"name": "amihud_illiq_20", "expression": "mean(abs(returns) / (amount / 1e6 + 0.001), 20)", "description": "Amihud illiquidity (20-day)."},
        ],
    },
    {
        "name": "alpha101_price_volume",
        "source": "research_seed",
        "hypothesis": "WorldQuant Alpha101-style price-volume correlation factors. Negative correlation between volume and price change signals informed vs. uninformed trading.",
        "templates": [
            {"name": "alpha_neg_vol_ret_corr_6", "expression": "-corr(rank(delta(log(vol + 0.001), 2)), rank(returns), 6)", "description": "Alpha2: neg corr of volume delta rank and return rank."},
            {"name": "alpha_neg_open_vol_corr_10", "expression": "-corr(rank(open), rank(vol), 10)", "description": "Alpha3: neg rank corr of open price and volume."},
            {"name": "alpha_high_vol_corr_5", "expression": "-corr(rank(high), rank(vol), 5)", "description": "Alpha16 variant: neg rank corr of high and volume."},
            {"name": "alpha_vol_momentum_8", "expression": "ts_rank(vol / (mean(vol, 20) + 0.001), 20) * ts_rank(-(delta(close, 7)), 8)", "description": "Alpha43: vol ratio rank times price reversal rank."},
            {"name": "alpha_vwap_spread", "expression": "sqrt(abs(high * low)) - vwap", "description": "Alpha41: geometric mid-price minus vwap."},
        ],
    },
    {
        "name": "alpha101_structural",
        "source": "research_seed",
        "hypothesis": "Structural price patterns from WorldQuant Alpha101. Open-close spread and range patterns predict future returns through market microstructure.",
        "templates": [
            {"name": "alpha_open_close_rank", "expression": "rank(open / close - 1)", "description": "Alpha33: rank of open-to-close return."},
            {"name": "alpha_ts_rank_times_oc", "expression": "-ts_rank(close, 10) * rank(close / open)", "description": "Alpha38: time-series rank times open-close ratio."},
            {"name": "alpha_high_vol_std", "expression": "-std(high, 10) * corr(high, vol, 10)", "description": "Alpha40: negative high volatility weighted by vol correlation."},
            {"name": "alpha_vwap_ratio", "expression": "rank(vwap - close) / (rank(vwap + close) + 0.001)", "description": "Alpha42: vwap premium ratio."},
            {"name": "alpha_max_corr_3", "expression": "-ts_max(corr(ts_rank(vol, 5), ts_rank(high, 5), 5), 3)", "description": "Alpha26: max rolling volume-high rank correlation."},
        ],
    },
    {
        "name": "ic_stable_factors",
        "source": "research_seed",
        "hypothesis": "Factors designed for IC stability across market regimes. Uses cross-sectional rank normalization and multi-window smoothing to reduce IC decay.",
        "templates": [
            {"name": "smooth_rank_momentum_20", "expression": "mean(rank(pctchange(close, 5)), 20) - mean(rank(pctchange(close, 5)), 60)", "description": "Smoothed rank momentum: recent vs long-term rank percentile."},
            {"name": "rank_reversal_stable", "expression": "-(rank(pctchange(close, 5)) * rank(1 / (std(returns, 20) + 0.001)))", "description": "Low-vol reversal with rank normalization for IC stability."},
            {"name": "vol_weighted_return_10", "expression": "mean(returns * (1 / (std(returns, 20) + 0.001)), 10)", "description": "Volatility-inverse weighted return (IC-stable momentum)."},
            {"name": "signed_log_volume_delta", "expression": "sign(delta(vol, 1)) * log(abs(delta(vol, 1)) + 1)", "description": "Signed log volume change (mitigates outlier impact)."},
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
    graph_json = None
    if candidate_id:
        result = await db.execute(select(FactorMiningCandidate).where(FactorMiningCandidate.id == int(candidate_id)))
        candidate = result.scalar_one_or_none()
        if candidate:
            session = None
            session_result = await db.execute(select(FactorMiningSession).where(FactorMiningSession.session_id == candidate.session_id))
            session = session_result.scalar_one_or_none()
            params = (session.params or {}) if session else {}
            backtest_metrics = candidate.backtest_metrics or {}
            display_curve = backtest_metrics.get("display_equity_curve") or candidate.equity_curve or []
            curve_dates = [str(point.get("date") or "") for point in display_curve if point.get("date")]
            test_curve = backtest_metrics.get("test_equity_curve") or []
            test_dates = [str(point.get("date") or "") for point in test_curve if point.get("date")]
            name = name or candidate.name
            expression = candidate.expression
            description = description or candidate.description
            description = (
                f"{description}\n\n"
                f"挖掘会话: {candidate.session_id}\n"
                f"因子方向: {'因子值越小越好' if candidate.direction == 'bottom' else '因子值越大越好'}\n"
                f"调仓周期: {int(backtest_metrics.get('chosen_rebalance_days') or params.get('rebalance_days') or 5)} 日\n"
                f"选股比例: {float(params.get('select_pct') or 0.1):.4f}\n"
                f"综合评分: {_safe_float(candidate.score):.4f}\n"
                f"验证指标: {candidate.valid_metrics or {}}\n"
                f"测试指标: {candidate.test_metrics or {}}"
            ).strip()
            description = (
                f"{description}\n"
                f"protocol_version: {(candidate.backtest_metrics or {}).get('protocol_version', 'legacy_unverified')}\n"
                f"strict_note: only {STRICT_MINING_PROTOCOL_VERSION} candidates should be treated as current out-of-sample results."
            ).strip()
            graph_json = {
                "source": "factor_mining",
                "mining": {
                    "candidate_id": candidate.id,
                    "session_id": candidate.session_id,
                    "direction": candidate.direction or "top",
                    "high_is_better": candidate.direction != "bottom",
                    "select_pct": float(params.get("select_pct") or 0.1),
                    "rebalance_days": int(backtest_metrics.get("chosen_rebalance_days") or params.get("rebalance_days") or 5),
                    "target_exposure": DEFAULT_TARGET_EXPOSURE,
                    "max_position_pct": DEFAULT_MAX_POSITION_PCT,
                    "protocol_version": candidate.protocol_version or backtest_metrics.get("protocol_version") or STRICT_MINING_PROTOCOL_VERSION,
                    "validation_status": candidate.revalidation_status or backtest_metrics.get("validation_status", ""),
                    "score": _safe_float(candidate.score),
                    "test_excess_return": _safe_float(backtest_metrics.get("excess_return")),
                    "test_total_return": _safe_float(backtest_metrics.get("total_return")),
                    "test_start": test_dates[0] if test_dates else "",
                    "test_end": test_dates[-1] if test_dates else "",
                    "display_start": backtest_metrics.get("display_start_date") or (curve_dates[0] if curve_dates else ""),
                    "display_end": backtest_metrics.get("display_end_date") or (curve_dates[-1] if curve_dates else ""),
                    "display_scope": backtest_metrics.get("display_scope", ""),
                    "session_start_date": session.start_date if session else "",
                    "session_end_date": session.end_date if session else "",
                    "universe_type": session.universe_type if session else "",
                    "universe_code": session.universe_code if session else "",
                    "universe_name": session.universe_name if session else "",
                    "custom_pool_id": session.custom_pool_id if session else None,
                    "benchmark": session.universe_code if session and session.universe_type == "system" else "000300.SH",
                },
            }
    factor = Factor(
        name=name,
        description=description,
        expression=expression,
        category="因子挖掘",
        source="mining_session" if candidate_id else "mining",
        graph_json=graph_json,
        factor_type="technical",
    )
    db.add(factor)
    await db.commit()
    await db.refresh(factor)
    return {"id": factor.id, "name": factor.name}


FIB_WINDOW_SPACE = [2, 3, 5, 8, 13, 21, 34, 55, 89, 144]
EXTRA_WINDOW_SPACE = [4, 6, 7, 9, 10, 11, 12, 15, 16, 18, 20, 24, 30, 40, 60, 90, 120]
WINDOW_SPACE = sorted(set(FIB_WINDOW_SPACE + EXTRA_WINDOW_SPACE))
FIB_RATIO_WEIGHTS = [0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
TERMINAL_SPACE = [
    "close", "open", "high", "low", "vol", "returns", "vwap", "amount",
    "pe", "pb", "ps", "turnover_rate", "circ_mv",
]


def _sample_window(min_window: int = 2, max_window: int = 144, *, fib_bias: float = 0.78) -> int:
    """Sample windows with a Fibonacci bias plus non-Fibonacci exploration."""
    min_window = int(min_window or 2)
    max_window = int(max_window or 144)
    fib_pool = [w for w in FIB_WINDOW_SPACE if min_window <= w <= max_window]
    extra_pool = [w for w in EXTRA_WINDOW_SPACE if min_window <= w <= max_window]
    if fib_pool and random.random() < fib_bias:
        return int(random.choice(fib_pool))
    if extra_pool and random.random() < 0.75:
        return int(random.choice(extra_pool))
    return int(random.randint(min_window, max(max_window, min_window)))


def _sample_weight() -> float:
    if random.random() < 0.68:
        return float(random.choice(FIB_RATIO_WEIGHTS))
    return round(random.uniform(0.15, 0.85), 3)
UNARY_TEMPLATES = [
    "rank({})", "cs_rank({})", "abs({})", "neg({})",
    "log(abs({}) + 1)", "sqrt(abs({}) + 0.0001)",
    "signedpower({}, 2)", "clip({}, -3, 3)",
    "rsi({}, 14)", "macd_hist({}, 12, 26, 9)",
    "drawdown_from_high({}, 20)", "breakout({}, 20)",
    "efficiency_ratio({}, 20)",
]
BINARY_TEMPLATES = [
    "({} + {})", "({} - {})", "({} * {})", "({} / (abs({}) + 0.0001))",
    "max({}, {})", "min({}, {})",
]
ROLLING_TEMPLATES = [
    "mean({}, {w})", "std({}, {w})", "sum({}, {w})", "ts_max({}, {w})",
    "ts_min({}, {w})", "ts_rank({}, {w})", "delta({}, {w})", "delay({}, {w})",
    "pctchange({}, {w})", "ts_decay({}, {w})", "ewm({}, {w})",
    "ema({}, {w})", "zscore({}, {w})",
    "skew({}, {w})", "kurt({}, {w})", "downside_std({}, {w})", "upside_std({}, {w})", "efficiency_ratio({}, {w})",
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
    "where((cross_up({}, {}) > 0), {}, {})",
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
        self.stock_names: dict[str, str] = {}
        self.all_dates: list = []
        self.analysis_dates: list = []
        self.benchmark_close = pd.Series(dtype=float)
        self.expression_cache: dict[str, dict[str, pd.Series]] = {}

    def preload(self, settings: SimpleNamespace, stop_event: threading.Event, progress_callback=None) -> None:
        completed = 0
        failed = 0
        total = len(self.universe)
        # Use more workers; pickle cache is per-stock-filename so threads never conflict.
        # One shared TushareClient is used via self.cache to avoid expensive per-stock init.
        max_workers = min(max(os.cpu_count() or 4, 8), 16)
        warmup_start = _warmup_start_date(self.start_date)
        display_start = pd.to_datetime(self.start_date)
        display_end = pd.to_datetime(self.end_date)

        def load_one(code: str):
            # Reuse the shared cache (pickle files are per-stock, thread-safe for different codes)
            daily = self.cache.get_daily(code, warmup_start, self.end_date, adj="qfq")
            exec_daily = self.cache.get_daily(code, warmup_start, self.end_date, adj=None)
            basic = self.cache.get_daily_basic(code, warmup_start, self.end_date)
            return code, daily, exec_daily, basic

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(load_one, code): code for code in self.universe}
            for future in as_completed(futures):
                if stop_event.is_set():
                    break
                code = futures[future]
                try:
                    _, daily, exec_daily, basic = future.result()
                    item = self._prepare_stock_data(daily, basic, exec_daily=exec_daily)
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
            all_dates.update([dt for dt in item["close"].dropna().index.tolist() if pd.to_datetime(dt) <= display_end])
        self.all_dates = sorted(all_dates)
        self.analysis_dates = [dt for dt in self.all_dates if display_start <= pd.to_datetime(dt) <= display_end]
        try:
            basics = self.cache.client.get_stock_basic()
            if basics is not None and not basics.empty:
                self.stock_names = {
                    str(row.get("ts_code") or "").upper(): str(row.get("name") or "")
                    for row in basics.to_dict("records")
                }
        except Exception:
            self.stock_names = {}
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

    def _prepare_stock_data(self, daily: pd.DataFrame, basic: pd.DataFrame, exec_daily: Optional[pd.DataFrame] = None) -> Optional[dict]:
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
        exec_indexed = indexed
        if exec_daily is not None and not exec_daily.empty:
            edf = exec_daily.copy()
            edf["trade_date"] = pd.to_datetime(edf["trade_date"], errors="coerce")
            edf = edf.dropna(subset=["trade_date"]).sort_values("trade_date")
            if not edf.empty:
                exec_indexed = edf.set_index("trade_date").reindex(indexed.index)
        exec_closes = pd.to_numeric(exec_indexed.get("close", closes), errors="coerce").astype(float).reindex(indexed.index).ffill()
        exec_highs = pd.to_numeric(exec_indexed.get("high", highs), errors="coerce").astype(float).reindex(indexed.index).ffill()
        exec_lows = pd.to_numeric(exec_indexed.get("low", lows), errors="coerce").astype(float).reindex(indexed.index).ffill()
        exec_opens = pd.to_numeric(exec_indexed.get("open", opens), errors="coerce").astype(float).reindex(indexed.index).ffill()
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
            "close": exec_closes, "high": exec_highs, "low": exec_lows, "vol": volumes,
            "open": exec_opens,
            "factor_close": closes, "factor_open": opens, "factor_high": highs, "factor_low": lows,
            "amount": amounts, "basic": basic_data,
            "prev_close": exec_closes.shift(1),
            "forward_return": closes.pct_change(5).shift(-5),
        }

    def compute_expression(self, expression: str) -> dict[str, pd.Series]:
        expression_key = _expression_hash(expression)
        if expression_key in self.expression_cache:
            return self.expression_cache[expression_key]
        result = {}
        for code, data in self.stock_data.items():
            values = self.engine._eval_expression(
                expression,
                data["factor_close"], data["factor_high"], data["factor_low"], data["vol"],
                data["factor_open"], data["basic"], data["amount"],
            )
            if values is None or not isinstance(values, pd.Series):
                continue
            cleaned = values.replace([np.inf, -np.inf], np.nan)
            if cleaned.dropna().empty:
                continue
            result[code] = cleaned
        if len(self.expression_cache) > 512:
            for key in list(self.expression_cache.keys())[:128]:
                self.expression_cache.pop(key, None)
        self.expression_cache[expression_key] = result
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


def _strip_outer_parentheses(text: str) -> str:
    text = str(text or "").strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        balanced = True
        for idx, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and idx != len(text) - 1:
                    balanced = False
                    break
            if depth < 0:
                balanced = False
                break
        if not balanced or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _split_expression_sign(expression: str) -> tuple[int, str]:
    text = re.sub(r"\s+", "", str(expression or ""))
    sign = 1
    changed = True
    while changed:
        changed = False
        text = _strip_outer_parentheses(text)
        if text.startswith("neg(") and text.endswith(")"):
            sign *= -1
            text = text[4:-1]
            changed = True
            continue
        if text.startswith("-"):
            sign *= -1
            text = text[1:]
            changed = True
    return sign, _strip_outer_parentheses(text)


def _opposite_direction(direction: str) -> str:
    return "top" if direction == "bottom" else "bottom"


def _trading_equivalence_key(expression: str, direction: str) -> str:
    sign, core = _split_expression_sign(expression)
    effective_direction = str(direction or "top").lower()
    if sign < 0:
        effective_direction = _opposite_direction(effective_direction)
    normalized = f"{core}|{effective_direction}"
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
        commission_rate=float(settings.commission_rate if settings.commission_rate is not None else 0.0003),
        stamp_tax_rate=float(settings.stamp_tax_rate if settings.stamp_tax_rate is not None else 0.001),
        slippage=float(settings.slippage if settings.slippage is not None else 0.002),
        use_mysql=getattr(settings, "use_mysql", 0),
        mysql_host=getattr(settings, "mysql_host", ""),
        mysql_port=getattr(settings, "mysql_port", 3306),
        mysql_user=getattr(settings, "mysql_user", ""),
        mysql_password=getattr(settings, "mysql_password", ""),
        mysql_database=getattr(settings, "mysql_database", "qtsys"),
        llm_api_key=getattr(settings, "llm_api_key", ""),
        llm_base_url=getattr(settings, "llm_base_url", ""),
        llm_model=getattr(settings, "llm_model", ""),
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


def _forward_open_return(ctx: MiningDataContext, code: str, signal_date, forward_days: int) -> float:
    try:
        idx = ctx.all_dates.index(signal_date)
    except ValueError:
        return np.nan
    entry_idx = idx + 1
    exit_idx = idx + 1 + max(1, int(forward_days or 1))
    if entry_idx >= len(ctx.all_dates) or exit_idx >= len(ctx.all_dates):
        return np.nan
    data = ctx.stock_data.get(code)
    if not data:
        return np.nan
    open_series = data.get("open", pd.Series(dtype=float))
    entry_open = open_series.get(ctx.all_dates[entry_idx])
    exit_open = open_series.get(ctx.all_dates[exit_idx])
    if pd.isna(entry_open) or pd.isna(exit_open) or entry_open <= 0 or exit_open <= 0:
        return np.nan
    return float(exit_open) / float(entry_open) - 1.0



def _compute_ic_halflife(factors: dict[str, pd.Series], ctx: MiningDataContext, dates: set) -> dict:
    """WorldQuant-style IC decay analysis: compute IC at multiple forward windows.
    
    Returns the IC at each period and an estimated half-life (days until IC drops to 50% of peak).
    Used to determine optimal rebalancing frequency.
    """
    windows = [1, 3, 5, 10, 20]
    ic_by_window = {}
    for w in windows:
        ic_vals = []
        for dt in sorted(dates):
            row = []
            for code, series in factors.items():
                if dt not in series.index:
                    continue
                fv = series.get(dt)
                fr = _forward_open_return(ctx, code, dt, w)
                if pd.notna(fv) and pd.notna(fr):
                    row.append((float(fv), float(fr)))
            if len(row) >= 5:
                ic = _rank_ic([x[0] for x in row], [x[1] for x in row])
                if pd.notna(ic):
                    ic_vals.append(ic)
        ic_by_window[w] = abs(float(np.nanmean(ic_vals))) if ic_vals else 0.0

    peak_ic = max(ic_by_window.values()) if ic_by_window else 0.0
    half_threshold = peak_ic * 0.5
    optimal_window = max(ic_by_window, key=ic_by_window.get) if ic_by_window else 5
    # Estimate half-life: first window where IC drops below 50% of peak
    halflife = 20  # default
    if peak_ic > 0:
        for w in windows:
            if ic_by_window[w] <= half_threshold:
                halflife = w
                break
    return {
        "ic_by_window": {str(w): round(v, 6) for w, v in ic_by_window.items()},
        "optimal_window": int(optimal_window),
        "peak_ic": round(peak_ic, 6),
        "halflife_days": int(halflife),
    }


def _segment_metrics(factors: dict[str, pd.Series], ctx: MiningDataContext, dates: set, forward_days: int = 5) -> dict:
    ic_values = []
    long_short = []
    valid_points = 0
    group_spreads = []
    for dt in sorted(dates):
        row = []
        for code, series in factors.items():
            if dt not in series.index:
                continue
            fv = series.get(dt)
            fr = _forward_open_return(ctx, code, dt, forward_days)
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
        "ic_stability": round(float(np.mean(ic_arr * ic_direction > 0.01)) if len(ic_arr) > 4 else 0.0, 4),
        "ic_skew": round(float(np.skew(ic_arr)) if len(ic_arr) > 4 else 0.0, 4) if hasattr(np, 'skew') else 0.0,
    }


def _legacy_unused_backtest_from_factors(
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


def _legacy_unused_score_candidate(valid_metrics: dict, test_metrics: dict, backtest_metrics: dict, complexity: int) -> float:
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


def _legacy_unused_evaluate_candidate(candidate: Candidate, ctx: MiningDataContext, config: StreamingMiningConfig) -> Optional[dict]:
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

    splits = _date_splits(getattr(ctx, "analysis_dates", None) or ctx.all_dates)
    train_metrics = _segment_metrics(factors, ctx, splits["train"], forward_days=config.rebalance_days)
    valid_metrics = _segment_metrics(factors, ctx, splits["valid"], forward_days=config.rebalance_days)
    test_metrics = _segment_metrics(factors, ctx, splits["test"] or splits["valid"], forward_days=config.rebalance_days)
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
    def _setting_float(name: str, default: float) -> float:
        value = getattr(settings, name, None)
        if value is None:
            return default
        try:
            return float(value)
        except Exception:
            return default

    all_dates = list(ctx.all_dates)
    segment_dates = [dt for dt in all_dates if not dates or dt in dates]
    market = PanelMarketData(
        ctx.stock_data,
        all_dates,
        benchmark_close=ctx.benchmark_close,
        benchmark_code=ctx.benchmark_code or "universe_equal_weight",
        stock_names=getattr(ctx, "stock_names", {}) or {},
    )
    simulator = CanonicalExecutionSimulator(
        ExecutionSettings(
            initial_cash=max(1.0, _setting_float("default_cash", 1_000_000)),
            commission_rate=max(0.0, _setting_float("commission_rate", 0.0003)),
            stamp_tax_rate=max(0.0, _setting_float("stamp_tax_rate", 0.001)),
            slippage=max(0.0, _setting_float("slippage", 0.002)),
            transfer_fee_rate=0.0,
            volume_limit_pct=max(0.0, _setting_float("capacity_limit_pct", DEFAULT_VOLUME_LIMIT_PCT)),
            max_position_pct=DEFAULT_MAX_POSITION_PCT,
            target_exposure=DEFAULT_TARGET_EXPOSURE,
            exclude_star_market=True,
            exclude_st=True,
            protocol_version=STRICT_MINING_PROTOCOL_VERSION,
        )
    )
    result = simulator.run_factor_selection(
        factors=factors,
        market=market,
        select_pct=select_pct,
        rebalance_days=rebalance_days,
        direction=direction,
        segment_dates=dates,
    )
    result.setdefault("benchmark_curve", [])
    result.setdefault("excess_curve", [])
    result.setdefault("rejection_reasons", {})
    if segment_dates and "assumption_report" in result:
        result["assumption_report"]["segment_start"] = _date_text(segment_dates[0])
        result["assumption_report"]["segment_end"] = _date_text(segment_dates[-1])
    return result


def _adaptive_rebalance_candidates(base_days: int) -> list[int]:
    base = max(1, min(int(base_days or 5), 60))
    seeds = [base, max(1, base // 2), min(60, base * 2), 3, 5, 10, 20]
    result: list[int] = []
    for item in seeds:
        value = max(1, min(int(item), 60))
        if value not in result:
            result.append(value)
        if len(result) >= 3:
            break
    return result


def _validation_protocol_score(valid_metrics: dict, valid_backtest_metrics: dict, complexity: int, tested_count: int) -> float:
    directional_ic = abs(_safe_float(valid_metrics.get("ic_mean")))
    ic_ir = min(abs(_safe_float(valid_metrics.get("ic_ir"))), 3.0)
    excess = _safe_float(valid_backtest_metrics.get("excess_return"))
    drawdown = max(_safe_float(valid_backtest_metrics.get("max_drawdown")), 0.0)
    trade_count = min(_safe_float(valid_backtest_metrics.get("trade_count")), 80.0)
    turnover = max(_safe_float(valid_backtest_metrics.get("turnover")), 0.0)
    return round(
        _score_candidate(valid_metrics, valid_backtest_metrics, complexity, tested_count)
        + directional_ic * 2.0
        + ic_ir * 0.18
        + max(excess, -10.0) / 80.0
        + min(trade_count / 120.0, 0.35)
        - drawdown / 320.0
        - min(turnover / 1000.0, 0.20),
        6,
    )


def _direction_candidate_score(valid_metrics: dict, valid_backtest_metrics: dict, direction: str, complexity: int, tested_count: int) -> float:
    directional_ic = _directional_ic(valid_metrics, direction)
    ic_ir = min(abs(_safe_float(valid_metrics.get("ic_ir"))), 3.0)
    excess = _safe_float(valid_backtest_metrics.get("excess_return"))
    total_return = _safe_float(valid_backtest_metrics.get("total_return"))
    drawdown = max(_safe_float(valid_backtest_metrics.get("max_drawdown")), 0.0)
    trade_count = min(_safe_float(valid_backtest_metrics.get("trade_count")), 120.0)
    turnover = max(_safe_float(valid_backtest_metrics.get("turnover")), 0.0)
    complexity_penalty = min(max(complexity, 0) / 120.0, 0.35)
    multiple_test_penalty = min(math.log1p(max(tested_count, 0)) / 18.0, 0.45)
    return round(
        max(directional_ic, -0.05) * 5.0
        + ic_ir * 0.12
        + max(excess, -15.0) / 55.0
        + max(total_return, -20.0) / 180.0
        + min(trade_count / 200.0, 0.30)
        - drawdown / 240.0
        - min(turnover / 1000.0, 0.20)
        - complexity_penalty
        - multiple_test_penalty,
        6,
    )


def _select_validation_protocol(
    factors: dict[str, pd.Series],
    ctx: MiningDataContext,
    config: StreamingMiningConfig,
    splits: dict[str, set],
    quality_metrics: dict,
    complexity: int,
    tested_count: int,
) -> Optional[dict]:
    best: Optional[dict] = None
    for rebalance_days in _adaptive_rebalance_candidates(config.rebalance_days):
        train_metrics = _segment_metrics(factors, ctx, splits["train"], forward_days=rebalance_days)
        valid_core = _segment_metrics(factors, ctx, splits["valid"], forward_days=rebalance_days)
        if _safe_float(valid_core.get("ic_count")) < 3:
            continue
        valid_metrics = {**valid_core, **quality_metrics}
        ic_hint = _safe_float(valid_core.get("ic_mean"))
        ls_hint = _safe_float(valid_core.get("long_short_return"))
        primary_direction = "bottom" if (ic_hint < 0 or ls_hint < 0) else "top"
        directions = [primary_direction]
        if abs(ic_hint) < 0.006 or abs(ls_hint) < 0.16:
            directions.append(_opposite_direction(primary_direction))
        for direction in directions:
            valid_backtest = _backtest_from_factors(
                factors,
                ctx,
                config.settings,
                select_pct=config.select_pct,
                rebalance_days=rebalance_days,
                direction=direction,
                dates=splits["valid"],
            )
            if "error" in valid_backtest or not valid_backtest.get("normalized_curve"):
                continue
            score = _direction_candidate_score(valid_metrics, valid_backtest["metrics"], direction, complexity, tested_count)
            candidate = {
                "rebalance_days": rebalance_days,
                "train_metrics": train_metrics,
                "valid_metrics": valid_metrics,
                "valid_backtest": valid_backtest,
                "direction": direction,
                "selection_score": score,
            }
            if best is None or score > best["selection_score"]:
                best = candidate
    return best


def _fast_screen_candidate(
    factors: dict[str, pd.Series],
    ctx: MiningDataContext,
    splits: dict[str, set],
    quality_metrics: dict,
    rebalance_days: int,
) -> tuple[bool, dict]:
    """Cheap IC/coverage screen before expensive execution simulation.

    This does not decide validity; it only avoids running full A-share execution
    simulation for expressions with no measurable validation signal or coverage.
    Final displayed results still come from `_backtest_from_factors`.
    """
    metrics = _segment_metrics(factors, ctx, splits.get("valid", set()), forward_days=rebalance_days)
    valid_count = int(metrics.get("ic_count") or 0)
    directional_ic = abs(_safe_float(metrics.get("ic_mean")))
    coverage = _safe_float(quality_metrics.get("coverage_ratio"))
    if valid_count < 3:
        return False, {**metrics, "fast_reject_reason": "valid_ic_count_below_3"}
    if coverage < 45.0:
        return False, {**metrics, "fast_reject_reason": "coverage_below_45pct"}
    if directional_ic < 0.0015 and abs(_safe_float(metrics.get("long_short_return"))) < 0.015:
        return False, {**metrics, "fast_reject_reason": "validation_signal_near_zero"}
    icir = abs(_safe_float(metrics.get("ic_ir")))
    if valid_count >= 10 and directional_ic < 0.003 and icir < 0.08:
        return False, {**metrics, "fast_reject_reason": "ic_and_icir_both_too_low"}
    return True, metrics


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
    technical_ops = sorted(set(operators) & {
        "ema", "macd_dif", "macd_dea", "macd_hist", "rsi", "cross_up", "cross_down",
        "bars_since", "count_true", "vol_zscore", "gap_pct", "breakout", "drawdown_from_high",
        "skew", "kurt", "downside_std", "upside_std", "efficiency_ratio",
    })
    shape_ops = sorted(set(operators) & {"skew", "kurt", "downside_std", "upside_std", "efficiency_ratio"})
    if shape_ops:
        family = "distribution_shape"
    elif any(var in variables for var in ("pe", "pb", "ps", "turnover_rate", "circ_mv", "total_mv")):
        family = "fundamental_or_liquidity"
    elif technical_ops:
        family = "technical_event"
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
        "technical_operators": technical_ops,
        "technical_operator_count": len(technical_ops),
        "shape_operators": shape_ops,
        "event_driven": bool(technical_ops or "where" in operators or "ternary" in operators),
        "variables": variables,
        "windows": windows,
        "max_window": max(windows) if windows else 0,
        "expression_length": len(text),
    }


def _window_bucket(window: int) -> str:
    value = int(window or 0)
    if value <= 0:
        return "none"
    if value <= 5:
        return "xs"
    if value <= 15:
        return "s"
    if value <= 30:
        return "m"
    if value <= 60:
        return "l"
    return "xl"


def _novelty_key_from_fingerprint(fingerprint: dict) -> str:
    operators = [op for op in (fingerprint.get("operators") or []) if op not in {"rank", "cs_rank", "abs", "neg", "clip"}]
    variables = sorted(fingerprint.get("variables") or [])
    buckets = sorted({_window_bucket(int(w)) for w in (fingerprint.get("windows") or [])})
    key_parts = [
        str(fingerprint.get("family") or "unknown"),
        "+".join(sorted(operators)[:8]) or "raw",
        "+".join(variables[:8]) or "no_var",
        "+".join(buckets) or "no_window",
        "event" if fingerprint.get("event_driven") else "continuous",
    ]
    return "|".join(key_parts)


def _expression_novelty_key(expression: str) -> str:
    return _novelty_key_from_fingerprint(_factor_fingerprint(expression))


def _payload_homogeneity_reason(payload: dict, elites: list[dict], accepted_structure_counts: dict[str, int]) -> str:
    fingerprint = payload.get("fingerprint") or _factor_fingerprint(payload.get("expression", ""))
    novelty_key = _novelty_key_from_fingerprint(fingerprint)
    if accepted_structure_counts.get(novelty_key, 0) >= 3:
        return "same_structure_quota_reached"
    curve = _curve_value_map(payload.get("normalized_curve") or [])
    if len(curve) < 5:
        return ""
    family = fingerprint.get("family")
    theme = payload.get("theme")
    score = _safe_float(payload.get("score"))
    for item in elites:
        other_curve = _curve_value_map(item.get("normalized_curve") or [])
        dates = sorted(set(curve) & set(other_curve))
        if len(dates) < 20:
            continue
        left = np.array([curve[d] for d in dates], dtype=float)
        right = np.array([other_curve[d] for d in dates], dtype=float)
        if np.nanstd(left) <= 0 or np.nanstd(right) <= 0:
            continue
        corr = abs(float(np.corrcoef(left, right)[0, 1]))
        other_fp = item.get("fingerprint") or {}
        if corr >= 0.985:
            return "near_duplicate_equity_curve"
        if corr >= 0.96 and family and family == other_fp.get("family") and score <= _safe_float(item.get("score")) + 0.05:
            return "same_family_high_curve_correlation"
        if corr >= 0.94 and theme and theme == item.get("theme") and score <= _safe_float(item.get("score")) + 0.10:
            return "same_theme_high_curve_correlation"
    return ""


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
    ic_decay = _safe_float(robustness.get("ic_decay"), 1.0)
    # Reward factors with slow IC decay (ic_decay < 0.3 = factor sustained well in test set)
    decay_bonus = max(0.0, (0.5 - ic_decay)) * 0.4
    return round(
        base
        + dsr * 0.20
        + _safe_float(robustness.get("robustness_score")) / 120.0
        + _safe_float(capacity.get("capacity_score")) / 350.0
        - p_value * 0.30
        - pbo_penalty
        + decay_bonus,
        6,
    )


def _validation_status(
    rejection_reasons: list[str],
    significance: dict,
    overfit_risk: dict,
    capacity: dict,
    robustness: dict,
    test_metrics: dict,
    test_backtest_metrics: dict,
    config: StreamingMiningConfig,
) -> str:
    directional_ic = abs(_safe_float(test_metrics.get("ic_mean")))
    excess_return = _safe_float(test_backtest_metrics.get("excess_return"))
    dsr = _safe_float(significance.get("dsr"))
    robustness_score = _safe_float(robustness.get("robustness_score"))
    capacity_score = _safe_float(capacity.get("capacity_score"))
    pbo = str(overfit_risk.get("pbo_risk") or "unknown")
    if not rejection_reasons and dsr >= config.min_dsr and pbo != "high" and robustness_score >= 35 and capacity_score >= 25:
        return "institutional_pass"
    if excess_return > 0 or directional_ic > 0.003 or robustness_score >= 25:
        return "research_candidate"
    return "evaluated_weak"


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

    analysis_dates = getattr(ctx, "analysis_dates", None) or ctx.all_dates
    folds = _walk_forward_splits(analysis_dates, config.walk_forward_windows, config.embargo_days)
    splits = folds[-1] if folds else _date_splits(analysis_dates)
    if len(splits["valid"]) < 3 or len(splits["test"]) < 3:
        return None
    complexity = _expression_complexity(expression)
    passed_fast_screen, fast_metrics = _fast_screen_candidate(
        factors,
        ctx,
        splits,
        quality_metrics,
        max(1, int(config.rebalance_days or 5)),
    )
    if not passed_fast_screen:
        return None
    protocol = _select_validation_protocol(factors, ctx, config, splits, quality_metrics, complexity, tested_count)
    if not protocol:
        return None
    chosen_rebalance_days = int(protocol["rebalance_days"])
    train_metrics = protocol["train_metrics"]
    valid_metrics = protocol["valid_metrics"]
    valid_backtest = protocol["valid_backtest"]
    direction = protocol["direction"]
    test_metrics = _segment_metrics(factors, ctx, splits["test"], forward_days=chosen_rebalance_days)

    fold_checks = []
    for fold in folds:
        fold_valid = _segment_metrics(factors, ctx, fold["valid"], forward_days=chosen_rebalance_days)
        fold_test = _segment_metrics(factors, ctx, fold["test"], forward_days=chosen_rebalance_days)
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
        if (_safe_float(fold.get("test_ic")) >= 0 and direction == "top")
        or (_safe_float(fold.get("test_ic")) < 0 and direction == "bottom")
    ]

    test_backtest = _backtest_from_factors(
        factors,
        ctx,
        config.settings,
        select_pct=config.select_pct,
        rebalance_days=chosen_rebalance_days,
        direction=direction,
        dates=splits["test"],
    )
    if "error" in test_backtest or not test_backtest.get("normalized_curve"):
        return None

    display_backtest = _backtest_from_factors(
        factors,
        ctx,
        config.settings,
        select_pct=config.select_pct,
        rebalance_days=chosen_rebalance_days,
        direction=direction,
        dates=set(analysis_dates),
    )
    if "error" in display_backtest or not display_backtest.get("normalized_curve"):
        display_backtest = test_backtest

    discovery_score = _score_candidate(valid_metrics, valid_backtest["metrics"], complexity, tested_count)
    rejection_reasons = _strict_acceptance_reasons(
        valid_metrics,
        test_metrics,
        valid_backtest["metrics"],
        test_backtest["metrics"],
        direction,
    )
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
    trading_equivalence_key = _trading_equivalence_key(expression, direction)
    fingerprint["trading_equivalence_key"] = trading_equivalence_key
    validation_status = _validation_status(
        rejection_reasons,
        significance,
        overfit_risk,
        capacity,
        robustness,
        test_metrics,
        test_backtest["metrics"],
        config,
    )
    final_score = _institutional_final_score(test_metrics, test_backtest["metrics"], significance, overfit_risk, capacity, robustness, complexity)
    test_excess = _safe_float(test_backtest["metrics"].get("excess_return"))
    max_drawdown = max(_safe_float(test_backtest["metrics"].get("max_drawdown")), 0.0)
    directional_test_ic = _directional_ic(test_metrics, direction)
    if validation_status == "evaluated_weak":
        final_score -= 0.35
    elif validation_status == "research_candidate":
        final_score += 0.05
    if _safe_float(test_backtest["metrics"].get("trade_count")) <= 0:
        return None

    backtest_metrics = {
        **test_backtest["metrics"],
        "discovery_score": discovery_score,
        "validation_selection_score": protocol["selection_score"],
        "chosen_rebalance_days": chosen_rebalance_days,
        "search_source": candidate.source,
        "search_theme": candidate.theme or "general_formula",
        "event_driven": bool(fingerprint.get("event_driven")),
        "technical_operators": fingerprint.get("technical_operators", []),
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
        "validation_status": validation_status,
        "validation_reasons": rejection_reasons,
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
        "trading_equivalence_key": trading_equivalence_key,
        "correlation_cluster": "",
        "revalidation_status": validation_status,
        "direction": direction,
        "complexity": complexity,
        "score": final_score,
        "metrics": {**valid_metrics, "score_basis": "validation_only", "protocol_version": STRICT_MINING_PROTOCOL_VERSION},
        "train_metrics": {**train_metrics, "protocol_version": STRICT_MINING_PROTOCOL_VERSION, "forward_days": chosen_rebalance_days},
        "valid_metrics": {**valid_metrics, "forward_days": chosen_rebalance_days},
        "test_metrics": {**test_metrics, "protocol_version": STRICT_MINING_PROTOCOL_VERSION, "forward_days": chosen_rebalance_days},
        "backtest_metrics": {
            **backtest_metrics,
            "benchmark_metrics": test_backtest.get("benchmark_metrics", {}),
            "benchmark_curve": display_backtest.get("benchmark_curve", []),
            "benchmark_normalized_curve": display_backtest.get("benchmark_normalized_curve", []),
            "excess_curve": display_backtest.get("excess_curve", []),
            "display_scope": "full_period" if display_backtest is not test_backtest else "test_segment_fallback",
            "display_start_date": config.start_date,
            "display_end_date": config.end_date,
            "display_metrics": display_backtest.get("metrics", {}),
            "display_benchmark_metrics": display_backtest.get("benchmark_metrics", {}),
            "display_equity_curve": display_backtest.get("equity_curve", []),
            "display_normalized_curve": display_backtest.get("normalized_curve", []),
            "display_benchmark_curve": display_backtest.get("benchmark_curve", []),
            "display_benchmark_normalized_curve": display_backtest.get("benchmark_normalized_curve", []),
            "display_excess_curve": display_backtest.get("excess_curve", []),
            "test_equity_curve": test_backtest.get("equity_curve", []),
            "test_normalized_curve": test_backtest.get("normalized_curve", []),
            "test_benchmark_curve": test_backtest.get("benchmark_curve", []),
            "test_benchmark_normalized_curve": test_backtest.get("benchmark_normalized_curve", []),
            "test_excess_curve": test_backtest.get("excess_curve", []),
            "trades": test_backtest.get("trades", []),
            "rejection_reasons": test_backtest.get("rejection_reasons", {}),
        },
        "equity_curve": display_backtest["equity_curve"],
        "normalized_curve": display_backtest["normalized_curve"],
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
    display_start = pd.to_datetime(start_date)
    display_end = pd.to_datetime(end_date)
    ctx.analysis_dates = [dt for dt in ctx.all_dates if display_start <= pd.to_datetime(dt) <= display_end]
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
        return random.choice(ROLLING_TEMPLATES).format(_random_expr(depth + 1, max_depth), w=_sample_window())
    if roll < 0.92:
        return random.choice(PAIR_ROLLING_TEMPLATES).format(
            _random_expr(depth + 1, max_depth),
            _random_expr(depth + 1, max_depth),
            w=_sample_window(),
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
        new = _sample_window(max_window=max(max(WINDOW_SPACE), old + 5))
        if new == old:
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
        weight = _sample_weight()
        return f"({weight} * rank({left}) + {round(1 - weight, 2)} * rank({right}))"
    return f"(rank({left}) + rank({right})) / 2"


def _stochastic_leaf() -> str:
    variable = random.choice(TERMINAL_SPACE)
    roll = random.random()
    if roll < 0.18:
        return f"pctchange({variable}, {_sample_window(2, 34)})"
    if roll < 0.34:
        return f"zscore({variable}, {_sample_window(5, 55)})"
    if roll < 0.48:
        return f"delta({variable}, {_sample_window(2, 34)})"
    if roll < 0.62:
        return f"mean({variable}, {_sample_window(3, 55)})"
    if roll < 0.76:
        return f"std({variable}, {_sample_window(3, 55)})"
    if roll < 0.88:
        return f"efficiency_ratio({variable}, {_sample_window(5, 55)})"
    return variable


def _stochastic_series_expr(depth: int = 0, max_depth: int = 4) -> str:
    if depth >= max_depth or random.random() < 0.24:
        return _stochastic_leaf()
    roll = random.random()
    if roll < 0.18:
        op = random.choice(["rank", "cs_rank", "abs", "neg", "log", "sqrt"])
        inner = _stochastic_series_expr(depth + 1, max_depth)
        if op == "log":
            return f"log(abs({inner}) + 1)"
        if op == "sqrt":
            return f"sqrt(abs({inner}) + 0.0001)"
        return f"{op}({inner})"
    if roll < 0.45:
        op = random.choice(["+", "-", "*"])
        return f"({_stochastic_series_expr(depth + 1, max_depth)} {op} {_stochastic_series_expr(depth + 1, max_depth)})"
    if roll < 0.56:
        left = _stochastic_series_expr(depth + 1, max_depth)
        right = _stochastic_series_expr(depth + 1, max_depth)
        return f"({left} / (abs({right}) + 0.0001))"
    if roll < 0.74:
        op = random.choice(["mean", "std", "ts_rank", "ts_decay", "skew", "kurt", "downside_std", "upside_std", "efficiency_ratio"])
        min_window = 5 if op in {"skew", "kurt"} else 3
        return f"{op}({_stochastic_series_expr(depth + 1, max_depth)}, {_sample_window(min_window, 55)})"
    if roll < 0.88:
        op = random.choice(["corr", "cov", "regbeta"])
        return f"{op}({_stochastic_series_expr(depth + 1, max_depth)}, {_stochastic_series_expr(depth + 1, max_depth)}, {_sample_window(5, 89)})"
    condition = _stochastic_condition(depth + 1, max_depth)
    return f"where({condition}, {_stochastic_series_expr(depth + 1, max_depth)}, {_stochastic_series_expr(depth + 1, max_depth)})"


def _stochastic_condition(depth: int = 0, max_depth: int = 4) -> str:
    threshold = random.choice([-2.0, -1.0, -0.618, -0.382, 0.0, 0.382, 0.618, 1.0, 2.0])
    roll = random.random()
    if roll < 0.28:
        return f"(zscore({_stochastic_leaf()}, {_sample_window(5, 55)}) > {threshold})"
    if roll < 0.50:
        return f"({_stochastic_series_expr(depth + 1, max_depth)} > {_stochastic_series_expr(depth + 1, max_depth)})"
    if roll < 0.68:
        return f"(vol_zscore({_sample_window(5, 55)}) > {threshold})"
    if roll < 0.84:
        return f"(drawdown_from_high(close, {_sample_window(13, 144)}) < {-abs(threshold) / 20 - 0.02:.4f})"
    return f"(cross_up({_stochastic_series_expr(depth + 1, max_depth)}, {_stochastic_series_expr(depth + 1, max_depth)}) > 0)"


def _stochastic_formula_expression(max_depth: int = 4) -> str:
    parts = []
    for _ in range(random.choice([2, 3, 3, 4])):
        expr = _stochastic_series_expr(0, max_depth)
        if random.random() < 0.72:
            expr = f"rank({expr})"
        sign = "-" if random.random() < 0.35 else ""
        weight = _sample_weight()
        parts.append(f"{sign}{weight} * {expr}")
    expression = "(" + " + ".join(parts) + ")"
    if random.random() < 0.38:
        expression = f"where({_stochastic_condition(0, max_depth)}, {expression}, -rank(std(returns, {_sample_window(5, 55)})))"
    return expression


def _stochastic_expression_budget_ok(expression: str, max_length: int) -> bool:
    if not _safe_mining_expression(expression, max_length):
        return False
    windows = [int(x) for x in re.findall(r",\s*(\d+)\)", str(expression or ""))]
    if sum(windows) > 160:
        return False
    if len(re.findall(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", str(expression or ""))) > 26:
        return False
    return True


def _stochastic_grammar_candidate_bank(config: StreamingMiningConfig, limit: int = 140) -> list[Candidate]:
    rows: list[Candidate] = []
    attempts = max(int(limit or 0) * 5, 80)
    for idx in range(attempts):
        expression = _stochastic_formula_expression(min(config.max_depth, 2))
        if not _stochastic_expression_budget_ok(expression, config.max_expression_length):
            continue
        fingerprint = _factor_fingerprint(expression)
        windows = fingerprint.get("windows") or []
        fib_count = sum(1 for window in windows if int(window) in FIB_WINDOW_SPACE)
        theme = "fib_stochastic_formula" if fib_count >= max(1, len(windows) // 2) else "stochastic_formula"
        rows.append(
            Candidate(
                name=f"{theme}_{idx + 1}",
                expression=expression,
                description=f"Randomly composed operator tree with Fibonacci-biased windows; windows={windows[:8]}.",
                source="stochastic_grammar",
                theme=theme,
                hypothesis="The system explores non-template operator trees and relies on strict real-data validation to decide whether any random structure is useful.",
            )
        )
        if len(rows) >= limit:
            break
    if config.factor_themes:
        allowed = set(config.factor_themes)
        rows = [item for item in rows if item.theme in allowed]
    return _dedupe_candidate_list(rows)


def _safe_mining_expression(expression: str, max_length: int = 600) -> bool:
    text = str(expression or "").strip()
    if not text or len(text) > max_length:
        return False
    lowered = text.lower()
    forbidden = [
        "__", "import", "eval", "exec", "open(", "compile(", "lambda",
        "os.", "sys.", "subprocess", "socket", "http", "urllib", "requests",
        "read", "write", "globals", "locals", "getattr", "setattr",
    ]
    if any(token in lowered for token in forbidden):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\s,\.\+\-\*/\(\)><=&\|]+", text):
        return False
    balance = 0
    for char in text:
        if char == "(":
            balance += 1
        elif char == ")":
            balance -= 1
        if balance < 0:
            return False
    return balance == 0


def _institutional_candidate_bank(config: StreamingMiningConfig) -> list[Candidate]:
    """Research-first candidates used before unconstrained grammar search."""
    windows = [3, 5, 8, 10, 15, 20, 30, 60, 120]
    rows: list[Candidate] = []
    for window in windows:
        short = max(2, min(10, window // 2 or 2))
        long = max(window * 2, 20)
        rows.extend(
            [
                Candidate(
                    f"短反转低拥挤_{window}日",
                    f"(-pctchange(close, {short}) - 0.45 * std(returns, {window}) - 0.15 * turnover_rate)",
                    "短期过度反应在低波动、低换手环境下更容易修复，避免买入拥挤反弹。",
                    "institutional_seed",
                    "short_reversal",
                    "A股短周期噪声交易占比高，反转信号需要用波动和拥挤度约束以降低交易成本后的失效概率。",
                ),
                Candidate(
                    f"质量动量_{window}日",
                    f"(pctchange(close, {window}) / (std(returns, {window}) + 0.0001) - abs(close / (ts_max(high, {long}) + 0.0001) - 1))",
                    "收益动量除以波动，并惩罚距离阶段高点过远的弱趋势。",
                    "institutional_seed",
                    "volatility_adjusted_momentum",
                    "趋势延续需要同时满足单位风险收益较高和回撤可控，否则容易只是高噪声反弹。",
                ),
                Candidate(
                    f"低估值低波_{window}日",
                    f"(-pb - 0.5 * std(returns, {window}) - 0.1 * turnover_rate)",
                    "低PB、低波动、低换手组合，偏防御型价值暴露。",
                    "institutional_seed",
                    "valuation_quality_proxy",
                    "价值因子需要结合风险质量和拥挤度，降低价值陷阱和交易拥挤风险。",
                ),
                Candidate(
                    f"收盘强度压缩_{window}日",
                    f"(mean((close - low) / (high - low + 0.0001), {short}) - std(returns, {window}))",
                    "持续收盘靠近高点且波动收敛，刻画温和资金推动。",
                    "institutional_seed",
                    "range_breakout_quality",
                    "突破前的波动压缩和收盘强度比单纯价格创新高更接近可交易趋势。",
                ),
                Candidate(
                    f"量先价后确认_{window}日",
                    f"(corr(delay(pctchange(vol, 1), 1), pctchange(close, 1), {window}) - 0.35 * std(returns, {window}))",
                    "成交量变化领先价格变化，并用波动惩罚过滤噪声。",
                    "institutional_seed",
                    "price_volume_structure",
                    "资金行为通常先体现为成交变化，若后续价格跟随且波动不过高，信号更稳定。",
                ),
                Candidate(
                    f"拥挤反向_{window}日",
                    f"(-abs(pctchange(close, {short})) * (mean(vol, {short}) / (mean(vol, {long}) + 0.0001)))",
                    "短期大幅涨跌叠加放量视为拥挤冲击，做反向评分。",
                    "institutional_seed",
                    "liquidity_and_crowding",
                    "拥挤交易后的边际买盘/卖盘容易衰减，交易成本后需要规避高冲击标的。",
                ),
            ]
        )
    if config.factor_themes:
        allowed = set(config.factor_themes)
        rows = [item for item in rows if item.theme in allowed]
    deduped: list[Candidate] = []
    seen: set[str] = set()
    for item in rows:
        if item.expression in seen:
            continue
        seen.add(item.expression)
        deduped.append(item)
    return deduped


def _technical_event_candidate_bank(config: StreamingMiningConfig) -> list[Candidate]:
    """Event-style technical factors: trend state, repeated crosses, volume confirmation.

    These candidates are still ordinary factor expressions. The execution path remains
    canonical next-open selection; no result is displayed without real-data validation.
    """
    windows = [5, 8, 10, 13, 20, 30, 60]
    threshold_pairs = [(1.0, 2), (1.5, 2), (2.0, 1)]
    rows: list[Candidate] = []
    for window in windows:
        short = max(3, min(12, window // 2 + 2))
        long = max(20, window * 2)
        signal = 9
        rows.extend(
            [
                Candidate(
                    f"macd_second_cross_volume_{window}d",
                    f"where((count_true(cross_up(macd_dif(close, 12, 26), macd_dea(close, 12, 26, {signal})), {long}) >= 2) & (vol_zscore({window}) > 1.0), macd_hist(close, 12, 26, {signal}) + pctchange(close, {short}), -std(returns, {window}))",
                    "Second MACD golden-cross with volume confirmation; penalizes noisy high-volatility states.",
                    "technical_event",
                    "macd_volume_confirmation",
                    "Repeated bullish crosses with expanding volume may identify renewed institutional participation; volatility penalty reduces chase risk.",
                ),
                Candidate(
                    f"ma_second_cross_volume_{window}d",
                    f"where((count_true(cross_up(ema(close, {short}), ema(close, {long})), {long}) >= 2) & (vol_zscore({window}) > 1.0), pctchange(close, {short}) - drawdown_from_high(close, {long}), -std(returns, {window}))",
                    "Second moving-average golden-cross with volume expansion and drawdown control.",
                    "technical_event",
                    "ma_cross_volume_confirmation",
                    "A second cross after a prior failed attempt can mark trend repair, but only when volume confirms and drawdown is controlled.",
                ),
                Candidate(
                    f"rsi_repair_volume_{window}d",
                    f"where((rsi(close, 14) > 35) & (delay(rsi(close, 14), {short}) < 35) & (vol_zscore({window}) > 0.5), (50 - abs(rsi(close, 14) - 50)) / 50 + pctchange(close, {short}), -abs(drawdown_from_high(close, {long})))",
                    "RSI recovery from oversold zone with moderate volume confirmation.",
                    "technical_event",
                    "rsi_mean_reversion_repair",
                    "Oversold recovery becomes more tradable when price stabilizes and volume confirms demand rather than pure illiquidity bounce.",
                ),
                Candidate(
                    f"breakout_compression_volume_{window}d",
                    f"where((breakout(close, {long}) > 0) & (std(returns, {window}) < std(returns, {long})) & (vol_zscore({window}) > 0.8), pctchange(close, {window}) / (std(returns, {window}) + 0.0001), -abs(pctchange(close, {short})))",
                    "Low-volatility compression followed by volume-confirmed price breakout.",
                    "technical_event",
                    "compression_breakout",
                    "Breakouts after volatility compression are more informative than breakouts from already crowded high-volatility states.",
                ),
                Candidate(
                    f"pullback_then_volume_reclaim_{window}d",
                    f"where((drawdown_from_high(close, {long}) < -0.04) & (close > ema(close, {window})) & (vol_zscore({window}) > 0.5), -drawdown_from_high(close, {long}) + pctchange(close, {short}), -std(returns, {window}))",
                    "Pullback recovery: drawdown from recent high, reclaim EMA, and volume expansion.",
                    "technical_event",
                    "pullback_reclaim",
                    "Controlled pullbacks that reclaim trend averages on volume may offer better entry than chasing new highs.",
                ),
                Candidate(
                    f"gap_reversal_liquidity_{window}d",
                    f"where((gap_pct() < -0.015) & (close > open) & (vol_zscore({window}) > 0.5), (close - open) / (high - low + 0.0001) - std(returns, {window}), -abs(gap_pct()))",
                    "Down-gap intraday repair with volume; captures panic reversal rather than weak follow-through.",
                    "technical_event",
                    "gap_reversal",
                    "A negative open gap that is repaired intraday can indicate forced selling absorption, but volume and volatility controls are required.",
                ),
            ]
        )
        for threshold, cross_count in threshold_pairs:
            rows.append(
                Candidate(
                    f"adaptive_cross_flow_{window}d_z{threshold}",
                    f"where((count_true(cross_up(ema(close, {short}), ema(close, {long})), {long}) >= {cross_count}) & (vol_zscore({window}) > {threshold}), rank(pctchange(close, {short})) + rank(macd_hist(close, 12, 26, 9)), -rank(std(returns, {window})))",
                    "Parameterized cross-flow candidate with adaptive volume threshold.",
                    "technical_event_param_scan",
                    "adaptive_cross_flow",
                    "Parameter scanning tests whether the cross signal is only useful under specific volume-confirmation strength.",
                )
            )
    if config.factor_themes:
        allowed = set(config.factor_themes)
        rows = [item for item in rows if item.theme in allowed or item.source == "technical_event_param_scan"]
    deduped: list[Candidate] = []
    seen: set[str] = set()
    for item in rows:
        if item.expression in seen:
            continue
        seen.add(item.expression)
        deduped.append(item)
    return deduped


def _creative_random_candidate_bank(config: StreamingMiningConfig, limit: int = 120) -> list[Candidate]:
    """Diverse formula candidates beyond MACD/cross logic.

    This bank intentionally mixes behavioral reversal, liquidity, range shape,
    gap repair, volatility compression, valuation overlays and price-volume lead/lag.
    Candidates are still only hypotheses; `_evaluate_candidate` performs the real
    out-of-sample validation and canonical execution backtest.
    """
    rows: list[Candidate] = []
    windows = [3, 5, 8, 10, 11, 15, 20, 30, 60]
    long_windows = [20, 30, 60, 120]
    for idx in range(max(int(limit or 0) * 2, 40)):
        w = random.choice(windows)
        long = random.choice([x for x in long_windows if x >= max(20, w)])
        short = random.choice([2, 3, 5, 8, 10])
        lag = random.choice([1, 2, 3])
        mode = random.choice(
            [
                "gap_absorption",
                "volume_dryup_squeeze",
                "wick_supply_pressure",
                "amihud_liquidity",
                "volume_lead_price",
                "crowding_reversal",
                "trend_efficiency",
                "vwap_dislocation",
                "range_entropy",
                "valuation_risk_overlay",
                "overnight_intraday_contrast",
                "drawdown_repair",
            ]
        )
        if mode == "gap_absorption":
            expr = (
                f"where((gap_pct() < -0.015) & (((close - low) / (high - low + 0.0001)) > 0.65), "
                f"rank((close - open) / (high - low + 0.0001)) - rank(std(returns, {w})), -abs(gap_pct()))"
            )
            hypothesis = "Panic down-gaps that are absorbed intraday may signal forced selling exhaustion."
        elif mode == "volume_dryup_squeeze":
            expr = (
                f"where((vol_zscore({w}) < -0.8) & (std(returns, {w}) < std(returns, {long})), "
                f"rank((close - ts_min(low, {long})) / (ts_max(high, {long}) - ts_min(low, {long}) + 0.0001)) - rank(std(returns, {w})), "
                f"-rank(abs(pctchange(close, {short}))))"
            )
            hypothesis = "Quiet volume and compressed volatility can precede cleaner repricing when range position improves."
        elif mode == "wick_supply_pressure":
            expr = (
                f"-mean((high - max(open, close)) / (high - low + 0.0001), {w}) "
                f"+ mean((close - low) / (high - low + 0.0001), {short})"
            )
            hypothesis = "Persistent upper wicks indicate supply pressure; strong closes reduce that penalty."
        elif mode == "amihud_liquidity":
            expr = f"-mean(abs(returns) / (amount + 1.0), {w}) - std(returns, {w})"
            hypothesis = "Lower price impact and lower realized volatility should improve implementable alpha capacity."
        elif mode == "volume_lead_price":
            expr = f"corr(delay(pctchange(vol, 1), {lag}), pctchange(close, 1), {w}) - std(returns, {short})"
            hypothesis = "Volume changes that lead price changes can proxy early participation before prices fully adjust."
        elif mode == "crowding_reversal":
            expr = (
                f"where((abs(pctchange(close, {short})) > 0.035) & (vol_zscore({w}) > 1.2), "
                f"-pctchange(close, {short}) / (std(returns, {w}) + 0.0001), "
                f"pctchange(close, {long}) / (std(returns, {long}) + 0.0001))"
            )
            hypothesis = "Crowded short-term moves on unusual volume often mean-revert, while calmer long trends can persist."
        elif mode == "trend_efficiency":
            expr = (
                f"pctchange(close, {w}) / (mean(tr, {w}) / (mean(close, {w}) + 0.0001) + 0.0001) "
                f"- abs(drawdown_from_high(close, {long}))"
            )
            hypothesis = "Efficient trends travel far relative to true range while avoiding deep drawdowns."
        elif mode == "vwap_dislocation":
            expr = f"-((close - vwap) / (mean(tr, {w}) + 0.0001)) + rank(pctchange(amount, {short}))"
            hypothesis = "Close-to-VWAP dislocations can fade unless confirmed by genuine amount expansion."
        elif mode == "range_entropy":
            expr = (
                f"-(std((close - low) / (high - low + 0.0001), {w})) "
                f"+ mean((close - low) / (high - low + 0.0001), {short})"
            )
            hypothesis = "Stable closes near the upper intraday range can be more informative than noisy breakouts."
        elif mode == "valuation_risk_overlay":
            expr = f"rank(-pb) + rank(-pe) - rank(std(returns, {w})) - rank(turnover_rate)"
            hypothesis = "Cheap valuation needs volatility and turnover controls to avoid value traps and crowded exits."
        elif mode == "overnight_intraday_contrast":
            expr = f"-(open / delay(close, 1) - 1) + (close / open - 1) - std(returns, {w})"
            hypothesis = "Weak overnight sentiment that repairs intraday may reflect liquidity absorption."
        else:
            expr = (
                f"where((drawdown_from_high(close, {long}) < -0.06) & (close > mean(close, {w})), "
                f"-drawdown_from_high(close, {long}) + pctchange(amount, {short}) / (std(returns, {w}) + 0.0001), "
                f"-abs(drawdown_from_high(close, {long})))"
            )
            hypothesis = "Post-drawdown reclaim with amount confirmation can capture repaired risk appetite."
        rows.append(
            Candidate(
                name=f"{mode}_{w}_{short}_{long}_{idx + 1}",
                expression=expr,
                description=f"Creative randomized research candidate: {mode}; windows={w}/{short}/{long}.",
                source="creative_random",
                theme=mode,
                hypothesis=hypothesis,
            )
        )
        if len(rows) >= limit:
            break
    if config.factor_themes:
        allowed = set(config.factor_themes)
        rows = [item for item in rows if item.theme in allowed]
    return _dedupe_candidate_list(rows)


def _novel_motif_candidate_bank(config: StreamingMiningConfig, limit: int = 160) -> list[Candidate]:
    """Generate higher-novelty motifs from distribution shape and cross-domain disagreement."""
    rows: list[Candidate] = []
    windows = [5, 8, 10, 11, 15, 20, 30, 60]
    long_windows = [30, 60, 90, 120]
    motif_builders = [
        (
            "asymmetric_vol_carry",
            lambda w, s, long: f"rank(upside_std(returns, {w}) - downside_std(returns, {w})) + rank(efficiency_ratio(close, {long})) - rank(kurt(returns, {long}))",
            "Upside/downside volatility asymmetry combined with efficient price travel and tail-risk penalty.",
            "Return asymmetry may separate orderly accumulation from noisy high-volatility rebounds.",
        ),
        (
            "tail_reversal_filter",
            lambda w, s, long: f"where((kurt(returns, {long}) > 3) & (pctchange(close, {s}) < 0), -pctchange(close, {s}) - downside_std(returns, {w}), rank(efficiency_ratio(close, {w})) - rank(kurt(returns, {w})))",
            "Tail-event reversal with downside-volatility filter.",
            "Extreme downside tails can mean-revert, but only if recent downside dispersion is controlled.",
        ),
        (
            "efficient_pullback",
            lambda w, s, long: f"where((drawdown_from_high(close, {long}) < -0.04) & (efficiency_ratio(close, {w}) > 0.35), -drawdown_from_high(close, {long}) + rank(pctchange(amount, {s})), -rank(downside_std(returns, {w})))",
            "Drawdown repair gated by trend efficiency and amount confirmation.",
            "A pullback is more tradable when path efficiency improves and amount expands without excess downside volatility.",
        ),
        (
            "shape_volume_disagreement",
            lambda w, s, long: f"rank(skew(returns, {w})) - rank(kurt(returns, {long})) + rank(corr(pctchange(amount, 1), pctchange(close, 1), {w}))",
            "Return skew, tail risk and amount-price agreement combined.",
            "Positive return asymmetry plus real amount-price agreement can identify healthier demand than price alone.",
        ),
        (
            "valuation_flow_dislocation",
            lambda w, s, long: f"rank(-pb) + rank(-pe) + rank(pctchange(amount, {s})) - rank(abs(pctchange(close, {s}))) - rank(downside_std(returns, {w}))",
            "Cheap valuation with amount improvement but without crowded price chase.",
            "Value exposure is more useful when flow improves before large price moves and downside volatility remains low.",
        ),
        (
            "quiet_accumulation",
            lambda w, s, long: f"where((vol_zscore({w}) < 0.3) & (efficiency_ratio(close, {long}) > 0.25), rank(mean((close - low) / (high - low + 0.0001), {w})) + rank(pctchange(amount, {s})), -rank(std(returns, {w})))",
            "Quiet accumulation via close-location persistence and amount drift.",
            "Institutional accumulation often appears as persistent strong closes with moderate rather than explosive volume.",
        ),
        (
            "fragility_penalty",
            lambda w, s, long: f"-(rank(kurt(returns, {w})) + rank(downside_std(returns, {w})) + rank(abs(gap_pct()))) + rank(efficiency_ratio(close, {long}))",
            "Anti-fragility score penalizing tail risk, downside dispersion and gap instability.",
            "Lower fragility can improve realized implementability even if raw alpha is weak.",
        ),
        (
            "range_memory",
            lambda w, s, long: f"rank(mean((close - ts_min(low, {w})) / (ts_max(high, {w}) - ts_min(low, {w}) + 0.0001), {s})) - rank(std((close - low) / (high - low + 0.0001), {w}))",
            "Persistent high range position with low close-location entropy.",
            "Stable range memory can indicate persistent demand rather than one-day breakout noise.",
        ),
    ]
    for idx in range(max(int(limit or 0) * 2, 40)):
        w = random.choice(windows)
        short = random.choice([2, 3, 5, 8, 10])
        long = random.choice([x for x in long_windows if x >= max(30, w)])
        theme, builder, description, hypothesis = random.choice(motif_builders)
        rows.append(
            Candidate(
                name=f"{theme}_{w}_{short}_{long}_{idx + 1}",
                expression=builder(w, short, long),
                description=description,
                source="novel_motif",
                theme=theme,
                hypothesis=hypothesis,
            )
        )
        if len(rows) >= limit:
            break
    if config.factor_themes:
        allowed = set(config.factor_themes)
        rows = [item for item in rows if item.theme in allowed]
    return _dedupe_candidate_list(rows)


def _candidate_allowed(candidate: Candidate, config: StreamingMiningConfig) -> bool:
    if not config.factor_themes:
        return True
    if not candidate.theme:
        return True
    return candidate.theme in set(config.factor_themes)


def _dedupe_candidate_list(items: list[Candidate]) -> list[Candidate]:
    result: list[Candidate] = []
    seen: set[str] = set()
    for item in items:
        expression_key = re.sub(r"\s+", "", str(item.expression or ""))
        if not expression_key or expression_key in seen:
            continue
        seen.add(expression_key)
        result.append(item)
    return result


def _candidate_with_parameter_neighbors(candidate: Candidate, *, max_neighbors: int = 4):
    yield candidate
    for mutated, window in _parameter_neighbors(candidate.expression)[:max_neighbors]:
        yield Candidate(
            _parameterized_factor_name(candidate.name, window),
            mutated,
            f"Parameter-neighborhood validation using a {window}-day window.",
            "param_scan",
            candidate.theme,
            candidate.hypothesis,
        )


def _priority_seed_candidates(config: StreamingMiningConfig) -> list[Candidate]:
    """High-signal candidates evaluated with source diversity.

    The previous scheduler evaluated most technical-event candidates first, so
    early sessions looked dominated by MACD/MA cross variants. This interleaves
    creative random hypotheses, institutional seeds, broad templates and
    technical events to surface diverse ideas immediately.
    """
    stochastic = _stochastic_grammar_candidate_bank(config, 180)
    novel = _novel_motif_candidate_bank(config, 160)
    creative = _creative_random_candidate_bank(config, 140)
    institutional = _institutional_candidate_bank(config)
    templates = _generate_template_candidates(180)
    technical = _technical_event_candidate_bank(config)
    buckets = [stochastic, novel, creative, institutional, templates, technical]
    for bucket in buckets:
        random.shuffle(bucket)
    ordered: list[Candidate] = []
    max_len = max((len(bucket) for bucket in buckets), default=0)
    for idx in range(max_len):
        for bucket in buckets:
            if idx >= len(bucket):
                continue
            item = bucket[idx]
            if _candidate_allowed(item, config):
                ordered.append(item)
    return _dedupe_candidate_list(ordered)


def _theme_score_map(elites: list[dict]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for rank, item in enumerate(elites[:80], start=1):
        theme = str(item.get("theme") or "general_formula")
        score = max(_safe_float(item.get("score")), -0.5)
        metrics = item.get("backtest_metrics") or {}
        excess = _safe_float(metrics.get("excess_return"))
        bonus = max(excess, -5.0) / 50.0
        scores[theme] = scores.get(theme, 0.0) + max(score + bonus, 0.02) / math.sqrt(rank)
    return scores


def _adaptive_theme_candidates(config: StreamingMiningConfig, elites: list[dict], limit: int = 10) -> list[Candidate]:
    if not elites:
        return []
    theme_scores = _theme_score_map(elites)
    if not theme_scores:
        return []
    themes = [theme for theme, _ in sorted(theme_scores.items(), key=lambda item: item[1], reverse=True)[:3]]
    bank = _dedupe_candidate_list(
        _stochastic_grammar_candidate_bank(config, 120)
        + _novel_motif_candidate_bank(config, 100)
        + _creative_random_candidate_bank(config, 80)
        + _institutional_candidate_bank(config)
        + _generate_template_candidates(120)
        + _technical_event_candidate_bank(config)
    )
    rows: list[Candidate] = []
    for theme in themes:
        for item in bank:
            if item.theme != theme:
                continue
            rows.append(
                Candidate(
                    f"{item.name}_adaptive",
                    _mutate_expression(item.expression),
                    f"Adaptive theme search from recent elite theme: {theme}.",
                    "adaptive_theme_search",
                    item.theme,
                    item.hypothesis,
                )
            )
            if len(rows) >= limit:
                return _dedupe_candidate_list(rows)
    return _dedupe_candidate_list(rows)


def _adaptive_stochastic_candidates(config: StreamingMiningConfig, elites: list[dict], limit: int = 10) -> list[Candidate]:
    """Cross validated elites with fresh stochastic trees to adjust exploration online."""
    if not elites:
        return _stochastic_grammar_candidate_bank(config, limit)
    rows: list[Candidate] = []
    ranked = sorted(elites[:50], key=lambda item: _safe_float(item.get("score")), reverse=True)
    for idx in range(max(limit * 3, 12)):
        base = random.choice(ranked[: min(len(ranked), 12)])
        base_expr = str(base.get("expression") or "")
        if not base_expr:
            continue
        fresh = _stochastic_formula_expression(1)
        weight = _sample_weight()
        mode = random.choice(["elite_residual", "elite_gate", "elite_spread", "elite_blend"])
        if mode == "elite_gate":
            expression = f"where({_stochastic_condition(0, 1)}, rank({base_expr}), rank({fresh}))"
        elif mode == "elite_spread":
            expression = f"(rank({base_expr}) - {weight} * rank({fresh}))"
        elif mode == "elite_residual":
            expression = f"(rank({fresh}) - {weight} * rank({base_expr}))"
        else:
            expression = f"({weight} * rank({base_expr}) + {round(1 - weight, 3)} * rank({fresh}))"
        if not _stochastic_expression_budget_ok(expression, config.max_expression_length):
            continue
        rows.append(
            Candidate(
                name=f"adaptive_stochastic_{mode}_{idx + 1}",
                expression=expression,
                description=f"Online adaptive crossover between elite factor {base.get('name', 'elite')} and a fresh stochastic operator tree.",
                source="adaptive_stochastic",
                theme=base.get("theme") or "adaptive_stochastic",
                hypothesis="Validated weak signals are crossed with fresh random operator trees to escape local parameter neighborhoods while preserving useful structure.",
            )
        )
        if len(rows) >= limit:
            break
    return _dedupe_candidate_list(rows)


def _parse_llm_candidates(raw_text: str, config: StreamingMiningConfig) -> list[Candidate]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    try:
        payload = json.loads(text)
    except Exception:
        return []
    rows = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    allowed_themes = set(config.factor_themes or ())
    result: list[Candidate] = []
    for idx, row in enumerate(rows[:12], start=1):
        if not isinstance(row, dict):
            continue
        expression = str(row.get("expression") or "").strip()
        if not _safe_mining_expression(expression, config.max_expression_length):
            continue
        theme = str(row.get("theme") or "llm_research").strip()[:80]
        if allowed_themes and theme not in allowed_themes:
            continue
        result.append(
            Candidate(
                name=str(row.get("name") or f"AI研究候选_{idx}")[:80],
                expression=expression,
                description=str(row.get("description") or "LLM proposed formulaic alpha candidate; QTsys real-data validation determines whether it is useful.")[:600],
                source="llm_guided",
                theme=theme,
                hypothesis=str(row.get("hypothesis") or "Economically motivated formulaic alpha candidate.")[:800],
            )
        )
    return result


def _llm_guided_candidates(config: StreamingMiningConfig, elites: list[dict]) -> list[Candidate]:
    settings = config.settings
    api_key = str(getattr(settings, "llm_api_key", "") or "").strip()
    base_url = str(getattr(settings, "llm_base_url", "") or "").strip()
    model = str(getattr(settings, "llm_model", "") or "").strip()
    if not api_key or not base_url or not model:
        return []
    elite_lines = []
    for item in (elites or [])[:8]:
        elite_lines.append(
            f"- {item.get('name')}: expression={item.get('expression')}; "
            f"score={_safe_float(item.get('score')):.4f}; "
            f"test_excess={_safe_float((item.get('backtest_metrics') or {}).get('excess_return')):.2f}%"
        )
    prompt = f"""
You generate formulaic alpha candidates for QTsys A-share factor mining.
Return JSON only:
{{"candidates":[{{"name":"","expression":"","theme":"","hypothesis":"","description":""}}]}}

Allowed variables:
close, open, high, low, vol, volume, returns, vwap, amount, pe, pb, ps, turnover_rate, circ_mv, total_mv, tr.

Allowed functions/operators:
rank, cs_rank, abs, neg, log, sqrt, signedpower, clip, mean, std, sum, ts_max, ts_min,
ts_rank, delta, delay, pctchange, ts_decay, ewm, ts_argmax, ts_argmin, wma, decaylinear,
corr, cov, regbeta, regresi, max, min, where, ternary,
ema, macd_dif, macd_dea, macd_hist, rsi, cross_up, cross_down, bars_since,
count_true, zscore, vol_zscore, gap_pct, breakout, drawdown_from_high,
skew, kurt, downside_std, upside_std, efficiency_ratio.

Rules:
- Use only data available at or before the signal day; no future functions.
- Do not output Python code, imports, comments, markdown, or explanations outside JSON.
- Prefer simple economically grounded weak alphas, not high-complexity curve fitting.
- Use A-share research themes: short reversal, volatility-adjusted momentum, liquidity/crowding,
  value-quality, price-volume confirmation, range-compression breakout.
- Generate 8 diverse expressions under {config.max_expression_length} characters.
- Prefer Fibonacci-style windows when relevant: 2, 3, 5, 8, 13, 21, 34, 55, 89, 144 days,
  but occasionally use non-Fibonacci windows such as 7, 10, 15, 20, 30, 60, 120.

Current validated elite factors:
{chr(10).join(elite_lines) if elite_lines else "- No elite factor yet; prioritize robust research seeds."}
""".strip()
    try:
        response = asyncio.run(
            chat_complete_text(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=[
                    {"role": "system", "content": "You are a skeptical quantitative researcher. You only propose testable factor expressions; real validation is handled elsewhere."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.45,
                max_tokens=2600,
            )
        )
    except Exception:
        return []
    return _parse_llm_candidates(response.get("content", ""), config)


def _candidate_stream(config: StreamingMiningConfig, elite_provider) -> Candidate:
    random.seed(int(time.time()) % 10_000_000)
    for item in _generate_template_candidates(220):
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
    for item in _institutional_candidate_bank(config):
        yield item
    for item in _technical_event_candidate_bank(config):
        yield item
    for item in _llm_guided_candidates(config, []):
        yield item
    index = 1
    llm_batches = 1
    _operator_pool_available()
    while True:
        elites = elite_provider()
        if llm_batches < 4 and (index == 1 or (elites and index % 80 == 0)):
            llm_batches += 1
            for item in _llm_guided_candidates(config, elites):
                yield item
        if len(elites) >= 2 and random.random() < 0.42:
            left, right = random.sample(elites[: min(len(elites), 20)], 2)
            left_name = re.sub(r"\s+", "", str(left.get("name") or "因子A"))[:12]
            right_name = re.sub(r"\s+", "", str(right.get("name") or "因子B"))[:12]
            yield Candidate(
                f"精英组合_{left_name}_{right_name}",
                _combine_expressions(left["expression"], right["expression"]),
                "Non-negative rank ensemble of validated elite factors.",
                "ensemble",
                "ensemble_factor",
                "Low-correlation validated factors may diversify idiosyncratic noise.",
            )
        elif elites and random.random() < 0.38:
            base = random.choice(elites[: min(len(elites), 20)])
            base_name = re.sub(r"\s+", "", str(base.get("name") or "因子"))[:16]
            yield Candidate(
                f"{base_name}_邻域优化{index}",
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


def _candidate_stream_v2(config: StreamingMiningConfig, elite_provider) -> Candidate:
    """Priority and adaptive scheduler for professional factor mining."""
    random.seed(int(time.time()) % 10_000_000)
    emitted: set[str] = set()
    structure_counts: dict[str, int] = {}

    def should_emit(item: Candidate) -> bool:
        if not _candidate_allowed(item, config):
            return False
        expression_key = re.sub(r"\s+", "", str(item.expression or ""))
        if not expression_key or expression_key in emitted:
            return False
        novelty_key = _expression_novelty_key(item.expression)
        limit = 2 if item.source == "param_scan" else 5
        if structure_counts.get(novelty_key, 0) >= limit:
            return False
        emitted.add(expression_key)
        structure_counts[novelty_key] = structure_counts.get(novelty_key, 0) + 1
        return True

    neighbor_queue: list[Candidate] = []
    for idx, seed in enumerate(_priority_seed_candidates(config), start=1):
        if should_emit(seed):
            yield seed
        neighbor_queue.extend(list(_candidate_with_parameter_neighbors(seed, max_neighbors=2))[1:])
        if idx % 4 == 0 and neighbor_queue:
            neighbor = neighbor_queue.pop(0)
            if should_emit(neighbor):
                yield neighbor

    for item in _llm_guided_candidates(config, []):
        if should_emit(item):
            yield item

    index = 1
    llm_batches = 1
    _operator_pool_available()
    while True:
        elites = elite_provider()
        if elites and index % 40 == 0:
            for item in _adaptive_theme_candidates(config, elites, limit=10):
                if should_emit(item):
                    yield item
        if elites and index % 13 == 0:
            for item in _adaptive_stochastic_candidates(config, elites, limit=8):
                if should_emit(item):
                    yield item
        if index % 5 == 0 and neighbor_queue:
            neighbor = neighbor_queue.pop(0)
            if should_emit(neighbor):
                yield neighbor
        if index % 7 == 0:
            for item in _novel_motif_candidate_bank(config, 4):
                if should_emit(item):
                    yield item
        if index % 9 == 0:
            for item in _stochastic_grammar_candidate_bank(config, 4):
                if should_emit(item):
                    yield item
        if llm_batches < 4 and (index == 1 or (elites and index % 80 == 0)):
            llm_batches += 1
            for item in _llm_guided_candidates(config, elites):
                if should_emit(item):
                    yield item
        if len(elites) >= 2 and random.random() < 0.42:
            left, right = random.sample(elites[: min(len(elites), 20)], 2)
            left_name = re.sub(r"\s+", "", str(left.get("name") or "factorA"))[:12]
            right_name = re.sub(r"\s+", "", str(right.get("name") or "factorB"))[:12]
            candidate = Candidate(
                f"elite_combo_{left_name}_{right_name}",
                _combine_expressions(left["expression"], right["expression"]),
                "Non-negative rank ensemble of validated elite factors.",
                "ensemble",
                "ensemble_factor",
                "Low-correlation validated factors may diversify idiosyncratic noise.",
            )
            if should_emit(candidate):
                yield candidate
        elif elites and random.random() < 0.38:
            base = random.choice(elites[: min(len(elites), 20)])
            base_name = re.sub(r"\s+", "", str(base.get("name") or "factor"))[:16]
            candidate = Candidate(
                f"{base_name}_adaptive_neighbor_{index}",
                _mutate_expression(base["expression"]),
                "Neighborhood mutation of a validated elite factor.",
                "elite_mutation",
                base.get("theme") or "elite_mutation",
                base.get("hypothesis") or "Elite mutation tests whether nearby formulas preserve economic signal.",
            )
            if should_emit(candidate):
                yield candidate
        else:
            candidate = Candidate(
                f"typed_grammar_{index}",
                _random_expr(0, config.max_depth),
                "Typed grammar expression generated from the factor operator space.",
                "grammar",
                "grammar_search",
                "Machine search explores economically plausible formula neighborhoods, then relies on strict out-of-sample validation.",
            )
            if should_emit(candidate):
                yield candidate
        index += 1


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
    clean = clean.strip("_- ") or "因子"
    return f"{clean}_{int(window)}日"


async def _update_mining_session(session_id: str, **patch):
    for attempt in range(6):
        try:
            async with async_session() as db:
                patch["updated_at"] = datetime.utcnow()
                await db.execute(update(FactorMiningSession).where(FactorMiningSession.session_id == session_id).values(**patch))
                await db.commit()
                return
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= 5:
                raise
            await _sleep_for_db_retry(attempt)


async def _insert_candidate(session_id: str, payload: dict) -> Optional[int]:
    for attempt in range(6):
        try:
            async with async_session() as db:
                trading_key = payload.get("trading_equivalence_key") or ""
                model_payload = dict(payload)
                model_payload.pop("trading_equivalence_key", None)
                existing = await db.execute(
                    select(FactorMiningCandidate).where(
                        FactorMiningCandidate.session_id == session_id,
                        FactorMiningCandidate.expression_hash == payload["expression_hash"],
                    )
                )
                if existing.scalar_one_or_none():
                    return None
                if trading_key:
                    rows = await db.execute(
                        select(FactorMiningCandidate).where(FactorMiningCandidate.session_id == session_id)
                    )
                    for row in rows.scalars().all():
                        fingerprint = row.fingerprint or {}
                        if fingerprint.get("trading_equivalence_key") == trading_key:
                            return None
                candidate = FactorMiningCandidate(session_id=session_id, **model_payload)
                db.add(candidate)
                await db.commit()
                await db.refresh(candidate)
                return candidate.id
        except IntegrityError:
            return None
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= 5:
                raise
            await _sleep_for_db_retry(attempt)
    return None


async def _set_candidate_cluster(candidate_id: int, cluster_key: str):
    for attempt in range(6):
        try:
            async with async_session() as db:
                await db.execute(
                    update(FactorMiningCandidate)
                    .where(FactorMiningCandidate.id == candidate_id)
                    .values(correlation_cluster=cluster_key)
                )
                await db.commit()
                return
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= 5:
                raise
            await _sleep_for_db_retry(attempt)


async def _insert_trial_log(session_id: str, candidate: Candidate, stage: str, reasons: list[str], score: float = 0.0, metrics: Optional[dict] = None):
    for attempt in range(6):
        try:
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
                return
        except IntegrityError:
            return
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= 5:
                raise
            await _sleep_for_db_retry(attempt)


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
    member = {
        "candidate_id": candidate_id,
        "name": payload.get("name"),
        "expression_hash": payload.get("expression_hash") or _expression_hash(expression),
        "score": payload.get("score", 0.0),
    }
    for attempt in range(6):
        try:
            async with async_session() as db:
                existing = await db.execute(
                    select(FactorCorrelationCluster).where(
                        FactorCorrelationCluster.session_id == session_id,
                        FactorCorrelationCluster.cluster_key == cluster_key,
                    )
                )
                row = existing.scalar_one_or_none()
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
        except IntegrityError:
            return cluster_key
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or attempt >= 5:
                raise
            await _sleep_for_db_retry(attempt)
    return cluster_key


def _run_streaming_mining_thread(config: StreamingMiningConfig, stop_event: threading.Event):
    session_id = config.session_id
    tested = 0
    accepted = 0
    best_score = -999.0
    seen_hashes: set[str] = set()
    elites: list[dict] = []
    accepted_structure_counts: dict[str, int] = {}
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
        config.settings.capacity_limit_pct = config.capacity_limit_pct
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

        for candidate in _candidate_stream_v2(config, elite_provider):
            if stop_event.is_set():
                break
            expression_hash = _expression_hash(candidate.expression)
            expression_seen_key = f"expr:{expression_hash}"
            if expression_seen_key in seen_hashes:
                continue
            seen_hashes.add(expression_seen_key)
            tested += 1
            if tested % 5 == 1:
                run_async(_update_mining_session(
                    session_id,
                    phase="screening",
                    message=f"正在真实评估第 {tested} 个候选因子，已展示 {accepted} 个",
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
            equivalence_key = str(payload.get("trading_equivalence_key") or "")
            equivalence_seen_key = f"trade:{equivalence_key}"
            if equivalence_key and equivalence_seen_key in seen_hashes:
                run_async(_insert_trial_log(
                    session_id,
                    candidate,
                    "trading_equivalent_duplicate",
                    ["same_factor_values_after_direction_normalization"],
                    score=_safe_float(payload.get("score")),
                    metrics={"equivalence_key": equivalence_key, "direction": payload.get("direction")},
                ))
                if config.max_trials and tested >= config.max_trials:
                    break
                continue
            if equivalence_key:
                seen_hashes.add(equivalence_seen_key)
            homogeneity_reason = _payload_homogeneity_reason(payload, elites, accepted_structure_counts)
            if homogeneity_reason:
                run_async(_insert_trial_log(
                    session_id,
                    candidate,
                    "homogeneity_reject",
                    [homogeneity_reason],
                    score=_safe_float(payload.get("score")),
                    metrics={
                        "novelty_key": _novelty_key_from_fingerprint(payload.get("fingerprint") or {}),
                        "theme": payload.get("theme"),
                        "source": payload.get("source"),
                    },
                ))
                if config.max_trials and tested >= config.max_trials:
                    break
                continue
            inserted_id = run_async(_insert_candidate(session_id, payload))
            if inserted_id is None:
                continue
            cluster_key = run_async(_upsert_correlation_cluster(session_id, payload, inserted_id, elites))
            if cluster_key:
                run_async(_set_candidate_cluster(inserted_id, cluster_key))
            accepted += 1
            payload["id"] = inserted_id
            payload["correlation_cluster"] = cluster_key
            novelty_key = _novelty_key_from_fingerprint(payload.get("fingerprint") or {})
            accepted_structure_counts[novelty_key] = accepted_structure_counts.get(novelty_key, 0) + 1
            elites.append(payload)
            elites.sort(key=lambda item: item["score"], reverse=True)
            elites[:] = elites[:200]
            best_score = max(best_score, _safe_float(payload.get("score")))
            run_async(_update_mining_session(
                session_id,
                phase="mining",
                message=f"已完成真实评估：{payload['name']}，评级 {payload.get('revalidation_status', '-')}，评分 {payload['score']:.4f}",
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


async def get_active_or_latest_mining_session(db: AsyncSession) -> dict:
    """Return the active mining session, or the latest session for UI restoration."""
    active_id = MINING_MANAGER.can_start()
    if active_id:
        status = await get_streaming_mining_status(db, active_id)
        return {"has_session": True, "active": True, "session": status}

    result = await db.execute(
        select(FactorMiningSession)
        .order_by(FactorMiningSession.updated_at.desc(), FactorMiningSession.id.desc())
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if not session:
        return {"has_session": False, "active": False, "session": None}
    status = await get_streaming_mining_status(db, session.session_id)
    return {"has_session": True, "active": False, "session": status}


def _serialize_candidate(item: FactorMiningCandidate) -> dict:
    backtest_metrics = item.backtest_metrics or {}
    display_equity_curve = backtest_metrics.get("display_equity_curve") or item.equity_curve or []
    display_normalized_curve = backtest_metrics.get("display_normalized_curve") or item.normalized_curve or []
    display_benchmark_curve = backtest_metrics.get("display_benchmark_curve") or backtest_metrics.get("benchmark_curve", [])
    display_benchmark_normalized_curve = (
        backtest_metrics.get("display_benchmark_normalized_curve")
        or backtest_metrics.get("benchmark_normalized_curve", [])
    )
    display_excess_curve = backtest_metrics.get("display_excess_curve") or backtest_metrics.get("excess_curve", [])
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
        "trading_equivalence_key": (item.fingerprint or {}).get("trading_equivalence_key", ""),
        "correlation_cluster": item.correlation_cluster or "",
        "revalidation_status": item.revalidation_status or "",
        "validation_status": item.revalidation_status or backtest_metrics.get("validation_status", ""),
        "direction": item.direction,
        "complexity": item.complexity,
        "score": item.score,
        "metrics": item.metrics or {},
        "train_metrics": item.train_metrics or {},
        "valid_metrics": item.valid_metrics or {},
        "test_metrics": item.test_metrics or {},
        "backtest_metrics": backtest_metrics,
        "equity_curve": display_equity_curve,
        "normalized_curve": display_normalized_curve,
        "display_equity_curve": display_equity_curve,
        "display_normalized_curve": display_normalized_curve,
        "display_benchmark_curve": display_benchmark_curve,
        "display_benchmark_normalized_curve": display_benchmark_normalized_curve,
        "display_excess_curve": display_excess_curve,
        "display_metrics": backtest_metrics.get("display_metrics", {}),
        "display_benchmark_metrics": backtest_metrics.get("display_benchmark_metrics", {}),
        "display_scope": backtest_metrics.get("display_scope", ""),
        "display_start_date": backtest_metrics.get("display_start_date", ""),
        "display_end_date": backtest_metrics.get("display_end_date", ""),
        "test_equity_curve": backtest_metrics.get("test_equity_curve", []),
        "test_normalized_curve": backtest_metrics.get("test_normalized_curve", []),
        "test_benchmark_normalized_curve": backtest_metrics.get("test_benchmark_normalized_curve", []),
        "test_excess_curve": backtest_metrics.get("test_excess_curve", []),
        "protocol_version": item.protocol_version or backtest_metrics.get("protocol_version") or (item.metrics or {}).get("protocol_version") or "legacy_unverified",
        "benchmark_curve": display_benchmark_curve,
        "benchmark_normalized_curve": display_benchmark_normalized_curve,
        "excess_curve": display_excess_curve,
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
        "search_source": backtest_metrics.get("search_source", item.source or ""),
        "search_theme": backtest_metrics.get("search_theme", item.theme or ""),
        "event_driven": bool(backtest_metrics.get("event_driven") or (item.fingerprint or {}).get("event_driven")),
        "technical_operators": backtest_metrics.get("technical_operators", (item.fingerprint or {}).get("technical_operators", [])),
        "rejection_reasons": backtest_metrics.get("rejection_reasons", {}),
        "validation_reasons": backtest_metrics.get("validation_reasons", []),
        "legacy_warning": (backtest_metrics.get("protocol_version") or (item.metrics or {}).get("protocol_version")) != STRICT_MINING_PROTOCOL_VERSION,
        "is_pinned": bool(getattr(item, "is_pinned", 0)),
        "is_deleted": bool(getattr(item, "is_deleted", 0)),
        "created_at": item.created_at.isoformat() if item.created_at else "",
    }


async def get_all_pinned_candidates(db: AsyncSession, limit: int = 200) -> dict:
    """Return all pinned candidates across all sessions, newest first."""
    limit = max(1, min(int(limit or 200), 500))
    result = await db.execute(
        select(FactorMiningCandidate)
        .where(FactorMiningCandidate.is_pinned == 1, FactorMiningCandidate.is_deleted == 0)
        .order_by(FactorMiningCandidate.id.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return {"items": [_serialize_candidate(item) for item in rows], "count": len(rows)}


async def get_streaming_mining_results(db: AsyncSession, session_id: str, after_id: int = 0, limit: int = 50, pinned_only: bool = False) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    conditions = [
        FactorMiningCandidate.id > int(after_id or 0),
        FactorMiningCandidate.is_deleted == 0,
    ]
    if pinned_only:
        conditions.append(FactorMiningCandidate.is_pinned == 1)
    else:
        conditions.append(FactorMiningCandidate.session_id == session_id)
    result = await db.execute(
        select(FactorMiningCandidate)
        .where(*conditions)
        .order_by(FactorMiningCandidate.id.asc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return {
        "items": [_serialize_candidate(item) for item in rows],
        "last_id": rows[-1].id if rows else int(after_id or 0),
    }


async def pin_mining_candidate(db: AsyncSession, candidate_id: int) -> dict:
    result = await db.execute(
        select(FactorMiningCandidate).where(
            FactorMiningCandidate.id == int(candidate_id),
            FactorMiningCandidate.is_deleted == 0,
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise ValueError("mining candidate not found")
    candidate.is_pinned = 1
    await db.commit()
    await db.refresh(candidate)
    return {"success": True, "candidate": _serialize_candidate(candidate)}


async def delete_mining_candidate(db: AsyncSession, candidate_id: int) -> dict:
    result = await db.execute(select(FactorMiningCandidate).where(FactorMiningCandidate.id == int(candidate_id)))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise ValueError("mining candidate not found")
    candidate.is_deleted = 1
    await db.commit()
    return {"success": True, "candidate_id": int(candidate_id)}


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
                "equity": item.get("display_equity_curve") or item.get("equity_curve") or [],
                "normalized": item.get("display_normalized_curve") or item.get("normalized_curve") or [],
                "benchmark": item.get("display_benchmark_normalized_curve") or item.get("benchmark_normalized_curve") or [],
                "excess": item.get("display_excess_curve") or item.get("excess_curve") or [],
                "test_equity": item.get("test_equity_curve") or [],
                "test_normalized": item.get("test_normalized_curve") or [],
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
        candidate.revalidation_status = payload.get("revalidation_status") or "research_candidate"
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
            .values(status="stopping", phase="stopping", message="已收到停止请求，正在安全停止", updated_at=datetime.utcnow())
        )
        await db.commit()
        return {"success": True, "message": "已发送停止请求"}
    result = await db.execute(select(FactorMiningSession).where(FactorMiningSession.session_id == session_id))
    if not result.scalar_one_or_none():
        raise ValueError("挖掘会话不存在")
    return {"success": True, "message": "当前会话没有运行中的后台任务"}

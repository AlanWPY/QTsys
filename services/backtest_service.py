"""回测服务。"""
import asyncio
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BacktestResult, StockPool, Strategy
from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from engine.backtest_engine import BacktestEngine
from services.factor_board_service import get_system_universes
from services.settings_service import get_or_create_settings
from strategy.strategy_loader import load_strategy


def _normalize_codes(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items or []:
        code = str(item or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _normalize_stock_items(stock_items: list[dict]) -> list[dict]:
    normalized = []
    seen = set()
    for item in stock_items or []:
        raw = item or {}
        code = str(raw.get("ts_code") or raw.get("code") or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append({"ts_code": code, "name": str(raw.get("name") or "").strip()})
    return normalized


def _build_universe_label(
    universe_type: str,
    universe_code: str,
    universe_name: str,
    codes: list[str],
) -> str:
    count = len(codes)
    if universe_type == "system":
        label_name = universe_name or universe_code or "系统股票池"
        label_code = f" {universe_code}" if universe_code else ""
        return f"{label_name}{label_code} ({count}只)"
    if universe_type == "custom":
        label_name = universe_name or "自定义股票池"
        return f"{label_name} ({count}只)"
    if count <= 8:
        return ",".join(codes)
    return f"自定义列表 ({count}只): {','.join(codes[:5])}..."


async def resolve_backtest_universe(
    db: AsyncSession,
    settings,
    *,
    universe: Optional[list[str]] = None,
    universe_type: Optional[str] = None,
    universe_code: Optional[str] = None,
    universe_name: Optional[str] = None,
    custom_pool_id: Optional[int] = None,
    stock_items: Optional[list[dict]] = None,
) -> dict:
    explicit_codes = _normalize_codes(universe or [])
    normalized_type = str(universe_type or "").strip().lower()
    normalized_code = str(universe_code or "").strip().upper()
    normalized_name = str(universe_name or "").strip()

    if normalized_type == "custom":
        pool = None
        if custom_pool_id:
            result = await db.execute(select(StockPool).where(StockPool.id == int(custom_pool_id)))
            pool = result.scalar_one_or_none()
            if not pool:
                raise LookupError("自定义股票池不存在")
        items = _normalize_stock_items(stock_items or (pool.stock_items if pool else []))
        codes = _normalize_codes([item["ts_code"] for item in items] or explicit_codes)
        if not codes:
            raise ValueError("自定义股票池为空")
        resolved_name = normalized_name or (pool.name if pool else "自定义股票池")
        resolved_code = normalized_code or (f"custom:{pool.id}" if pool else "custom")
        return {
            "codes": codes,
            "universe_type": "custom",
            "universe_code": resolved_code,
            "universe_name": resolved_name,
            "universe_label": _build_universe_label("custom", resolved_code, resolved_name, codes),
        }

    if normalized_type == "system":
        if not normalized_code:
            raise ValueError("缺少系统股票池代码")
        system_map = {item["code"]: item for item in get_system_universes()}
        universe_meta = system_map.get(normalized_code)
        if not universe_meta:
            raise ValueError("系统股票池不存在")
        client = TushareClient(settings.tushare_token)
        end_date = pd.Timestamp.today().strftime("%Y%m%d")
        index_weight = pd.DataFrame()
        for lookback_days in (45, 120, 365):
            start_date = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")
            candidate = client.get_index_weight(normalized_code, start_date, end_date)
            if candidate is not None and not candidate.empty:
                index_weight = candidate
                break
        if index_weight is None or index_weight.empty:
            raise ValueError("系统股票池暂无可用成分股")
        frame = index_weight.copy()
        frame["trade_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False).str[:8]
        frame["con_code"] = frame["con_code"].astype(str).str.strip().str.upper()
        frame["weight"] = pd.to_numeric(frame.get("weight", 0), errors="coerce").fillna(0.0)
        latest_trade_date = str(frame["trade_date"].max() or "")
        latest = frame[frame["trade_date"] == latest_trade_date].copy()
        latest = latest.sort_values(["weight", "con_code"], ascending=[False, True])
        codes = _normalize_codes(latest["con_code"].tolist())
        if not codes:
            raise ValueError("系统股票池暂无可用成分股")
        resolved_name = normalized_name or str(universe_meta.get("name") or normalized_code)
        return {
            "codes": codes,
            "universe_type": "system",
            "universe_code": normalized_code,
            "universe_name": resolved_name,
            "universe_label": _build_universe_label("system", normalized_code, resolved_name, codes),
        }

    if not explicit_codes:
        raise ValueError("股票池不能为空")
    return {
        "codes": explicit_codes,
        "universe_type": "",
        "universe_code": normalized_code,
        "universe_name": normalized_name,
        "universe_label": _build_universe_label("", normalized_code, normalized_name, explicit_codes),
    }


async def run_backtest_workflow(
    db: AsyncSession,
    *,
    strategy_id: int,
    universe: list[str],
    start_date: str,
    end_date: str,
    initial_cash: float,
    benchmark: str,
    max_position_pct: float,
    max_drawdown_limit: float,
    universe_type: Optional[str] = None,
    universe_code: Optional[str] = None,
    universe_name: Optional[str] = None,
    custom_pool_id: Optional[int] = None,
    stock_items: Optional[list[dict]] = None,
):
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise LookupError("策略不存在")

    init_func, handle_func = load_strategy(strategy.code)
    resolved_universe = await resolve_backtest_universe(
        db,
        settings,
        universe=universe,
        universe_type=universe_type,
        universe_code=universe_code,
        universe_name=universe_name,
        custom_pool_id=custom_pool_id,
        stock_items=stock_items,
    )
    resolved_codes = resolved_universe["codes"]

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = BacktestEngine(
        cache=cache,
        initial_cash=initial_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        max_position_pct=max_position_pct,
        max_drawdown_limit=max_drawdown_limit,
    )
    engine.universe = resolved_codes

    result_data = await asyncio.to_thread(
        engine.run,
        universe=resolved_codes,
        start_date=start_date,
        end_date=end_date,
        initialize_func=init_func,
        handle_data_func=handle_func,
        benchmark=benchmark,
    )
    if "error" in result_data:
        raise ValueError(result_data["error"])

    bt_result = BacktestResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        start_date=start_date,
        end_date=end_date,
        universe=resolved_universe["universe_label"],
        initial_cash=initial_cash,
        final_value=result_data["final_value"],
        metrics=result_data["metrics"],
        equity_curve=result_data["equity_curve"],
        trades=result_data["trades"],
        daily_returns=result_data["daily_returns"],
        benchmark_curve=result_data.get("benchmark_curve", []),
    )
    db.add(bt_result)
    await db.commit()
    await db.refresh(bt_result)

    return {
        "id": bt_result.id,
        "metrics": result_data["metrics"],
        "equity_curve": result_data["equity_curve"],
        "trades": result_data["trades"],
        "logs": result_data.get("logs", []),
        "final_value": result_data["final_value"],
        "benchmark_curve": result_data.get("benchmark_curve", []),
        "universe": resolved_universe["universe_label"],
        "resolved_universe_count": len(resolved_codes),
    }

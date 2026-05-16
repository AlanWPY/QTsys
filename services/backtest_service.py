"""回测服务。"""
import asyncio
import re
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BacktestResult, Factor, StockPool, Strategy
from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from engine.backtest_engine import BacktestEngine
from factor.factor_backtest import run_selection_backtest
from factor.factor_engine import FactorEngine
from services.factor_board_service import get_system_universes
from services.factor_catalog_service import load_factor_catalog
from services.settings_service import get_or_create_settings
from strategy.strategy_loader import load_strategy


def _extract_generated_factor_strategy_config(strategy_code: str) -> Optional[dict]:
    code = strategy_code or ""
    factor_match = re.search(r"context\.factor_ref\s*=\s*[\"']id:(\d+)[\"']", code)
    if not factor_match or "context.get_factor(context.factor_ref" not in code:
        return None

    def _float(name: str, default: float) -> float:
        match = re.search(rf"context\.{name}\s*=\s*([0-9.]+)", code)
        if not match:
            return default
        try:
            return float(match.group(1))
        except Exception:
            return default

    def _int(name: str, default: int) -> int:
        match = re.search(rf"context\.{name}\s*=\s*(\d+)", code)
        if not match:
            return default
        try:
            return int(match.group(1))
        except Exception:
            return default

    direction = "top"
    direction_match = re.search(r"context\.factor_direction_high_is_better\s*=\s*(True|False)", code)
    if direction_match and direction_match.group(1) == "False":
        direction = "bottom"

    return {
        "factor_id": int(factor_match.group(1)),
        "direction": direction,
        "select_pct": max(0.001, min(_float("select_pct", 0.1), 1.0)),
        "rebalance_days": max(1, min(_int("rebalance_days", 5), 60)),
        "target_exposure": max(0.0, min(_float("target_exposure", 0.95), 1.0)),
        "max_position_pct": max(0.0, min(_float("max_position_pct", 0.12), 1.0)),
    }


def _detect_required_history_fields(strategy_code: str) -> set[str]:
    required = set()
    code = strategy_code or ""
    for field in ("turnover_rate", "volume", "vol", "open", "high", "low", "close", "amount"):
        if f"'{field}'" in code or f'"{field}"' in code:
            required.add(field)
    return required


def infer_strategy_profile(strategy_code: str, resolved_codes: list[str]) -> dict:
    code = (strategy_code or "").lower()
    uses_universe = "context.universe" in code
    has_rebalance = any(token in code for token in ["rebalance", "target_stocks", "select_count", "candidates", "sorted("])
    has_single_asset_bias = any(token in code for token in ["holding_stock", "context.holding_stock"])
    holds_many = any(token in code for token in ["max_positions", "target_weight", "order_target_percent"])

    if uses_universe and (has_rebalance or holds_many):
        strategy_type = "选股策略"
    elif len(resolved_codes) <= 3 or has_single_asset_bias:
        strategy_type = "择时策略"
    else:
        strategy_type = "混合策略"

    execution_scope = "多标的" if len(resolved_codes) > 1 else "单标的"
    notes = []
    if uses_universe:
        notes.append("使用 context.universe 作为股票池入口")
    if has_rebalance:
        notes.append("包含排序/调仓/候选集逻辑")
    if has_single_asset_bias:
        notes.append("包含单标的持仓控制逻辑")
    return {
        "strategy_type": strategy_type,
        "execution_scope": execution_scope,
        "universe_size": len(resolved_codes),
        "notes": notes,
    }


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


def _normalize_trade_date(value: Optional[str]) -> str:
    text = str(value or "").replace("-", "").strip()
    return text[:8] if len(text) >= 8 else ""


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
    as_of_date: Optional[str] = None,
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
        aux_cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
        resolved_as_of = _normalize_trade_date(as_of_date) or pd.Timestamp.today().strftime("%Y%m%d")
        end_date = resolved_as_of
        start_date = (pd.to_datetime(resolved_as_of) - pd.Timedelta(days=365 * 5)).strftime("%Y%m%d")
        try:
            index_weight = aux_cache.get_index_weight(normalized_code, start_date, end_date)
        finally:
            if aux_cache.mysql:
                try:
                    aux_cache.mysql.close()
                except Exception:
                    pass
        if index_weight is None or index_weight.empty:
            raise ValueError("系统股票池暂无可用成分股")
        frame = index_weight.copy()
        frame["trade_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False).str[:8]
        frame["con_code"] = frame["con_code"].astype(str).str.strip().str.upper()
        frame["weight"] = pd.to_numeric(frame.get("weight", 0), errors="coerce").fillna(0.0)
        frame = frame[frame["trade_date"] <= resolved_as_of]
        if frame.empty:
            raise ValueError("绯荤粺鑲＄エ姹犲湪鍥炴祴璧风偣鍓嶆殏鏃犲彲鐢ㄦ垚鍒嗚偂")
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
            "universe_as_of_date": latest_trade_date,
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
    required_fields = _detect_required_history_fields(strategy.code)
    resolved_universe = await resolve_backtest_universe(
        db,
        settings,
        universe=universe,
        universe_type=universe_type,
        universe_code=universe_code,
        universe_name=universe_name,
        custom_pool_id=custom_pool_id,
        stock_items=stock_items,
        as_of_date=start_date,
    )
    resolved_codes = resolved_universe["codes"]

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    factor_catalog = await load_factor_catalog(db)
    factor_strategy_config = _extract_generated_factor_strategy_config(strategy.code)
    if factor_strategy_config:
        factor_result = await db.execute(select(Factor).where(Factor.id == factor_strategy_config["factor_id"]))
        factor = factor_result.scalar_one_or_none()
        if not factor:
            raise ValueError("因子策略引用的因子不存在")
        factor_engine = FactorEngine(cache)
        result_data = await asyncio.to_thread(
            run_selection_backtest,
            cache,
            factor_engine,
            factor.expression,
            resolved_codes,
            start_date,
            end_date,
            factor_strategy_config["direction"],
            factor_strategy_config["select_pct"],
            factor_strategy_config["rebalance_days"],
            initial_cash,
            benchmark,
            settings.commission_rate,
            settings.stamp_tax_rate,
            settings.slippage,
            factor_strategy_config["max_position_pct"],
            factor_strategy_config["target_exposure"],
        )
        if "error" not in result_data:
            result_data["final_value"] = round(float((result_data.get("equity_curve") or [{"value": initial_cash}])[-1].get("value", initial_cash)), 2)
            result_data["logs"] = [
                f"因子策略使用 canonical 因子选股回测: factor_id={factor.id}, direction={factor_strategy_config['direction']}, "
                f"select_pct={factor_strategy_config['select_pct']}, rebalance_days={factor_strategy_config['rebalance_days']}"
            ]
            result_data["order_rejections"] = [
                {"reason": reason, "count": count}
                for reason, count in sorted((result_data.get("rejection_reasons") or {}).items(), key=lambda item: (-item[1], item[0]))
            ]
            result_data["order_trace"] = result_data.get("trades", [])[-1000:]
            result_data["data_coverage"] = {
                "requested_stock_count": len(resolved_codes),
                "loaded_stock_count": len(resolved_codes),
                "missing_stock_count": 0,
                "missing_stocks": [],
                "trade_date_count": len(result_data.get("equity_curve") or []),
                "start_date": start_date,
                "end_date": end_date,
                "factor_id": factor.id,
                "factor_name": factor.name,
            }
            result_data["execution_model"] = {
                **(result_data.get("assumption_report") or {}),
                "engine": "canonical_factor_selection_from_generated_strategy",
                "factor_strategy_config": factor_strategy_config,
            }
    else:
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
            required_fields=required_fields,
            factor_catalog=factor_catalog,
        )
    if "error" in result_data:
        raise ValueError(result_data["error"])

    data_coverage = {
        **(result_data.get("data_coverage") or {}),
        "universe_type": resolved_universe.get("universe_type", ""),
        "universe_code": resolved_universe.get("universe_code", ""),
        "universe_name": resolved_universe.get("universe_name", ""),
        "universe_label": resolved_universe.get("universe_label", ""),
        "universe_as_of_date": resolved_universe.get("universe_as_of_date", ""),
    }
    execution_model = result_data.get("execution_model") or {}
    persisted_metrics = {
        **(result_data["metrics"] or {}),
        "data_coverage": data_coverage,
        "execution_model": execution_model,
        "order_trace": result_data.get("order_trace", [])[-1000:],
        "validation_note": "信号按截至当日数据生成，订单下一交易日开盘执行；系统股票池按回测起点前成分股快照解析。",
    }

    bt_result = BacktestResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        start_date=start_date,
        end_date=end_date,
        universe=resolved_universe["universe_label"],
        initial_cash=initial_cash,
        final_value=result_data["final_value"],
        metrics=persisted_metrics,
        equity_curve=result_data["equity_curve"],
        trades=result_data["trades"],
        daily_returns=result_data["daily_returns"],
        benchmark_curve=result_data.get("benchmark_curve", []),
    )
    db.add(bt_result)
    await db.commit()
    await db.refresh(bt_result)

    strategy_profile = infer_strategy_profile(strategy.code, resolved_codes)

    return {
        "id": bt_result.id,
        "metrics": result_data["metrics"],
        "equity_curve": result_data["equity_curve"],
        "trades": result_data["trades"],
        "order_rejections": result_data.get("order_rejections", []),
        "order_trace": result_data.get("order_trace", []),
        "logs": result_data.get("logs", []),
        "final_value": result_data["final_value"],
        "benchmark_curve": result_data.get("benchmark_curve", []),
        "universe": resolved_universe["universe_label"],
        "resolved_universe_count": len(resolved_codes),
        "strategy_profile": strategy_profile,
        "factor_catalog_count": len(factor_catalog),
        "data_coverage": data_coverage,
        "execution_model": execution_model,
        "validation_note": persisted_metrics["validation_note"],
    }

"""因子回测引擎 - 选股因子回测 + 技术因子回测"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from engine.execution_simulator import CanonicalExecutionSimulator, ExecutionSettings, PanelMarketData
from logging_config import get_logger

logger = get_logger("qtsys.factor.backtest")


def _warmup_start_date(start_date: str, days: int = 540) -> str:
    try:
        return (datetime.strptime(str(start_date), "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    except Exception:
        return start_date


def _stock_name_map(cache) -> dict[str, str]:
    try:
        client = getattr(cache, "client", None)
        if client is None:
            return {}
        frame = client.get_stock_basic()
        if frame is None or frame.empty:
            return {}
        return {
            str(row.get("ts_code") or "").upper(): str(row.get("name") or "")
            for row in frame.to_dict("records")
        }
    except Exception:
        return {}


def run_selection_backtest(
    cache, factor_engine, expression: str, universe: list[str],
    start_date: str, end_date: str,
    select_mode: str = "top", select_pct: float = 0.1,
    rebalance_days: int = 5, initial_cash: float = 1_000_000,
    benchmark: str = "000300.SH",
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage: float = 0.001,
    max_position_pct: float = 0.12,
    target_exposure: float = 0.95,
) -> dict:
    """选股因子回测
    每个调仓日: 计算全股票池因子值 → 排序 → 选取top/bottom N% → 等权买入
    """
    logger.info(f"选股回测: {expression}, 股票池={len(universe)}, {start_date}-{end_date}")

    stock_factors = {}
    stock_data = {}
    stock_names = _stock_name_map(cache)
    warmup_start = _warmup_start_date(start_date)
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)

    for ts_code in universe:
        fv = factor_engine.compute_factor_values(expression, ts_code, warmup_start, end_date)
        if fv is None:
            continue
        exec_df = cache.get_daily(ts_code, warmup_start, end_date, adj=None)
        if exec_df.empty:
            continue
        frame = exec_df.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame = frame.dropna(subset=["trade_date"]).sort_values("trade_date")
        if frame.empty or "close" not in frame.columns:
            continue
        indexed = frame.set_index("trade_date")
        factor_series = pd.Series(fv).copy()
        factor_series.index = pd.to_datetime(factor_series.index, errors="coerce")
        factor_series = pd.to_numeric(factor_series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        factor_series = factor_series[factor_series.index <= end_ts]
        if factor_series.empty:
            continue
        indexed = indexed[indexed.index <= end_ts]
        if indexed.empty:
            continue
        closes = pd.to_numeric(indexed["close"], errors="coerce")
        opens = pd.to_numeric(indexed["open"], errors="coerce") if "open" in indexed.columns else closes.shift(1)
        highs = pd.to_numeric(indexed["high"], errors="coerce") if "high" in indexed.columns else pd.concat([opens, closes], axis=1).max(axis=1)
        lows = pd.to_numeric(indexed["low"], errors="coerce") if "low" in indexed.columns else pd.concat([opens, closes], axis=1).min(axis=1)
        vols = pd.to_numeric(indexed["vol"], errors="coerce") if "vol" in indexed.columns else pd.Series(0, index=indexed.index, dtype=float)
        pre_close = pd.to_numeric(indexed["pre_close"], errors="coerce") if "pre_close" in indexed.columns else closes.shift(1)
        stock_factors[ts_code] = factor_series
        stock_data[ts_code] = {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "vol": vols,
            "pre_close": pre_close.fillna(closes),
            "prev_close": pre_close.fillna(closes),
        }

    if len(stock_factors) < 5:
        return {"error": f"有效股票不足(仅{len(stock_factors)}只)"}

    all_dates = sorted(set().union(*[data["close"].dropna().index for data in stock_data.values()]))
    segment_dates = {dt for dt in all_dates if start_ts <= pd.to_datetime(dt) <= end_ts}
    if len(all_dates) < rebalance_days * 2:
        return {"error": "交易日不足"}
    if len(segment_dates) < 2:
        return {"error": "回测正式区间交易日不足"}

    benchmark_close = pd.Series(dtype=float)
    try:
        bench_df = cache.get_index_daily(benchmark, start_date, end_date)
        if bench_df is not None and not bench_df.empty and "close" in bench_df.columns:
            bench = bench_df.copy()
            bench["trade_date"] = pd.to_datetime(bench["trade_date"], errors="coerce")
            bench = bench.dropna(subset=["trade_date"]).sort_values("trade_date")
            benchmark_close = pd.to_numeric(bench.set_index("trade_date")["close"], errors="coerce").dropna()
    except Exception:
        benchmark_close = pd.Series(dtype=float)

    simulator = CanonicalExecutionSimulator(
        ExecutionSettings(
            initial_cash=float(initial_cash or 1_000_000),
            commission_rate=max(0.0, float(commission_rate or 0.0)),
            stamp_tax_rate=max(0.0, float(stamp_tax_rate or 0.0)),
            slippage=max(0.0, float(slippage or 0.0)),
            transfer_fee_rate=0.0,
            volume_limit_pct=0.10,
            max_position_pct=max(0.0, float(max_position_pct or 0.12)),
            target_exposure=max(0.0, min(float(target_exposure or 0.95), 1.0)),
            exclude_star_market=True,
            exclude_st=True,
            protocol_version="factor_selection_canonical_v1",
        )
    )
    market = PanelMarketData(stock_data, all_dates, benchmark_close=benchmark_close, benchmark_code=benchmark, stock_names=stock_names)
    result = simulator.run_factor_selection(
        factors=stock_factors,
        market=market,
        select_pct=select_pct,
        rebalance_days=rebalance_days,
        direction="top" if select_mode == "top" else "bottom",
        segment_dates=segment_dates,
    )
    if "error" in result:
        return result
    result["daily_returns"] = result.get("daily_returns", [])
    result["stock_pool_history"] = []
    return result


def run_technical_backtest(
    cache, factor_engine, expression: str, ts_codes: list[str],
    start_date: str, end_date: str,
    open_threshold: float = 0.0, close_threshold: float = 0.0,
    open_condition: str = "gt", close_condition: str = "lt",
    initial_cash: float = 1_000_000,
) -> dict:
    """技术因子回测
    单/多股票: 因子值 > open_threshold 开仓, < close_threshold 平仓
    """
    logger.info(f"技术回测: {expression}, 股票={ts_codes}, {start_date}-{end_date}")

    all_equity = []
    all_trades = []
    all_daily_ret = []

    per_stock_cash = initial_cash / max(len(ts_codes), 1)

    for ts_code in ts_codes:
        fv = factor_engine.compute_factor_values(expression, ts_code, start_date, end_date)
        if fv is None:
            continue
        df = cache.get_daily(ts_code, start_date, end_date)
        if df.empty:
            continue

        closes = df.set_index("trade_date")["close"]
        cash = per_stock_cash
        position = 0
        prev_val = per_stock_cash
        equity = []
        trades = []
        daily_ret = []

        dates = list(closes.index)
        for idx, dt in enumerate(dates):
            price = closes.loc[dt]
            signal_dt = dates[idx - 1] if idx > 0 else None
            fval = fv.get(signal_dt) if signal_dt is not None and signal_dt in fv.index else None
            exec_price = df.set_index("trade_date")["open"].get(dt) if "open" in df.columns else price

            if fval is not None and pd.notna(fval):
                # 开仓信号
                should_open = (
                    (open_condition == "gt" and fval > open_threshold) or
                    (open_condition == "lt" and fval < open_threshold)
                )
                # 平仓信号
                should_close = (
                    (close_condition == "lt" and fval < close_threshold) or
                    (close_condition == "gt" and fval > close_threshold)
                )

                date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)

                if should_open and position == 0 and exec_price > 0:
                    shares = int(cash / exec_price / 100) * 100
                    if shares > 0:
                        cash -= shares * exec_price * (1 + 0.0003)
                        position = shares
                        trades.append({"date": date_str, "code": ts_code,
                                       "action": "buy", "price": float(exec_price),
                                       "shares": shares})

                elif should_close and position > 0:
                    cash += position * exec_price * (1 - 0.0003 - 0.001)
                    trades.append({"date": date_str, "code": ts_code,
                                   "action": "sell", "price": float(exec_price),
                                   "shares": position})
                    position = 0

            # 当日组合价值
            cur_val = cash + position * price
            date_str2 = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)
            equity.append({"date": date_str2, "value": round(cur_val, 2)})
            ret = (cur_val - prev_val) / prev_val if prev_val > 0 else 0.0
            daily_ret.append(round(ret, 6))
            prev_val = cur_val

        all_equity.extend(equity)
        all_trades.extend(trades)
        all_daily_ret.extend(daily_ret)

    if not all_equity:
        return {"error": "回测无有效数据"}

    final_value = all_equity[-1]["value"]
    metrics = calc_metrics(all_daily_ret, [], initial_cash, final_value)

    return {
        "metrics": metrics,
        "equity_curve": all_equity,
        "trades": all_trades[:500],
        "daily_returns": all_daily_ret,
    }

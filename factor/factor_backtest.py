"""因子回测引擎 - 选股因子回测 + 技术因子回测"""
import numpy as np
import pandas as pd
from typing import Optional
from engine.metrics import calc_metrics
from logging_config import get_logger

logger = get_logger("qtsys.factor.backtest")


def run_selection_backtest(
    cache, factor_engine, expression: str, universe: list[str],
    start_date: str, end_date: str,
    select_mode: str = "top", select_pct: float = 0.1,
    rebalance_days: int = 5, initial_cash: float = 1_000_000,
    benchmark: str = "000300.SH",
) -> dict:
    """选股因子回测
    每个调仓日: 计算全股票池因子值 → 排序 → 选取top/bottom N% → 等权买入
    """
    logger.info(f"选股回测: {expression}, 股票池={len(universe)}, {start_date}-{end_date}")

    # 1. 收集所有股票的因子值和收盘价
    stock_factors = {}  # {ts_code: Series(date->factor_value)}
    stock_closes = {}  # {ts_code: Series(date->close)}

    for ts_code in universe:
        fv = factor_engine.compute_factor_values(expression, ts_code, start_date, end_date)
        if fv is None:
            continue
        df = cache.get_daily(ts_code, start_date, end_date)
        if df.empty:
            continue
        closes = df.set_index("trade_date")["close"]
        stock_factors[ts_code] = fv
        stock_closes[ts_code] = closes

    if len(stock_factors) < 5:
        return {"error": f"有效股票不足(仅{len(stock_factors)}只)"}

    # 2. 获取所有交易日
    all_dates = sorted(set().union(*[s.index for s in stock_closes.values()]))
    if len(all_dates) < rebalance_days * 2:
        return {"error": "交易日不足"}

    # 3. 模拟回测
    cash = initial_cash
    holdings = {}  # {ts_code: shares}
    equity_curve = []
    daily_returns = []
    trades = []
    stock_pool_history = []
    prev_value = initial_cash

    rebalance_idx = list(range(0, len(all_dates), rebalance_days))

    for i, dt in enumerate(all_dates):
        # 调仓日
        if i in rebalance_idx:
            # 计算当日因子值
            scores = {}
            for ts_code, fv in stock_factors.items():
                if dt in fv.index and pd.notna(fv.loc[dt]):
                    scores[ts_code] = fv.loc[dt]

            if len(scores) < 3:
                continue

            # 排序选股
            sorted_stocks = sorted(scores.items(), key=lambda x: x[1],
                                   reverse=(select_mode == "top"))
            n_select = max(1, int(len(sorted_stocks) * select_pct))
            selected = [s[0] for s in sorted_stocks[:n_select]]
            stock_pool_history.append({
                "date": dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt),
                "stocks": selected,
            })

            # 卖出不在新组合中的持仓
            for ts_code in list(holdings.keys()):
                if ts_code not in selected and ts_code in stock_closes:
                    price = stock_closes[ts_code].get(dt)
                    if price and holdings[ts_code] > 0:
                        sell_val = holdings[ts_code] * price
                        cash += sell_val
                        trades.append({"date": str(dt)[:10], "code": ts_code,
                                       "action": "sell", "price": float(price),
                                       "shares": holdings[ts_code]})
                        del holdings[ts_code]

            # 等权买入新选股
            total_val = cash + sum(
                holdings.get(c, 0) * stock_closes[c].get(dt, 0)
                for c in holdings if c in stock_closes
            )
            per_stock = total_val / n_select if n_select > 0 else 0
            for ts_code in selected:
                if ts_code not in stock_closes:
                    continue
                price = stock_closes[ts_code].get(dt)
                if not price or price <= 0:
                    continue
                target_shares = int(per_stock / price / 100) * 100
                current = holdings.get(ts_code, 0)
                diff = target_shares - current
                if diff > 0:
                    cost = diff * price
                    if cost <= cash:
                        cash -= cost
                        holdings[ts_code] = target_shares
                        trades.append({"date": str(dt)[:10], "code": ts_code,
                                       "action": "buy", "price": float(price),
                                       "shares": diff})
                elif diff < 0:
                    cash += abs(diff) * price
                    holdings[ts_code] = target_shares

        # 计算当日组合价值
        port_val = cash
        for ts_code, shares in holdings.items():
            if ts_code in stock_closes:
                p = stock_closes[ts_code].get(dt)
                if p:
                    port_val += shares * p

        date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)
        equity_curve.append({"date": date_str, "value": round(port_val, 2)})
        day_ret = (port_val - prev_value) / prev_value if prev_value > 0 else 0.0
        daily_returns.append(round(day_ret, 6))
        prev_value = port_val

    if not equity_curve:
        return {"error": "回测无有效数据"}

    final_value = equity_curve[-1]["value"]

    # 4. 基准收益
    benchmark_returns = []
    try:
        bench_df = cache.get_index_daily(benchmark, start_date, end_date)
        if not bench_df.empty:
            bench_close = bench_df.set_index("trade_date")["close"]
            benchmark_returns = bench_close.pct_change().dropna().tolist()
    except Exception:
        pass

    # 5. 计算指标
    metrics = calc_metrics(daily_returns, benchmark_returns, initial_cash, final_value)

    return {
        "metrics": metrics,
        "equity_curve": equity_curve,
        "trades": trades[:500],
        "daily_returns": daily_returns,
        "stock_pool_history": stock_pool_history,
    }


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

        for dt in closes.index:
            price = closes.loc[dt]
            fval = fv.get(dt) if dt in fv.index else None

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

                if should_open and position == 0 and price > 0:
                    shares = int(cash / price / 100) * 100
                    if shares > 0:
                        cash -= shares * price
                        position = shares
                        trades.append({"date": date_str, "code": ts_code,
                                       "action": "buy", "price": float(price),
                                       "shares": shares})

                elif should_close and position > 0:
                    cash += position * price
                    trades.append({"date": date_str, "code": ts_code,
                                   "action": "sell", "price": float(price),
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
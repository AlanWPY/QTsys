"""策略归因分析模块 - 个股盈亏分解、行业归因、月度归因"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def compute_stock_pnl(
    trades: list[dict],
    initial_cash: float,
    end_prices: Optional[dict[str, float]] = None,
) -> list[dict]:
    """按个股分解盈亏。

    使用 FIFO 匹配成交，避免把未平仓买入成本错误计入已实现亏损。

    Args:
        trades: [{date, ts_code, side, amount, price, commission, tax}, ...]
        initial_cash: 初始资金
        end_prices: {ts_code: last_close}，用于计算期末未实现盈亏

    Returns:
        [{
            ts_code, buy_cost, sell_income, realized_pnl, unrealized_pnl, total_pnl,
            contribution_pct, trade_count, buy_count, sell_count,
            ending_position, ending_cost, ending_price
        }, ...]
    """
    if not trades:
        return []

    end_prices = end_prices or {}
    grouped: dict[str, dict] = {}
    ordered_trades = sorted(
        trades,
        key=lambda item: (
            str(item.get("date") or ""),
            0 if str(item.get("side") or "").lower() == "buy" else 1,
        ),
    )

    for trade in ordered_trades:
        code = str(trade.get("ts_code") or "").strip()
        if not code:
            continue
        if code not in grouped:
            grouped[code] = {
                "buy_cost": 0.0,
                "sell_income": 0.0,
                "realized_pnl": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "lots": [],
            }

        item = grouped[code]
        side = str(trade.get("side") or "").lower()
        amount = max(_to_int(trade.get("amount")), 0)
        price = _to_float(trade.get("price"))
        commission = _to_float(trade.get("commission"))
        tax = _to_float(trade.get("tax"))

        if amount <= 0 or price <= 0:
            continue

        if side == "buy":
            gross = amount * price
            item["buy_cost"] += gross + commission + tax
            item["buy_count"] += 1
            unit_cost = (gross + commission + tax) / amount if amount > 0 else price
            item["lots"].append(
                {
                    "amount": amount,
                    "unit_cost": unit_cost,
                    "price": price,
                }
            )
            continue

        # sell
        net_income = amount * price - commission - tax
        item["sell_income"] += net_income
        item["sell_count"] += 1

        remaining = amount
        matched_cost = 0.0
        while remaining > 0 and item["lots"]:
            lot = item["lots"][0]
            matched = min(remaining, lot["amount"])
            matched_cost += matched * lot["unit_cost"]
            lot["amount"] -= matched
            remaining -= matched
            if lot["amount"] <= 0:
                item["lots"].pop(0)

        if remaining > 0:
            matched_cost += remaining * price

        item["realized_pnl"] += net_income - matched_cost

    result = []
    for code, item in grouped.items():
        ending_position = sum(max(_to_int(lot.get("amount")), 0) for lot in item["lots"])
        ending_cost = sum(max(_to_int(lot.get("amount")), 0) * _to_float(lot.get("unit_cost")) for lot in item["lots"])
        ending_price = _to_float(end_prices.get(code))
        market_value = ending_position * ending_price if ending_position > 0 and ending_price > 0 else 0.0
        unrealized_pnl = market_value - ending_cost if ending_position > 0 and ending_price > 0 else 0.0
        total_pnl = item["realized_pnl"] + unrealized_pnl
        contribution_pct = (total_pnl / initial_cash * 100) if initial_cash > 0 else 0.0

        result.append(
            {
                "ts_code": code,
                "buy_cost": round(item["buy_cost"], 2),
                "sell_income": round(item["sell_income"], 2),
                "realized_pnl": round(item["realized_pnl"], 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "contribution_pct": round(contribution_pct, 4),
                "trade_count": item["buy_count"] + item["sell_count"],
                "buy_count": item["buy_count"],
                "sell_count": item["sell_count"],
                "ending_position": ending_position,
                "ending_cost": round(ending_cost, 2),
                "ending_price": round(ending_price, 4) if ending_price > 0 else 0.0,
            }
        )

    result.sort(key=lambda x: x["total_pnl"], reverse=True)
    return result


def compute_sector_attribution(
    trades: list[dict],
    industry_map: dict[str, str],
    benchmark_sector_returns: Optional[dict[str, float]] = None,
    benchmark_total_return: float = 0.0,
    initial_cash: float = 1_000_000.0,
    stock_pnl: Optional[list[dict]] = None,
) -> list[dict]:
    """行业归因分析。

    当未提供基准行业收益时，返回基于个股总盈亏聚合的行业贡献，避免伪 Brinson 结果失真。
    """
    if not trades and not stock_pnl:
        return []

    if stock_pnl:
        sector_map: dict[str, dict] = defaultdict(lambda: {"invested": 0.0, "pnl": 0.0, "count": 0})
        total_invested = 0.0
        for item in stock_pnl:
            code = str(item.get("ts_code") or "").strip()
            sector = industry_map.get(code, "未知")
            invested = _to_float(item.get("buy_cost"))
            pnl = _to_float(item.get("total_pnl"))
            sector_map[sector]["invested"] += invested
            sector_map[sector]["pnl"] += pnl
            sector_map[sector]["count"] += 1
            total_invested += invested

        if not sector_map:
            return []

        # 当前接口没有真实 benchmark_sector_returns，直接返回真实行业贡献更可靠。
        if not benchmark_sector_returns:
            rows = []
            for sector, item in sector_map.items():
                weight = (item["invested"] / total_invested * 100) if total_invested > 0 else 0.0
                contribution = (item["pnl"] / initial_cash * 100) if initial_cash > 0 else 0.0
                sector_return = (item["pnl"] / item["invested"] * 100) if item["invested"] > 0 else 0.0
                rows.append(
                    {
                        "sector": sector,
                        "portfolio_weight": round(weight, 2),
                        "portfolio_return": round(sector_return, 2),
                        "allocation": 0.0,
                        "selection": round(contribution, 4),
                        "interaction": 0.0,
                        "total": round(contribution, 4),
                        "stock_count": item["count"],
                    }
                )
            rows.sort(key=lambda x: abs(x["total"]), reverse=True)
            return rows

    # 保留旧接口的兜底逻辑
    sector_data = {}
    total_invested = 0.0
    for trade in trades:
        code = str(trade.get("ts_code") or "").strip()
        sector = industry_map.get(code, "未知")
        sector_data.setdefault(sector, {"invested": 0.0, "pnl": 0.0})
        amount = _to_float(trade.get("amount"))
        price = _to_float(trade.get("price"))
        comm = _to_float(trade.get("commission"))
        tax = _to_float(trade.get("tax"))
        if str(trade.get("side") or "").lower() == "buy":
            cost = amount * price + comm + tax
            sector_data[sector]["invested"] += cost
            total_invested += cost
        else:
            income = amount * price - comm - tax
            sector_data[sector]["pnl"] += income
    for sector in sector_data:
        sector_data[sector]["pnl"] -= sector_data[sector]["invested"]
    if total_invested <= 0:
        return []

    rows = []
    n_sectors = len(sector_data)
    bm_weight = 1.0 / n_sectors if n_sectors > 0 else 0.0
    for sector, item in sector_data.items():
        w_p = item["invested"] / total_invested if total_invested > 0 else 0.0
        r_p = item["pnl"] / item["invested"] if item["invested"] > 0 else 0.0
        r_b_sector = (_to_float(benchmark_sector_returns.get(sector)) / 100) if benchmark_sector_returns and sector in benchmark_sector_returns else 0.0
        r_b_total = benchmark_total_return / 100
        allocation = (w_p - bm_weight) * (r_b_sector - r_b_total)
        selection = bm_weight * (r_p - r_b_sector)
        interaction = (w_p - bm_weight) * (r_p - r_b_sector)
        rows.append(
            {
                "sector": sector,
                "portfolio_weight": round(w_p * 100, 2),
                "portfolio_return": round(r_p * 100, 2),
                "allocation": round(allocation * 100, 4),
                "selection": round(selection * 100, 4),
                "interaction": round(interaction * 100, 4),
                "total": round((allocation + selection + interaction) * 100, 4),
            }
        )
    rows.sort(key=lambda x: abs(x["total"]), reverse=True)
    return rows


def compute_monthly_attribution(
    equity_curve: list[dict],
    benchmark_curve: list[dict],
) -> list[dict]:
    """按月计算超额收益。"""
    if not equity_curve or len(equity_curve) < 2:
        return []

    bm_map = {}
    if benchmark_curve:
        for point in benchmark_curve:
            bm_map[point["date"]] = point["value"]

    monthly_eq = {}
    monthly_bm = {}
    for point in equity_curve:
        date_value = point["date"]
        ym = date_value[:6] if len(date_value) == 8 else date_value[:7].replace("-", "")
        value = point["value"]
        if ym not in monthly_eq:
            monthly_eq[ym] = [value, value]
        else:
            monthly_eq[ym][1] = value

        bm_value = bm_map.get(date_value)
        if bm_value is not None:
            if ym not in monthly_bm:
                monthly_bm[ym] = [bm_value, bm_value]
            else:
                monthly_bm[ym][1] = bm_value

    result = []
    for ym in sorted(monthly_eq.keys()):
        first, last = monthly_eq[ym]
        strategy_return = (last - first) / first * 100 if first > 0 else 0.0
        benchmark_return = 0.0
        if ym in monthly_bm:
            bm_first, bm_last = monthly_bm[ym]
            benchmark_return = (bm_last - bm_first) / bm_first * 100 if bm_first > 0 else 0.0
        result.append(
            {
                "month": ym[:4] + "-" + ym[4:],
                "strategy_return": round(strategy_return, 2),
                "benchmark_return": round(benchmark_return, 2),
                "excess_return": round(strategy_return - benchmark_return, 2),
            }
        )
    return result

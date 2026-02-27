"""策略归因分析模块 - 个股盈亏分解、行业归因、月度归因"""
import numpy as np
from typing import Optional


def compute_stock_pnl(trades: list[dict], initial_cash: float) -> list[dict]:
    """按个股分解盈亏。

    Args:
        trades: [{date, ts_code, side, amount, price, commission, tax}, ...]
        initial_cash: 初始资金

    Returns:
        [{ts_code, buy_cost, sell_income, realized_pnl, contribution_pct}, ...]
    """
    if not trades:
        return []

    stock_data = {}  # ts_code -> {buy_cost, sell_income, commission, tax}
    for t in trades:
        code = t.get("ts_code", "")
        if not code:
            continue
        if code not in stock_data:
            stock_data[code] = {
                "buy_cost": 0.0,
                "sell_income": 0.0,
                "commission": 0.0,
                "tax": 0.0,
                "buy_count": 0,
                "sell_count": 0,
            }
        sd = stock_data[code]
        amount = float(t.get("amount", 0))
        price = float(t.get("price", 0))
        comm = float(t.get("commission", 0))
        tax = float(t.get("tax", 0))

        if t.get("side") == "buy":
            sd["buy_cost"] += amount * price + comm
            sd["buy_count"] += 1
        else:
            sd["sell_income"] += amount * price - comm - tax
            sd["sell_count"] += 1
        sd["commission"] += comm
        sd["tax"] += tax

    result = []
    for code, sd in stock_data.items():
        realized_pnl = sd["sell_income"] - sd["buy_cost"]
        contribution_pct = (realized_pnl / initial_cash * 100) if initial_cash > 0 else 0
        result.append({
            "ts_code": code,
            "buy_cost": round(sd["buy_cost"], 2),
            "sell_income": round(sd["sell_income"], 2),
            "realized_pnl": round(realized_pnl, 2),
            "contribution_pct": round(contribution_pct, 4),
            "trade_count": sd["buy_count"] + sd["sell_count"],
        })

    result.sort(key=lambda x: x["realized_pnl"], reverse=True)
    return result


def compute_sector_attribution(
    trades: list[dict],
    industry_map: dict[str, str],
    benchmark_sector_returns: Optional[dict[str, float]] = None,
    benchmark_total_return: float = 0.0,
    initial_cash: float = 1_000_000.0,
) -> list[dict]:
    """行业归因分析（简化版 Brinson 模型）。

    Args:
        trades: 交易记录
        industry_map: {ts_code: industry_name}
        benchmark_sector_returns: {industry: return_pct} 基准行业收益
        benchmark_total_return: 基准总收益率
        initial_cash: 初始资金

    Returns:
        [{sector, portfolio_weight, portfolio_return, allocation, selection, interaction, total}]
    """
    if not trades:
        return []

    # 按行业聚合策略持仓
    sector_data = {}
    total_invested = 0.0

    for t in trades:
        code = t.get("ts_code", "")
        sector = industry_map.get(code, "未知")
        if sector not in sector_data:
            sector_data[sector] = {"invested": 0.0, "pnl": 0.0}

        amount = float(t.get("amount", 0))
        price = float(t.get("price", 0))
        comm = float(t.get("commission", 0))
        tax = float(t.get("tax", 0))

        if t.get("side") == "buy":
            cost = amount * price + comm
            sector_data[sector]["invested"] += cost
            total_invested += cost
        else:
            income = amount * price - comm - tax
            sector_data[sector]["pnl"] += income

    # 减去买入成本得到净盈亏
    for sector in sector_data:
        sector_data[sector]["pnl"] -= sector_data[sector]["invested"]

    if total_invested == 0:
        return []

    # 计算权重和收益
    result = []
    n_sectors = len(sector_data)
    bm_weight = 1.0 / n_sectors if n_sectors > 0 else 0  # 等权基准假设

    for sector, sd in sector_data.items():
        w_p = sd["invested"] / total_invested if total_invested > 0 else 0
        r_p = sd["pnl"] / sd["invested"] if sd["invested"] > 0 else 0

        r_b_sector = 0.0
        if benchmark_sector_returns and sector in benchmark_sector_returns:
            r_b_sector = benchmark_sector_returns[sector] / 100

        r_b_total = benchmark_total_return / 100

        allocation = (w_p - bm_weight) * (r_b_sector - r_b_total)
        selection = bm_weight * (r_p - r_b_sector)
        interaction = (w_p - bm_weight) * (r_p - r_b_sector)

        result.append({
            "sector": sector,
            "portfolio_weight": round(w_p * 100, 2),
            "portfolio_return": round(r_p * 100, 2),
            "allocation": round(allocation * 100, 4),
            "selection": round(selection * 100, 4),
            "interaction": round(interaction * 100, 4),
            "total": round((allocation + selection + interaction) * 100, 4),
        })

    result.sort(key=lambda x: abs(x["total"]), reverse=True)
    return result


def compute_monthly_attribution(
    equity_curve: list[dict],
    benchmark_curve: list[dict],
) -> list[dict]:
    """按月计算超额收益。

    Returns:
        [{month, strategy_return, benchmark_return, excess_return}]
    """
    if not equity_curve or len(equity_curve) < 2:
        return []

    # 构建基准查找表
    bm_map = {}
    if benchmark_curve:
        for p in benchmark_curve:
            bm_map[p["date"]] = p["value"]

    # 按月分组
    monthly_eq = {}
    monthly_bm = {}
    for p in equity_curve:
        d = p["date"]
        ym = d[:6] if len(d) == 8 else d[:7].replace("-", "")
        val = p["value"]
        if ym not in monthly_eq:
            monthly_eq[ym] = [val, val]
        else:
            monthly_eq[ym][1] = val

        bm_val = bm_map.get(d)
        if bm_val is not None:
            if ym not in monthly_bm:
                monthly_bm[ym] = [bm_val, bm_val]
            else:
                monthly_bm[ym][1] = bm_val

    result = []
    for ym in sorted(monthly_eq.keys()):
        first, last = monthly_eq[ym]
        s_ret = (last - first) / first * 100 if first > 0 else 0

        b_ret = 0.0
        if ym in monthly_bm:
            bf, bl = monthly_bm[ym]
            b_ret = (bl - bf) / bf * 100 if bf > 0 else 0

        result.append({
            "month": ym[:4] + "-" + ym[4:],
            "strategy_return": round(s_ret, 2),
            "benchmark_return": round(b_ret, 2),
            "excess_return": round(s_ret - b_ret, 2),
        })

    return result

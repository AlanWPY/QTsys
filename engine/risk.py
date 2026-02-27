"""风险分析模块 - 回撤分解、风险度量、月度收益矩阵"""
import numpy as np
from typing import Optional


def analyze_drawdowns(equity_curve: list[dict], threshold: float = 0.02) -> dict:
    """分析所有回撤事件，生成水下曲线。

    Args:
        equity_curve: [{date, value, cash}, ...]
        threshold: 最小回撤深度阈值（默认2%）

    Returns:
        {drawdowns: [...], underwater: [{date, depth}]}
    """
    if not equity_curve or len(equity_curve) < 2:
        return {"drawdowns": [], "underwater": []}

    dates = [p["date"] for p in equity_curve]
    values = np.array([p["value"] for p in equity_curve], dtype=float)

    # 水下曲线
    peak = np.maximum.accumulate(values)
    underwater = ((values - peak) / peak).tolist()
    underwater_series = [
        {"date": dates[i], "depth": round(underwater[i] * 100, 4)}
        for i in range(len(dates))
    ]

    # 识别回撤事件
    drawdowns = []
    in_dd = False
    dd_start = 0
    trough_idx = 0
    trough_val = 0.0

    for i in range(len(values)):
        if values[i] < peak[i]:
            if not in_dd:
                in_dd = True
                dd_start = max(0, i - 1)
                trough_idx = i
                trough_val = values[i]
            elif values[i] < trough_val:
                trough_idx = i
                trough_val = values[i]
        elif in_dd:
            # 恢复到前高
            depth = (trough_val - peak[dd_start]) / peak[dd_start]
            if abs(depth) >= threshold:
                duration = trough_idx - dd_start
                recovery = i - trough_idx
                drawdowns.append({
                    "start_date": dates[dd_start],
                    "trough_date": dates[trough_idx],
                    "recovery_date": dates[i],
                    "depth": round(depth * 100, 4),
                    "duration_days": duration,
                    "recovery_days": recovery,
                })
            in_dd = False

    # 处理未恢复的回撤
    if in_dd:
        depth = (trough_val - peak[dd_start]) / peak[dd_start]
        if abs(depth) >= threshold:
            drawdowns.append({
                "start_date": dates[dd_start],
                "trough_date": dates[trough_idx],
                "recovery_date": None,
                "depth": round(depth * 100, 4),
                "duration_days": trough_idx - dd_start,
                "recovery_days": None,
            })

    drawdowns.sort(key=lambda x: x["depth"])
    return {"drawdowns": drawdowns, "underwater": underwater_series}


def compute_risk_metrics(
    daily_returns: list[float],
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> dict:
    """计算风险度量指标。

    Returns:
        {var_95, cvar_95, rolling_vol_20, rolling_vol_60, rolling_sharpe_60,
         skewness, kurtosis, jarque_bera, jb_pvalue, histogram}
    """
    if not daily_returns or len(daily_returns) < 20:
        return {}

    ret = np.array(daily_returns, dtype=float)
    n = len(ret)
    daily_rf = risk_free_rate / trading_days

    # VaR / CVaR (历史模拟法)
    sorted_ret = np.sort(ret)
    var_idx = int(np.floor(n * 0.05))
    var_95 = float(sorted_ret[var_idx]) if var_idx < n else float(sorted_ret[0])
    cvar_95 = float(np.mean(sorted_ret[: max(var_idx, 1)]))

    # 滚动波动率
    rolling_vol_20 = _rolling_std(ret, 20, trading_days)
    rolling_vol_60 = _rolling_std(ret, 60, trading_days)

    # 滚动夏普
    rolling_sharpe_60 = _rolling_sharpe(ret, 60, daily_rf, trading_days)

    # 收益分布统计
    skewness = float(_skew(ret))
    kurtosis = float(_kurt(ret))
    jb_stat, jb_pvalue = _jarque_bera(ret)

    # 直方图数据
    counts, bin_edges = np.histogram(ret, bins=50)
    histogram = {
        "counts": counts.tolist(),
        "bin_edges": [round(float(b), 6) for b in bin_edges],
    }

    return {
        "var_95": round(var_95 * 100, 4),
        "cvar_95": round(cvar_95 * 100, 4),
        "rolling_vol_20": rolling_vol_20,
        "rolling_vol_60": rolling_vol_60,
        "rolling_sharpe_60": rolling_sharpe_60,
        "skewness": round(skewness, 4),
        "kurtosis": round(kurtosis, 4),
        "jarque_bera": round(jb_stat, 4),
        "jb_pvalue": round(jb_pvalue, 4),
        "histogram": histogram,
    }


def compute_monthly_returns(equity_curve: list[dict]) -> list[dict]:
    """按年-月聚合日收益率为月度收益矩阵。

    Returns:
        [{year, months: [jan..dec], annual}]
    """
    if not equity_curve or len(equity_curve) < 2:
        return []

    # 按月分组计算收益
    monthly = {}  # (year, month) -> (first_value, last_value)
    for p in equity_curve:
        d = p["date"]  # YYYYMMDD or YYYY-MM-DD
        y = int(d[:4])
        m = int(d[4:6]) if len(d) == 8 else int(d[5:7])
        key = (y, m)
        val = p["value"]
        if key not in monthly:
            monthly[key] = [val, val]
        else:
            monthly[key][1] = val

    # 按年聚合
    years = sorted(set(k[0] for k in monthly))
    result = []
    for y in years:
        months = [None] * 12
        for m in range(1, 13):
            key = (y, m)
            if key in monthly:
                first, last = monthly[key]
                months[m - 1] = round((last - first) / first * 100, 2) if first > 0 else 0
        # 年度收益
        year_vals = [(y, m) for m in range(1, 13) if (y, m) in monthly]
        if year_vals:
            first_key = year_vals[0]
            last_key = year_vals[-1]
            annual = round(
                (monthly[last_key][1] - monthly[first_key][0])
                / monthly[first_key][0]
                * 100,
                2,
            ) if monthly[first_key][0] > 0 else 0
        else:
            annual = 0
        result.append({"year": y, "months": months, "annual": annual})

    return result


# ---- 内部辅助函数 ----

def _rolling_std(ret: np.ndarray, window: int, trading_days: int) -> list[dict]:
    """滚动年化波动率"""
    if len(ret) < window:
        return []
    result = []
    for i in range(window - 1, len(ret)):
        val = float(np.std(ret[i - window + 1 : i + 1], ddof=1) * np.sqrt(trading_days))
        result.append({"idx": i, "value": round(val * 100, 2)})
    return result


def _rolling_sharpe(
    ret: np.ndarray, window: int, daily_rf: float, trading_days: int
) -> list[dict]:
    """滚动夏普比率"""
    if len(ret) < window:
        return []
    result = []
    for i in range(window - 1, len(ret)):
        chunk = ret[i - window + 1 : i + 1]
        excess = chunk - daily_rf
        std = np.std(excess, ddof=1)
        sharpe = float(np.mean(excess) / std * np.sqrt(trading_days)) if std > 0 else 0
        result.append({"idx": i, "value": round(sharpe, 4)})
    return result


def _skew(x: np.ndarray) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    m = np.mean(x)
    s = np.std(x, ddof=1)
    if s == 0:
        return 0.0
    return float(n / ((n - 1) * (n - 2)) * np.sum(((x - m) / s) ** 3))


def _kurt(x: np.ndarray) -> float:
    n = len(x)
    if n < 4:
        return 0.0
    m = np.mean(x)
    s = np.std(x, ddof=1)
    if s == 0:
        return 0.0
    k = float(np.mean(((x - m) / s) ** 4))
    return k - 3  # excess kurtosis


def _jarque_bera(x: np.ndarray) -> tuple[float, float]:
    n = len(x)
    if n < 8:
        return 0.0, 1.0
    s = _skew(x)
    k = _kurt(x)
    jb = n / 6 * (s ** 2 + k ** 2 / 4)
    # chi2 近似 p-value (df=2)
    p = float(np.exp(-jb / 2)) if jb < 100 else 0.0
    return float(jb), p

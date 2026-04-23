"""回测绩效指标计算."""
from __future__ import annotations

import numpy as np

EPSILON = 1e-12


def _safe_std(values: np.ndarray, ddof: int = 1) -> float:
    if values.size <= ddof:
        return 0.0
    std = float(np.std(values, ddof=ddof))
    if not np.isfinite(std) or abs(std) < EPSILON:
        return 0.0
    return std


def calc_metrics(
    daily_returns: list[float],
    benchmark_returns: list[float],
    initial_cash: float,
    final_value: float,
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> dict:
    if not daily_returns or initial_cash <= 0:
        return {}

    ret = np.asarray(daily_returns, dtype=float)
    ret = np.nan_to_num(ret, nan=0.0, posinf=0.0, neginf=0.0)
    n_days = len(ret)

    total_return = (final_value - initial_cash) / initial_cash
    years = n_days / trading_days
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 and final_value > 0 else 0.0

    annual_vol = _safe_std(ret, ddof=1) * np.sqrt(trading_days)
    daily_rf = risk_free_rate / trading_days
    excess = ret - daily_rf
    excess_std = _safe_std(excess, ddof=1)
    sharpe = (float(np.mean(excess)) / excess_std * np.sqrt(trading_days)) if excess_std > EPSILON else 0.0

    cumulative = np.cumprod(1 + ret)
    peak = np.maximum.accumulate(cumulative)
    drawdown = np.divide(cumulative - peak, peak, out=np.zeros_like(cumulative), where=peak != 0)
    max_drawdown = float(np.min(drawdown)) if drawdown.size else 0.0

    downside = ret[ret < daily_rf] - daily_rf
    downside_std = _safe_std(downside, ddof=1)
    sortino = (float(np.mean(excess)) / downside_std * np.sqrt(trading_days)) if downside_std > EPSILON else 0.0
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    wins = ret[ret > 0]
    losses = ret[ret < 0]
    avg_win = float(np.mean(wins)) if wins.size else 0.0
    avg_loss = abs(float(np.mean(losses))) if losses.size else 0.0

    metrics = {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "annual_volatility": round(annual_vol * 100, 2),
        "sharpe_ratio": round(float(sharpe), 4),
        "max_drawdown": round(max_drawdown * 100, 2),
        "sortino_ratio": round(float(sortino), 4),
        "calmar_ratio": round(float(calmar), 4),
        "win_rate": round(len(wins) / n_days * 100, 2) if n_days > 0 else 0.0,
        "profit_loss_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else 0.0,
    }
    metrics.update(_calc_ab(ret, benchmark_returns, risk_free_rate, trading_days))
    return metrics


def _calc_ab(
    ret: np.ndarray,
    benchmark_returns: list[float],
    risk_free_rate: float,
    trading_days: int,
) -> dict:
    result = {"alpha": 0.0, "beta": 0.0, "information_ratio": 0.0}
    if not benchmark_returns:
        return result

    benchmark = np.asarray(benchmark_returns, dtype=float)
    benchmark = np.nan_to_num(benchmark, nan=0.0, posinf=0.0, neginf=0.0)
    min_len = min(len(ret), len(benchmark))
    if min_len < 2:
        return result

    r = ret[:min_len]
    b = benchmark[:min_len]

    if np.allclose(b, b[0]):
        beta = 0.0
    else:
        cov_matrix = np.cov(r, b)
        var_b = float(cov_matrix[1, 1]) if cov_matrix.shape == (2, 2) else 0.0
        beta = float(cov_matrix[0, 1] / var_b) if var_b > EPSILON and np.isfinite(var_b) else 0.0

    daily_rf = risk_free_rate / trading_days
    alpha = (float(np.mean(r)) - daily_rf - beta * (float(np.mean(b)) - daily_rf)) * trading_days
    tracking_diff = r - b
    tracking_error = _safe_std(tracking_diff, ddof=1)
    information_ratio = (
        float(np.mean(tracking_diff)) / tracking_error * np.sqrt(trading_days)
        if tracking_error > EPSILON else 0.0
    )

    result["alpha"] = round(float(alpha) * 100, 2)
    result["beta"] = round(float(beta), 4)
    result["information_ratio"] = round(float(information_ratio), 4)
    return result

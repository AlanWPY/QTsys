"""绩效指标计算"""
import numpy as np
from typing import Optional


def calc_metrics(
    daily_returns: list[float],
    benchmark_returns: list[float],
    initial_cash: float,
    final_value: float,
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> dict:
    """计算回测绩效指标"""
    if not daily_returns:
        return {}

    ret = np.array(daily_returns)
    n_days = len(ret)

    # 总收益率
    total_return = (final_value - initial_cash) / initial_cash

    # 年化收益率
    years = n_days / trading_days
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0.0

    # 年化波动率
    annual_vol = np.std(ret, ddof=1) * np.sqrt(trading_days) if n_days > 1 else 0.0

    # 夏普比率
    daily_rf = risk_free_rate / trading_days
    excess = ret - daily_rf
    sharpe = (np.mean(excess) / np.std(excess, ddof=1) * np.sqrt(trading_days)) if np.std(excess, ddof=1) > 0 else 0.0

    # 最大回撤
    cumulative = np.cumprod(1 + ret)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak
    max_drawdown = float(np.min(drawdown))

    # 索提诺比率
    downside = ret[ret < daily_rf] - daily_rf
    downside_std = np.std(downside, ddof=1) if len(downside) > 1 else 0.0
    sortino = (np.mean(ret - daily_rf) / downside_std * np.sqrt(trading_days)) if downside_std > 0 else 0.0

    # 卡尔玛比率
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    metrics = {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "annual_volatility": round(annual_vol * 100, 2),
        "sharpe_ratio": round(float(sharpe), 4),
        "max_drawdown": round(max_drawdown * 100, 2),
        "sortino_ratio": round(float(sortino), 4),
        "calmar_ratio": round(float(calmar), 4),
    }

    # 胜率和盈亏比
    wins = ret[ret > 0]
    losses = ret[ret < 0]
    metrics["win_rate"] = round(len(wins) / n_days * 100, 2) if n_days > 0 else 0.0
    avg_win = np.mean(wins) if len(wins) > 0 else 0.0
    avg_loss = abs(np.mean(losses)) if len(losses) > 0 else 0.0
    metrics["profit_loss_ratio"] = round(avg_win / avg_loss, 4) if avg_loss > 0 else 0.0

    # Alpha, Beta, 信息比率
    metrics.update(_calc_ab(ret, benchmark_returns, risk_free_rate, trading_days))

    return metrics


def _calc_ab(
    ret: np.ndarray,
    benchmark_returns: list[float],
    risk_free_rate: float,
    trading_days: int,
) -> dict:
    """计算Alpha, Beta, 信息比率"""
    result = {"alpha": 0.0, "beta": 0.0, "information_ratio": 0.0}

    if not benchmark_returns:
        return result

    # 对齐长度
    min_len = min(len(ret), len(benchmark_returns))
    if min_len < 2:
        return result

    r = ret[:min_len]
    b = np.array(benchmark_returns[:min_len])

    # Beta
    cov_matrix = np.cov(r, b)
    var_b = cov_matrix[1, 1]
    beta = cov_matrix[0, 1] / var_b if var_b > 0 else 0.0

    # Alpha (年化)
    daily_rf = risk_free_rate / trading_days
    alpha = (np.mean(r) - daily_rf - beta * (np.mean(b) - daily_rf)) * trading_days

    # 信息比率
    tracking_diff = r - b
    te = np.std(tracking_diff, ddof=1)
    ir = (np.mean(tracking_diff) / te * np.sqrt(trading_days)) if te > 0 else 0.0

    result["alpha"] = round(float(alpha) * 100, 2)
    result["beta"] = round(float(beta), 4)
    result["information_ratio"] = round(float(ir), 4)
    return result

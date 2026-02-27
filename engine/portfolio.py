"""策略组合分析模块 - 相关性分析、最优权重、组合回测"""
import numpy as np
from typing import Optional
from engine.metrics import calc_metrics


def analyze_correlations(results: list[dict]) -> dict:
    """计算多策略日收益率相关性矩阵和滚动相关性。

    Args:
        results: [{id, strategy_name, daily_returns, equity_curve}, ...]

    Returns:
        {names, correlation_matrix, rolling_correlations}
    """
    if len(results) < 2:
        return {"names": [], "correlation_matrix": [], "rolling_correlations": []}

    names = [r.get("strategy_name", f"策略{r['id']}") for r in results]

    # 对齐日收益率长度
    min_len = min(len(r["daily_returns"]) for r in results)
    if min_len < 2:
        return {"names": names, "correlation_matrix": [], "rolling_correlations": []}

    returns_matrix = np.array([r["daily_returns"][:min_len] for r in results])
    n = len(results)

    # Pearson 相关系数矩阵
    corr = np.corrcoef(returns_matrix)
    corr_matrix = [[round(float(corr[i][j]), 4) for j in range(n)] for i in range(n)]

    # 60日滚动相关性（仅计算相邻策略对）
    window = 60
    rolling_corrs = []
    if min_len >= window:
        for i in range(n):
            for j in range(i + 1, n):
                pair_name = f"{names[i]} vs {names[j]}"
                series = []
                for k in range(window - 1, min_len):
                    chunk_i = returns_matrix[i, k - window + 1: k + 1]
                    chunk_j = returns_matrix[j, k - window + 1: k + 1]
                    std_i = np.std(chunk_i)
                    std_j = np.std(chunk_j)
                    if std_i > 0 and std_j > 0:
                        c = float(np.corrcoef(chunk_i, chunk_j)[0, 1])
                    else:
                        c = 0.0
                    series.append({"idx": k, "value": round(c, 4)})
                rolling_corrs.append({"pair": pair_name, "series": series})

    return {
        "names": names,
        "correlation_matrix": corr_matrix,
        "rolling_correlations": rolling_corrs,
    }


def compute_optimal_weights(
    results: list[dict],
    method: str = "equal",
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> dict:
    """计算最优策略权重。

    Args:
        results: [{id, strategy_name, daily_returns}, ...]
        method: equal / inverse_variance / max_sharpe / risk_parity

    Returns:
        {method, weights: [{name, weight}]}
    """
    n = len(results)
    if n == 0:
        return {"method": method, "weights": []}

    names = [r.get("strategy_name", f"策略{r['id']}") for r in results]

    if n == 1 or method == "equal":
        w = [1.0 / n] * n
        return {"method": method, "weights": _fmt_weights(names, w)}

    min_len = min(len(r["daily_returns"]) for r in results)
    if min_len < 10:
        w = [1.0 / n] * n
        return {"method": "equal", "weights": _fmt_weights(names, w)}

    ret_matrix = np.array([r["daily_returns"][:min_len] for r in results])
    cov = np.cov(ret_matrix) * trading_days
    means = np.mean(ret_matrix, axis=1) * trading_days
    variances = np.diag(cov)

    if method == "inverse_variance":
        inv_var = 1.0 / np.maximum(variances, 1e-10)
        w = (inv_var / inv_var.sum()).tolist()

    elif method == "max_sharpe":
        w = _max_sharpe_weights(means, cov, risk_free_rate)

    elif method == "risk_parity":
        w = _risk_parity_weights(cov)

    else:
        w = [1.0 / n] * n

    return {"method": method, "weights": _fmt_weights(names, w)}


def simulate_portfolio(
    results: list[dict],
    weights: list[float],
    initial_cash: float = 1_000_000.0,
    risk_free_rate: float = 0.03,
) -> dict:
    """按权重合成组合日收益率并计算组合指标。"""
    n = len(results)
    if n == 0 or len(weights) != n:
        return {}

    min_len = min(len(r["daily_returns"]) for r in results)
    if min_len < 2:
        return {}

    w = np.array(weights)
    ret_matrix = np.array([r["daily_returns"][:min_len] for r in results])
    portfolio_returns = (w[:, None] * ret_matrix).sum(axis=0)

    cumulative = np.cumprod(1 + portfolio_returns)
    values = (cumulative * initial_cash).tolist()

    # 从第一个结果获取日期
    dates = []
    for r in results:
        ec = r.get("equity_curve", [])
        if ec and len(ec) >= min_len:
            dates = [p["date"] for p in ec[:min_len]]
            break

    equity_curve = [
        {"date": dates[i] if i < len(dates) else str(i), "value": round(values[i], 2)}
        for i in range(min_len)
    ]

    final_value = values[-1] if values else initial_cash
    metrics = calc_metrics(
        daily_returns=portfolio_returns.tolist(),
        benchmark_returns=[],
        initial_cash=initial_cash,
        final_value=final_value,
        risk_free_rate=risk_free_rate,
    )

    return {
        "equity_curve": equity_curve,
        "daily_returns": portfolio_returns.tolist(),
        "metrics": metrics,
        "final_value": round(final_value, 2),
    }


# ---- 内部辅助函数 ----

def _fmt_weights(names, weights):
    return [{"name": names[i], "weight": round(weights[i], 4)} for i in range(len(names))]


def _max_sharpe_weights(means, cov, rf):
    """最大夏普比率权重（解析解）"""
    n = len(means)
    try:
        inv_cov = np.linalg.inv(cov)
    except np.linalg.LinAlgError:
        return [1.0 / n] * n
    excess = means - rf
    w = inv_cov @ excess
    w = np.maximum(w, 0)
    total = w.sum()
    if total <= 0:
        return [1.0 / n] * n
    return (w / total).tolist()


def _risk_parity_weights(cov, max_iter=100):
    """风险平价权重（迭代求解）"""
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(max_iter):
        sigma_p = np.sqrt(w @ cov @ w)
        if sigma_p < 1e-10:
            break
        marginal_risk = cov @ w
        risk_contrib = w * marginal_risk / sigma_p
        target = sigma_p / n
        for i in range(n):
            if risk_contrib[i] > 0:
                w[i] *= target / risk_contrib[i]
        w = np.maximum(w, 1e-10)
        w /= w.sum()
    return w.tolist()

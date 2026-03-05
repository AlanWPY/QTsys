"""Alpha191 因子模板定义"""

ALPHA191_FORMULAS = {
    1: "rank(ts_argmax(signedpower(ternary(returns < 0, std(returns, 20), close), 2.0), 5)) - 0.5",
    2: "-1 * corr(rank(delta(log(volume), 2)), rank((close - open) / open), 6)",
    3: "-1 * corr(rank(open), rank(volume), 10)",
    4: "-1 * ts_rank(rank(low), 9)",
    5: "rank((open - mean(vwap, 10))) * (-1 * abs(rank((close - vwap))))",
    6: "-1 * corr(open, volume, 10)",
    7: "ternary(advm(close, 20) < volume, -1 * ts_rank(abs(delta(close, 7)), 60) * sign(delta(close, 7)), -1)",
    8: "-1 * rank(sum(open, 5) * sum(returns, 5) - delay(sum(open, 5) * sum(returns, 5), 10))",
    9: "ternary(0 < ts_min(delta(close, 1), 5), delta(close, 1), ternary(ts_max(delta(close, 1), 5) < 0, delta(close, 1), -1 * delta(close, 1)))",
    10: "rank(ternary(0 < ts_min(delta(close, 1), 4), delta(close, 1), ternary(ts_max(delta(close, 1), 4) < 0, delta(close, 1), -1 * delta(close, 1))))",
}

def get_alpha191_formula(number: int) -> str:
    """获取指定编号的Alpha191因子公式"""
    if number < 1 or number > 191:
        raise ValueError("Alpha191 编号必须在 1-191 之间")
    return ALPHA191_FORMULAS.get(number, "")

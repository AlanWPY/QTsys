"""遗传算法因子挖掘 - 基于表达式树的GP"""
import random
import numpy as np
import pandas as pd
from typing import Optional
from factor.factor_engine import FactorEngine
from logging_config import get_logger

logger = get_logger("qtsys.factor.genetic")


class GPContext:
    """GP预加载上下文 - 避免每次适应度评估重复加载数据"""

    def __init__(self, cache, universe: list[str], start_date: str, end_date: str):
        self.universe = universe
        self.start_date = start_date
        self.end_date = end_date
        # 预加载所有股票数据: {ts_code: {closes, highs, lows, volumes, opens, fwd_ret_5}}
        self.stock_data: dict[str, dict] = {}
        self.all_dates: set = set()
        self._preload(cache)

    def _preload(self, cache):
        """一次性加载所有股票的行情数据"""
        logger.info(f"GPContext: 预加载 {len(self.universe)} 只股票数据...")
        for ts_code in self.universe:
            df = cache.get_daily(ts_code, self.start_date, self.end_date)
            if df.empty or len(df) < 30:
                continue
            indexed = df.set_index("trade_date")
            closes = indexed["close"]
            highs = indexed["high"]
            lows = indexed["low"]
            volumes = indexed["vol"]
            opens = indexed["open"] if "open" in indexed.columns else closes.shift(1)
            self.stock_data[ts_code] = {
                "closes": closes, "highs": highs,
                "lows": lows, "volumes": volumes, "opens": opens,
            }
            self.all_dates.update(closes.dropna().index.tolist())
        logger.info(f"GPContext: 已加载 {len(self.stock_data)} 只有效股票")


# 表达式树节点
TERMINALS = ["close", "high", "low", "vol", "returns"]
CONSTANTS = ["5", "10", "20", "60"]
UNARY_OPS = ["rank", "abs({})", "log(abs({}) + 1)", "-({})"]
BINARY_OPS = [
    "({}) + ({})", "({}) - ({})", "({}) * ({})",
    "({}) / (({}) + 0.0001)",
]
ROLLING_OPS = [
    "mean({}, {})", "std({}, {})", "ts_max({}, {})",
    "ts_min({}, {})", "delta({}, {})", "delay({}, {})",
]


def _random_terminal():
    return random.choice(TERMINALS)


def _random_const():
    return random.choice(CONSTANTS)


def _random_expr(depth=0, max_depth=3):
    """递归生成随机因子表达式"""
    if depth >= max_depth:
        return _random_terminal()

    r = random.random()
    if r < 0.3:
        return _random_terminal()
    elif r < 0.5:
        # 一元运算
        op = random.choice(UNARY_OPS)
        child = _random_expr(depth + 1, max_depth)
        if op == "rank":
            return f"rank({child})"
        return op.format(child)
    elif r < 0.75:
        # 二元运算
        op = random.choice(BINARY_OPS)
        left = _random_expr(depth + 1, max_depth)
        right = _random_expr(depth + 1, max_depth)
        return op.format(left, right)
    else:
        # 滚动窗口运算
        op = random.choice(ROLLING_OPS)
        child = _random_expr(depth + 1, max_depth)
        window = _random_const()
        return op.format(child, window)


def _mutate(expr, max_depth=3):
    """变异: 随机替换子表达式"""
    if random.random() < 0.3:
        return _random_expr(0, max_depth)
    # 尝试替换一个终端
    for t in TERMINALS:
        if t in expr and random.random() < 0.4:
            new_t = random.choice([x for x in TERMINALS if x != t])
            return expr.replace(t, new_t, 1)
    return _random_expr(0, max_depth)


def _crossover(expr1, expr2):
    """交叉: 交换两个表达式的子树"""
    # 简单策略: 在二元运算符处切割
    for op_sym in [" + ", " - ", " * "]:
        if op_sym in expr1 and op_sym in expr2:
            parts1 = expr1.split(op_sym, 1)
            parts2 = expr2.split(op_sym, 1)
            if random.random() < 0.5:
                return parts1[0] + op_sym + parts2[1]
            else:
                return parts2[0] + op_sym + parts1[1]
    return expr1 if random.random() < 0.5 else expr2


def _fitness(expr, engine, universe, start_date, end_date, gp_ctx: Optional[GPContext] = None):
    """适应度函数: 用IC绝对值作为适应度

    当提供gp_ctx时，使用预加载数据直接计算因子值，避免重复IO。
    """
    try:
        if gp_ctx is not None:
            return _fitness_fast(expr, engine, gp_ctx)
        result = engine.evaluate(expr, universe, start_date, end_date, groups=5)
        if "error" in result:
            return -1.0
        ic_mean = abs(result["metrics"].get("ic_mean", 0))
        ic_ir = abs(result["metrics"].get("ic_ir", 0))
        return ic_mean * 0.6 + min(ic_ir, 2.0) / 2.0 * 0.4
    except Exception:
        return -1.0


def _fitness_fast(expr, engine, ctx: GPContext, forward_days: int = 5):
    """使用预加载数据快速评估因子适应度"""
    try:
        stock_factors = {}
        all_dates = set()

        for ts_code, sd in ctx.stock_data.items():
            fv = engine._eval_expression(
                expr, sd["closes"], sd["highs"], sd["lows"],
                sd["volumes"], sd["opens"],
            )
            if fv is None:
                continue
            fwd_ret = sd["closes"].pct_change(forward_days).shift(-forward_days)
            stock_factors[ts_code] = {"factor": fv, "fwd_ret": fwd_ret}
            all_dates.update(fv.dropna().index.tolist())

        if len(stock_factors) < 3:
            return -1.0

        dates = sorted(all_dates)
        ic_vals = []

        for dt in dates:
            fvals, frets = [], []
            for ts_code, sd in stock_factors.items():
                if dt in sd["factor"].index and dt in sd["fwd_ret"].index:
                    fv = sd["factor"].loc[dt]
                    fr = sd["fwd_ret"].loc[dt]
                    if pd.notna(fv) and pd.notna(fr):
                        fvals.append(fv)
                        frets.append(fr)

            if len(fvals) < 3:
                continue

            rank_f = pd.Series(fvals).rank().values
            rank_r = pd.Series(frets).rank().values
            ic = np.corrcoef(rank_f, rank_r)[0, 1]
            if not np.isnan(ic):
                ic_vals.append(ic)

        if not ic_vals:
            return -1.0

        ic_mean = abs(float(np.mean(ic_vals)))
        ic_std = float(np.std(ic_vals, ddof=1)) if len(ic_vals) > 1 else 1.0
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
        return ic_mean * 0.6 + min(ic_ir, 2.0) / 2.0 * 0.4
    except Exception:
        return -1.0


def run_gp(
    engine: FactorEngine,
    universe: list[str],
    start_date: str,
    end_date: str,
    pop_size: int = 30,
    generations: int = 10,
    top_n: int = 5,
) -> list[dict]:
    """运行遗传算法因子挖掘"""
    # 预加载数据上下文，所有适应度评估共享
    gp_ctx = GPContext(engine.cache, universe, start_date, end_date)
    if not gp_ctx.stock_data:
        logger.warning("GPContext无有效股票数据，回退到逐次加载模式")
        gp_ctx = None

    # 初始种群
    population = [_random_expr(0, 3) for _ in range(pop_size)]
    best_results = []

    for gen in range(generations):
        logger.info(f"GP第{gen+1}/{generations}代, 种群{len(population)}")
        # 评估适应度
        scored = []
        for expr in population:
            fit = _fitness(expr, engine, universe, start_date, end_date, gp_ctx)
            if fit > 0:
                scored.append((expr, fit))

        if not scored:
            population = [_random_expr(0, 3) for _ in range(pop_size)]
            continue

        scored.sort(key=lambda x: x[1], reverse=True)

        # 记录本代最优
        for expr, fit in scored[:top_n]:
            if not any(b["expression"] == expr for b in best_results):
                best_results.append({
                    "expression": expr,
                    "fitness": round(fit, 4),
                    "generation": gen + 1,
                })

        # 选择 + 交叉 + 变异
        elite = [x[0] for x in scored[:max(3, len(scored) // 3)]]
        new_pop = list(elite)

        while len(new_pop) < pop_size:
            r = random.random()
            if r < 0.3:
                new_pop.append(_random_expr(0, 3))
            elif r < 0.65:
                p1 = random.choice(elite)
                p2 = random.choice(elite)
                new_pop.append(_crossover(p1, p2))
            else:
                p = random.choice(elite)
                new_pop.append(_mutate(p))

        population = new_pop

    # 对最优因子做完整评价
    best_results.sort(key=lambda x: x["fitness"], reverse=True)
    return best_results[:top_n]

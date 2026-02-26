"""遗传算法因子挖掘 - 基于表达式树的GP"""
import random
import numpy as np
import pandas as pd
from typing import Optional
from factor.factor_engine import FactorEngine


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


def _fitness(expr, engine, universe, start_date, end_date):
    """适应度函数: 用IC绝对值作为适应度"""
    try:
        result = engine.evaluate(expr, universe, start_date, end_date, groups=5)
        if "error" in result:
            return -1.0
        ic_mean = abs(result["metrics"].get("ic_mean", 0))
        ic_ir = abs(result["metrics"].get("ic_ir", 0))
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
    # 初始种群
    population = [_random_expr(0, 3) for _ in range(pop_size)]
    best_results = []

    for gen in range(generations):
        # 评估适应度
        scored = []
        for expr in population:
            fit = _fitness(expr, engine, universe, start_date, end_date)
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

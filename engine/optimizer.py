"""策略参数优化器 - 网格搜索"""
import itertools
import re
from typing import Any
from engine.backtest_engine import BacktestEngine
from data.data_cache import DataCache
from strategy.strategy_loader import load_strategy


def grid_search(
    strategy_code: str,
    param_grid: dict[str, list[Any]],
    universe: list[str],
    start_date: str,
    end_date: str,
    cache: DataCache,
    initial_cash: float = 1_000_000.0,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage: float = 0.002,
    benchmark: str = "000300.SH",
) -> list[dict]:
    """网格搜索策略参数组合,返回按夏普比率排序的结果"""
    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))

    results = []
    for combo in combinations:
        params = dict(zip(param_names, combo))
        code = _inject_params(strategy_code, params)

        try:
            init_func, handle_func = load_strategy(code)
        except ValueError:
            continue

        engine = BacktestEngine(
            cache=cache,
            initial_cash=initial_cash,
            commission_rate=commission_rate,
            stamp_tax_rate=stamp_tax_rate,
            slippage=slippage,
        )

        result = engine.run(
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            initialize_func=init_func,
            handle_data_func=handle_func,
            benchmark=benchmark,
        )

        if "error" in result:
            continue

        metrics = result.get("metrics", {})
        results.append({
            "params": params,
            "sharpe_ratio": metrics.get("sharpe_ratio", 0.0),
            "total_return": metrics.get("total_return", 0.0),
            "annual_return": metrics.get("annual_return", 0.0),
            "max_drawdown": metrics.get("max_drawdown", 0.0),
            "final_value": result.get("final_value", 0.0),
        })

    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)
    return results


def _inject_params(code: str, params: dict) -> str:
    """将参数注入策略代码中 (替换 PARAM_xxx = ... 形式的赋值)"""
    for name, value in params.items():
        pattern = rf"({re.escape(name)}\s*=\s*)([^\n#]+)"
        replacement = rf"\g<1>{repr(value)}"
        code = re.sub(pattern, replacement, code, count=1)
    return code

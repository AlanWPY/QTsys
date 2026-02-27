"""策略参数优化器 - 网格搜索 + Walk-Forward滚动验证"""
import itertools
import re
from typing import Any
from engine.backtest_engine import BacktestEngine
from data.data_cache import DataCache
from strategy.strategy_loader import load_strategy
from logging_config import get_logger

logger = get_logger("qtsys.engine.optimizer")


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


def walk_forward(
    strategy_code: str,
    param_grid: dict[str, list[Any]],
    universe: list[str],
    full_start: str,
    full_end: str,
    cache: DataCache,
    train_days: int = 252,
    test_days: int = 63,
    initial_cash: float = 1_000_000.0,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage: float = 0.002,
    benchmark: str = "000300.SH",
) -> dict:
    """Walk-Forward滚动验证 - 训练期优化参数 → 测试期验证"""
    # 获取完整交易日历
    trade_dates = cache.get_trade_cal(full_start, full_end)
    if len(trade_dates) < train_days + test_days:
        return {"error": "交易日不足，无法进行滚动验证"}

    windows = []
    i = 0
    while i + train_days + test_days <= len(trade_dates):
        train_start = trade_dates[i]
        train_end = trade_dates[i + train_days - 1]
        test_start = trade_dates[i + train_days]
        test_end = trade_dates[min(i + train_days + test_days - 1, len(trade_dates) - 1)]
        windows.append((train_start, train_end, test_start, test_end))
        i += test_days  # 滚动步长 = 测试期长度

    if not windows:
        return {"error": "无法构建滚动窗口"}

    logger.info(f"Walk-Forward: {len(windows)}个窗口, 训练{train_days}日, 测试{test_days}日")

    oos_results = []  # out-of-sample results

    for wi, (tr_s, tr_e, te_s, te_e) in enumerate(windows):
        # 训练期: 网格搜索最优参数
        train_results = grid_search(
            strategy_code, param_grid, universe,
            tr_s, tr_e, cache,
            initial_cash, commission_rate, stamp_tax_rate, slippage, benchmark,
        )

        if not train_results:
            logger.warning(f"窗口{wi+1}: 训练期无有效结果")
            continue

        best_params = train_results[0]["params"]
        best_train_sharpe = train_results[0]["sharpe_ratio"]

        # 测试期: 用最优参数回测
        code = _inject_params(strategy_code, best_params)
        try:
            init_func, handle_func = load_strategy(code)
        except ValueError:
            continue

        engine = BacktestEngine(
            cache=cache, initial_cash=initial_cash,
            commission_rate=commission_rate,
            stamp_tax_rate=stamp_tax_rate, slippage=slippage,
        )
        test_result = engine.run(
            universe=universe, start_date=te_s, end_date=te_e,
            initialize_func=init_func, handle_data_func=handle_func,
            benchmark=benchmark,
        )

        if "error" in test_result:
            continue

        test_metrics = test_result.get("metrics", {})
        oos_results.append({
            "window": wi + 1,
            "train_period": f"{tr_s}-{tr_e}",
            "test_period": f"{te_s}-{te_e}",
            "best_params": best_params,
            "train_sharpe": best_train_sharpe,
            "test_sharpe": test_metrics.get("sharpe_ratio", 0.0),
            "test_return": test_metrics.get("total_return", 0.0),
            "test_max_drawdown": test_metrics.get("max_drawdown", 0.0),
        })

    if not oos_results:
        return {"error": "所有窗口均无有效结果"}

    # 汇总样本外表现
    avg_oos_sharpe = sum(r["test_sharpe"] for r in oos_results) / len(oos_results)
    avg_oos_return = sum(r["test_return"] for r in oos_results) / len(oos_results)
    avg_train_sharpe = sum(r["train_sharpe"] for r in oos_results) / len(oos_results)

    return {
        "windows": oos_results,
        "summary": {
            "total_windows": len(oos_results),
            "avg_train_sharpe": round(avg_train_sharpe, 4),
            "avg_oos_sharpe": round(avg_oos_sharpe, 4),
            "avg_oos_return": round(avg_oos_return, 4),
            "overfit_ratio": round(avg_oos_sharpe / avg_train_sharpe, 4) if avg_train_sharpe != 0 else 0.0,
        },
    }

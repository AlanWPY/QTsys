"""Validate canonical execution simulator invariants.

These are deterministic unit-style checks for execution rules. They do not
produce investment results and do not use simulated data as user-facing output.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.execution_simulator import CanonicalExecutionSimulator, ExecutionSettings, PanelMarketData


def _panel(*, limit_up: bool = False, volume_hands: int = 10000) -> PanelMarketData:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    codes = ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"]
    stock_data = {}
    for idx, code in enumerate(codes):
        open_values = [10.0 + idx, 10.0 + idx, 12.0 + idx, 13.0 + idx]
        if limit_up and idx == 0:
            open_values = [10.0, 11.0, 12.1, 13.2]
        close_values = [10.0 + idx, 11.0 + idx, 12.0 + idx, 13.0 + idx]
        stock_data[code] = {
            "open": pd.Series(open_values, index=dates, dtype=float),
            "high": pd.Series([11.5 + idx, 12.5 + idx, 13.5 + idx, 14.5 + idx], index=dates, dtype=float),
            "low": pd.Series([9.5 + idx, 9.5 + idx, 11.5 + idx, 12.5 + idx], index=dates, dtype=float),
            "close": pd.Series(close_values, index=dates, dtype=float),
            "vol": pd.Series([volume_hands, volume_hands, volume_hands, volume_hands], index=dates, dtype=float),
            "prev_close": pd.Series([9.0 + idx, 10.0 + idx, 11.0 + idx, 12.0 + idx], index=dates, dtype=float),
        }
    benchmark = pd.Series([1000, 1010, 1020, 1030], index=dates, dtype=float)
    return PanelMarketData(stock_data, dates, benchmark_close=benchmark, benchmark_code="000016.SH")


def _warmup_panel() -> PanelMarketData:
    dates = pd.to_datetime([
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
    ])
    codes = ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"]
    stock_data = {}
    for idx, code in enumerate(codes):
        stock_data[code] = {
            "open": pd.Series([10 + idx] * len(dates), index=dates, dtype=float),
            "high": pd.Series([11 + idx] * len(dates), index=dates, dtype=float),
            "low": pd.Series([9 + idx] * len(dates), index=dates, dtype=float),
            "close": pd.Series([10 + idx] * len(dates), index=dates, dtype=float),
            "vol": pd.Series([10000] * len(dates), index=dates, dtype=float),
            "prev_close": pd.Series([10 + idx] * len(dates), index=dates, dtype=float),
        }
    return PanelMarketData(stock_data, dates, benchmark_close=pd.Series([1000] * len(dates), index=dates, dtype=float))


def _factors() -> dict[str, pd.Series]:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    return {
        code: pd.Series([10 - idx, 10 - idx, 10 - idx, 10 - idx], index=dates, dtype=float)
        for idx, code in enumerate(["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"])
    }


def _warmup_factors() -> dict[str, pd.Series]:
    dates = pd.to_datetime([
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
    ])
    values = {
        "600000.SH": [1, 1, 1, 9, 9, 9],
        "600001.SH": [2, 2, 2, 8, 8, 8],
        "600002.SH": [3, 3, 3, 7, 7, 7],
        "600003.SH": [4, 4, 4, 6, 6, 6],
        "600004.SH": [9, 9, 9, 5, 5, 5],
    }
    return {code: pd.Series(vals, index=dates, dtype=float) for code, vals in values.items()}


def _sim(volume_limit_pct: float = 1.0) -> CanonicalExecutionSimulator:
    return CanonicalExecutionSimulator(
        ExecutionSettings(
            initial_cash=100_000,
            commission_rate=0,
            stamp_tax_rate=0,
            slippage=0,
            min_commission=0,
            transfer_fee_rate=0,
            volume_limit_pct=volume_limit_pct,
            max_position_pct=1.0,
            target_exposure=0.95,
            protocol_version="test",
        )
    )


def validate_next_open_and_lot_rounding():
    result = _sim().run_factor_selection(
        factors=_factors(),
        market=_panel(),
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    first_buy = next(trade for trade in result["trades"] if trade["action"] == "buy")
    if first_buy["date"] != "2024-01-03":
        raise AssertionError(f"expected next-open execution on 2024-01-03, got {first_buy}")
    if first_buy["shares"] % 100 != 0 or first_buy["shares"] != 9500:
        raise AssertionError(f"expected 9500 shares rounded to board lot, got {first_buy}")
    if len(result.get("daily_returns", [])) != len(result.get("equity_curve", [])):
        raise AssertionError("canonical simulator must return full daily_returns aligned with equity curve")


def validate_first_segment_day_rebalances_with_warmup_signal():
    segment_dates = set(pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]))
    result = _sim().run_factor_selection(
        factors=_warmup_factors(),
        market=_warmup_panel(),
        select_pct=0.2,
        rebalance_days=2,
        direction="top",
        segment_dates=segment_dates,
    )
    buys = [trade for trade in result["trades"] if trade["action"] == "buy"]
    if not buys or buys[0]["date"] != "2024-01-04":
        raise AssertionError(f"first formal segment day must rebalance using previous warmup signal, got {buys[:3]}")
    trade_dates = sorted({trade["date"] for trade in result["trades"]})
    if "2024-01-04" not in trade_dates:
        raise AssertionError(f"rebalance schedule must include first formal trading day, got {trade_dates}")
    if "2024-01-08" not in trade_dates:
        raise AssertionError(f"rebalance schedule must include second formal trading day after rebalance step, got {trade_dates}")


def validate_limit_up_blocks_buy():
    result = _sim().run_factor_selection(
        factors=_factors(),
        market=_panel(limit_up=True),
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    if not result["trades"]:
        raise AssertionError("limit-up top candidate should be skipped and next tradable candidate should be bought")
    if result["trades"][0]["code"] == "600000.SH":
        raise AssertionError(f"limit-up top candidate must not be bought, got trades {result['trades']}")
    if result["rejection_reasons"].get("limit_up_buy_blocked", 0) <= 0:
        raise AssertionError("expected limit_up_buy_blocked rejection")


def validate_volume_capacity_cap():
    result = _sim(volume_limit_pct=0.1).run_factor_selection(
        factors=_factors(),
        market=_panel(volume_hands=10),
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    first_buy = next(trade for trade in result["trades"] if trade["action"] == "buy")
    if first_buy["shares"] != 100:
        raise AssertionError(f"expected volume capacity to cap buy at 100 shares, got {first_buy}")


def validate_excess_curve_is_percentage():
    result = _sim().run_factor_selection(
        factors=_factors(),
        market=_panel(),
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    curve = result.get("excess_curve") or []
    if not curve:
        raise AssertionError("canonical simulator must expose excess_curve")
    values = [float(point["value"]) for point in curve if point.get("value") is not None]
    if not values:
        raise AssertionError("excess_curve has no numeric values")
    if all(0.5 <= value <= 1.5 for value in values):
        raise AssertionError(f"excess_curve must be percentage returns, not normalized NAV values: {values[:5]}")
    final_excess = float(result["metrics"]["excess_return"])
    if abs(values[-1] - final_excess) > 0.05:
        raise AssertionError(f"final excess curve value {values[-1]} must match metrics.excess_return {final_excess}")


def validate_missing_price_uses_last_valuation():
    market = _panel()
    missing_dt = pd.to_datetime("2024-01-04")
    market.stock_data["600000.SH"]["open"].loc[missing_dt] = float("nan")
    market.stock_data["600000.SH"]["close"].loc[missing_dt] = float("nan")
    market.stock_data["600000.SH"]["vol"].loc[missing_dt] = 0.0
    result = _sim().run_factor_selection(
        factors=_factors(),
        market=market,
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    curve = result.get("equity_curve") or []
    by_date = {point["date"]: float(point["value"]) for point in curve}
    value = by_date.get("2024-01-04", 0.0)
    if value < 90_000:
        raise AssertionError(f"missing quote day must carry forward last valuation, got equity {value}")
    returns = result.get("daily_returns") or []
    if len(returns) >= 3 and min(returns) < -0.20:
        raise AssertionError(f"missing quote day created artificial drawdown: {returns}")


def validate_backtest_trace_interfaces():
    from engine.backtest_engine import BacktestEngine
    import api.routes_backtest as routes_backtest
    import api.routes_factor_mining as routes_factor_mining
    import factor.factor_backtest as factor_backtest
    from services import factor_mining_service
    import factor.factor_backtest as factor_backtest_module
    import inspect

    engine_source = inspect.getsource(BacktestEngine)
    for token in ["cancelled_orders", "_format_rejections", "_format_order_trace", "order_rejections"]:
        if token not in engine_source:
            raise AssertionError(f"BacktestEngine must expose execution trace token: {token}")
    route_source = inspect.getsource(routes_backtest)
    for token in ["parity_package", "parity_export", "trades_by_date", "joinquant_alignment", "order_trace"]:
        if token not in route_source:
            raise AssertionError(f"Backtest parity route is incomplete; missing {token}")
    factor_backtest_source = inspect.getsource(factor_backtest.run_selection_backtest)
    for token in ["CanonicalExecutionSimulator", "PanelMarketData", "factor_selection_canonical_v1"]:
        if token not in factor_backtest_source:
            raise AssertionError(f"Factor selection backtest must use canonical simulator; missing {token}")
    mining_source = inspect.getsource(factor_mining_service._backtest_from_factors)
    for token in ["CanonicalExecutionSimulator", "PanelMarketData", "STRICT_MINING_PROTOCOL_VERSION"]:
        if token not in mining_source:
            raise AssertionError(f"Factor mining backtest must use canonical simulator; missing {token}")
    for token in ["0.0003", "0.001", "0.002"]:
        if token not in mining_source:
            raise AssertionError(f"Factor mining backtest must share system default cost/slippage fallback; missing {token}")
    if "1.0 + strategy[date] - benchmark[date]" in inspect.getsource(factor_mining_service._build_excess_curve):
        raise AssertionError("Factor mining legacy excess curve helper must return percentage excess, not normalized NAV")
    route_source = inspect.getsource(routes_factor_mining.run_mining)
    for token in ["start_streaming_mining_session", "streaming", "sessions/status/results"]:
        if token not in route_source:
            raise AssertionError(f"Legacy /api/factor_mining/run must route to streaming canonical mining; missing {token}")
    backtest_engine_source = inspect.getsource(BacktestEngine.run)
    for token in ["warmup_start", "history_dates", "_load_symbol_data(ts_code, warmup_start"]:
        if token not in backtest_engine_source:
            raise AssertionError(f"BacktestEngine must load warmup history for JoinQuant parity; missing {token}")
    full_backtest_source = inspect.getsource(BacktestEngine)
    for token in [
        "_max_affordable_lot",
        "actual_amount = (int(actual_amount) // 100) * 100",
        "stock_names",
        "exclude_st",
        "name=stock_name",
        "date_source = self.history_dates or self.trade_dates",
    ]:
        if token not in full_backtest_source:
            raise AssertionError(f"BacktestEngine realistic A-share constraint missing: {token}")
    factor_selection_source = inspect.getsource(factor_backtest_module.run_selection_backtest)
    for token in ["warmup_start", "compute_factor_values(expression, ts_code, warmup_start", "stock_names=stock_names"]:
        if token not in factor_selection_source:
            raise AssertionError(f"Factor selection backtest must use warmup and stock names for parity; missing {token}")
    mining_preload_source = inspect.getsource(factor_mining_service.MiningDataContext.preload)
    for token in ["warmup_start", "worker_cache.get_daily(code, warmup_start", "stock_names"]:
        if token not in mining_preload_source:
            raise AssertionError(f"Factor mining preload must use warmup and stock names for parity; missing {token}")


def validate_joinquant_cost_parameters():
    from services.factor_service import build_joinquant_backtest_code
    import inspect

    source = inspect.getsource(build_joinquant_backtest_code)
    for token in [
        "commission_rate",
        "stamp_tax_rate",
        "min_commission",
        "__COMMISSION_RATE__",
        "__STAMP_TAX_RATE__",
        "fixed_universe",
        "universe_as_of_date",
        "get_strategy_universe",
        "universe_codes",
    ]:
        if token not in source:
            raise AssertionError(f"JoinQuant template must expose execution cost parameter: {token}")
    if "open_commission=0.0003" in source or "close_tax=0.001" in source:
        raise AssertionError("JoinQuant template must not hard-code commission or stamp tax")


def validate_mined_factor_strategy_context():
    import inspect
    from services import backtest_service
    from services.factor_service import create_strategy_from_factor_workflow

    source = inspect.getsource(create_strategy_from_factor_workflow)
    for token in [
        "mined_direction",
        "effective_direction = mined_direction",
        "backtest_context",
        "session_start_date",
        "session_end_date",
        "universe_type",
        "universe_code",
        "custom_pool_id",
        "benchmark",
    ]:
        if token not in source:
            raise AssertionError(f"Mined factor strategy creation must preserve mining metadata: {token}")

    frontend = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    if "create_strategy`, { direction: 'top' }" in frontend:
        raise AssertionError("Factor page must not force mined factor strategy direction to top")
    for token in ["res.backtest_context", "mining.direction", "mining.session_start_date", "mining.universe_type"]:
        if token not in frontend:
            raise AssertionError(f"Factor page must pass mined factor backtest context to backtest page: {token}")
    for token in ["displayBacktestMetrics", "展示超额", "展示最大回撤"]:
        if token not in frontend:
            raise AssertionError(f"Factor mining UI must distinguish displayed full-period metrics from test metrics: {token}")
    workflow_source = inspect.getsource(backtest_service.run_backtest_workflow)
    for token in [
        "_extract_generated_factor_strategy_config",
        "run_selection_backtest",
        "canonical_factor_selection_from_generated_strategy",
        "factor_strategy_config",
    ]:
        if token not in workflow_source:
            raise AssertionError(f"Backtest workflow must route generated factor strategies to canonical factor backtest: {token}")
    factor_backtest_source = inspect.getsource(__import__("factor.factor_backtest", fromlist=["run_selection_backtest"]).run_selection_backtest)
    for token in ["max_position_pct", "target_exposure"]:
        if token not in factor_backtest_source:
            raise AssertionError(f"Canonical factor backtest must accept generated strategy portfolio constraint: {token}")


def validate_factor_mining_wrapper_matches_canonical():
    import types
    from services import factor_mining_service

    market = _panel()
    ctx = types.SimpleNamespace(
        stock_data=market.stock_data,
        all_dates=market.trade_dates,
        benchmark_close=market.benchmark_close,
        benchmark_code=market.benchmark_code,
    )
    settings = types.SimpleNamespace(
        default_cash=100_000,
        commission_rate=0,
        stamp_tax_rate=0,
        slippage=0,
        capacity_limit_pct=1.0,
    )
    wrapped = factor_mining_service._backtest_from_factors(
        _factors(),
        ctx,
        settings,
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
        dates=None,
    )
    canonical = CanonicalExecutionSimulator(
        ExecutionSettings(
            initial_cash=100_000,
            commission_rate=0,
            stamp_tax_rate=0,
            slippage=0,
            min_commission=5.0,
            transfer_fee_rate=0.0,
            volume_limit_pct=1.0,
            max_position_pct=0.12,
            target_exposure=0.95,
            protocol_version="test",
        )
    ).run_factor_selection(
        factors=_factors(),
        market=market,
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    if wrapped.get("normalized_curve") != canonical.get("normalized_curve"):
        raise AssertionError("Factor mining wrapper normalized curve must match canonical simulator")
    if wrapped.get("excess_curve") != canonical.get("excess_curve"):
        raise AssertionError("Factor mining wrapper excess curve must match canonical simulator")


def validate_factor_mining_direction_equivalence_key():
    from services.factor_mining_service import _trading_equivalence_key

    momentum_bottom = _trading_equivalence_key("pctchange(close, 5)", "bottom")
    reversal_top = _trading_equivalence_key("-pctchange(close, 5)", "top")
    momentum_top = _trading_equivalence_key("pctchange(close, 5)", "top")
    reversal_bottom = _trading_equivalence_key("-(pctchange(close, 5))", "bottom")
    if momentum_bottom != reversal_top:
        raise AssertionError("Momentum bottom and reversal top must be recognized as the same trading exposure")
    if momentum_top != reversal_bottom:
        raise AssertionError("Momentum top and reversal bottom must be recognized as the same trading exposure")
    if momentum_top == reversal_top:
        raise AssertionError("Opposite trading exposures must not share the same equivalence key")


def validate_strategy_engine_order_realism_helpers():
    from engine.backtest_engine import BacktestEngine
    from engine.context import OrderSide

    engine = BacktestEngine(cache=object(), initial_cash=1000, commission_rate=0.0003, stamp_tax_rate=0.001, slippage=0)
    shares = engine._max_affordable_lot(9.99, "600000.SH", max_shares=1000)
    commission = engine.broker.calc_commission(shares, 9.99)
    tax = engine.broker.calc_tax(shares, 9.99, OrderSide.BUY, ts_code="600000.SH")
    if shares % 100 != 0:
        raise AssertionError(f"strategy engine buy sizing must round to board lots, got {shares}")
    if shares * 9.99 + commission + tax > engine.cash + 1e-9:
        raise AssertionError("strategy engine affordable sizing must include minimum commission and transfer fee")


def main():
    validate_next_open_and_lot_rounding()
    validate_first_segment_day_rebalances_with_warmup_signal()
    validate_limit_up_blocks_buy()
    validate_volume_capacity_cap()
    validate_excess_curve_is_percentage()
    validate_missing_price_uses_last_valuation()
    validate_backtest_trace_interfaces()
    validate_joinquant_cost_parameters()
    validate_mined_factor_strategy_context()
    validate_factor_mining_wrapper_matches_canonical()
    validate_factor_mining_direction_equivalence_key()
    validate_strategy_engine_order_realism_helpers()
    print("canonical execution simulator validation passed")


if __name__ == "__main__":
    main()

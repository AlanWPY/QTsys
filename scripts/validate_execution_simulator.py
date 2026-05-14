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


def _factors() -> dict[str, pd.Series]:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    return {
        code: pd.Series([10 - idx, 10 - idx, 10 - idx, 10 - idx], index=dates, dtype=float)
        for idx, code in enumerate(["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"])
    }


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


def validate_limit_up_blocks_buy():
    result = _sim().run_factor_selection(
        factors=_factors(),
        market=_panel(limit_up=True),
        select_pct=0.2,
        rebalance_days=1,
        direction="top",
    )
    if result["trades"]:
        raise AssertionError(f"limit-up buy should be blocked, got trades {result['trades']}")
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


def validate_backtest_trace_interfaces():
    from engine.backtest_engine import BacktestEngine
    import api.routes_backtest as routes_backtest
    import factor.factor_backtest as factor_backtest
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


def validate_joinquant_cost_parameters():
    from services.factor_service import build_joinquant_backtest_code
    import inspect

    source = inspect.getsource(build_joinquant_backtest_code)
    for token in ["commission_rate", "stamp_tax_rate", "min_commission", "__COMMISSION_RATE__", "__STAMP_TAX_RATE__"]:
        if token not in source:
            raise AssertionError(f"JoinQuant template must expose execution cost parameter: {token}")
    if "open_commission=0.0003" in source or "close_tax=0.001" in source:
        raise AssertionError("JoinQuant template must not hard-code commission or stamp tax")


def main():
    validate_next_open_and_lot_rounding()
    validate_limit_up_blocks_buy()
    validate_volume_capacity_cap()
    validate_backtest_trace_interfaces()
    validate_joinquant_cost_parameters()
    print("canonical execution simulator validation passed")


if __name__ == "__main__":
    main()

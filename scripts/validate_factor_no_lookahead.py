"""Validate factor/backtest anti-lookahead invariants."""
from __future__ import annotations

import numpy as np
import pandas as pd
import sys
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor.factor_engine import FactorEngine


def validate_rank_no_future():
    engine = FactorEngine(cache=None)
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 0.5], index=dates)
    high = close + 0.1
    low = close - 0.1
    vol = pd.Series([100, 100, 100, 100, 100], index=dates, dtype=float)
    result = engine._eval_expression("rank(close)", close, high, low, vol, close, {}, vol * close)
    expected = pd.Series([1.0, 1.0, 1.0, 1.0, 0.2], index=dates)
    if not np.allclose(result.values, expected.values, equal_nan=True):
        raise AssertionError(f"rank(close) uses future data: got {result.tolist()}, expected {expected.tolist()}")


def validate_joinquant_template_rank_no_future():
    from types import SimpleNamespace

    from services.factor_service import build_joinquant_backtest_code

    factor = SimpleNamespace(id=1, name="rank_test", expression="rank(close)")
    code = build_joinquant_backtest_code(factor)
    if "def _expanding_rank_pct" not in code or "'rank': _expanding_rank_pct" not in code:
        raise AssertionError("JoinQuant template must use expanding rank, not full-sample rank")
    if ".positions.get(" in code:
        raise AssertionError("JoinQuant template must not call positions.get(), which triggers JoinQuant warnings")


def validate_system_universe_as_of_date():
    import services.backtest_service as backtest_service

    source = inspect.getsource(backtest_service.resolve_backtest_universe)
    if "as_of_date" not in source or "frame[\"trade_date\"] <= resolved_as_of" not in source:
        raise AssertionError("System universe resolution must use an as-of date to avoid survivorship bias")


def validate_factor_board_next_open_execution():
    from factor.factor_board_analyzer import FactorBoardAnalyzer

    source = inspect.getsource(FactorBoardAnalyzer._calculate_quantile_returns)
    required_tokens = ["signal_date", "entry_date", "exit_date", "entry_prices", "exit_prices"]
    missing = [token for token in required_tokens if token not in source]
    if missing:
        raise AssertionError(f"Factor board quantile backtest must execute after signal date; missing {missing}")


def validate_safe_ic_corr():
    source = inspect.getsource(FactorEngine._calc_metrics)
    if "np.corrcoef(rank_f, rank_r)" in source:
        raise AssertionError("Factor IC calculation must guard zero-variance ranks before np.corrcoef")


def validate_factor_mining_strict_protocol():
    import services.factor_mining_service as mining_service

    score_source = inspect.getsource(mining_service._score_candidate)
    if " test_metrics" in score_source or "(test_metrics" in score_source or "test_ic" in score_source:
        raise AssertionError("Factor mining discovery score must not use test metrics")
    if "multiple_test_penalty" not in score_source:
        raise AssertionError("Factor mining discovery score must include a multiple-testing penalty")

    evaluate_source = inspect.getsource(mining_service._evaluate_candidate)
    required_tokens = ["valid_backtest", "test_backtest", "_strict_acceptance_reasons", "STRICT_MINING_PROTOCOL_VERSION"]
    missing = [token for token in required_tokens if token not in evaluate_source]
    if missing:
        raise AssertionError(f"Factor mining must use strict validation/test gates; missing {missing}")
    institutional_tokens = ["_walk_forward_splits", "_preprocess_factor_panel", "_candidate_significance", "_pbo_risk", "_institutional_final_score", "_robustness_summary", "_factor_fingerprint", "protocol_version"]
    missing = [token for token in institutional_tokens if token not in evaluate_source]
    if missing:
        raise AssertionError(f"Factor mining institutional protocol checks are incomplete; missing {missing}")
    display_tokens = ["display_backtest", "analysis_dates", "display_normalized_curve", "test_normalized_curve"]
    missing = [token for token in display_tokens if token not in evaluate_source]
    if missing:
        raise AssertionError(f"Factor mining must store full-period display curves separately from strict test curves; missing {missing}")

    backtest_source = inspect.getsource(mining_service._backtest_from_factors)
    if "usable_dates" in backtest_source:
        raise AssertionError("Factor mining backtest must not slice the calendar before applying rebalance logic")
    required_backtest_tokens = ["all_dates", "segment_dates", "benchmark_curve", "excess_curve", "rejection_reasons"]
    missing = [token for token in required_backtest_tokens if token not in backtest_source]
    if missing:
        raise AssertionError(f"Factor mining backtest must expose realistic OOS diagnostics; missing {missing}")
    route_source = inspect.getsource(mining_service.start_streaming_mining_session)
    for token in ["walk_forward_windows", "neutralize", "max_trials", "capacity_limit_pct", "embargo_days", "min_dsr"]:
        if token not in route_source:
            raise AssertionError(f"Factor mining session must persist institutional parameter: {token}")
    split_source = inspect.getsource(mining_service._walk_forward_splits)
    if "embargo_days" not in split_source or "valid_start" not in split_source or "test_start" not in split_source:
        raise AssertionError("Factor mining walk-forward splits must include embargo gaps")


def main():
    validate_rank_no_future()
    validate_joinquant_template_rank_no_future()
    validate_system_universe_as_of_date()
    validate_factor_board_next_open_execution()
    validate_safe_ic_corr()
    validate_factor_mining_strict_protocol()
    print("factor no-lookahead validation passed")


if __name__ == "__main__":
    main()

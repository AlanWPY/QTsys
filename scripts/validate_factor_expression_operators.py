"""Validate factor expression operators used by factor mining.

This is a deterministic syntax/operator check. It does not produce user-facing
investment results and does not claim factor efficacy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor.factor_engine import FactorEngine
from services.factor_mining_service import (
    FIB_WINDOW_SPACE,
    _adaptive_stochastic_candidates,
    _candidate_stream_v2,
    _creative_random_candidate_bank,
    _factor_fingerprint,
    _novel_motif_candidate_bank,
    _stochastic_grammar_candidate_bank,
    _technical_event_candidate_bank,
    StreamingMiningConfig,
)
from types import SimpleNamespace


def _series_panel():
    idx = pd.date_range("2024-01-01", periods=260, freq="B")
    close = pd.Series(np.linspace(10, 15, len(idx)) + np.sin(np.arange(len(idx)) / 3), index=idx)
    high = close * 1.02
    low = close * 0.98
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(10000 + np.sin(np.arange(len(idx)) / 4) * 2000 + np.arange(len(idx)) * 20, index=idx)
    amount = volume * close
    return close, high, low, volume, open_, amount


def _engine() -> FactorEngine:
    return FactorEngine(cache=None)


def _eval(expr: str) -> pd.Series:
    close, high, low, volume, open_, amount = _series_panel()
    basic_data = {
        "pe": pd.Series(12 + np.sin(np.arange(len(close)) / 9), index=close.index),
        "pb": pd.Series(1.5 + np.cos(np.arange(len(close)) / 11) * 0.2, index=close.index),
        "ps": pd.Series(2.5 + np.sin(np.arange(len(close)) / 7) * 0.3, index=close.index),
        "turnover_rate": pd.Series(1.0 + np.sin(np.arange(len(close)) / 5) * 0.2, index=close.index),
        "circ_mv": pd.Series(100 + np.arange(len(close)) * 0.1, index=close.index),
        "total_mv": pd.Series(120 + np.arange(len(close)) * 0.1, index=close.index),
    }
    result = _engine()._eval_expression(expr, close, high, low, volume, open_, basic_data, amount)
    if result is None or not isinstance(result, pd.Series) or result.dropna().empty:
        raise AssertionError(f"factor expression produced no valid series: {expr}")
    values = pd.to_numeric(result, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        raise AssertionError(f"factor expression produced only non-finite values: {expr}")
    return result


def validate_core_operators():
    expressions = [
        "macd_hist(close, 12, 26, 9)",
        "rsi(close, 14)",
        "cross_up(ema(close, 5), ema(close, 20))",
        "count_true(cross_up(macd_dif(close, 12, 26), macd_dea(close, 12, 26, 9)), 40)",
        "vol_zscore(20)",
        "gap_pct()",
        "breakout(close, 40)",
        "drawdown_from_high(close, 40)",
        "skew(returns, 20)",
        "kurt(returns, 20)",
        "downside_std(returns, 20)",
        "upside_std(returns, 20)",
        "efficiency_ratio(close, 20)",
        "where((count_true(cross_up(ema(close, 5), ema(close, 20)), 40) >= 1) & (vol_zscore(20) > -5), pctchange(close, 5), -std(returns, 20))",
    ]
    for expr in expressions:
        _eval(expr)


def validate_technical_event_bank():
    config = StreamingMiningConfig(
        session_id="test",
        settings=SimpleNamespace(),
        resolved={"codes": ["600000.SH"] * 5},
        universe_type="system",
        universe_code="000016.SH",
        custom_pool_id=None,
        start_date="20240101",
        end_date="20240630",
        max_stocks=50,
        candidate_count=12,
        gp_generations=0,
        gp_population=0,
        select_pct=0.1,
        rebalance_days=5,
        max_depth=4,
        max_expression_length=1200,
        auto_stop_candidates=0,
        factor_themes=[],
        research_mode="professional",
        neutralize="rank_zscore",
        walk_forward_windows=3,
        embargo_days=5,
        max_trials=0,
        capacity_limit_pct=0.1,
        min_dsr=-0.25,
    )
    rows = _technical_event_candidate_bank(config)
    if len(rows) < 20:
        raise AssertionError(f"technical event bank too small: {len(rows)}")
    for item in rows:
        _eval(item.expression)


def validate_creative_random_bank():
    config = _test_config()
    rows = _creative_random_candidate_bank(config, 36)
    if len(rows) < 30:
        raise AssertionError(f"creative random bank too small: {len(rows)}")
    themes = {item.theme for item in rows}
    if len(themes) < 8:
        raise AssertionError(f"creative random bank lacks theme diversity: {sorted(themes)}")
    valid_count = 0
    invalid_examples = []
    for item in rows[:36]:
        try:
            _eval(item.expression)
            valid_count += 1
        except AssertionError as exc:
            invalid_examples.append(str(exc))
    if valid_count < 18:
        sample = invalid_examples[:3]
        raise AssertionError(f"stochastic grammar valid expression ratio too low: {valid_count}/36; examples={sample}")


def validate_novel_motif_bank():
    config = _test_config()
    rows = _novel_motif_candidate_bank(config, 48)
    if len(rows) < 40:
        raise AssertionError(f"novel motif bank too small: {len(rows)}")
    themes = {item.theme for item in rows}
    if len(themes) < 6:
        raise AssertionError(f"novel motif bank lacks theme diversity: {sorted(themes)}")
    shape_ops = {"skew", "kurt", "downside_std", "upside_std", "efficiency_ratio"}
    if not any(shape_ops & set(_factor_fingerprint(item.expression).get("operators") or []) for item in rows):
        raise AssertionError("novel motif bank must use distribution-shape operators")
    for item in rows[:28]:
        _eval(item.expression)


def validate_stochastic_grammar_bank():
    config = _test_config()
    rows = _stochastic_grammar_candidate_bank(config, 48)
    if len(rows) < 30:
        raise AssertionError(f"stochastic grammar bank too small: {len(rows)}")
    structures = {item.expression for item in rows}
    if len(structures) < int(len(rows) * 0.9):
        raise AssertionError("stochastic grammar bank generated too many duplicate expressions")
    windows = []
    complex_count = 0
    for item in rows:
        fingerprint = _factor_fingerprint(item.expression)
        windows.extend(int(w) for w in (fingerprint.get("windows") or []))
        if fingerprint.get("operator_count", 0) >= 6:
            complex_count += 1
    if complex_count < len(rows) * 0.5:
        raise AssertionError("stochastic grammar bank should produce non-trivial operator trees")
    if windows:
        fib_ratio = sum(1 for window in windows if window in FIB_WINDOW_SPACE) / len(windows)
        if fib_ratio < 0.45:
            raise AssertionError(f"Fibonacci window usage too low: {fib_ratio:.2%}")
        if fib_ratio >= 0.98:
            raise AssertionError("window sampling is too deterministic; non-Fibonacci exploration disappeared")
    for item in rows[:24]:
        _eval(item.expression)


def validate_priority_stream_is_diverse():
    config = _test_config()
    stream = _candidate_stream_v2(config, lambda: [])
    first_items = [next(stream) for _ in range(24)]
    sources = {item.source for item in first_items}
    required = {"stochastic_grammar", "novel_motif", "creative_random", "institutional_seed", "param_scan"}
    missing = required - sources
    if missing:
        raise AssertionError(f"priority stream must start with diverse sources, missing {sorted(missing)}")
    macd_like = sum(1 for item in first_items if "macd" in item.expression.lower() or "cross_up" in item.expression.lower())
    if macd_like > 10:
        raise AssertionError(f"priority stream overweights MACD/cross candidates: {macd_like}/24")
    themes = {item.theme for item in first_items}
    if len(themes) < 8:
        raise AssertionError(f"priority stream lacks early theme diversity: {sorted(themes)}")


def validate_priority_stream_source_coverage():
    config = _test_config()
    stream = _candidate_stream_v2(config, lambda: [])
    sources = {next(stream).source for _ in range(320)}
    required = {"stochastic_grammar", "novel_motif", "creative_random", "technical_event", "technical_event_param_scan", "param_scan", "institutional_seed"}
    missing = required - sources
    if missing:
        raise AssertionError(f"priority stream missing expected candidate sources: {sorted(missing)}")


def validate_adaptive_stochastic_candidates():
    config = _test_config()
    elites = [
        {"name": "elite_a", "expression": "rank(pctchange(close, 13))", "score": 0.3, "theme": "momentum"},
        {"name": "elite_b", "expression": "-rank(std(returns, 21))", "score": 0.2, "theme": "low_volatility"},
    ]
    rows = _adaptive_stochastic_candidates(config, elites, 12)
    if len(rows) < 6:
        raise AssertionError(f"adaptive stochastic generator returned too few candidates: {len(rows)}")
    if not all(item.source == "adaptive_stochastic" for item in rows):
        raise AssertionError("adaptive stochastic candidates must be marked with source=adaptive_stochastic")
    for item in rows[:8]:
        _eval(item.expression)


def validate_fingerprint_diagnostics():
    macd_expr = (
        "where((count_true(cross_up(macd_dif(close, 12, 26), "
        "macd_dea(close, 12, 26, 9)), 40) >= 2) & (vol_zscore(20) > 1.0), "
        "macd_hist(close, 12, 26, 9), -std(returns, 20))"
    )
    rsi_expr = "where((rsi(close, 14) > 35) & (vol_zscore(20) > 0.5), pctchange(close, 5), -std(returns, 20))"
    for expr, expected_ops in {
        macd_expr: {"macd_dif", "macd_dea", "macd_hist", "cross_up", "count_true", "vol_zscore"},
        rsi_expr: {"rsi", "vol_zscore"},
    }.items():
        fingerprint = _factor_fingerprint(expr)
        ops = set(fingerprint.get("technical_operators") or [])
        if not fingerprint.get("event_driven"):
            raise AssertionError(f"technical expression was not marked event-driven: {expr}")
        missing = expected_ops - ops
        if missing:
            raise AssertionError(f"fingerprint missing technical operators {sorted(missing)} for {expr}")


def _test_config() -> StreamingMiningConfig:
    return StreamingMiningConfig(
        session_id="test",
        settings=SimpleNamespace(),
        resolved={"codes": ["600000.SH"] * 5},
        universe_type="system",
        universe_code="000016.SH",
        custom_pool_id=None,
        start_date="20240101",
        end_date="20240630",
        max_stocks=50,
        candidate_count=12,
        gp_generations=0,
        gp_population=0,
        select_pct=0.1,
        rebalance_days=5,
        max_depth=4,
        max_expression_length=1200,
        auto_stop_candidates=0,
        factor_themes=[],
        research_mode="professional",
        neutralize="rank_zscore",
        walk_forward_windows=3,
        embargo_days=5,
        max_trials=0,
        capacity_limit_pct=0.1,
        min_dsr=-0.25,
    )


def main():
    validate_core_operators()
    validate_technical_event_bank()
    validate_creative_random_bank()
    validate_novel_motif_bank()
    validate_stochastic_grammar_bank()
    validate_priority_stream_is_diverse()
    validate_priority_stream_source_coverage()
    validate_adaptive_stochastic_candidates()
    validate_fingerprint_diagnostics()
    print("factor expression operator validation passed")


if __name__ == "__main__":
    main()

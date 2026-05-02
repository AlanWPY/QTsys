"""因子看板分析引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from factor.alpha191_templates import ALPHA191_FORMULAS
from factor.real_alpha191_engine import RealAlpha191Engine


@dataclass
class AnalysisQuality:
    coverage_ratio: float = 0.0
    universe_size: int = 0
    rebalance_count: int = 0
    latest_trade_date: str = ""


class FactorBoardAnalyzer:
    def __init__(
        self,
        db_manager,
        commission: float = 0.0003,
        stamp_tax: float = 0.001,
        slippage: float = 0.001,
        rebalance_days: int = 5,
        quantiles: int = 5,
    ):
        self.db = db_manager
        self.engine = RealAlpha191Engine()
        self.commission = commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        self.rebalance_days = rebalance_days
        self.quantiles = quantiles

    def analyze_single_factor(
        self,
        factor_num: int,
        market_data: pd.DataFrame,
        start_date: str,
        end_date: str,
        backtest_days: int = 730,
        membership_by_date: Optional[dict[str, set[str]]] = None,
        benchmark_data: Optional[pd.DataFrame] = None,
    ):
        expression = ALPHA191_FORMULAS.get(factor_num)
        if not expression:
            raise ValueError(f"Alpha#{factor_num} 缺少真实公式")

        self.engine.load_data(market_data)
        factor_values = self.engine.calculate_factor(factor_num, expression)
        if not factor_values:
            return None

        ic_series = self._calculate_ic(factor_values, market_data, membership_by_date)
        quantile_result = self._calculate_quantile_returns(
            factor_values,
            market_data,
            membership_by_date=membership_by_date,
            benchmark_data=benchmark_data,
        )

        factor_name = f"Alpha#{factor_num}"
        self._save_daily_data(factor_name, quantile_result["daily_data"])

        turnover = quantile_result["turnover"]
        quality = quantile_result["quality"]

        return {
            "factor_name": factor_name,
            "min_q_return": quantile_result["annualized_returns"][0] * 100,
            "max_q_return": quantile_result["annualized_returns"][self.quantiles - 1] * 100,
            "benchmark_return": quantile_result["benchmark_annualized"] * 100,
            "min_q_excess": (quantile_result["annualized_returns"][0] - quantile_result["benchmark_annualized"]) * 100,
            "max_q_excess": (quantile_result["annualized_returns"][self.quantiles - 1] - quantile_result["benchmark_annualized"]) * 100,
            "min_q_turnover": turnover.get(0, 0.0) * 100,
            "max_q_turnover": turnover.get(self.quantiles - 1, 0.0) * 100,
            "ic_mean": float(np.nanmean(ic_series)) if len(ic_series) > 0 else 0.0,
            "ir_value": float(np.nanmean(ic_series) / np.nanstd(ic_series)) if len(ic_series) > 1 and np.nanstd(ic_series) > 0 else 0.0,
            "start_date": start_date,
            "end_date": end_date,
            "backtest_days": backtest_days,
            "coverage_ratio": quality.coverage_ratio * 100,
            "universe_size": quality.universe_size,
            "rebalance_count": quality.rebalance_count,
            "latest_trade_date": quality.latest_trade_date,
        }

    def _normalize_date(self, value) -> str:
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y%m%d")
        text = str(value)
        return text.replace("-", "")[:8]

    def _eligible_stocks(self, date, stocks: set[str], membership_by_date: Optional[dict[str, set[str]]]) -> set[str]:
        if not membership_by_date:
            return stocks
        date_key = self._normalize_date(date)
        members = membership_by_date.get(date_key)
        if not members:
            return stocks
        return stocks & members

    def _safe_corr(self, left: np.ndarray, right: np.ndarray) -> float:
        left = np.asarray(left, dtype=float)
        right = np.asarray(right, dtype=float)
        mask = np.isfinite(left) & np.isfinite(right)
        if mask.sum() < 2:
            return float("nan")
        left = left[mask]
        right = right[mask]
        if left.size < 2 or right.size < 2:
            return float("nan")
        if np.allclose(left, left[0]) or np.allclose(right, right[0]):
            return float("nan")
        left_std = float(np.std(left))
        right_std = float(np.std(right))
        if left_std <= 0 or right_std <= 0:
            return float("nan")
        return float(np.corrcoef(left, right)[0, 1])

    def _calculate_ic(self, factor_values, market_data, membership_by_date=None):
        ic_list = []
        dates = sorted(factor_values.keys())

        for idx, date in enumerate(dates[:-self.rebalance_days]):
            future_date = dates[idx + self.rebalance_days]
            current_factors = factor_values[date]
            current_prices = market_data[market_data["trade_date"] == date].set_index("ts_code")["close"]
            future_prices = market_data[market_data["trade_date"] == future_date].set_index("ts_code")["close"]

            common_stocks = set(current_factors.keys()) & set(current_prices.index) & set(future_prices.index)
            common_stocks = self._eligible_stocks(date, common_stocks, membership_by_date)
            if len(common_stocks) < max(10, self.quantiles * 3):
                continue

            factors = np.array([current_factors[s] for s in common_stocks], dtype=float)
            returns = np.array([(future_prices[s] / current_prices[s] - 1) for s in common_stocks], dtype=float)
            if len(factors) < 2:
                continue
            ic = self._safe_corr(factors, returns)
            if not np.isnan(ic):
                ic_list.append(float(ic))

        return np.array(ic_list, dtype=float)

    def _calculate_turnover_weights(self, prev_weights: dict[str, float], curr_weights: dict[str, float]):
        stocks = set(prev_weights) | set(curr_weights)
        buy_weight = 0.0
        sell_weight = 0.0
        for stock in stocks:
            delta = curr_weights.get(stock, 0.0) - prev_weights.get(stock, 0.0)
            if delta > 0:
                buy_weight += delta
            elif delta < 0:
                sell_weight += -delta
        return buy_weight, sell_weight

    def _calculate_quantile_returns(self, factor_values, market_data, membership_by_date=None, benchmark_data=None):
        portfolio_values = {q: [1.0] for q in range(self.quantiles)}
        turnover_series = {q: [] for q in range(self.quantiles)}
        prev_weights = {q: {} for q in range(self.quantiles)}
        holdings_data = []
        returns_data = []
        benchmark_value = 1.0
        coverage_ratios = []
        universe_sizes = []

        benchmark_prices = {}
        if benchmark_data is not None and not benchmark_data.empty:
            benchmark_prices = {
                self._normalize_date(row.trade_date): {
                    "open": float(getattr(row, "open", row.close) or row.close),
                    "close": float(row.close),
                }
                for row in benchmark_data.itertuples()
            }

        dates = sorted(factor_values.keys())
        first_date = None
        last_date = None

        rebalance_indices = list(range(0, len(dates) - 1, max(1, self.rebalance_days)))
        for signal_idx in rebalance_indices:
            entry_idx = signal_idx + 1
            next_signal_idx = signal_idx + max(1, self.rebalance_days)
            if entry_idx >= len(dates) or next_signal_idx >= len(dates):
                continue
            exit_idx = min(next_signal_idx + 1, len(dates) - 1)
            if exit_idx <= entry_idx:
                continue

            signal_date = dates[signal_idx]
            entry_date = dates[entry_idx]
            exit_date = dates[exit_idx]
            factors = factor_values.get(signal_date) or {}
            entry_frame = market_data[market_data["trade_date"] == entry_date].set_index("ts_code")
            exit_frame = market_data[market_data["trade_date"] == exit_date].set_index("ts_code")
            entry_prices = entry_frame["open"] if "open" in entry_frame.columns else entry_frame["close"]
            exit_prices = exit_frame["open"] if "open" in exit_frame.columns else exit_frame["close"]

            common_stocks = set(factors.keys()) & set(entry_prices.index) & set(exit_prices.index)
            eligible_stocks = self._eligible_stocks(signal_date, common_stocks, membership_by_date)
            if membership_by_date:
                date_key = self._normalize_date(signal_date)
                members = membership_by_date.get(date_key, set())
                if members:
                    coverage_ratios.append(len(eligible_stocks) / len(members))
                    universe_sizes.append(len(members))
            else:
                coverage_ratios.append(1.0)
                universe_sizes.append(len(eligible_stocks))

            if len(eligible_stocks) < max(20, self.quantiles * 5):
                continue

            if first_date is None:
                first_date = entry_date
            last_date = exit_date

            entry_date_key = self._normalize_date(entry_date)
            exit_date_key = self._normalize_date(exit_date)
            if entry_date_key in benchmark_prices and exit_date_key in benchmark_prices:
                entry_benchmark = benchmark_prices[entry_date_key].get("open") or benchmark_prices[entry_date_key].get("close")
                exit_benchmark = benchmark_prices[exit_date_key].get("open") or benchmark_prices[exit_date_key].get("close")
                benchmark_ret = exit_benchmark / entry_benchmark - 1 if entry_benchmark else 0.0
                benchmark_value *= (1 + benchmark_ret)
            elif eligible_stocks:
                benchmark_returns = [exit_prices[s] / entry_prices[s] - 1 for s in eligible_stocks]
                if benchmark_returns:
                    benchmark_value *= (1 + float(np.mean(benchmark_returns)))

            factor_list = sorted(((stock, factors[stock]) for stock in eligible_stocks), key=lambda item: item[1])
            quantile_size = max(1, len(factor_list) // self.quantiles)

            for q in range(self.quantiles):
                start_idx = q * quantile_size
                end_idx = (q + 1) * quantile_size if q < self.quantiles - 1 else len(factor_list)
                quantile_stocks = factor_list[start_idx:end_idx]
                if not quantile_stocks:
                    continue

                weight = 1.0 / len(quantile_stocks)
                current_weights = {stock: weight for stock, _ in quantile_stocks}
                buy_weight, sell_weight = self._calculate_turnover_weights(prev_weights[q], current_weights)
                turnover_series[q].append((buy_weight + sell_weight) / 2)

                for stock, factor_value in quantile_stocks:
                    holdings_data.append({
                        "date": exit_date_key,
                        "stock": stock,
                        "factor_value": factor_value,
                        "quantile": q,
                        "weight": weight,
                    })

                gross_returns = [exit_prices[s] / entry_prices[s] - 1 for s, _ in quantile_stocks]
                gross_return = float(np.mean(gross_returns)) if gross_returns else 0.0
                if prev_weights[q]:
                    cost = (
                        buy_weight * (self.commission + self.slippage)
                        + sell_weight * (self.commission + self.slippage + self.stamp_tax)
                    )
                else:
                    cost = sum(current_weights.values()) * (self.commission + self.slippage)
                net_return = gross_return - cost

                new_value = portfolio_values[q][-1] * (1 + net_return)
                portfolio_values[q].append(new_value)
                returns_data.append({
                    "date": exit_date_key,
                    "quantile": q,
                    "portfolio_value": new_value,
                    "daily_return": net_return,
                    "cumulative_return": new_value - 1.0,
                })
                prev_weights[q] = current_weights

        if first_date and last_date:
            start_ts = pd.to_datetime(first_date)
            end_ts = pd.to_datetime(last_date)
            actual_years = max((end_ts - start_ts).days / 365.0, 1 / 365.0)
        else:
            actual_years = 0.0

        annualized_returns = {}
        for q in range(self.quantiles):
            if len(portfolio_values[q]) > 1 and actual_years > 0:
                annualized_returns[q] = portfolio_values[q][-1] ** (1 / actual_years) - 1
            else:
                annualized_returns[q] = 0.0

        benchmark_annualized = benchmark_value ** (1 / actual_years) - 1 if actual_years > 0 else 0.0
        turnover = {q: float(np.mean(values)) if values else 0.0 for q, values in turnover_series.items()}
        latest_trade_date = self._normalize_date(last_date) if last_date is not None else ""
        quality = AnalysisQuality(
            coverage_ratio=float(np.mean(coverage_ratios)) if coverage_ratios else 0.0,
            universe_size=int(round(np.mean(universe_sizes))) if universe_sizes else 0,
            rebalance_count=len({item["date"] for item in returns_data}),
            latest_trade_date=latest_trade_date,
        )

        return {
            "annualized_returns": annualized_returns,
            "benchmark_annualized": benchmark_annualized,
            "turnover": turnover,
            "daily_data": {"holdings": holdings_data, "returns": returns_data},
            "quality": quality,
        }

    def _save_daily_data(self, factor_name, daily_data):
        self.db.replace_factor_daily_data(factor_name)
        if daily_data["holdings"]:
            self.db.save_daily_holdings(factor_name, daily_data["holdings"])
        if daily_data["returns"]:
            self.db.save_daily_returns(factor_name, daily_data["returns"])

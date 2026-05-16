"""Canonical execution simulator shared by factor and strategy backtests.

The simulator deliberately models a conservative A-share long-only workflow:
signals are generated on day T, trades execute at the next trading day's open,
buys are rounded to 100-share lots, sells respect available holdings, and
orders can be rejected by price/volume/limit/ST/STAR-board constraints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np
import pandas as pd


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        result = float(value)
        if not math.isfinite(result):
            return default
        return result
    except Exception:
        return default


def _date_text(value) -> str:
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def _round_lot(shares: int, lot_size: int = 100) -> int:
    lot_size = max(1, int(lot_size or 100))
    return max(0, int(shares) // lot_size * lot_size)


@dataclass
class ExecutionSettings:
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.001
    slippage: float = 0.001
    min_commission: float = 5.0
    transfer_fee_rate: float = 0.0
    volume_limit_pct: float = 0.10
    max_position_pct: float = 0.12
    target_exposure: float = 0.95
    lot_size: int = 100
    exclude_star_market: bool = True
    exclude_st: bool = True
    limit_up_pct: float = 0.10
    limit_down_pct: float = 0.10
    star_limit_pct: float = 0.20
    st_limit_pct: float = 0.05
    protocol_version: str = "canonical_execution_v1"


@dataclass
class ExecutionResult:
    equity_curve: list[dict] = field(default_factory=list)
    normalized_curve: list[dict] = field(default_factory=list)
    benchmark_curve: list[dict] = field(default_factory=list)
    benchmark_normalized_curve: list[dict] = field(default_factory=list)
    excess_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    benchmark_metrics: dict = field(default_factory=dict)
    rejection_reasons: dict = field(default_factory=dict)
    assumption_report: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "metrics": self.metrics,
            "benchmark_metrics": self.benchmark_metrics,
            "equity_curve": self.equity_curve,
            "normalized_curve": self.normalized_curve,
            "benchmark_curve": self.benchmark_curve,
            "benchmark_normalized_curve": self.benchmark_normalized_curve,
            "excess_curve": self.excess_curve,
            "trades": self.trades,
            "daily_returns": self.daily_returns,
            "rejection_reasons": self.rejection_reasons,
            "assumption_report": self.assumption_report,
        }


class PanelMarketData:
    """Small adapter over `{code: {field: Series}}` panels."""

    def __init__(
        self,
        stock_data: dict[str, dict],
        trade_dates: Iterable,
        *,
        benchmark_close: Optional[pd.Series] = None,
        benchmark_code: str = "",
        stock_names: Optional[dict[str, str]] = None,
    ):
        self.stock_data = stock_data or {}
        self.trade_dates = sorted(pd.to_datetime(list(trade_dates)))
        self.benchmark_close = benchmark_close if benchmark_close is not None else pd.Series(dtype=float)
        self.benchmark_code = str(benchmark_code or "")
        self.stock_names = stock_names or {}
        self._valuation_cache: dict[tuple[str, str, pd.Timestamp], float] = {}

    def price(self, code: str, field: str, dt) -> float:
        data = self.stock_data.get(code, {})
        series = data.get(field)
        if series is None and field == "high":
            series = data.get("high_exec")
        if series is None and field == "low":
            series = data.get("low_exec")
        if series is None and field == "close":
            series = data.get("close_exec")
        if series is None:
            return 0.0
        return _safe_float(series.get(pd.to_datetime(dt)), 0.0)

    def valuation_price(self, code: str, field: str, dt) -> float:
        dt = pd.to_datetime(dt)
        cache_key = (str(code), str(field), dt)
        if cache_key in self._valuation_cache:
            return self._valuation_cache[cache_key]
        direct = self.price(code, field, dt)
        if direct > 0:
            self._valuation_cache[cache_key] = direct
            return direct
        series = self.stock_data.get(code, {}).get(field)
        if series is None or getattr(series, "empty", True):
            self._valuation_cache[cache_key] = 0.0
            return 0.0
        try:
            normalized = pd.to_numeric(series.copy(), errors="coerce")
            normalized.index = pd.to_datetime(normalized.index)
            history = normalized.loc[normalized.index <= dt].dropna()
            value = _safe_float(history.iloc[-1], 0.0) if not history.empty else 0.0
        except Exception:
            value = 0.0
        self._valuation_cache[cache_key] = value
        return value

    def name(self, code: str) -> str:
        return str(self.stock_names.get(code) or "")


class CanonicalExecutionSimulator:
    def __init__(self, settings: Optional[ExecutionSettings] = None):
        self.settings = settings or ExecutionSettings()

    def run_factor_selection(
        self,
        *,
        factors: dict[str, pd.Series],
        market: PanelMarketData,
        select_pct: float,
        rebalance_days: int,
        direction: str,
        segment_dates: Optional[set] = None,
    ) -> dict:
        all_dates = list(market.trade_dates)
        selected_segment = {pd.to_datetime(x) for x in segment_dates} if segment_dates else set(all_dates)
        segment_ordered = [dt for dt in all_dates if dt in selected_segment]
        if len(all_dates) <= 3 or len(segment_ordered) <= 2:
            return {"error": "insufficient trading dates"}

        settings = self.settings
        cash = float(settings.initial_cash or 1_000_000)
        initial_cash = cash
        holdings: dict[str, int] = {}
        prev_value = initial_cash
        equity_curve: list[dict] = []
        daily_returns: list[float] = []
        trades: list[dict] = []
        rejection_counts: dict[str, int] = {}
        turnover_values: list[float] = []
        reverse = direction != "bottom"
        rebalance_step = max(1, int(rebalance_days or 1))
        execution_dates = set(segment_ordered[::rebalance_step])

        for idx, current_dt in enumerate(all_dates):
            in_segment = current_dt in selected_segment
            traded_value = 0.0
            should_rebalance = idx > 0 and in_segment and current_dt in execution_dates
            if should_rebalance:
                signal_date = all_dates[idx - 1]
                scores = self._collect_scores(factors, signal_date)
                if len(scores) >= 5:
                    tradable_scores = {
                        code: value
                        for code, value in scores.items()
                        if self._can_trade(market, code, current_dt, "buy", rejection_counts)
                    }
                    target_count = max(1, int(len(scores) * max(0.001, min(float(select_pct or 0.1), 1.0))))
                    tradable_targets = self._target_codes(tradable_scores, select_pct, reverse, target_count=target_count)
                    traded_value += self._liquidate_unselected(
                        market, current_dt, holdings, tradable_targets, trades, rejection_counts, cash_ref := {"cash": cash}
                    )
                    cash = cash_ref["cash"]
                    traded_value += self._rebalance_selected(
                        market, current_dt, holdings, tradable_targets, trades, rejection_counts, cash_ref := {"cash": cash}
                    )
                    cash = cash_ref["cash"]

            if in_segment:
                value = self._portfolio_value(market, cash, holdings, current_dt, "close")
                daily_returns.append((value - prev_value) / prev_value if prev_value > 0 else 0.0)
                if value > 0:
                    turnover_values.append(traded_value / value * 100.0)
                equity_curve.append({"date": _date_text(current_dt), "value": round(value, 2)})
                prev_value = value

        if not equity_curve:
            return {"error": "empty equity curve"}

        benchmark_curve = self._benchmark_curve(market, segment_ordered, initial_cash)
        benchmark_metrics = self._curve_metrics(benchmark_curve, initial_cash)
        metrics = self._metrics(equity_curve, daily_returns, trades, turnover_values, benchmark_metrics)
        metrics.update(
            {
                "protocol_version": settings.protocol_version,
                "execution_model": "canonical_next_open_a_share_v1",
                "benchmark_source": market.benchmark_code or "universe_equal_weight",
            }
        )
        return ExecutionResult(
            equity_curve=self._downsample_curve(equity_curve),
            normalized_curve=self._downsample_curve(self._normalize_curve(equity_curve)),
            benchmark_curve=self._downsample_curve(benchmark_curve),
            benchmark_normalized_curve=self._downsample_curve(self._normalize_curve(benchmark_curve)),
            excess_curve=self._downsample_curve(self._excess_curve(equity_curve, benchmark_curve)),
            trades=trades[-500:],
            daily_returns=[round(float(item), 6) for item in daily_returns],
            metrics=metrics,
            benchmark_metrics=benchmark_metrics,
            rejection_reasons=rejection_counts,
            assumption_report=self.assumption_report(),
        ).as_dict()

    def assumption_report(self) -> dict:
        settings = self.settings
        return {
            "execution": "T日信号，下一交易日开盘成交，收盘计值",
            "market": "A股只做多，买入按100股整数手",
            "costs": {
                "commission_rate": settings.commission_rate,
                "min_commission": settings.min_commission,
                "stamp_tax_rate": settings.stamp_tax_rate,
                "slippage": settings.slippage,
                "transfer_fee_rate": settings.transfer_fee_rate,
            },
            "constraints": {
                "target_exposure": settings.target_exposure,
                "max_position_pct": settings.max_position_pct,
                "volume_limit_pct": settings.volume_limit_pct,
                "exclude_star_market": settings.exclude_star_market,
                "exclude_st": settings.exclude_st,
            },
        }

    def _collect_scores(self, factors: dict[str, pd.Series], signal_date) -> dict[str, float]:
        scores: dict[str, float] = {}
        signal_date = pd.to_datetime(signal_date)
        for code, series in factors.items():
            try:
                value = series.get(signal_date)
            except Exception:
                value = np.nan
            if pd.notna(value):
                scores[code] = float(value)
        return scores

    def _target_codes(self, scores: dict[str, float], select_pct: float, reverse: bool, target_count: Optional[int] = None) -> list[str]:
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=reverse)
        count = int(target_count) if target_count is not None else max(1, int(len(ranked) * max(0.001, min(float(select_pct or 0.1), 1.0))))
        count = max(1, min(count, len(ranked)))
        return [code for code, _ in ranked[:count]]

    def _liquidate_unselected(
        self,
        market: PanelMarketData,
        dt,
        holdings: dict[str, int],
        selected: list[str],
        trades: list[dict],
        rejection_counts: dict[str, int],
        cash_ref: dict[str, float],
    ) -> float:
        traded_value = 0.0
        selected_set = set(selected)
        for code in list(holdings):
            if code in selected_set:
                continue
            shares = int(holdings.get(code, 0) or 0)
            if shares <= 0:
                holdings.pop(code, None)
                continue
            sold, trade_value = self._sell(market, dt, code, shares, holdings, trades, rejection_counts, cash_ref)
            if sold <= 0:
                continue
            traded_value += trade_value
        return traded_value

    def _rebalance_selected(
        self,
        market: PanelMarketData,
        dt,
        holdings: dict[str, int],
        selected: list[str],
        trades: list[dict],
        rejection_counts: dict[str, int],
        cash_ref: dict[str, float],
    ) -> float:
        if not selected:
            return 0.0
        settings = self.settings
        portfolio_value = self._portfolio_value(market, cash_ref["cash"], holdings, dt, "open")
        gross_target = portfolio_value * max(0.0, min(settings.target_exposure, 1.0))
        target_value = min(gross_target / max(len(selected), 1), portfolio_value * max(0.0, settings.max_position_pct))
        traded_value = 0.0
        for code in selected:
            open_price = market.price(code, "open", dt)
            if open_price <= 0:
                self._reject(rejection_counts, "missing_open_price")
                continue
            buy_price = self._slipped_price(open_price, "buy")
            target_shares = _round_lot(int(target_value / max(buy_price, 1e-12)), settings.lot_size)
            current = int(holdings.get(code, 0) or 0)
            diff = target_shares - current
            if diff > 0:
                bought, trade_value = self._buy(market, dt, code, diff, holdings, trades, rejection_counts, cash_ref)
                traded_value += trade_value if bought > 0 else 0.0
            elif diff < 0:
                sold, trade_value = self._sell(market, dt, code, abs(diff), holdings, trades, rejection_counts, cash_ref)
                traded_value += trade_value if sold > 0 else 0.0
        return traded_value

    def _buy(self, market, dt, code, shares, holdings, trades, rejection_counts, cash_ref) -> tuple[int, float]:
        settings = self.settings
        if not self._can_trade(market, code, dt, "buy", rejection_counts):
            return 0, 0.0
        open_price = market.price(code, "open", dt)
        exec_price = self._clamped_exec_price(market, code, dt, self._slipped_price(open_price, "buy"))
        desired = _round_lot(shares, settings.lot_size)
        desired = min(desired, self._volume_limited_shares(market, code, dt, desired))
        if desired < settings.lot_size:
            self._reject(rejection_counts, "below_lot_or_volume_cap")
            return 0, 0.0

        portfolio_value = self._portfolio_value(market, cash_ref["cash"], holdings, dt, "open")
        existing_value = int(holdings.get(code, 0) or 0) * open_price
        max_value = portfolio_value * max(0.0, settings.max_position_pct)
        if settings.max_position_pct > 0 and existing_value + desired * exec_price > max_value:
            desired = _round_lot(int(max(0.0, max_value - existing_value) / max(exec_price, 1e-12)), settings.lot_size)
            desired = min(desired, self._volume_limited_shares(market, code, dt, desired))
            if desired < settings.lot_size:
                self._reject(rejection_counts, "max_position_pct")
                return 0, 0.0

        total_cost = self._trade_value(desired, exec_price) + self._commission(desired, exec_price) + self._transfer_fee(code, desired, exec_price)
        if total_cost > cash_ref["cash"]:
            affordable = _round_lot(int(cash_ref["cash"] / max(exec_price * (1 + settings.commission_rate + settings.transfer_fee_rate), 1e-12)), settings.lot_size)
            desired = min(desired, affordable)
            if desired < settings.lot_size:
                self._reject(rejection_counts, "insufficient_cash")
                return 0, 0.0
            total_cost = self._trade_value(desired, exec_price) + self._commission(desired, exec_price) + self._transfer_fee(code, desired, exec_price)

        cash_ref["cash"] -= total_cost
        holdings[code] = int(holdings.get(code, 0) or 0) + desired
        commission = self._commission(desired, exec_price)
        transfer_fee = self._transfer_fee(code, desired, exec_price)
        trades.append(self._trade_record(dt, code, "buy", desired, exec_price, commission, 0.0, transfer_fee))
        return desired, desired * exec_price

    def _sell(self, market, dt, code, shares, holdings, trades, rejection_counts, cash_ref) -> tuple[int, float]:
        settings = self.settings
        current = int(holdings.get(code, 0) or 0)
        if current <= 0:
            self._reject(rejection_counts, "no_position")
            return 0, 0.0
        if not self._can_trade(market, code, dt, "sell", rejection_counts):
            return 0, 0.0
        open_price = market.price(code, "open", dt)
        exec_price = self._clamped_exec_price(market, code, dt, self._slipped_price(open_price, "sell"))
        desired = min(int(shares), current)
        desired = min(desired, self._volume_limited_shares(market, code, dt, desired))
        if desired <= 0:
            self._reject(rejection_counts, "below_lot_or_volume_cap")
            return 0, 0.0
        commission = self._commission(desired, exec_price)
        stamp_tax = self._stamp_tax(desired, exec_price)
        transfer_fee = self._transfer_fee(code, desired, exec_price)
        proceeds = desired * exec_price - commission - stamp_tax - transfer_fee
        cash_ref["cash"] += proceeds
        remaining = max(0, current - desired)
        if remaining > 0:
            holdings[code] = remaining
        else:
            holdings.pop(code, None)
        trades.append(self._trade_record(dt, code, "sell", desired, exec_price, commission, stamp_tax, transfer_fee))
        return desired, desired * exec_price

    def _can_trade(self, market: PanelMarketData, code: str, dt, side: str, rejection_counts: dict[str, int]) -> bool:
        settings = self.settings
        code_text = str(code or "").upper()
        name = market.name(code)
        if settings.exclude_star_market and code_text.startswith("688"):
            self._reject(rejection_counts, "star_market_filtered")
            return False
        if settings.exclude_st and "ST" in name.upper():
            self._reject(rejection_counts, "st_filtered")
            return False
        open_price = market.price(code, "open", dt)
        close_price = market.price(code, "close", dt)
        volume = market.price(code, "vol", dt)
        if open_price <= 0 or close_price <= 0 or volume <= 0:
            self._reject(rejection_counts, "paused_or_missing_price")
            return False
        pre_close = market.price(code, "pre_close", dt) or market.price(code, "prev_close", dt)
        if pre_close > 0:
            limit_pct = settings.star_limit_pct if code_text.startswith(("300", "688")) else settings.limit_up_pct
            if "ST" in name.upper():
                limit_pct = settings.st_limit_pct
            limit_up = round(pre_close * (1 + limit_pct), 2)
            limit_down = round(pre_close * (1 - limit_pct), 2)
            if side == "buy" and open_price >= limit_up:
                self._reject(rejection_counts, "limit_up_buy_blocked")
                return False
            if side == "sell" and open_price <= limit_down:
                self._reject(rejection_counts, "limit_down_sell_blocked")
                return False
        return True

    def _volume_limited_shares(self, market: PanelMarketData, code: str, dt, desired_shares: int) -> int:
        settings = self.settings
        desired = _round_lot(int(desired_shares), settings.lot_size)
        volume_hands = market.price(code, "vol", dt)
        if volume_hands <= 0:
            return 0
        max_shares = _round_lot(int(volume_hands * 100 * max(0.0, settings.volume_limit_pct)), settings.lot_size)
        return max(0, min(desired, max_shares))

    def _portfolio_value(self, market: PanelMarketData, cash: float, holdings: dict[str, int], dt, price_field: str) -> float:
        value = float(cash)
        for code, shares in holdings.items():
            price = market.valuation_price(code, price_field, dt)
            if price <= 0 and price_field != "close":
                price = market.valuation_price(code, "close", dt)
            if price > 0:
                value += int(shares) * price
        return value

    def _slipped_price(self, price: float, side: str) -> float:
        slip = max(0.0, float(self.settings.slippage or 0.0))
        adjusted = float(price) + slip if side == "buy" else float(price) - slip
        return round(max(0.0, adjusted), 2)

    def _clamped_exec_price(self, market: PanelMarketData, code: str, dt, price: float) -> float:
        high = market.price(code, "high", dt)
        low = market.price(code, "low", dt)
        if high > 0 and low > 0:
            return max(low, min(high, float(price)))
        return float(price)

    def _commission(self, shares: int, price: float) -> float:
        amount = self._trade_value(shares, price)
        rate = max(0.0, float(self.settings.commission_rate or 0.0))
        if amount <= 0 or rate <= 0:
            return 0.0
        return max(amount * rate, max(0.0, float(self.settings.min_commission or 0.0)))

    def _stamp_tax(self, shares: int, price: float) -> float:
        return self._trade_value(shares, price) * max(0.0, float(self.settings.stamp_tax_rate or 0.0))

    def _transfer_fee(self, code: str, shares: int, price: float) -> float:
        if ".SH" not in str(code or "").upper():
            return 0.0
        return self._trade_value(shares, price) * max(0.0, float(self.settings.transfer_fee_rate or 0.0))

    @staticmethod
    def _trade_value(shares: int, price: float) -> float:
        return max(0, int(shares)) * max(0.0, float(price))

    @staticmethod
    def _reject(rejection_counts: dict[str, int], reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    @staticmethod
    def _trade_record(
        dt,
        code: str,
        action: str,
        shares: int,
        price: float,
        commission: float = 0.0,
        stamp_tax: float = 0.0,
        transfer_fee: float = 0.0,
    ) -> dict:
        trade_value = max(0, int(shares)) * max(0.0, float(price))
        return {
            "date": _date_text(dt),
            "code": code,
            "action": action,
            "price": round(float(price), 4),
            "shares": int(shares),
            "trade_value": round(trade_value, 2),
            "commission": round(float(commission or 0.0), 2),
            "stamp_tax": round(float(stamp_tax or 0.0), 2),
            "transfer_fee": round(float(transfer_fee or 0.0), 2),
            "fee": round(float(commission or 0.0) + float(stamp_tax or 0.0) + float(transfer_fee or 0.0), 2),
        }

    def _benchmark_curve(self, market: PanelMarketData, dates: list, initial_cash: float) -> list[dict]:
        if not dates:
            return []
        series = market.benchmark_close
        if series is None or series.empty:
            return [{"date": _date_text(dt), "value": round(float(initial_cash), 2)} for dt in dates]
        aligned = series.copy()
        aligned.index = pd.to_datetime(aligned.index)
        aligned = pd.to_numeric(aligned, errors="coerce").reindex(pd.to_datetime(dates)).ffill().bfill()
        base = _safe_float(aligned.iloc[0], 0.0) if len(aligned) else 0.0
        if base <= 0:
            return [{"date": _date_text(dt), "value": round(float(initial_cash), 2)} for dt in dates]
        return [
            {"date": _date_text(dt), "value": round(float(initial_cash) * _safe_float(aligned.iloc[idx], base) / base, 2)}
            for idx, dt in enumerate(dates)
        ]

    def _metrics(self, equity_curve: list[dict], daily_returns: list[float], trades: list[dict], turnover_values: list[float], benchmark_metrics: dict) -> dict:
        initial_cash = float(self.settings.initial_cash or 1_000_000)
        final_value = _safe_float(equity_curve[-1].get("value"), initial_cash)
        total_return = (final_value / initial_cash - 1) * 100 if initial_cash > 0 else 0.0
        returns = np.array(daily_returns, dtype=float)
        returns = returns[np.isfinite(returns)]
        vol = float(np.std(returns, ddof=1)) if len(returns) > 2 else 0.0
        sharpe = float(np.mean(returns) / vol * math.sqrt(252)) if vol > 0 else 0.0
        benchmark_return = _safe_float(benchmark_metrics.get("total_return"), 0.0)
        return {
            "total_return": round(total_return, 2),
            "annual_return": round(self._annualized_return(total_return, len(equity_curve)), 2),
            "max_drawdown": round(self._max_drawdown_pct(equity_curve), 2),
            "sharpe_ratio": round(sharpe, 3),
            "trade_count": len(trades),
            "turnover": round(float(np.nanmean(turnover_values)), 2) if turnover_values else 0.0,
            "benchmark_return": round(benchmark_return, 2),
            "excess_return": round(total_return - benchmark_return, 2),
        }

    @staticmethod
    def _annualized_return(total_return_pct: float, days: int) -> float:
        if days <= 0:
            return 0.0
        return (math.pow(max(0.000001, 1 + total_return_pct / 100.0), 252 / max(days, 1)) - 1) * 100

    @classmethod
    def _curve_metrics(cls, curve: list[dict], initial_cash: float) -> dict:
        if not curve:
            return {"total_return": 0.0, "max_drawdown": 0.0}
        final_value = _safe_float(curve[-1].get("value"), initial_cash)
        total_return = (final_value / initial_cash - 1) * 100 if initial_cash > 0 else 0.0
        return {"total_return": round(total_return, 2), "max_drawdown": round(cls._max_drawdown_pct(curve), 2)}

    @staticmethod
    def _max_drawdown_pct(curve: list[dict]) -> float:
        peak = 0.0
        drawdown = 0.0
        for point in curve:
            value = _safe_float(point.get("value"), 0.0)
            peak = max(peak, value)
            if peak > 0:
                drawdown = max(drawdown, (peak - value) / peak)
        return drawdown * 100

    @staticmethod
    def _normalize_curve(curve: list[dict]) -> list[dict]:
        if not curve:
            return []
        base = _safe_float(curve[0].get("value"), 0.0)
        if base <= 0:
            return curve
        return [{"date": point.get("date"), "value": round(_safe_float(point.get("value"), base) / base, 6)} for point in curve]

    @staticmethod
    def _excess_curve(equity_curve: list[dict], benchmark_curve: list[dict]) -> list[dict]:
        if not equity_curve or not benchmark_curve:
            return []
        benchmark_by_date = {point.get("date"): _safe_float(point.get("value")) for point in benchmark_curve}
        base_strategy = _safe_float(equity_curve[0].get("value"), 0.0)
        base_benchmark = _safe_float(benchmark_curve[0].get("value"), 0.0)
        if base_strategy <= 0 or base_benchmark <= 0:
            return []
        result = []
        for point in equity_curve:
            date = point.get("date")
            benchmark_value = benchmark_by_date.get(date)
            if not benchmark_value:
                continue
            strategy_ret = _safe_float(point.get("value"), base_strategy) / base_strategy - 1
            benchmark_ret = benchmark_value / base_benchmark - 1
            result.append({"date": date, "value": round((strategy_ret - benchmark_ret) * 100, 4)})
        return result

    @staticmethod
    def _downsample_curve(curve: list[dict], max_points: int = 260) -> list[dict]:
        if len(curve) <= max_points:
            return curve
        step = max(1, math.ceil(len(curve) / max_points))
        sampled = curve[::step]
        if sampled[-1].get("date") != curve[-1].get("date"):
            sampled.append(curve[-1])
        return sampled

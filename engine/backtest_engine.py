"""回测引擎主循环."""
from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from data.data_cache import DataCache
from engine.broker import Broker
from engine.context import HistoryWindow, Order, OrderSide, OrderStatus, Position
from engine.metrics import calc_metrics
from factor.factor_engine import FactorEngine
from logging_config import get_logger

logger = get_logger("qtsys.engine.backtest")


HISTORY_FIELD_ALIASES = {
    "volume": "vol",
    "turnover": "turnover_rate",
}

DAILY_BASIC_FIELDS = {
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
}


def _warmup_start_date(start_date: str, days: int = 540) -> str:
    try:
        return (datetime.strptime(str(start_date), "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    except Exception:
        return start_date


class BacktestEngine:
    def __init__(
        self,
        cache: DataCache,
        initial_cash: float = 1_000_000.0,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.001,
        slippage: float = 0.002,
        max_position_pct: float = 0.25,
        max_drawdown_limit: float = 0.0,
    ):
        self.cache = cache
        self.initial_cash = initial_cash
        self.broker = Broker(commission_rate, stamp_tax_rate, slippage)
        self.max_position_pct = max_position_pct
        self.max_drawdown_limit = max_drawdown_limit
        self.reset()

    def reset(self):
        self.cash = self.initial_cash
        self.positions: dict[str, Position] = {}
        self.pending_orders: list[Order] = []
        self.filled_orders: list[Order] = []
        self.cancelled_orders: list[Order] = []
        self.equity_curve: list[dict] = []
        self.daily_returns: list[float] = []
        self.current_date: str = ""
        self.current_dt: Optional[pd.Timestamp] = None
        self.trade_dates: list[str] = []
        self._date_to_idx: dict[str, int] = {}
        self.all_data: dict[str, pd.DataFrame] = {}
        self.date_index: dict[str, dict[str, dict]] = {}
        self.benchmark_data: Optional[pd.DataFrame] = None
        self.benchmark_date_index: dict[str, dict] = {}
        self.log_messages: list[str] = []
        self.stop_loss: dict[str, float] = {}
        self.take_profit: dict[str, float] = {}
        self._peak_value = self.initial_cash
        self._risk_breached = False
        self.universe: list[str] = []
        self.factor_catalog: list[dict] = []
        self.factor_lookup: dict[str, dict] = {}
        self.factor_engine = FactorEngine(self.cache)
        self.factor_series_cache: dict[tuple[str, str], pd.Series] = {}
        self.factor_run_start = ""
        self.factor_run_end = ""
        self.exclude_star_market = True

    @property
    def portfolio_value(self) -> float:
        market_value = sum(p.market_value for p in self.positions.values() if p.amount > 0)
        return self.cash + market_value

    def log(self, msg: str):
        self.log_messages.append(f"[{self.current_date}] {msg}")

    def set_stop_loss(self, ts_code: str, pct: float):
        self.stop_loss[ts_code] = abs(pct)

    def set_take_profit(self, ts_code: str, pct: float):
        self.take_profit[ts_code] = abs(pct)

    def order(self, ts_code: str, amount: int, price: Optional[float] = None):
        if hasattr(amount, "amount"):
            amount = int(getattr(amount, "amount", 0))
        amount = int(amount)
        if amount == 0:
            return
        side = OrderSide.BUY if amount > 0 else OrderSide.SELL
        abs_amount = abs(amount)
        if side == OrderSide.BUY:
            abs_amount = (abs_amount // 100) * 100
            if abs_amount == 0:
                return
        order = Order(
            ts_code=ts_code,
            side=side,
            amount=abs_amount,
            price=price,
            created_date=self.current_date,
        )
        self.pending_orders.append(order)

    def order_target_percent(self, ts_code: str, percent: float):
        target_value = self.portfolio_value * float(percent)
        pos = self.positions.get(ts_code)
        current_value = pos.market_value if pos and pos.amount > 0 else 0.0
        diff = target_value - current_value
        price = self._get_reference_price(ts_code)
        if price and price > 0:
            amount = int(diff / price)
            if amount != 0:
                self.order(ts_code, amount)

    def order_value(self, ts_code: str, value: float):
        price = self._get_reference_price(ts_code)
        if price and price > 0:
            amount = int(float(value) / price)
            if amount != 0:
                self.order(ts_code, amount)

    def _get_reference_price(self, ts_code: str) -> Optional[float]:
        if ts_code not in self.date_index:
            return None
        current_row = self.date_index[ts_code].get(self.current_date)
        if current_row and current_row.get("close") is not None:
            return float(current_row["close"])
        current_pos = self._date_to_idx.get(self.current_date)
        if current_pos is None:
            return None
        idx = self.date_index[ts_code]
        if current_pos <= 0:
            row = idx.get(self.current_date)
            return float(row["pre_close"]) if row and row.get("pre_close") else None
        prev_date = self.trade_dates[current_pos - 1]
        row = idx.get(prev_date)
        return float(row["close"]) if row and row.get("close") is not None else None

    def get_price(self, ts_code: str) -> Optional[float]:
        row = self.date_index.get(ts_code, {}).get(self.current_date)
        if row and row.get("close") is not None:
            return float(row["close"])
        return None

    def get_history(self, ts_code: str, count: int = 10, field: str = "close") -> HistoryWindow:
        if ts_code not in self.all_data:
            return HistoryWindow(name=field)
        current_pos = self._date_to_idx.get(self.current_date)
        if current_pos is None:
            return HistoryWindow(name=field)
        idx = self.date_index.get(ts_code, {})
        if self.current_date not in idx:
            return HistoryWindow(name=field)

        normalized_field = HISTORY_FIELD_ALIASES.get(field, field)
        relevant_dates = self.trade_dates[max(0, current_pos - count + 1): current_pos + 1]
        values = []
        for date_value in relevant_dates:
            row = idx.get(date_value)
            if row and normalized_field in row and row.get(normalized_field) is not None:
                values.append(row[normalized_field])
        return HistoryWindow(values, name=normalized_field)

    def _execute_orders(self):
        for order in self.pending_orders:
            ts_code = order.ts_code
            if ts_code not in self.date_index:
                order.status = OrderStatus.CANCELLED
                order.reason = "无行情数据"
                continue

            if self.exclude_star_market and str(ts_code).upper().startswith("688"):
                order.status = OrderStatus.CANCELLED
                order.reason = "科创板默认过滤"
                continue

            if self._risk_breached and order.side == OrderSide.BUY:
                order.status = OrderStatus.CANCELLED
                order.reason = "回撤熔断，禁止买入"
                continue

            row = self.date_index[ts_code].get(self.current_date)
            if not row:
                order.status = OrderStatus.CANCELLED
                order.reason = "当日无交易数据"
                continue

            open_price = row["open"]
            high = row["high"]
            low = row["low"]
            pre_close = row["pre_close"]
            volume = float(row.get("vol", 0) or 0) * 100

            limit_up, limit_down = self.broker.check_limit(pre_close, ts_code=ts_code)
            if order.side == OrderSide.BUY and open_price >= limit_up:
                order.status = OrderStatus.CANCELLED
                order.reason = "涨停无法买入"
                continue
            if order.side == OrderSide.SELL and open_price <= limit_down:
                order.status = OrderStatus.CANCELLED
                order.reason = "跌停无法卖出"
                continue

            max_vol = int(volume * self.broker.volume_limit)
            actual_amount = min(order.amount, max_vol) if max_vol > 0 else order.amount

            if order.side == OrderSide.SELL:
                pos = self.positions.get(ts_code)
                if not pos or pos.available <= 0:
                    order.status = OrderStatus.CANCELLED
                    order.reason = "无可卖持仓"
                    continue
                actual_amount = min(actual_amount, pos.available)

            if actual_amount <= 0:
                order.status = OrderStatus.CANCELLED
                order.reason = "成交量不足"
                continue

            exec_price = self.broker.apply_slippage(open_price, order.side)
            exec_price = max(low, min(high, exec_price))

            if order.side == OrderSide.BUY and self.max_position_pct > 0:
                portfolio_value = self.portfolio_value
                existing = self.positions.get(ts_code)
                existing_value = existing.market_value if existing and existing.amount > 0 else 0.0
                new_value = actual_amount * exec_price + existing_value
                max_value = portfolio_value * self.max_position_pct
                if new_value > max_value:
                    allowed_value = max(0.0, max_value - existing_value)
                    actual_amount = int(allowed_value / exec_price)
                    actual_amount = (actual_amount // 100) * 100
                    if actual_amount <= 0:
                        order.status = OrderStatus.CANCELLED
                        order.reason = f"超出单股仓位上限{self.max_position_pct * 100:.0f}%"
                        continue

            commission = self.broker.calc_commission(actual_amount, exec_price)
            tax = self.broker.calc_tax(actual_amount, exec_price, order.side, ts_code=ts_code)
            total_cost = actual_amount * exec_price + commission + tax

            if order.side == OrderSide.BUY and total_cost > self.cash:
                actual_amount = int(self.cash / (exec_price * (1 + self.broker.commission_rate)))
                actual_amount = (actual_amount // 100) * 100
                if actual_amount <= 0:
                    order.status = OrderStatus.CANCELLED
                    order.reason = "资金不足"
                    continue
                commission = self.broker.calc_commission(actual_amount, exec_price)
                tax = self.broker.calc_tax(actual_amount, exec_price, order.side, ts_code=ts_code)

            self._fill_order(order, actual_amount, exec_price, commission, tax)

        self.filled_orders.extend([o for o in self.pending_orders if o.status == OrderStatus.FILLED])
        self.cancelled_orders.extend([o for o in self.pending_orders if o.status == OrderStatus.CANCELLED])
        self.pending_orders.clear()

    def _fill_order(self, order: Order, amount: int, price: float, commission: float, tax: float):
        order.status = OrderStatus.FILLED
        order.filled_amount = amount
        order.filled_price = price
        order.commission = commission
        order.tax = tax
        order.filled_date = self.current_date

        ts_code = order.ts_code
        if ts_code not in self.positions:
            self.positions[ts_code] = Position(ts_code=ts_code)
        pos = self.positions[ts_code]

        if order.side == OrderSide.BUY:
            total_cost = amount * price + commission + tax
            self.cash -= total_cost
            if pos.amount > 0:
                pos.avg_cost = (pos.avg_cost * pos.amount + price * amount) / (pos.amount + amount)
            else:
                pos.avg_cost = price
            pos.amount += amount
            pos.last_price = price
        else:
            proceeds = amount * price - commission - tax
            self.cash += proceeds
            pos.amount -= amount
            pos.available -= amount
            if pos.amount <= 0:
                self.positions.pop(ts_code, None)
                return

    def _update_prices(self):
        for ts_code, pos in self.positions.items():
            row = self.date_index.get(ts_code, {}).get(self.current_date)
            if row and row.get("close") is not None:
                pos.last_price = row["close"]

    def _settle_t1(self):
        for pos in self.positions.values():
            if pos.amount > 0:
                pos.available = pos.amount

    def _check_stop_loss_take_profit(self):
        for ts_code, pos in list(self.positions.items()):
            if pos.amount <= 0 or pos.avg_cost <= 0:
                continue
            profit_pct = pos.profit_pct
            if ts_code in self.stop_loss and profit_pct <= -self.stop_loss[ts_code]:
                self.log(f"止损触发: {ts_code} 亏损{profit_pct * 100:.1f}%")
                self.order(ts_code, -pos.available)
            elif ts_code in self.take_profit and profit_pct >= self.take_profit[ts_code]:
                self.log(f"止盈触发: {ts_code} 盈利{profit_pct * 100:.1f}%")
                self.order(ts_code, -pos.available)

    def _check_portfolio_risk(self):
        if self.max_drawdown_limit <= 0:
            return
        portfolio_value = self.portfolio_value
        if portfolio_value > self._peak_value:
            self._peak_value = portfolio_value
        if self._peak_value <= 0:
            return
        drawdown = (self._peak_value - portfolio_value) / self._peak_value
        if drawdown >= self.max_drawdown_limit and not self._risk_breached:
            self._risk_breached = True
            self.log(
                f"回撤熔断触发: 回撤{drawdown * 100:.1f}% >= 限制{self.max_drawdown_limit * 100:.0f}%，执行清仓"
            )
            for ts_code, pos in list(self.positions.items()):
                if pos.amount > 0 and pos.available > 0:
                    self.order(ts_code, -pos.available)

    def _required_daily_basic_fields(self, required_fields: Optional[set[str]]) -> list[str]:
        normalized = {HISTORY_FIELD_ALIASES.get(field, field) for field in (required_fields or set())}
        return sorted(normalized & DAILY_BASIC_FIELDS)

    def set_factor_catalog(self, factor_catalog: Optional[list[dict]] = None):
        self.factor_catalog = list(factor_catalog or [])
        self.factor_lookup = {}
        for item in self.factor_catalog:
            factor_id = item.get("id")
            name = str(item.get("name") or "").strip()
            expression = str(item.get("expression") or "").strip()
            if factor_id is not None:
                self.factor_lookup[f"id:{factor_id}"] = item
                self.factor_lookup[str(factor_id)] = item
            if name:
                self.factor_lookup[name] = item
                self.factor_lookup[name.lower()] = item
            if expression:
                self.factor_lookup[expression] = item
        self.factor_series_cache = {}

    def list_factors(self, keyword: str = "") -> list[dict]:
        keyword_text = str(keyword or "").strip().lower()
        if not keyword_text:
            return list(self.factor_catalog)
        return [
            item for item in self.factor_catalog
            if keyword_text in str(item.get("name") or "").lower()
            or keyword_text in str(item.get("description") or "").lower()
            or keyword_text in str(item.get("category") or "").lower()
        ]

    def _resolve_factor_meta(self, factor_ref) -> Optional[dict]:
        if isinstance(factor_ref, dict):
            name = str(factor_ref.get("name") or "").strip()
            return self.factor_lookup.get(name) or factor_ref
        if factor_ref is None:
            return None
        text = str(factor_ref).strip()
        if not text:
            return None
        return self.factor_lookup.get(text) or self.factor_lookup.get(text.lower())

    def _normalize_factor_series(self, series) -> pd.Series:
        if series is None:
            return pd.Series(dtype=float)
        normalized = pd.Series(series).copy()
        try:
            normalized.index = [
                idx.strftime("%Y%m%d") if hasattr(idx, "strftime") else str(idx)[:10].replace("-", "")
                for idx in normalized.index
            ]
        except Exception:
            normalized.index = [str(idx) for idx in normalized.index]
        normalized = pd.to_numeric(normalized, errors="coerce")
        return normalized

    def _ensure_factor_series(self, factor_ref, ts_code: str) -> pd.Series:
        factor_meta = self._resolve_factor_meta(factor_ref)
        if not factor_meta:
            return pd.Series(dtype=float)
        factor_key = str(factor_meta.get("id") if factor_meta.get("id") is not None else factor_meta.get("name") or factor_meta.get("expression") or factor_ref)
        cache_key = (factor_key, ts_code)
        if cache_key in self.factor_series_cache:
            return self.factor_series_cache[cache_key]

        expression = str(factor_meta.get("expression") or "").strip()
        if not expression:
            return pd.Series(dtype=float)
        try:
            series = self.factor_engine.compute_factor_values(expression, ts_code, self.factor_run_start, self.factor_run_end)
            normalized = self._normalize_factor_series(series)
        except Exception as exc:
            self.log(f"因子计算失败: {factor_meta.get('name') or factor_ref} / {ts_code} / {exc}")
            normalized = pd.Series(dtype=float)
        self.factor_series_cache[cache_key] = normalized
        return normalized

    def get_factor(self, factor_ref, ts_code: str, default: float = 0.0) -> float:
        series = self._ensure_factor_series(factor_ref, ts_code)
        if series.empty:
            return default
        value = series.get(self.current_date)
        if value is None or pd.isna(value):
            return default
        return float(value)

    def get_factor_history(self, factor_ref, ts_code: str, count: int = 10) -> HistoryWindow:
        series = self._ensure_factor_series(factor_ref, ts_code)
        if series.empty:
            return HistoryWindow(name=str(factor_ref))
        current_pos = self._date_to_idx.get(self.current_date)
        if current_pos is None:
            return HistoryWindow(name=str(factor_ref))
        relevant_dates = self.trade_dates[max(0, current_pos - count + 1): current_pos + 1]
        values = []
        for date_value in relevant_dates:
            value = series.get(date_value)
            if value is None or pd.isna(value):
                continue
            values.append(float(value))
        return HistoryWindow(values, name=str(factor_ref))

    def _load_symbol_data(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
        daily_basic_fields: list[str],
    ) -> pd.DataFrame:
        daily = self.cache.get_daily(ts_code, start_date, end_date)
        if daily.empty:
            return pd.DataFrame()
        merged = daily.copy()
        if daily_basic_fields:
            daily_basic = self.cache.get_daily_basic(ts_code, start_date, end_date)
            if not daily_basic.empty:
                available_fields = [field for field in daily_basic_fields if field in daily_basic.columns]
                if available_fields:
                    merged = merged.merge(
                        daily_basic[["trade_date", *available_fields]],
                        on="trade_date",
                        how="left",
                    )
        if "pre_close" not in merged.columns:
            merged["pre_close"] = merged["close"].shift(1)
        merged["pre_close"] = merged["pre_close"].fillna(merged["close"])
        return merged.sort_values("trade_date").reset_index(drop=True)

    def _build_data_coverage_summary(self, requested_universe: list[str], daily_basic_fields: list[str]) -> dict:
        requested = list(requested_universe or [])
        loaded = sorted(self.all_data.keys())
        missing = sorted(set(requested) - set(loaded))
        rows_by_symbol = {code: int(len(self.all_data.get(code, []))) for code in loaded}
        return {
            "requested_stock_count": len(requested),
            "loaded_stock_count": len(loaded),
            "missing_stock_count": len(missing),
            "missing_stocks": missing[:50],
            "trade_date_count": len(self.trade_dates),
            "start_date": self.trade_dates[0] if self.trade_dates else "",
            "end_date": self.trade_dates[-1] if self.trade_dates else "",
            "daily_basic_fields": daily_basic_fields,
            "min_rows_per_stock": min(rows_by_symbol.values()) if rows_by_symbol else 0,
            "max_rows_per_stock": max(rows_by_symbol.values()) if rows_by_symbol else 0,
        }

    def _build_execution_model_summary(self) -> dict:
        return {
            "signal_timing": "策略在交易日 T 使用截至 T 的历史数据形成信号",
            "execution_timing": "订单在下一交易日开盘撮合",
            "market_rules": "A股100股整数手、T+1可卖、涨跌停和成交量限制",
            "commission_rate": self.broker.commission_rate,
            "min_commission": self.broker.min_commission,
            "stamp_tax_rate": self.broker.stamp_tax_rate,
            "slippage": self.broker.slippage,
            "transfer_fee_rate": self.broker.transfer_fee_rate,
            "volume_limit": self.broker.volume_limit,
            "max_position_pct": self.max_position_pct,
            "max_drawdown_limit": self.max_drawdown_limit,
            "exclude_star_market": self.exclude_star_market,
            "engine": "backtest_engine_next_open_a_share",
        }

    def run(
        self,
        universe: list[str],
        start_date: str,
        end_date: str,
        initialize_func,
        handle_data_func,
        benchmark: str = "000300.SH",
        required_fields: Optional[set[str]] = None,
        factor_catalog: Optional[list[dict]] = None,
    ) -> dict:
        self.reset()
        self.universe = universe
        self.factor_run_start = _warmup_start_date(start_date)
        self.factor_run_end = end_date
        self.set_factor_catalog(factor_catalog)

        self.trade_dates = self.cache.get_trade_cal(start_date, end_date)
        if not self.trade_dates:
            return {"error": "无法获取交易日历"}
        self._date_to_idx = {date_value: idx for idx, date_value in enumerate(self.trade_dates)}

        daily_basic_fields = self._required_daily_basic_fields(required_fields)
        for ts_code in universe:
            df = self._load_symbol_data(ts_code, start_date, end_date, daily_basic_fields)
            if df.empty:
                continue
            self.all_data[ts_code] = df
            date_map: dict[str, dict] = {}
            for _, row in df.iterrows():
                dt = row["trade_date"]
                date_key = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)[:10].replace("-", "")
                date_map[date_key] = row.to_dict()
            self.date_index[ts_code] = date_map

        self.benchmark_data = self.cache.get_index_daily(benchmark, start_date, end_date)
        if self.benchmark_data is not None and not self.benchmark_data.empty:
            for _, row in self.benchmark_data.iterrows():
                dt = row["trade_date"]
                date_key = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)[:10].replace("-", "")
                self.benchmark_date_index[date_key] = row.to_dict()

        try:
            initialize_func(self)
        except Exception as exc:
            return {"error": f"初始化失败: {exc}\n{traceback.format_exc()}"}

        prev_value = self.initial_cash

        for date_str in self.trade_dates:
            self.current_date = date_str
            self.current_dt = pd.to_datetime(date_str)

            self._settle_t1()
            self._check_stop_loss_take_profit()
            self._check_portfolio_risk()
            self._execute_orders()
            self._update_prices()

            try:
                handle_data_func(self)
            except Exception as exc:
                self.log(f"策略异常: {exc}")


            portfolio_value = self.portfolio_value
            daily_return = (portfolio_value - prev_value) / prev_value if prev_value > 0 else 0.0
            self.daily_returns.append(daily_return)
            self.equity_curve.append(
                {"date": date_str, "value": round(portfolio_value, 2), "cash": round(self.cash, 2)}
            )
            prev_value = portfolio_value

        benchmark_returns = self._get_benchmark_returns()
        metrics = calc_metrics(
            self.daily_returns,
            benchmark_returns,
            self.initial_cash,
            self.portfolio_value,
        )
        order_rejections = self._format_rejections()
        if order_rejections:
            metrics["order_reject_count"] = sum(item["count"] for item in order_rejections)
            metrics["order_rejections"] = order_rejections

        return {
            "metrics": metrics,
            "equity_curve": self.equity_curve,
            "trades": self._format_trades(),
            "daily_returns": [round(item, 6) for item in self.daily_returns],
            "benchmark_curve": self._build_benchmark_curve(),
            "logs": self.log_messages[-500:],
            "order_rejections": order_rejections,
            "order_trace": self._format_order_trace(),
            "final_value": round(self.portfolio_value, 2),
            "data_coverage": self._build_data_coverage_summary(universe, daily_basic_fields),
            "execution_model": self._build_execution_model_summary(),
        }

    def _build_benchmark_curve(self) -> list[dict]:
        if self.benchmark_data is None or self.benchmark_data.empty:
            return []
        df = self.benchmark_data.sort_values("trade_date")
        closes = df["close"].values
        dates = df["trade_date"].values
        if len(closes) == 0 or closes[0] == 0:
            return []
        base = closes[0]
        curve = []
        for idx in range(len(closes)):
            dt = pd.Timestamp(dates[idx])
            date_str = dt.strftime("%Y%m%d")
            curve.append({"date": date_str, "value": round(float(closes[idx] / base), 6)})
        return curve

    def _get_benchmark_returns(self) -> list[float]:
        if not self.benchmark_date_index:
            return []
        returns = []
        prev_close = None
        for date_str in self.trade_dates:
            row = self.benchmark_date_index.get(date_str)
            if not row:
                returns.append(0.0)
                continue
            close = row["close"]
            if prev_close is not None and prev_close > 0:
                returns.append((close - prev_close) / prev_close)
            else:
                returns.append(0.0)
            prev_close = close
        return returns

    def _format_trades(self) -> list[dict]:
        trades = []
        for order in self.filled_orders:
            trades.append(
                {
                    "date": order.filled_date,
                    "ts_code": order.ts_code,
                    "side": order.side.value,
                    "amount": order.filled_amount,
                    "price": round(order.filled_price, 4),
                    "commission": round(order.commission, 2),
                    "tax": round(order.tax, 2),
                }
            )
        return trades

    def _format_rejections(self) -> list[dict]:
        counts: dict[str, int] = {}
        for order in self.cancelled_orders:
            reason = order.reason or "未知原因"
            counts[reason] = counts.get(reason, 0) + 1
        return [
            {"reason": reason, "count": count}
            for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def _format_order_trace(self) -> list[dict]:
        rows = []
        for order in [*self.filled_orders, *self.cancelled_orders]:
            rows.append(
                {
                    "created_date": order.created_date,
                    "filled_date": order.filled_date,
                    "ts_code": order.ts_code,
                    "side": order.side.value,
                    "target_amount": order.amount,
                    "filled_amount": order.filled_amount,
                    "filled_price": round(order.filled_price, 4),
                    "commission": round(order.commission, 2),
                    "tax": round(order.tax, 2),
                    "status": order.status.value,
                    "reason": order.reason,
                }
            )
        rows.sort(key=lambda item: (item.get("created_date") or "", item.get("filled_date") or "", item.get("ts_code") or ""))
        return rows[-1000:]

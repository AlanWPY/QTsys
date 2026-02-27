"""回测引擎主循环"""
import pandas as pd
import numpy as np
import traceback
from typing import Optional
from engine.context import Order, OrderSide, OrderStatus, Position
from engine.broker import Broker
from engine.metrics import calc_metrics
from data.data_cache import DataCache
from logging_config import get_logger

logger = get_logger("qtsys.engine.backtest")


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
        self.cash = initial_cash
        self.broker = Broker(commission_rate, stamp_tax_rate, slippage)
        self.positions: dict[str, Position] = {}
        self.pending_orders: list[Order] = []
        self.filled_orders: list[Order] = []
        self.equity_curve: list[dict] = []
        self.daily_returns: list[float] = []
        self.current_date: str = ""
        self.current_data: dict[str, pd.DataFrame] = {}
        self.trade_dates: list[str] = []
        self._date_to_idx: dict[str, int] = {}  # O(1) date->index lookup
        self.all_data: dict[str, pd.DataFrame] = {}
        # O(1) date-indexed lookup: {ts_code: {date_str: {col: val, ...}}}
        self.date_index: dict[str, dict[str, dict]] = {}
        self.benchmark_data: Optional[pd.DataFrame] = None
        self.benchmark_date_index: dict[str, dict] = {}
        self.log_messages: list[str] = []
        # 止损止盈配置: {ts_code: pct}
        self.stop_loss: dict[str, float] = {}
        self.take_profit: dict[str, float] = {}
        # 组合级风控
        self.max_position_pct = max_position_pct  # 单股最大仓位
        self.max_drawdown_limit = max_drawdown_limit  # 最大回撤熔断 (0=不启用)
        self._peak_value = initial_cash
        self._risk_breached = False

    @property
    def portfolio_value(self) -> float:
        mv = sum(p.market_value for p in self.positions.values() if p.amount > 0)
        return self.cash + mv

    def order(self, ts_code: str, amount: int, price: Optional[float] = None):
        """下单接口 - amount正数买入,负数卖出"""
        if amount == 0:
            return
        side = OrderSide.BUY if amount > 0 else OrderSide.SELL
        abs_amount = abs(amount)
        if side == OrderSide.BUY:
            abs_amount = (abs_amount // 100) * 100
            if abs_amount == 0:
                return
        o = Order(ts_code=ts_code, side=side, amount=abs_amount, price=price, created_date=self.current_date)
        self.pending_orders.append(o)

    def set_stop_loss(self, ts_code: str, pct: float):
        """设置止损比例 (如 0.05 表示亏损5%止损)"""
        self.stop_loss[ts_code] = abs(pct)

    def set_take_profit(self, ts_code: str, pct: float):
        """设置止盈比例 (如 0.10 表示盈利10%止盈)"""
        self.take_profit[ts_code] = abs(pct)

    def order_target_percent(self, ts_code: str, percent: float):
        """目标仓位百分比下单 - 使用前一日收盘价定价(避免前视偏差)"""
        target_value = self.portfolio_value * percent
        pos = self.positions.get(ts_code)
        current_value = pos.market_value if pos and pos.amount > 0 else 0.0
        diff = target_value - current_value
        price = self._get_prev_close(ts_code)
        if price and price > 0:
            amount = int(diff / price)
            if amount != 0:
                self.order(ts_code, amount)

    def order_value(self, ts_code: str, value: float):
        """按金额下单 - 使用前一日收盘价定价(避免前视偏差)"""
        price = self._get_prev_close(ts_code)
        if price and price > 0:
            amount = int(value / price)
            if amount != 0:
                self.order(ts_code, amount)

    def _get_prev_close(self, ts_code: str) -> Optional[float]:
        """获取前一日收盘价(用于下单定价,避免前视偏差) - O(1)查找"""
        if ts_code not in self.date_index:
            return None
        idx = self.date_index[ts_code]
        pos = self._date_to_idx.get(self.current_date)
        if pos is None:
            return None
        if pos <= 0:
            row = idx.get(self.current_date)
            return float(row["pre_close"]) if row and "pre_close" in row else None
        prev_date = self.trade_dates[pos - 1]
        row = idx.get(prev_date)
        return float(row["close"]) if row else None

    def get_price(self, ts_code: str) -> Optional[float]:
        """获取当前价格"""
        if ts_code in self.date_index:
            row = self.date_index[ts_code].get(self.current_date)
            if row:
                return float(row["close"])
        return None

    def get_history(self, ts_code: str, count: int = 10, field: str = "close") -> pd.Series:
        """获取历史数据 - O(1)日期查找"""
        if ts_code not in self.all_data:
            return pd.Series(dtype=float)
        cur_idx = self._date_to_idx.get(self.current_date)
        if cur_idx is None:
            return pd.Series(dtype=float)
        # 取当前日期及之前的交易日
        relevant_dates = self.trade_dates[max(0, cur_idx - count + 1):cur_idx + 1]
        idx = self.date_index.get(ts_code, {})
        values = []
        for d in relevant_dates:
            row = idx.get(d)
            if row and field in row:
                values.append(row[field])
        return pd.Series(values, dtype=float).reset_index(drop=True)

    def _execute_orders(self):
        """执行待处理订单 - 含仓位上限检查"""
        for order in self.pending_orders:
            ts_code = order.ts_code
            if ts_code not in self.date_index:
                order.status = OrderStatus.CANCELLED
                order.reason = "无行情数据"
                continue

            # 回撤熔断: 已触发则拒绝买入
            if self._risk_breached and order.side == OrderSide.BUY:
                order.status = OrderStatus.CANCELLED
                order.reason = "回撤熔断,禁止买入"
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
            volume = row.get("vol", 0) * 100

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

            # 仓位上限检查 (买入时)
            if order.side == OrderSide.BUY and self.max_position_pct > 0:
                pv = self.portfolio_value
                existing = self.positions.get(ts_code)
                existing_val = existing.market_value if existing and existing.amount > 0 else 0.0
                new_val = actual_amount * exec_price + existing_val
                max_val = pv * self.max_position_pct
                if new_val > max_val:
                    allowed = max(0, max_val - existing_val)
                    actual_amount = int(allowed / exec_price)
                    actual_amount = (actual_amount // 100) * 100
                    if actual_amount <= 0:
                        order.status = OrderStatus.CANCELLED
                        order.reason = f"超出单股仓位上限{self.max_position_pct*100:.0f}%"
                        continue

            commission = self.broker.calc_commission(actual_amount, exec_price)
            tax = self.broker.calc_tax(actual_amount, exec_price, order.side)
            total_cost = actual_amount * exec_price + commission + tax

            if order.side == OrderSide.BUY:
                if total_cost > self.cash:
                    actual_amount = int(self.cash / (exec_price * (1 + self.broker.commission_rate)))
                    actual_amount = (actual_amount // 100) * 100
                    if actual_amount <= 0:
                        order.status = OrderStatus.CANCELLED
                        order.reason = "资金不足"
                        continue
                    commission = self.broker.calc_commission(actual_amount, exec_price)
                    tax = self.broker.calc_tax(actual_amount, exec_price, order.side)
                    total_cost = actual_amount * exec_price + commission + tax

            self._fill_order(order, actual_amount, exec_price, commission, tax)

        self.filled_orders.extend(
            [o for o in self.pending_orders if o.status == OrderStatus.FILLED]
        )
        self.pending_orders.clear()

    def _fill_order(self, order: Order, amount: int, price: float, commission: float, tax: float):
        """成交订单并更新持仓"""
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
            # T+1: 买入当天不可卖, available不增加
        else:
            proceeds = amount * price - commission - tax
            self.cash += proceeds
            pos.amount -= amount
            pos.available -= amount
            if pos.amount <= 0:
                pos.amount = 0
                pos.available = 0
                pos.avg_cost = 0.0

    def _update_prices(self):
        """更新持仓最新价格 - 使用date_index O(1)查找"""
        for ts_code, pos in self.positions.items():
            if pos.amount <= 0:
                continue
            if ts_code in self.date_index:
                row = self.date_index[ts_code].get(self.current_date)
                if row:
                    pos.last_price = row["close"]

    def _settle_t1(self):
        """T+1交割: 每日开盘前将所有持仓设为可卖"""
        for pos in self.positions.values():
            if pos.amount > 0:
                pos.available = pos.amount

    def _check_stop_loss_take_profit(self):
        """检查持仓是否触发止损/止盈,触发则自动下卖出单"""
        for ts_code, pos in list(self.positions.items()):
            if pos.amount <= 0 or pos.avg_cost <= 0:
                continue
            pct = pos.profit_pct
            # 止损检查
            if ts_code in self.stop_loss and pct <= -self.stop_loss[ts_code]:
                self.log(f"止损触发: {ts_code} 亏损{pct*100:.1f}%")
                self.order(ts_code, -pos.available)
            # 止盈检查
            elif ts_code in self.take_profit and pct >= self.take_profit[ts_code]:
                self.log(f"止盈触发: {ts_code} 盈利{pct*100:.1f}%")
                self.order(ts_code, -pos.available)

    def _check_portfolio_risk(self):
        """组合级风控 - 回撤超限时自动清仓"""
        if self.max_drawdown_limit <= 0:
            return
        pv = self.portfolio_value
        if pv > self._peak_value:
            self._peak_value = pv
        drawdown = (self._peak_value - pv) / self._peak_value
        if drawdown >= self.max_drawdown_limit and not self._risk_breached:
            self._risk_breached = True
            self.log(f"回撤熔断触发: 回撤{drawdown*100:.1f}% >= 限制{self.max_drawdown_limit*100:.0f}%, 清仓")
            for ts_code, pos in list(self.positions.items()):
                if pos.amount > 0 and pos.available > 0:
                    self.order(ts_code, -pos.available)

    def log(self, msg: str):
        self.log_messages.append(f"[{self.current_date}] {msg}")

    def run(
        self,
        universe: list[str],
        start_date: str,
        end_date: str,
        initialize_func,
        handle_data_func,
        benchmark: str = "000300.SH",
    ) -> dict:
        """运行回测主循环"""
        self.universe = universe

        # 获取交易日历
        self.trade_dates = self.cache.get_trade_cal(start_date, end_date)
        if not self.trade_dates:
            return {"error": "无法获取交易日历"}

        # 构建O(1) date->index映射
        self._date_to_idx = {d: i for i, d in enumerate(self.trade_dates)}

        # 预加载所有股票数据 + 建立O(1)日期索引
        for ts_code in universe:
            df = self.cache.get_daily(ts_code, start_date, end_date)
            if not df.empty:
                self.all_data[ts_code] = df
                idx = {}
                for _, r in df.iterrows():
                    dt = r["trade_date"]
                    date_key = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)[:10].replace("-", "")
                    idx[date_key] = r.to_dict()
                self.date_index[ts_code] = idx

        # 加载基准数据 + 建立基准日期索引
        self.benchmark_data = self.cache.get_index_daily(benchmark, start_date, end_date)
        if self.benchmark_data is not None and not self.benchmark_data.empty:
            for _, r in self.benchmark_data.iterrows():
                dt = r["trade_date"]
                date_key = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)[:10].replace("-", "")
                self.benchmark_date_index[date_key] = r.to_dict()

        # 调用初始化
        try:
            initialize_func(self)
        except Exception as e:
            return {"error": f"初始化失败: {str(e)}\n{traceback.format_exc()}"}

        prev_value = self.initial_cash

        # 逐日驱动
        for date_str in self.trade_dates:
            self.current_date = date_str

            # T+1交割
            self._settle_t1()

            # 止损止盈检查(在策略逻辑之前)
            self._check_stop_loss_take_profit()

            # 组合级风控检查
            self._check_portfolio_risk()

            # 调用策略(此时持仓价格为前一日收盘价,无前视偏差)
            try:
                handle_data_func(self)
            except Exception as e:
                self.log(f"策略异常: {str(e)}")

            # 执行订单
            self._execute_orders()

            # 收盘后更新价格
            self._update_prices()

            # 记录净值
            pv = self.portfolio_value
            daily_ret = (pv - prev_value) / prev_value if prev_value > 0 else 0.0
            self.daily_returns.append(daily_ret)
            self.equity_curve.append({
                "date": date_str,
                "value": round(pv, 2),
                "cash": round(self.cash, 2),
            })
            prev_value = pv

        # 计算绩效指标
        benchmark_returns = self._get_benchmark_returns()
        metrics = calc_metrics(
            self.daily_returns, benchmark_returns, self.initial_cash, self.portfolio_value
        )

        # 构建基准曲线
        benchmark_curve = self._build_benchmark_curve()

        # 整理交易记录
        trades = self._format_trades()

        return {
            "metrics": metrics,
            "equity_curve": self.equity_curve,
            "trades": trades,
            "daily_returns": [round(r, 6) for r in self.daily_returns],
            "benchmark_curve": benchmark_curve,
            "logs": self.log_messages[-200:],
            "final_value": round(self.portfolio_value, 2),
        }

    def _build_benchmark_curve(self) -> list[dict]:
        """从benchmark_data构建基准净值曲线 [{date, value}]"""
        if self.benchmark_data is None or self.benchmark_data.empty:
            return []
        df = self.benchmark_data.sort_values("trade_date")
        closes = df["close"].values
        dates = df["trade_date"].values
        if len(closes) == 0:
            return []
        base = closes[0]
        curve = []
        for i in range(len(closes)):
            dt = pd.Timestamp(dates[i])
            date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dates[i])[:10].replace("-", "")
            curve.append({"date": date_str, "value": round(float(closes[i] / base), 6)})
        return curve

    def _get_benchmark_returns(self) -> list[float]:
        """获取基准收益率序列 - 按回测交易日对齐"""
        if not self.benchmark_date_index:
            return []
        returns = []
        prev_close = None
        for date_str in self.trade_dates:
            row = self.benchmark_date_index.get(date_str)
            if row:
                close = row["close"]
                if prev_close is not None and prev_close > 0:
                    returns.append((close - prev_close) / prev_close)
                else:
                    returns.append(0.0)
                prev_close = close
            else:
                returns.append(0.0)
        return returns

    def _format_trades(self) -> list[dict]:
        """格式化交易记录"""
        trades = []
        for o in self.filled_orders:
            trades.append({
                "date": o.filled_date,
                "ts_code": o.ts_code,
                "side": o.side.value,
                "amount": o.filled_amount,
                "price": round(o.filled_price, 4),
                "commission": round(o.commission, 2),
                "tax": round(o.tax, 2),
            })
        return trades
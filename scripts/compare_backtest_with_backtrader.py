from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import backtrader as bt
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connection import async_session
from database.models import Settings
from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from engine.backtest_engine import BacktestEngine
from strategy.builtin.bollinger_breakout import BOLLINGER_BREAKOUT_CODE
from strategy.builtin.macd_signal import MACD_SIGNAL_CODE
from strategy.builtin.ma_cross import MA_CROSS_CODE
from strategy.builtin.mean_reversion import MEAN_REVERSION_CODE
from strategy.builtin.momentum import MOMENTUM_CODE
from strategy.builtin.multi_factor import MULTI_FACTOR_CODE
from strategy.builtin.rsi_reversion import RSI_REVERSION_CODE
from strategy.builtin.turtle_trading import TURTLE_TRADING_CODE
from strategy.builtin.volume_price import VOLUME_PRICE_CODE
from strategy.strategy_loader import load_strategy


INITIAL_CASH = 1_000_000.0
MAX_POSITION_PCT = 0.25
DEFAULT_START = "20240101"
DEFAULT_END = "20250422"
DEFAULT_CODES = ["600519.SH", "601318.SH", "600036.SH", "600900.SH", "601166.SH"]
PICS_DIR = ROOT / "pics"
RESULT_TABLE = PICS_DIR / "backtest_builtin_strategy_parity.csv"
RESULT_CHART = PICS_DIR / "backtest_builtin_strategy_parity.png"
DIFF_CHART = PICS_DIR / "backtest_builtin_strategy_diff.png"


@dataclass
class StrategySpec:
    slug: str
    label: str
    code: str
    required_fields: set[str]


@dataclass
class CaseResult:
    strategy: str
    universe_size: int
    native_final: float
    native_return: float
    native_trades: int
    bt_final: float
    bt_return: float
    bt_trades: int
    return_diff: float
    final_diff: float


STRATEGIES: list[StrategySpec] = [
    StrategySpec("ma_cross", "MA Cross", MA_CROSS_CODE, set()),
    StrategySpec("macd_signal", "MACD Signal", MACD_SIGNAL_CODE, set()),
    StrategySpec("mean_reversion", "Mean Reversion", MEAN_REVERSION_CODE, set()),
    StrategySpec("momentum", "Momentum", MOMENTUM_CODE, set()),
    StrategySpec("rsi_reversion", "RSI Reversion", RSI_REVERSION_CODE, set()),
    StrategySpec("bollinger_breakout", "Bollinger Breakout", BOLLINGER_BREAKOUT_CODE, set()),
    StrategySpec("turtle_trading", "Turtle Trading", TURTLE_TRADING_CODE, set()),
    StrategySpec("volume_price", "Volume Price", VOLUME_PRICE_CODE, {"vol"}),
    StrategySpec("multi_factor", "Multi Factor", MULTI_FACTOR_CODE, {"vol"}),
]


def round_lot(amount: float | int) -> int:
    return max((int(amount) // 100) * 100, 0)


def line_values(line, count: int) -> list[float]:
    return [float(line[-offset]) for offset in range(count - 1, -1, -1)]


def ema_array(values: list[float], period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    result = np.empty_like(arr)
    result[0] = arr[0]
    alpha = 2.0 / (period + 1)
    for idx in range(1, len(arr)):
        result[idx] = arr[idx] * alpha + result[idx - 1] * (1 - alpha)
    return result


def rsi_wilder(values: list[float], period: int) -> float:
    prices = np.asarray(values, dtype=float)
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for idx in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


class ChinaAStockCommission(bt.CommInfoBase):
    params = (
        ("commission", 0.0003),
        ("stamp_duty", 0.001),
        ("transfer_fee", 0.00001),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec):
        value = abs(size) * price
        commission = max(value * self.p.commission, 5.0)
        transfer = value * self.p.transfer_fee
        stamp = value * self.p.stamp_duty if size < 0 else 0.0
        return commission + transfer + stamp


class BaseParityStrategy(bt.Strategy):
    params = dict(max_position_pct=MAX_POSITION_PCT)

    def __init__(self):
        self.completed_orders = 0

    def notify_order(self, order):
        if order.status == order.Completed:
            self.completed_orders += 1

    def buy_by_value(self, data, value: float):
        close_price = float(data.close[0])
        target_value = min(float(value), self.broker.getvalue() * self.p.max_position_pct)
        size = round_lot(target_value / close_price)
        if size > 0:
            self.buy(data=data, size=size)

    def exit_all(self, data):
        position = self.getposition(data)
        if position.size > 0:
            self.sell(data=data, size=position.size)

    def capped_buy_size(self, data, desired_size: int) -> int:
        desired_size = round_lot(desired_size)
        if desired_size <= 0:
            return 0
        close_price = float(data.close[0])
        max_size = round_lot((self.broker.getvalue() * self.p.max_position_pct) / close_price)
        if max_size <= 0:
            return 0
        return min(desired_size, max_size)

    def synced_datas(self):
        current_dt = self.datetime.date(0)
        for data in self.datas:
            if data.datetime.date(0) == current_dt:
                yield data


class MACrossBT(BaseParityStrategy):
    params = dict(short_period=5, long_period=20, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.long_period + 1:
                continue
            closes = line_values(data.close, self.p.long_period + 1)
            ma_short = float(np.mean(closes[-self.p.short_period:]))
            ma_long = float(np.mean(closes[-self.p.long_period:]))
            prev_closes = closes[:-1]
            ma_short_prev = float(np.mean(prev_closes[-self.p.short_period:]))
            ma_long_prev = float(np.mean(prev_closes[-self.p.long_period:]))
            position = self.getposition(data)
            if ma_short > ma_long and ma_short_prev <= ma_long_prev and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif ma_short < ma_long and ma_short_prev >= ma_long_prev and position.size > 0:
                self.exit_all(data)


class MACDSignalBT(BaseParityStrategy):
    params = dict(fast_period=12, slow_period=26, signal_period=9, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        need = self.p.slow_period + self.p.signal_period + 10
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.slow_period + self.p.signal_period:
                continue
            closes = line_values(data.close, min(len(data), need))
            ema_fast = ema_array(closes, self.p.fast_period)
            ema_slow = ema_array(closes, self.p.slow_period)
            dif = ema_fast - ema_slow
            dea = ema_array(dif.tolist(), self.p.signal_period)
            macd_cur = float(dif[-1] - dea[-1])
            macd_prev = float(dif[-2] - dea[-2])
            position = self.getposition(data)
            if macd_cur > 0 and macd_prev <= 0 and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif macd_cur < 0 and macd_prev >= 0 and position.size > 0:
                self.exit_all(data)


class MeanReversionBT(BaseParityStrategy):
    params = dict(lookback=20, entry_z=-1.0, exit_z=0.0, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.lookback:
                continue
            closes = line_values(data.close, self.p.lookback)
            mean = float(np.mean(closes))
            std = float(np.std(closes, ddof=1))
            if std == 0 or math.isnan(std):
                continue
            z_score = (closes[-1] - mean) / std
            position = self.getposition(data)
            if z_score < self.p.entry_z and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif z_score > self.p.exit_z and position.size > 0:
                self.exit_all(data)


class MomentumBT(BaseParityStrategy):
    params = dict(lookback=20, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.lookback + 1:
                continue
            closes = line_values(data.close, self.p.lookback + 1)
            momentum = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0.0
            position = self.getposition(data)
            if momentum > 0 and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif momentum < 0 and position.size > 0:
                self.exit_all(data)


class RSIReversionBT(BaseParityStrategy):
    params = dict(rsi_period=14, oversold=35, overbought=65, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.rsi_period + 1:
                continue
            closes = line_values(data.close, self.p.rsi_period + 2)
            rsi = rsi_wilder(closes, self.p.rsi_period)
            position = self.getposition(data)
            if rsi < self.p.oversold and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif rsi > self.p.overbought and position.size > 0:
                self.exit_all(data)


class BollingerBreakoutBT(BaseParityStrategy):
    params = dict(bb_period=20, bb_std=2.0, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.bb_period:
                continue
            closes = line_values(data.close, self.p.bb_period)
            mid = float(np.mean(closes))
            std = float(np.std(closes, ddof=1))
            upper = mid + self.p.bb_std * std
            current_price = closes[-1]
            position = self.getposition(data)
            if current_price > upper and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif current_price < mid and position.size > 0:
                self.exit_all(data)


class TurtleTradingBT(BaseParityStrategy):
    params = dict(entry_period=20, exit_period=10, atr_period=14, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        need = max(self.p.entry_period, self.p.atr_period) + 2
        for data in self.synced_datas():
            if len(data) < need:
                continue
            highs = line_values(data.high, need)
            lows = line_values(data.low, need)
            closes = line_values(data.close, need)
            upper = max(highs[:-1][-self.p.entry_period:])
            lower = min(lows[:-1][-self.p.exit_period:])
            tr_list = []
            for idx in range(1, len(closes)):
                tr = max(
                    highs[idx] - lows[idx],
                    abs(highs[idx] - closes[idx - 1]),
                    abs(lows[idx] - closes[idx - 1]),
                )
                tr_list.append(tr)
            atr = float(np.mean(tr_list[-self.p.atr_period:])) if tr_list else 1.0
            current_price = closes[-1]
            position = self.getposition(data)
            if current_price > upper and position.size == 0:
                risk_amount = self.broker.getvalue() * 0.02
                unit_size = round_lot(risk_amount / atr) if atr > 0 else 0
                if unit_size < 100:
                    unit_size = 100
                unit_size = self.capped_buy_size(data, unit_size)
                if unit_size > 0:
                    self.buy(data=data, size=unit_size)
            elif current_price < lower and position.size > 0:
                self.exit_all(data)


class VolumePriceBT(BaseParityStrategy):
    params = dict(ma_period=20, vol_ratio=1.2, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.ma_period + 1:
                continue
            closes = line_values(data.close, self.p.ma_period + 1)
            volumes = line_values(data.volume, self.p.ma_period + 1)
            ma = float(np.mean(closes[-self.p.ma_period:]))
            avg_vol = float(np.mean(volumes[:-1][-self.p.ma_period + 1:]))
            current_price = closes[-1]
            prev_price = closes[-2]
            current_vol = volumes[-1]
            is_breakout = current_price > ma and prev_price <= ma
            is_volume_surge = avg_vol > 0 and current_vol > avg_vol * self.p.vol_ratio
            position = self.getposition(data)
            if is_breakout and is_volume_surge and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif current_price < ma and position.size > 0:
                self.exit_all(data)


class MultiFactorBT(BaseParityStrategy):
    params = dict(lookback=20, buy_threshold=0.55, sell_threshold=0.45, max_position_pct=MAX_POSITION_PCT)

    def next(self):
        portfolio_value = self.broker.getvalue()
        for data in self.synced_datas():
            if len(data) < self.p.lookback + 1:
                continue
            closes = line_values(data.close, self.p.lookback + 1)
            volumes = line_values(data.volume, self.p.lookback + 1)
            prices = np.asarray(closes, dtype=float)
            momentum = (prices[-1] - prices[0]) / prices[0] if prices[0] else 0.0
            mom_score = 1.0 / (1.0 + math.exp(-momentum * 20))
            returns = np.diff(prices) / prices[:-1]
            vol = float(np.std(returns))
            vol_score = 1.0 / (1.0 + vol * 20)
            vol_arr = np.asarray(volumes, dtype=float)
            if len(vol_arr) >= 10:
                recent_vol = float(np.mean(vol_arr[-5:]))
                past_vol = float(np.mean(vol_arr[-10:-5]))
                vol_trend = recent_vol / past_vol if past_vol > 0 else 1.0
                vol_trend_score = min(vol_trend / 1.5, 1.0)
            else:
                vol_trend_score = 0.5
            score = mom_score * 0.5 + vol_score * 0.2 + vol_trend_score * 0.3
            position = self.getposition(data)
            if score > self.p.buy_threshold and position.size == 0:
                self.buy_by_value(data, portfolio_value * 0.3)
            elif score < self.p.sell_threshold and position.size > 0:
                self.exit_all(data)


BT_STRATEGY_MAP = {
    "ma_cross": MACrossBT,
    "macd_signal": MACDSignalBT,
    "mean_reversion": MeanReversionBT,
    "momentum": MomentumBT,
    "rsi_reversion": RSIReversionBT,
    "bollinger_breakout": BollingerBreakoutBT,
    "turtle_trading": TurtleTradingBT,
    "volume_price": VolumePriceBT,
    "multi_factor": MultiFactorBT,
}


async def load_settings() -> Settings:
    async with async_session() as db:
        return (await db.execute(select(Settings).where(Settings.id == 1))).scalar_one()


def build_cache(settings: Settings) -> DataCache:
    return DataCache(TushareClient(settings.tushare_token), mysql_conn=make_mysql_conn(settings))


def run_native(
    cache: DataCache,
    settings: Settings,
    spec: StrategySpec,
    codes: list[str],
    start_date: str,
    end_date: str,
) -> tuple[float, float, int]:
    init_func, handle_func = load_strategy(spec.code)
    engine = BacktestEngine(
        cache=cache,
        initial_cash=INITIAL_CASH,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        max_position_pct=MAX_POSITION_PCT,
        max_drawdown_limit=0.0,
    )
    result = engine.run(
        codes,
        start_date,
        end_date,
        init_func,
        handle_func,
        benchmark="000300.SH",
        required_fields=spec.required_fields,
    )
    if result.get("error"):
        raise RuntimeError(f"{spec.slug} native error: {result['error']}")
    metrics = result.get("metrics", {})
    return float(result.get("final_value", 0.0)), float(metrics.get("total_return", 0.0)), len(result.get("trades", []))


def load_bt_data(cache: DataCache, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = cache.get_daily(code, start_date, end_date, adj="qfq").copy()
    if df.empty:
        return df
    df = df.rename(columns={"vol": "volume"})
    df["openinterest"] = 0
    return df[["trade_date", "open", "high", "low", "close", "volume", "openinterest"]].set_index("trade_date")


def run_backtrader(
    cache: DataCache,
    settings: Settings,
    spec: StrategySpec,
    codes: list[str],
    start_date: str,
    end_date: str,
) -> tuple[float, float, int]:
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.broker.set_slippage_perc(
        perc=settings.slippage,
        slip_open=True,
        slip_match=True,
        slip_limit=True,
        slip_out=False,
    )
    cerebro.broker.addcommissioninfo(
        ChinaAStockCommission(
            commission=settings.commission_rate,
            stamp_duty=settings.stamp_tax_rate,
        )
    )
    added = 0
    for code in codes:
        df = load_bt_data(cache, code, start_date, end_date)
        if df.empty:
            continue
        cerebro.adddata(bt.feeds.PandasData(dataname=df), name=code)
        added += 1
    if added == 0:
        return INITIAL_CASH, 0.0, 0
    strategy_cls = BT_STRATEGY_MAP[spec.slug]
    strategies = cerebro.addstrategy(strategy_cls)
    results = cerebro.run()
    strat = results[0] if results else None
    final_value = float(cerebro.broker.getvalue())
    total_return = (final_value / INITIAL_CASH - 1) * 100
    trades = int(getattr(strat, "completed_orders", 0)) if strat is not None else 0
    return final_value, total_return, trades


async def run_case(
    settings: Settings,
    cache: DataCache,
    spec: StrategySpec,
    codes: list[str],
    start_date: str,
    end_date: str,
) -> CaseResult:
    native_final, native_return, native_trades = run_native(cache, settings, spec, codes, start_date, end_date)
    bt_final, bt_return, bt_trades = run_backtrader(cache, settings, spec, codes, start_date, end_date)
    return CaseResult(
        strategy=spec.label,
        universe_size=len(codes),
        native_final=round(native_final, 2),
        native_return=round(native_return, 2),
        native_trades=native_trades,
        bt_final=round(bt_final, 2),
        bt_return=round(bt_return, 2),
        bt_trades=bt_trades,
        return_diff=round(native_return - bt_return, 2),
        final_diff=round(native_final - bt_final, 2),
    )


def configure_plot_style():
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.dpi"] = 160


def save_charts(results: list[CaseResult]):
    PICS_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([asdict(item) for item in results])
    df.to_csv(RESULT_TABLE, index=False, encoding="utf-8-sig")

    configure_plot_style()
    x = np.arange(len(df))
    width = 0.36

    fig, ax = plt.subplots(figsize=(14, 7))
    bars_native = ax.bar(x - width / 2, df["native_return"], width=width, label="QTsys", color="#2563eb")
    bars_bt = ax.bar(x + width / 2, df["bt_return"], width=width, label="Backtrader", color="#0f766e")
    ax.set_xticks(x)
    ax.set_xticklabels(df["strategy"], rotation=20, ha="right")
    ax.set_ylabel("Total Return (%)")
    ax.set_title("Builtin Strategy Backtest Parity: QTsys vs Backtrader")
    ax.legend()
    for bar in list(bars_native) + list(bars_bt):
        height = bar.get_height()
        ax.annotate(
            f"{height:.2f}",
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, 3 if height >= 0 else -14),
            textcoords="offset points",
            ha="center",
            va="bottom" if height >= 0 else "top",
            fontsize=8,
        )
    fig.tight_layout()
    fig.savefig(RESULT_CHART, bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
    diff_colors = ["#dc2626" if abs(value) > 1 else "#16a34a" for value in df["return_diff"]]
    axes[0].bar(x, df["return_diff"], color=diff_colors)
    axes[0].axhline(0, color="#111827", linewidth=1)
    axes[0].set_ylabel("Return Diff (%)")
    axes[0].set_title("Return Difference and Final Equity Difference")
    for idx, value in enumerate(df["return_diff"]):
        axes[0].annotate(f"{value:.2f}", xy=(idx, value), xytext=(0, 4 if value >= 0 else -14), textcoords="offset points", ha="center", fontsize=8)

    final_diff_colors = ["#dc2626" if abs(value) > 15000 else "#16a34a" for value in df["final_diff"]]
    axes[1].bar(x, df["final_diff"], color=final_diff_colors)
    axes[1].axhline(0, color="#111827", linewidth=1)
    axes[1].set_ylabel("Final Equity Diff")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(df["strategy"], rotation=20, ha="right")
    for idx, value in enumerate(df["final_diff"]):
        axes[1].annotate(f"{value:.0f}", xy=(idx, value), xytext=(0, 4 if value >= 0 else -14), textcoords="offset points", ha="center", fontsize=8)

    fig.tight_layout()
    fig.savefig(DIFF_CHART, bbox_inches="tight")
    plt.close(fig)


async def main():
    parser = argparse.ArgumentParser(description="Compare QTsys builtin strategies against Backtrader on real market data.")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--codes", nargs="*", default=DEFAULT_CODES)
    parser.add_argument("--strategies", nargs="*", default=[item.slug for item in STRATEGIES])
    args = parser.parse_args()

    strategy_lookup = {item.slug: item for item in STRATEGIES}
    selected = [strategy_lookup[name] for name in args.strategies if name in strategy_lookup]
    if not selected:
        raise SystemExit("No valid strategies selected.")

    settings = await load_settings()
    cache = build_cache(settings)

    results: list[CaseResult] = []
    for spec in selected:
        print(f"[RUN] {spec.label}  | codes={len(args.codes)} | {args.start}-{args.end}")
        results.append(await run_case(settings, cache, spec, args.codes, args.start, args.end))

    save_charts(results)

    print("strategy,native_return,bt_return,return_diff,native_trades,bt_trades")
    for item in results:
        print(
            f"{item.strategy},{item.native_return:.2f},{item.bt_return:.2f},{item.return_diff:.2f},"
            f"{item.native_trades},{item.bt_trades}"
        )
    print(f"[OK] table={RESULT_TABLE}")
    print(f"[OK] chart={RESULT_CHART}")
    print(f"[OK] diff_chart={DIFF_CHART}")


if __name__ == "__main__":
    asyncio.run(main())

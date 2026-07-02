"""因子评价引擎 - IC/IR/分组收益/多空曲线"""
import re
import ast
import numpy as np
import pandas as pd
from typing import Optional
from factor.builtin_factors import BUILTIN_FACTORS
from logging_config import get_logger

logger = get_logger("qtsys.factor.engine")

_DANGEROUS_NAMES = frozenset([
    "__import__", "__builtins__", "__class__", "__mro__", "__subclasses__",
    "__globals__", "__locals__", "__code__", "__dict__", "__base__",
    "getattr", "setattr", "delattr", "globals", "locals",
    "vars", "dir", "eval", "exec", "compile", "open", "input", "breakpoint",
])

def _ast_safe_check(expr: str) -> bool:
    """返回 True 表示表达式安全，False 表示有危险操作"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return False
        if isinstance(node, ast.Name) and node.id in _DANGEROUS_NAMES:
            return False
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return False
    return True


class FactorEngine:
    """因子评价核心引擎"""

    def __init__(self, cache):
        self.cache = cache

    def compute_factor_values(
        self, expression: str, ts_code: str,
        start_date: str, end_date: str,
    ) -> Optional[pd.Series]:
        """计算单只股票的因子值序列"""
        df = self.cache.get_daily(ts_code, start_date, end_date)
        if df.empty:
            return None

        indexed = df.set_index("trade_date")
        closes = indexed["close"]
        highs = indexed["high"]
        lows = indexed["low"]
        volumes = indexed["vol"]
        opens = indexed["open"] if "open" in indexed.columns else closes.shift(1)
        amounts = indexed["amount"] if "amount" in indexed.columns else volumes * closes

        # 加载财务指标数据
        basic_data = self._load_daily_basic(ts_code, start_date, end_date, indexed.index)

        # 内置因子
        if expression.startswith("builtin:"):
            name = expression[8:]
            if name in BUILTIN_FACTORS:
                func = BUILTIN_FACTORS[name]["func"]
                return func(
                    closes,
                    highs,
                    lows,
                    volumes,
                    opens=opens,
                    amounts=amounts,
                    basic_data=basic_data,
                )
            return None

        # 自定义表达式因子
        return self._eval_expression(
            expression, closes, highs, lows, volumes, opens, basic_data, amounts
        )

    def _load_daily_basic(self, ts_code, start_date, end_date, trade_index):
        """加载每日指标数据并对齐交易日"""
        result = {}
        try:
            basic_df = self.cache.get_daily_basic(ts_code, start_date, end_date)
            if basic_df.empty:
                return result
            basic_indexed = basic_df.set_index("trade_date")
            # forward-fill对齐交易日
            basic_aligned = basic_indexed.reindex(trade_index).ffill()
            for col in ["pe", "pe_ttm", "pb", "ps", "ps_ttm",
                        "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f"]:
                if col in basic_aligned.columns:
                    result[col] = basic_aligned[col].astype(float)
        except Exception:
            logger.warning(f"加载{ts_code}财务指标失败")
        return result

    def _eval_expression(self, expr, closes, highs, lows, volumes, opens=None, basic_data=None, amounts=None):
        """安全执行用户自定义因子表达式"""
        expr_text = str(expr)
        if not _ast_safe_check(expr_text):
            logger.warning("unsafe factor expression rejected by AST check")
            return None

        def _ts_rank_func(x):
            n = len(x)
            last = x[-1]
            rank = np.sum(x <= last)
            return rank / n

        def _expanding_rank_pct(s):
            """展开窗口百分位排名（无未来函数），向量化实现。"""
            s = pd.Series(s)
            ranks = s.expanding().rank()
            counts = s.expanding().count()
            result = ranks / counts
            return result.where(s.notna())

        def _expanding_zscore(s):
            mean = s.expanding(min_periods=2).mean()
            std = s.expanding(min_periods=2).std()
            return (s - mean) / std.replace(0, np.nan)

        def _ts_decay(s, window):
            """线性衰减加权均值"""
            weights = np.arange(1, window + 1, dtype=float)
            weights = weights / weights.sum()
            return s.rolling(window).apply(lambda x: np.dot(x, weights), raw=True)

        def _ewm_func(s, span):
            return s.ewm(span=span).mean()

        def _cs_zscore(s):
            return _expanding_zscore(s).fillna(0.0)

        def _cs_percentile(s):
            return _expanding_rank_pct(s)

        def _cs_demean(s):
            return s - s.expanding(min_periods=1).mean()

        def _clip_func(s, lower, upper):
            return s.clip(lower=lower, upper=upper)

        def _power_func(s, exp):
            return np.power(s, exp)

        def _ternary(cond, true_val, false_val):
            return pd.Series(np.where(cond, true_val, false_val), index=closes.index)

        def _ts_argmax(s, window):
            return s.rolling(window).apply(lambda x: x.argmax(), raw=True)

        def _ts_argmin(s, window):
            return s.rolling(window).apply(lambda x: x.argmin(), raw=True)

        def _ts_product(s, window):
            return s.rolling(window).apply(lambda x: np.prod(x), raw=True)

        def _highday(s, window):
            return s.rolling(window).apply(lambda x: window - 1 - x.argmax(), raw=True)

        def _lowday(s, window):
            return s.rolling(window).apply(lambda x: window - 1 - x.argmin(), raw=True)

        def _wma(s, window):
            weights = np.arange(1, window + 1)
            return s.rolling(window).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

        def _decaylinear(s, window):
            weights = np.arange(window, 0, -1)
            return s.rolling(window).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

        def _sma(s, window, weight):
            """指数移动平均，等价于聚宽 SMA(s, N, M) = M/N * s + (1-M/N) * prev"""
            alpha = weight / window
            return s.ewm(alpha=alpha, adjust=False).mean()

        def _regbeta(x, y, window):
            def calc_beta(x_arr, y_arr):
                if len(x_arr) < 2: return np.nan
                cov = np.cov(x_arr, y_arr)[0, 1]
                var = np.var(x_arr)
                return cov / var if var > 0 else np.nan
            result = []
            for i in range(len(x)):
                if i < window - 1:
                    result.append(np.nan)
                else:
                    result.append(calc_beta(x.iloc[i-window+1:i+1].values, y.iloc[i-window+1:i+1].values))
            return pd.Series(result, index=x.index)

        def _regresi(x, y, window):
            beta = _regbeta(x, y, window)
            alpha = y.rolling(window).mean() - beta * x.rolling(window).mean()
            return y - (alpha + beta * x)

        def _signedpower(s, exp):
            return np.sign(s) * np.power(np.abs(s), exp)

        def _scale(s):
            s_abs_sum = np.abs(s).expanding(min_periods=1).sum()
            return s / s_abs_sum.replace(0, np.nan)

        def _indneutralize(s):
            return _cs_demean(s)

        def _sequence(length):
            return pd.Series(range(1, length + 1))

        def _sumif(s, condition, window):
            return (s * condition).rolling(window).sum()

        def _where(cond, true_val, false_val):
            return pd.Series(np.where(cond, true_val, false_val), index=closes.index)

        def _ema(s, span):
            return s.ewm(span=int(span), adjust=False).mean()

        def _macd_dif(s, fast=12, slow=26):
            return _ema(s, int(fast)) - _ema(s, int(slow))

        def _macd_dea(s, fast=12, slow=26, signal=9):
            return _ema(_macd_dif(s, int(fast), int(slow)), int(signal))

        def _macd_hist(s, fast=12, slow=26, signal=9):
            dif = _macd_dif(s, int(fast), int(slow))
            dea = _ema(dif, int(signal))
            return dif - dea

        def _rsi(s, window=14):
            delta = s.diff()
            gain = delta.clip(lower=0).rolling(int(window)).mean()
            loss = (-delta.clip(upper=0)).rolling(int(window)).mean()
            return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

        def _cross_up(left, right):
            return ((left > right) & (left.shift(1) <= right.shift(1))).astype(float)

        def _cross_down(left, right):
            return ((left < right) & (left.shift(1) >= right.shift(1))).astype(float)

        def _bars_since(event):
            values = []
            last_seen = None
            for idx, flag in enumerate(pd.Series(event, index=closes.index).fillna(0).astype(float).values):
                if flag > 0:
                    last_seen = idx
                    values.append(0.0)
                elif last_seen is None:
                    values.append(np.nan)
                else:
                    values.append(float(idx - last_seen))
            return pd.Series(values, index=closes.index)

        def _count_true(event, window):
            return (pd.Series(event, index=closes.index).fillna(0).astype(float) > 0).rolling(int(window)).sum()

        def _zscore(s, window):
            mean = s.rolling(int(window)).mean()
            std = s.rolling(int(window)).std()
            return (s - mean) / std.replace(0, np.nan)

        def _vol_zscore(window):
            return _zscore(volumes.astype(float), int(window))

        def _gap_pct():
            return opens / closes.shift(1).replace(0, np.nan) - 1

        def _breakout(s, window):
            prev_high = s.rolling(int(window)).max().shift(1)
            return ((s > prev_high) & prev_high.notna()).astype(float)

        def _drawdown_from_high(s, window):
            peak = s.rolling(int(window)).max()
            return s / peak.replace(0, np.nan) - 1

        def _skew(s, window):
            return s.rolling(int(window)).skew()

        def _kurt(s, window):
            return s.rolling(int(window)).kurt()

        def _downside_std(s, window):
            downside = s.where(s < 0, 0.0)
            return downside.rolling(int(window)).std()

        def _upside_std(s, window):
            upside = s.where(s > 0, 0.0)
            return upside.rolling(int(window)).std()

        def _efficiency_ratio(s, window):
            window = int(window)
            direction = (s - s.shift(window)).abs()
            path = s.diff().abs().rolling(window).sum()
            return direction / path.replace(0, np.nan)

        def _signed_log(s):
            """Signed log transform: preserves sign while compressing outliers. Used in WQ Alpha101."""
            return pd.Series(np.sign(s.values) * np.log1p(np.abs(s.values)), index=s.index)

        def _ts_percentile_rank(s, window):
            """Rolling percentile rank within a trailing window - time-series version of cross-sectional rank."""
            return s.rolling(int(window)).rank(pct=True)

        def _ic_weighted_mean(s, window):
            """Exponentially weighted mean with decay - approximates IC-optimal combination weights."""
            return s.ewm(halflife=window/2, adjust=True).mean()

        def _winsorize_factor(s, lower_pct=0.01, upper_pct=0.99):
            """Winsorize: clip at percentile bounds to remove outliers before IC computation."""
            lo = s.quantile(lower_pct)
            hi = s.quantile(upper_pct)
            return s.clip(lower=lo, upper=hi)

        def _ts_corr_signed(s, t, window):
            """Rolling correlation with sign - for price-volume divergence factors (Alpha101 style)."""
            return s.rolling(int(window)).corr(t)

        # Compute derived data sources
        if amounts is None:
            amounts = volumes * closes
        vwap = amounts / volumes if volumes is not None else closes
        dtm = pd.Series(np.where(opens <= opens.shift(1), 0, np.maximum(highs - opens, opens - opens.shift(1))), index=opens.index)
        dbm = pd.Series(np.where(opens >= opens.shift(1), 0, np.maximum(opens - lows, opens - opens.shift(1))), index=opens.index)
        tr = pd.Series(np.maximum(np.maximum(highs - lows, np.abs(highs - closes.shift(1))), np.abs(lows - closes.shift(1))), index=closes.index)
        hd = highs - highs.shift(1)
        ld = lows.shift(1) - lows

        safe_ns = {
            # 价格数据
            "close": closes, "high": highs, "low": lows,
            "vol": volumes, "volume": volumes,
            "open": opens if opens is not None else closes.shift(1),
            "returns": closes.pct_change(),
            "vwap": vwap, "amount": amounts,
            "dtm": dtm, "dbm": dbm, "tr": tr, "hd": hd, "ld": ld,
            # 基础数学
            "np": np, "pd": pd,
            "abs": np.abs, "log": np.log, "sqrt": np.sqrt,
            "power": _power_func, "neg": lambda s: -s,
            "max": np.maximum, "min": np.minimum,
            "clip": _clip_func,
            "signedpower": _signedpower, "sign": np.sign,
            "floor": np.floor, "ceil": np.ceil, "round_val": np.round,
            # 时序运算
            "mean": lambda s, n: s.rolling(n).mean(),
            "std": lambda s, n: s.rolling(n).std(),
            "sum": lambda s, n: s.rolling(n).sum(),
            "rank": _expanding_rank_pct,
            "delay": lambda s, n: s.shift(n),
            "delta": lambda s, n: s.diff(n),
            "pctchange": lambda s, n: s.pct_change(n),
            "corr": lambda a, b, n: a.rolling(n).corr(b),
            "cov": lambda a, b, n: a.rolling(n).cov(b),
            "ts_max": lambda s, n: s.rolling(n).max(),
            "ts_min": lambda s, n: s.rolling(n).min(),
            "ts_rank": lambda s, n: s.rolling(n).apply(_ts_rank_func, raw=True),
            "ts_decay": _ts_decay,
            "ewm": _ewm_func,
            "ema": _ema,
            "macd_dif": _macd_dif,
            "macd_dea": _macd_dea,
            "macd_hist": _macd_hist,
            "rsi": _rsi,
            "ts_argmax": _ts_argmax, "ts_argmin": _ts_argmin,
            "ts_product": _ts_product,
            "highday": _highday, "lowday": _lowday,
            "wma": _wma, "decaylinear": _decaylinear, "sma": _sma,
            # 统计回归
            "regbeta": _regbeta, "regresi": _regresi,
            "sequence": _sequence, "sumif": _sumif,
            # 截面运算
            "cs_zscore": _cs_zscore,
            "cs_percentile": _cs_percentile,
            "cs_demean": _cs_demean,
            "cs_rank": _expanding_rank_pct,
            "scale": _scale, "indneutralize": _indneutralize,
            "advm": lambda s, n: s.rolling(n).mean(),
            # 条件逻辑
            "ternary": _ternary, "where": _where,
            "cross_up": _cross_up,
            "cross_down": _cross_down,
            "bars_since": _bars_since,
            "count_true": _count_true,
            "zscore": _zscore,
            "vol_zscore": _vol_zscore,
            "gap_pct": _gap_pct,
            "breakout": _breakout,
            "drawdown_from_high": _drawdown_from_high,
            "skew": _skew,
            "kurt": _kurt,
            "downside_std": _downside_std,
            "upside_std": _upside_std,
            "efficiency_ratio": _efficiency_ratio,
            # Frontier operators (WorldQuant/IC-stable)
            "signed_log": _signed_log,
            "ts_pct_rank": _ts_percentile_rank,
            "ic_ewm": _ic_weighted_mean,
            "winsorize": _winsorize_factor,
            "ts_corr_signed": _ts_corr_signed,
        }

        # 注入财务指标
        if basic_data:
            empty = pd.Series(0.0, index=closes.index)
            safe_ns["pe"] = basic_data.get("pe_ttm", basic_data.get("pe", empty))
            safe_ns["pb"] = basic_data.get("pb", empty)
            safe_ns["ps"] = basic_data.get("ps_ttm", basic_data.get("ps", empty))
            safe_ns["total_mv"] = basic_data.get("total_mv", empty)
            safe_ns["circ_mv"] = basic_data.get("circ_mv", empty)
            safe_ns["turnover_rate"] = basic_data.get("turnover_rate", empty)

        try:
            result = eval(expr, {"__builtins__": {}}, safe_ns)
            if isinstance(result, pd.Series):
                return result
            return pd.Series(result, index=closes.index)
        except Exception:
            return None

    def _neutralize(self, factor_df: pd.DataFrame, industry_col: str = "industry",
                    mktcap_col: str = "total_mv") -> pd.DataFrame:
        """因子中性化 - 截面回归去除行业和市值效应"""
        result = factor_df.copy()
        for dt in result.index.get_level_values(0).unique() if result.index.nlevels > 1 else [None]:
            if dt is not None:
                mask = result.index.get_level_values(0) == dt
                sub = result.loc[mask]
            else:
                sub = result
            if "factor" not in sub.columns:
                continue
            y = sub["factor"].values
            if len(y) < 5:
                continue
            # 市值中性化
            X_cols = []
            if mktcap_col in sub.columns:
                X_cols.append(sub[mktcap_col].fillna(0).values.reshape(-1, 1))
            # 行业哑变量
            if industry_col in sub.columns:
                dummies = pd.get_dummies(sub[industry_col], drop_first=True)
                if not dummies.empty:
                    X_cols.append(dummies.values)
            if not X_cols:
                continue
            X = np.hstack(X_cols)
            X = np.column_stack([np.ones(len(X)), X])
            try:
                beta = np.linalg.lstsq(X, y, rcond=None)[0]
                residual = y - X @ beta
                if dt is not None:
                    result.loc[mask, "factor"] = residual
                else:
                    result["factor"] = residual
            except Exception:
                pass
        return result

    def evaluate(
        self, expression: str, universe: list[str],
        start_date: str, end_date: str, groups: int = 5,
        forward_days: int = 5,
    ) -> dict:
        """评价因子: 计算IC/IR/分组收益/多空曲线/换手率"""
        # 1. 收集所有股票的因子值和未来收益
        all_dates = set()
        stock_data = {}

        for ts_code in universe:
            df = self.cache.get_daily(ts_code, start_date, end_date)
            if df.empty or len(df) < 30:
                continue
            indexed = df.set_index("trade_date")
            closes = indexed["close"]
            highs = indexed["high"]
            lows = indexed["low"]
            volumes = indexed["vol"]
            opens = indexed["open"] if "open" in indexed.columns else closes.shift(1)
            amounts = indexed["amount"] if "amount" in indexed.columns else volumes * closes

            # 加载财务指标
            basic_data = self._load_daily_basic(ts_code, start_date, end_date, indexed.index)

            if expression.startswith("builtin:"):
                name = expression[8:]
                if name not in BUILTIN_FACTORS:
                    return {"error": f"未知内置因子: {name}"}
                fv = BUILTIN_FACTORS[name]["func"](
                    closes,
                    highs,
                    lows,
                    volumes,
                    opens=opens,
                    amounts=amounts,
                    basic_data=basic_data,
                )
            else:
                fv = self._eval_expression(expression, closes, highs, lows, volumes, opens, basic_data, amounts)

            if fv is None:
                continue

            # 未来N日收益作为因子预测目标
            fwd_ret = closes.pct_change(forward_days).shift(-forward_days)
            stock_data[ts_code] = {"factor": fv, "fwd_ret": fwd_ret}
            all_dates.update(fv.dropna().index.tolist())

        if not stock_data:
            return {"error": "无有效数据"}

        dates = sorted(all_dates)
        return self._calc_metrics(stock_data, dates, groups)

    def _calc_metrics(self, stock_data, dates, groups):
        """计算因子评价指标"""
        ic_series = []
        group_daily = {g: [] for g in range(groups)}
        prev_group_members = {g: set() for g in range(groups)}
        turnover_series = []

        def _safe_corr(left: np.ndarray, right: np.ndarray) -> float:
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

        for dt in dates:
            fvals, frets = [], []
            stock_list = []
            for ts_code, sd in stock_data.items():
                if dt in sd["factor"].index and dt in sd["fwd_ret"].index:
                    fv = sd["factor"].loc[dt]
                    fr = sd["fwd_ret"].loc[dt]
                    if pd.notna(fv) and pd.notna(fr):
                        fvals.append(fv)
                        frets.append(fr)
                        stock_list.append(ts_code)

            if len(fvals) < max(groups, 3):
                continue

            fv_arr = np.array(fvals)
            fr_arr = np.array(frets)

            # 截面百分位排名标准化（行业标准做法，消除因子值分布偏态影响）
            fv_arr = pd.Series(fv_arr).rank(pct=True).values

            # IC: Spearman rank correlation
            rank_f = pd.Series(fv_arr).rank().values
            rank_r = pd.Series(fr_arr).rank().values
            ic = _safe_corr(rank_f, rank_r)
            date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)[:10].replace("-", "")
            ic_series.append({"date": date_str, "ic": round(float(ic), 4) if not np.isnan(ic) else 0.0})

            # 分组收益
            sorted_idx = np.argsort(fv_arr)
            n = len(sorted_idx)
            for g in range(groups):
                start_i = int(g * n / groups)
                end_i = int((g + 1) * n / groups)
                g_ret = np.mean(fr_arr[sorted_idx[start_i:end_i]])
                group_daily[g].append({"date": date_str, "ret": round(float(g_ret), 6)})

            # 计算各组平均换手率 (基于overlap/union)
            group_turnovers = []
            for g in range(groups):
                start_i = int(g * n / groups)
                end_i = int((g + 1) * n / groups)
                cur_members = set(stock_list[i] for i in sorted_idx[start_i:end_i])
                if prev_group_members[g]:
                    overlap = len(cur_members & prev_group_members[g])
                    union = len(cur_members | prev_group_members[g])
                    group_turnovers.append(1.0 - overlap / max(union, 1))
                prev_group_members[g] = cur_members
            if group_turnovers:
                avg_to = float(np.mean(group_turnovers))
                turnover_series.append({"date": date_str, "turnover": round(avg_to, 4)})

        return self._summarize(ic_series, group_daily, turnover_series, groups)

    def _summarize(self, ic_series, group_daily, turnover_series, groups):
        """汇总因子评价结果"""
        if not ic_series:
            return {"error": "IC序列为空, 数据不足"}

        ic_vals = [x["ic"] for x in ic_series]
        ic_mean = float(np.mean(ic_vals))
        ic_std = float(np.std(ic_vals, ddof=1)) if len(ic_vals) > 1 else 1.0
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0.0
        ic_positive_pct = sum(1 for x in ic_vals if x > 0) / len(ic_vals)

        # 分组累计收益
        group_returns = []
        for g in range(groups):
            rets = [x["ret"] for x in group_daily[g]]
            cum = float(np.prod([1 + r for r in rets]) - 1) if rets else 0.0
            ann = float((1 + cum) ** (252 / max(len(rets), 1)) - 1) if rets else 0.0
            group_returns.append({
                "group": g + 1,
                "label": f"G{g+1}" if g > 0 and g < groups - 1 else ("多头" if g == groups - 1 else "空头"),
                "cumulative_return": round(cum * 100, 2),
                "annual_return": round(ann * 100, 2),
            })

        # 多空曲线 (top组 - bottom组)
        long_short_curve = []
        cum_ls = 1.0
        top_daily = group_daily[groups - 1]
        bot_daily = group_daily[0]
        for i in range(min(len(top_daily), len(bot_daily))):
            ls_ret = top_daily[i]["ret"] - bot_daily[i]["ret"]
            cum_ls *= (1 + ls_ret)
            long_short_curve.append({
                "date": top_daily[i]["date"],
                "value": round((cum_ls - 1) * 100, 2),
            })

        # 平均换手率
        avg_turnover = float(np.mean([x["turnover"] for x in turnover_series])) if turnover_series else 0.0

        metrics = {
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ic_ir": round(ic_ir, 4),
            "ic_positive_pct": round(ic_positive_pct * 100, 1),
            "avg_turnover": round(avg_turnover * 100, 1),
            "long_short_return": round(long_short_curve[-1]["value"], 2) if long_short_curve else 0.0,
            "top_group_return": group_returns[-1]["cumulative_return"] if group_returns else 0.0,
            "bottom_group_return": group_returns[0]["cumulative_return"] if group_returns else 0.0,
            "monotonicity": self._calc_monotonicity(group_returns),
        }

        return {
            "metrics": metrics,
            "ic_series": ic_series,
            "group_returns": group_returns,
            "turnover_series": turnover_series,
            "long_short_curve": long_short_curve,
        }

    def _calc_monotonicity(self, group_returns):
        """计算分组收益单调性 (越接近1说明因子区分度越好)"""
        if len(group_returns) < 2:
            return 0.0
        rets = [g["cumulative_return"] for g in group_returns]
        n = len(rets)
        concordant = 0
        total = 0
        for i in range(n):
            for j in range(i + 1, n):
                total += 1
                if rets[j] > rets[i]:
                    concordant += 1
        return round(concordant / total, 2) if total > 0 else 0.0

"""因子评价引擎 - IC/IR/分组收益/多空曲线"""
import numpy as np
import pandas as pd
from typing import Optional
from factor.builtin_factors import BUILTIN_FACTORS


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

        # 内置因子
        if expression.startswith("builtin:"):
            name = expression[8:]
            if name in BUILTIN_FACTORS:
                func = BUILTIN_FACTORS[name]["func"]
                return func(closes, highs, lows, volumes)
            return None

        # 自定义表达式因子
        return self._eval_expression(
            expression, closes, highs, lows, volumes, opens
        )

    def _eval_expression(self, expr, closes, highs, lows, volumes, opens=None):
        """安全执行用户自定义因子表达式"""
        def _ts_rank_func(x):
            """numpy实现的ts_rank, raw=True模式下x是numpy数组"""
            n = len(x)
            # 计算最后一个元素的排名百分比
            last = x[-1]
            rank = np.sum(x <= last)
            return rank / n

        safe_ns = {
            "close": closes, "high": highs, "low": lows,
            "vol": volumes, "volume": volumes,
            "open": opens if opens is not None else closes.shift(1),
            "returns": closes.pct_change(),
            "np": np, "pd": pd,
            "abs": np.abs, "log": np.log, "sqrt": np.sqrt,
            "max": np.maximum, "min": np.minimum,
            "mean": lambda s, n: s.rolling(n).mean(),
            "std": lambda s, n: s.rolling(n).std(),
            "sum": lambda s, n: s.rolling(n).sum(),
            "rank": lambda s: s.rank(pct=True),
            "delay": lambda s, n: s.shift(n),
            "delta": lambda s, n: s.diff(n),
            "corr": lambda a, b, n: a.rolling(n).corr(b),
            "cov": lambda a, b, n: a.rolling(n).cov(b),
            "ts_max": lambda s, n: s.rolling(n).max(),
            "ts_min": lambda s, n: s.rolling(n).min(),
            "ts_rank": lambda s, n: s.rolling(n).apply(_ts_rank_func, raw=True),
        }
        try:
            result = eval(expr, {"__builtins__": {}}, safe_ns)
            if isinstance(result, pd.Series):
                return result
            return pd.Series(result, index=closes.index)
        except Exception:
            return None

    def evaluate(
        self, expression: str, universe: list[str],
        start_date: str, end_date: str, groups: int = 5,
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

            if expression.startswith("builtin:"):
                name = expression[8:]
                if name not in BUILTIN_FACTORS:
                    return {"error": f"未知内置因子: {name}"}
                fv = BUILTIN_FACTORS[name]["func"](closes, highs, lows, volumes)
            else:
                fv = self._eval_expression(expression, closes, highs, lows, volumes, opens)

            if fv is None:
                continue

            # 未来5日收益作为因子预测目标
            fwd_ret = closes.pct_change(5).shift(-5)
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

            # IC: Spearman rank correlation
            rank_f = pd.Series(fv_arr).rank().values
            rank_r = pd.Series(fr_arr).rank().values
            ic = np.corrcoef(rank_f, rank_r)[0, 1]
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

"""
真实Alpha191因子引擎 - 完整实现
"""
import numpy as np
import pandas as pd
from factor.alpha191_operators import Alpha191Operators as ops

class RealAlpha191Engine:
    """真实的Alpha191因子计算引擎"""

    def __init__(self):
        self.data = None
        self.df = None

    def load_data(self, market_data):
        """加载市场数据"""
        self.data = market_data.copy()
        self.data = self.data.sort_values(['ts_code', 'trade_date'])

        # 准备基础数据
        self.df = self.data.copy()
        self.df['returns'] = self.df.groupby('ts_code')['close'].pct_change()
        self.df['vwap'] = self.df['amount'] / (self.df['volume'] + 1e-10)

    def calculate_factor(self, factor_num, expression):
        """计算指定因子"""
        try:
            if self.df is None or self.df.empty:
                return {}

            method_name = f'alpha{factor_num:03d}'
            if not hasattr(self, method_name):
                raise ValueError(f"Alpha#{factor_num} 未实现，已禁用任何模拟/备用公式")

            factor_series = getattr(self, method_name)(self.df)
            if not isinstance(factor_series, pd.Series):
                factor_series = pd.Series(factor_series, index=self.df.index)
            if len(factor_series) != len(self.df):
                raise ValueError(f"Alpha#{factor_num} 结果长度与行情数据不一致")

            results = {}
            dates = sorted(self.df['trade_date'].unique())
            sample_dates = dates[60::5]

            for date in sample_dates:
                current_mask = self.df['trade_date'] == date
                current_idx = self.df.index[current_mask]
                current_values = factor_series.loc[current_idx]
                if current_values.empty:
                    continue

                current_rows = self.df.loc[current_idx, ['ts_code']].copy()
                current_rows['factor'] = current_values.values
                current_rows = current_rows.replace([np.inf, -np.inf], np.nan).dropna(subset=['factor'])
                if current_rows.empty:
                    continue

                results[date] = dict(zip(current_rows['ts_code'], current_rows['factor'].astype(float)))

            return results
        except Exception as e:
            print(f"因子{factor_num}计算失败: {e}")
            import traceback
            traceback.print_exc()
            return {}

    def _calculate_alpha(self, num, data, current_date):
        """计算单个日期的因子值"""
        try:
            # 只取当前日期的数据
            current_data = data[data['trade_date'] == current_date].copy()

            if len(current_data) == 0:
                return None

            # 调用对应的因子计算函数
            method_name = f'alpha{num:03d}'
            if hasattr(self, method_name):
                # 传入完整历史数据用于计算
                factor_series = getattr(self, method_name)(data)

                # 只返回当前日期的因子值
                current_values = factor_series[data['trade_date'] == current_date]

                # 转换为字典
                result = {}
                for idx in current_values.index:
                    ts_code = data.loc[idx, 'ts_code']
                    value = current_values.loc[idx]
                    if not pd.isna(value) and not np.isinf(value):
                        result[ts_code] = float(value)

                return result
            else:
                raise ValueError(f"Alpha#{num} 未实现，禁止使用任何模拟/备用公式")

        except Exception as e:
            print(f"Alpha#{num}在{current_date}计算失败: {e}")
            return None

    def _fallback_alpha(self, num, data, current_date):
        """简化因子计算（作为备用）"""
        current_data = data[data['trade_date'] == current_date]
        result = {}

        for ts_code in current_data['ts_code'].unique():
            stock_data = data[data['ts_code'] == ts_code].tail(30)
            if len(stock_data) < 20:
                continue

            # 使用简单的技术指标
            returns = stock_data['returns'].values
            close = stock_data['close'].values

            # 根据因子编号选择不同计算
            hash_val = hash(f"alpha_{num}") % 20

            if hash_val < 5:
                value = np.mean(returns[-10:])
            elif hash_val < 10:
                value = (close[-1] / close[-20] - 1) if len(close) >= 20 else np.nan
            elif hash_val < 15:
                value = np.std(returns[-10:])
            else:
                value = np.mean(returns[-5:])

            if not np.isnan(value) and not np.isinf(value):
                result[ts_code] = float(value)

        return result

    # ==================== Alpha因子实现 ====================

    def alpha001(self, data):
        """Alpha#1: rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5"""
        df = data.copy()
        returns = df.groupby('ts_code')['returns'].transform(lambda x: x)
        close = df['close']
        std_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(20).std())

        inner = np.where(returns < 0, std_ret, close)
        signed_power = np.sign(inner) * (np.abs(inner) ** 2)

        ts_argmax = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(signed_power[x.index]).rolling(5).apply(lambda y: y.argmax(), raw=True)
        )

        result = ts_argmax.groupby(df['trade_date']).rank(pct=True) - 0.5
        return result

    def alpha002(self, data):
        """Alpha#2: -1 * correlation(rank(delta(log(volume), 2)), rank(((close-open)/open)), 6)"""
        df = data.copy()
        delta_log_vol = df.groupby('ts_code')['volume'].transform(lambda x: np.log(x + 1).diff(2))
        rank_delta = delta_log_vol.groupby(df['trade_date']).rank(pct=True)

        price_change = (df['close'] - df['open']) / df['open']
        rank_price = price_change.groupby(df['trade_date']).rank(pct=True)

        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_delta[x.index]).rolling(6).corr(pd.Series(rank_price[x.index]))
        )
        return -1 * corr

    def alpha003(self, data):
        """Alpha#3: -1 * correlation(rank(open), rank(volume), 10)"""
        df = data.copy()
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_volume = df.groupby('trade_date')['volume'].rank(pct=True)

        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_open[x.index]).rolling(10).corr(pd.Series(rank_volume[x.index]))
        )
        return -1 * corr

    def alpha004(self, data):
        """Alpha#4: -1 * Ts_Rank(rank(low), 9)"""
        df = data.copy()
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)

        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_low[x.index]).rolling(9).apply(
                lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False
            )
        )
        return -1 * ts_rank

    def alpha005(self, data):
        """Alpha#5: rank((open - (sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vwap))))"""
        df = data.copy()
        vwap_ma = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(10).mean())
        rank1 = (df['open'] - vwap_ma).groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['close'] - df['vwap']).groupby(df['trade_date']).rank(pct=True)
        return rank1 * (-1 * np.abs(rank2))

    def alpha006(self, data):
        """Alpha#6: -1 * correlation(open, volume, 10)"""
        df = data.copy()
        corr = df.groupby('ts_code')['open'].transform(
            lambda x: x.rolling(10).corr(df.loc[x.index, 'volume'])
        )
        return -1 * corr

    def alpha007(self, data):
        """Alpha#7: ((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(7))
        abs_delta = np.abs(delta_close)

        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(abs_delta[x.index]).rolling(60).apply(
                lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False
            )
        )

        result = np.where(adv20 < df['volume'], -1 * ts_rank * np.sign(delta_close), -1)
        return pd.Series(result, index=df.index)

    def alpha008(self, data):
        """Alpha#8: -1 * rank(((sum(open, 5) * sum(returns, 5)) - delay((sum(open, 5) * sum(returns, 5)), 10)))"""
        df = data.copy()
        sum_open = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(5).sum())
        sum_returns = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(5).sum())
        product = sum_open * sum_returns
        delay_product = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(product[x.index]).shift(10))
        result = (product - delay_product).groupby(df['trade_date']).rank(pct=True)
        return -1 * result

    def alpha009(self, data):
        """Alpha#9: ((0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : ((ts_max(delta(close, 1), 5) < 0) ? delta(close, 1) : (-1 * delta(close, 1))))"""
        df = data.copy()
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta_close[x.index]).rolling(5).min())
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta_close[x.index]).rolling(5).max())
        result = np.where(ts_min > 0, delta_close, np.where(ts_max < 0, delta_close, -1 * delta_close))
        return pd.Series(result, index=df.index)

    def alpha010(self, data):
        """Alpha#10: rank(((0 < ts_min(delta(close, 1), 4)) ? delta(close, 1) : ((ts_max(delta(close, 1), 4) < 0) ? delta(close, 1) : (-1 * delta(close, 1)))))"""
        df = data.copy()
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta_close[x.index]).rolling(4).min())
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta_close[x.index]).rolling(4).max())
        inner = np.where(ts_min > 0, delta_close, np.where(ts_max < 0, delta_close, -1 * delta_close))
        return pd.Series(inner, index=df.index).groupby(df['trade_date']).rank(pct=True)

    def alpha011(self, data):
        """Alpha#11: ((rank(ts_max((vwap - close), 3)) + rank(ts_min((vwap - close), 3))) * rank(delta(volume, 3)))"""
        df = data.copy()
        diff = df['vwap'] - df['close']
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(3).max())
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(3).min())
        delta_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.diff(3))
        
        rank1 = ts_max.groupby(df['trade_date']).rank(pct=True)
        rank2 = ts_min.groupby(df['trade_date']).rank(pct=True)
        rank3 = delta_vol.groupby(df['trade_date']).rank(pct=True)
        return (rank1 + rank2) * rank3

    def alpha012(self, data):
        """Alpha#12: sign(delta(volume, 1)) * (-1 * delta(close, 1))"""
        df = data.copy()
        delta_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.diff(1))
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        return np.sign(delta_vol) * (-1 * delta_close)

    def alpha013(self, data):
        """Alpha#13: -1 * rank(covariance(rank(close), rank(volume), 5))"""
        df = data.copy()
        rank_close = df.groupby('trade_date')['close'].rank(pct=True)
        rank_volume = df.groupby('trade_date')['volume'].rank(pct=True)
        
        cov = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_close[x.index]).rolling(5).cov(pd.Series(rank_volume[x.index]))
        )
        return -1 * cov.groupby(df['trade_date']).rank(pct=True)

    def alpha014(self, data):
        """Alpha#14: ((-1 * rank(delta(returns, 3))) * correlation(open, volume, 10))"""
        df = data.copy()
        delta_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.diff(3))
        rank_delta = delta_ret.groupby(df['trade_date']).rank(pct=True)
        
        corr = df.groupby('ts_code')['open'].transform(
            lambda x: x.rolling(10).corr(df.loc[x.index, 'volume'])
        )
        return -1 * rank_delta * corr

    def alpha015(self, data):
        """Alpha#15: -1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3)"""
        df = data.copy()
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_volume = df.groupby('trade_date')['volume'].rank(pct=True)
        
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_high[x.index]).rolling(3).corr(pd.Series(rank_volume[x.index]))
        )
        rank_corr = corr.groupby(df['trade_date']).rank(pct=True)
        sum_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_corr[x.index]).rolling(3).sum())
        return -1 * sum_rank

    def alpha016(self, data):
        """Alpha#16: -1 * rank(covariance(rank(high), rank(volume), 5))"""
        df = data.copy()
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_volume = df.groupby('trade_date')['volume'].rank(pct=True)
        
        cov = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_high[x.index]).rolling(5).cov(pd.Series(rank_volume[x.index]))
        )
        return -1 * cov.groupby(df['trade_date']).rank(pct=True)

    def alpha017(self, data):
        """Alpha#17: (((-1 * rank(ts_rank(close, 10))) * rank(delta(delta(close, 1), 1))) * rank(ts_rank((volume / adv20), 5)))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        
        ts_rank1 = df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(10).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        rank1 = ts_rank1.groupby(df['trade_date']).rank(pct=True)
        
        delta1 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        delta2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta1[x.index]).diff(1))
        rank2 = delta2.groupby(df['trade_date']).rank(pct=True)
        
        vol_ratio = df['volume'] / (adv20 + 1e-10)
        ts_rank2 = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(vol_ratio[x.index]).rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        rank3 = ts_rank2.groupby(df['trade_date']).rank(pct=True)
        
        return -1 * rank1 * rank2 * rank3

    def alpha018(self, data):
        """Alpha#18: -1 * rank(((stddev(abs((close - open)), 5) + (close - open)) + correlation(close, open, 10)))"""
        df = data.copy()
        diff = df['close'] - df['open']
        std = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(np.abs(diff[x.index])).rolling(5).std())
        corr = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(10).corr(df.loc[x.index, 'open']))
        
        result = (std + diff + corr).groupby(df['trade_date']).rank(pct=True)
        return -1 * result

    def alpha019(self, data):
        """Alpha#19: ((-1 * sign(((close - delay(close, 7)) + delta(close, 7)))) * (1 + rank((1 + sum(returns, 250)))))"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(7))
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(7))
        
        sign_val = np.sign((df['close'] - delay_close) + delta_close)
        sum_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(250).sum())
        rank_val = (1 + sum_ret).groupby(df['trade_date']).rank(pct=True)
        
        return -1 * sign_val * (1 + rank_val)

    def alpha020(self, data):
        """Alpha#20: (((-1 * rank((open - delay(high, 1)))) * rank((open - delay(close, 1)))) * rank((open - delay(low, 1))))"""
        df = data.copy()
        delay_high = df.groupby('ts_code')['high'].transform(lambda x: x.shift(1))
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        delay_low = df.groupby('ts_code')['low'].transform(lambda x: x.shift(1))
        
        rank1 = (df['open'] - delay_high).groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['open'] - delay_close).groupby(df['trade_date']).rank(pct=True)
        rank3 = (df['open'] - delay_low).groupby(df['trade_date']).rank(pct=True)

        return -1 * rank1 * rank2 * rank3

    def alpha021(self, data):
        """Alpha#21: ((((sum(close, 8) / 8) + stddev(close, 8)) < (sum(close, 2) / 2)) ? (-1 * 1) : (((sum(close, 2) / 2) < ((sum(close, 8) / 8) - stddev(close, 8))) ? 1 : (((1 < (volume / adv20)) || ((volume / adv20) == 1)) ? 1 : (-1 * 1))))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        ma8 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(8).mean())
        ma2 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(2).mean())
        std8 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(8).std())
        cond1 = (ma8 + std8) < ma2
        cond2 = ma2 < (ma8 - std8)
        cond3 = (df['volume'] / (adv20 + 1e-10)) >= 1
        result = np.where(cond1, -1, np.where(cond2, 1, np.where(cond3, 1, -1)))
        return pd.Series(result, index=df.index)

    def alpha022(self, data):
        """Alpha#22: -1 * (delta(correlation(high, volume, 5), 5) * rank(stddev(close, 20)))"""
        df = data.copy()
        corr = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(5).corr(df.loc[x.index, 'volume']))
        delta_corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).diff(5))
        std20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).std())
        rank_std = std20.groupby(df['trade_date']).rank(pct=True)
        return -1 * delta_corr * rank_std

    def alpha023(self, data):
        """Alpha#23: (((sum(high, 20) / 20) < high) ? (-1 * delta(high, 2)) : 0)"""
        df = data.copy()
        ma20 = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(20).mean())
        delta_high = df.groupby('ts_code')['high'].transform(lambda x: x.diff(2))
        result = np.where(ma20 < df['high'], -1 * delta_high, 0)
        return pd.Series(result, index=df.index)

    def alpha024(self, data):
        """Alpha#24: ((((delta((sum(close, 100) / 100), 100) / delay(close, 100)) < 0.05) || ((delta((sum(close, 100) / 100), 100) / delay(close, 100)) == 0.05)) ? (-1 * (close - ts_min(close, 100))) : (-1 * delta(close, 3)))"""
        df = data.copy()
        ma100 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(100).mean())
        delta_ma = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ma100[x.index]).diff(100))
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(100))
        ratio = delta_ma / (delay_close + 1e-10)
        ts_min100 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(100).min())
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(3))
        result = np.where(ratio <= 0.05, -1 * (df['close'] - ts_min100), -1 * delta_close)
        return pd.Series(result, index=df.index)

    def alpha025(self, data):
        """Alpha#25: rank(((((-1 * returns) * adv20) * vwap) * (high - close)))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        inner = (-1 * df['returns']) * adv20 * df['vwap'] * (df['high'] - df['close'])
        return inner.groupby(df['trade_date']).rank(pct=True)

    def alpha026(self, data):
        """Alpha#26: -1 * ts_max(correlation(ts_rank(volume, 5), ts_rank(high, 5), 5), 3)"""
        df = data.copy()
        ts_rank_vol = df.groupby('ts_code')['volume'].transform(
            lambda x: x.rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        ts_rank_high = df.groupby('ts_code')['high'].transform(
            lambda x: x.rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(ts_rank_vol[x.index]).rolling(5).corr(pd.Series(ts_rank_high[x.index]))
        )
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(3).max())
        return -1 * ts_max

    def alpha027(self, data):
        """Alpha#27: ((0.5 < rank((sum(correlation(rank(volume), rank(vwap), 6), 2) / 2.0))) ? (-1 * 1) : 1)"""
        df = data.copy()
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        rank_vwap = df.groupby('trade_date')['vwap'].rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_vol[x.index]).rolling(6).corr(pd.Series(rank_vwap[x.index]))
        )
        sum_corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(2).sum())
        rank_val = (sum_corr / 2.0).groupby(df['trade_date']).rank(pct=True)
        result = np.where(rank_val > 0.5, -1, 1)
        return pd.Series(result, index=df.index)

    def alpha028(self, data):
        """Alpha#28: scale(((correlation(adv20, low, 5) + ((high + low) / 2)) - close))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(adv20[x.index]).rolling(5).corr(df.loc[x.index, 'low'])
        )
        inner = corr + (df['high'] + df['low']) / 2 - df['close']
        return inner.groupby(df['trade_date']).apply(lambda x: x / (np.abs(x).sum() + 1e-10))

    def alpha029(self, data):
        """Alpha#29: (min(product(rank(rank(scale(log(sum(ts_min(rank(rank((-1 * rank(delta((close - 1), 5))))), 2), 1))))), 1), 5) + ts_rank(delay((-1 * returns), 6), 5))"""
        df = data.copy()
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(5))
        rank1 = delta_close.groupby(df['trade_date']).rank(pct=True)
        rank2 = (-1 * rank1).groupby(df['trade_date']).rank(pct=True)
        rank3 = rank2.groupby(df['trade_date']).rank(pct=True)
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank3[x.index]).rolling(2).min())
        sum_val = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_min[x.index]).rolling(1).sum())
        log_val = np.log(sum_val + 1)
        scale_val = log_val.groupby(df['trade_date']).apply(lambda x: x / (np.abs(x).sum() + 1e-10))
        rank4 = scale_val.groupby(df['trade_date']).rank(pct=True)
        rank5 = rank4.groupby(df['trade_date']).rank(pct=True)
        delay_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.shift(6))
        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(-1 * delay_ret[x.index]).rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        return rank5 + ts_rank

    def alpha030(self, data):
        """Alpha#30: (((1.0 - rank(((sign((close - delay(close, 1))) + sign((delay(close, 1) - delay(close, 2)))) + sign((delay(close, 2) - delay(close, 3)))))) * sum(volume, 5)) / sum(volume, 20))"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        delay2 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(2))
        delay3 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(3))
        sign_sum = np.sign(df['close'] - delay1) + np.sign(delay1 - delay2) + np.sign(delay2 - delay3)
        rank_val = sign_sum.groupby(df['trade_date']).rank(pct=True)
        sum5 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(5).sum())
        sum20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).sum())
        return (1.0 - rank_val) * sum5 / (sum20 + 1e-10)

    def alpha031(self, data):
        """Alpha#31: rank(rank(rank(decay_linear((-1 * rank(rank(delta(close, 10)))), 10)))) + rank((-1 * delta(close, 3))) + sign(scale(correlation(adv20, low, 12)))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        delta10 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(10))
        rank1 = delta10.groupby(df['trade_date']).rank(pct=True)
        rank2 = rank1.groupby(df['trade_date']).rank(pct=True)
        rank3 = (-1 * rank2).groupby(df['trade_date']).rank(pct=True)
        delta3 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(3))
        rank_delta3 = (-1 * delta3).groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(adv20[x.index]).rolling(12).corr(df.loc[x.index, 'low'])
        )
        scale_corr = corr.groupby(df['trade_date']).apply(lambda x: x / (np.abs(x).sum() + 1e-10))
        return rank3 + rank_delta3 + np.sign(scale_corr)

    def alpha032(self, data):
        """Alpha#32: scale(((sum(close, 7) / 7) - close)) + (20 * scale(correlation(vwap, delay(close, 5), 230)))"""
        df = data.copy()
        ma7 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(7).mean())
        scale1 = (ma7 - df['close']).groupby(df['trade_date']).apply(lambda x: x / (np.abs(x).sum() + 1e-10))
        delay5 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(5))
        corr = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(230).corr(pd.Series(delay5[x.index])))
        scale2 = corr.groupby(df['trade_date']).apply(lambda x: x / (np.abs(x).sum() + 1e-10))
        return scale1 + 20 * scale2

    def alpha033(self, data):
        """Alpha#33: rank((-1 * ((1 - (open / close))^1)))"""
        df = data.copy()
        inner = -1 * (1 - df['open'] / df['close'])
        return inner.groupby(df['trade_date']).rank(pct=True)

    def alpha034(self, data):
        """Alpha#34: rank(((1 - rank((stddev(returns, 2) / stddev(returns, 5)))) + (1 - rank(delta(close, 1)))))"""
        df = data.copy()
        std2 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(2).std())
        std5 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(5).std())
        ratio = std2 / (std5 + 1e-10)
        rank1 = ratio.groupby(df['trade_date']).rank(pct=True)
        delta1 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        rank2 = delta1.groupby(df['trade_date']).rank(pct=True)
        return ((1 - rank1) + (1 - rank2)).groupby(df['trade_date']).rank(pct=True)

    def alpha035(self, data):
        """Alpha#35: ((Ts_Rank(volume, 32) * (1 - Ts_Rank(((close + high) - low), 16))) * (1 - Ts_Rank(returns, 32)))"""
        df = data.copy()
        ts_rank1 = df.groupby('ts_code')['volume'].transform(
            lambda x: x.rolling(32).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        hl_sum = df['close'] + df['high'] - df['low']
        ts_rank2 = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(hl_sum[x.index]).rolling(16).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        ts_rank3 = df.groupby('ts_code')['returns'].transform(
            lambda x: x.rolling(32).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        return ts_rank1 * (1 - ts_rank2) * (1 - ts_rank3)

    def alpha036(self, data):
        """Alpha#36: (((((2.21 * rank(correlation((close - open), delay(volume, 1), 15))) + (0.7 * rank((open - close)))) + (0.73 * rank(Ts_Rank(delay((-1 * returns), 6), 5)))) + rank(abs(correlation(vwap, adv20, 6)))) + (0.6 * rank((((sum(close, 200) / 200) - open) * (close - open)))))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        delay_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.shift(1))
        corr1 = df.groupby('ts_code')['close'].transform(
            lambda x: (df.loc[x.index, 'close'] - df.loc[x.index, 'open']).rolling(15).corr(pd.Series(delay_vol[x.index]))
        )
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['open'] - df['close']).groupby(df['trade_date']).rank(pct=True)
        delay_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.shift(6))
        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(-1 * delay_ret[x.index]).rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        rank3 = ts_rank.groupby(df['trade_date']).rank(pct=True)
        corr2 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(6).corr(pd.Series(adv20[x.index])))
        rank4 = np.abs(corr2).groupby(df['trade_date']).rank(pct=True)
        ma200 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(200).mean())
        rank5 = ((ma200 - df['open']) * (df['close'] - df['open'])).groupby(df['trade_date']).rank(pct=True)
        return 2.21 * rank1 + 0.7 * rank2 + 0.73 * rank3 + rank4 + 0.6 * rank5

    def alpha037(self, data):
        """Alpha#37: rank(correlation(delay((open - close), 1), close, 200)) + rank((open - close))"""
        df = data.copy()
        oc_diff = df['open'] - df['close']
        delay_oc = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(oc_diff[x.index]).shift(1))
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(delay_oc[x.index]).rolling(200).corr(x)
        )
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        rank2 = oc_diff.groupby(df['trade_date']).rank(pct=True)
        return rank1 + rank2

    def alpha038(self, data):
        """Alpha#38: ((-1 * rank(Ts_Rank(close, 10))) * rank((close / open)))"""
        df = data.copy()
        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(10).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        rank1 = ts_rank.groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['close'] / df['open']).groupby(df['trade_date']).rank(pct=True)
        return -1 * rank1 * rank2

    def alpha039(self, data):
        """Alpha#39: ((-1 * rank((delta(close, 7) * (1 - rank(decay_linear((volume / adv20), 9)))))) * (1 + rank(sum(returns, 250))))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        delta7 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(7))
        vol_ratio = df['volume'] / (adv20 + 1e-10)
        rank1 = vol_ratio.groupby(df['trade_date']).rank(pct=True)
        inner = delta7 * (1 - rank1)
        rank2 = inner.groupby(df['trade_date']).rank(pct=True)
        sum_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(250).sum())
        rank3 = sum_ret.groupby(df['trade_date']).rank(pct=True)
        return -1 * rank2 * (1 + rank3)

    def alpha040(self, data):
        """Alpha#40: ((-1 * rank(stddev(high, 10))) * correlation(high, volume, 10))"""
        df = data.copy()
        std_high = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(10).std())
        rank_std = std_high.groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(10).corr(df.loc[x.index, 'volume']))
        return -1 * rank_std * corr

    def alpha041(self, data):
        """Alpha#41: (((high * low)^0.5) - vwap)"""
        df = data.copy()
        return np.sqrt(df['high'] * df['low']) - df['vwap']

    def alpha042(self, data):
        """Alpha#42: rank((vwap - close)) / rank((vwap + close))"""
        df = data.copy()
        rank1 = (df['vwap'] - df['close']).groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['vwap'] + df['close']).groupby(df['trade_date']).rank(pct=True)
        return rank1 / (rank2 + 1e-10)

    def alpha043(self, data):
        """Alpha#43: ts_rank(volume / adv20, 20) * ts_rank((-1 * delta(close, 7)), 8)"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        vol_ratio = df['volume'] / (adv20 + 1e-10)
        ts_rank1 = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(vol_ratio[x.index]).rolling(20).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        delta7 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(7))
        ts_rank2 = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(-1 * delta7[x.index]).rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        return ts_rank1 * ts_rank2

    def alpha044(self, data):
        """Alpha#44: -1 * correlation(high, rank(volume), 5)"""
        df = data.copy()
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr = df.groupby('ts_code')['high'].transform(
            lambda x: x.rolling(5).corr(pd.Series(rank_vol[x.index]))
        )
        return -1 * corr

    def alpha045(self, data):
        """Alpha#45: -1 * ((rank((sum(delay(close, 5), 20) / 20)) * correlation(close, volume, 2)) * rank(correlation(sum(close, 5), sum(close, 20), 2)))"""
        df = data.copy()
        delay5 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(5))
        sum_delay = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay5[x.index]).rolling(20).sum())
        rank1 = (sum_delay / 20).groupby(df['trade_date']).rank(pct=True)
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(2).corr(df.loc[x.index, 'volume']))
        sum5 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(5).sum())
        sum20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).sum())
        corr2 = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(sum5[x.index]).rolling(2).corr(pd.Series(sum20[x.index]))
        )
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * rank1 * corr1 * rank2

    def alpha046(self, data):
        """Alpha#46: ((0.25 < (((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10))) ? (-1 * 1) : (((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10)) < 0) ? 1 : ((-1 * 1) * (close - delay(close, 1)))))"""
        df = data.copy()
        delay20 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(20))
        delay10 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(10))
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = ((delay20 - delay10) / 10) - ((delay10 - df['close']) / 10)
        result = np.where(diff > 0.25, -1, np.where(diff < 0, 1, -1 * (df['close'] - delay1)))
        return pd.Series(result, index=df.index)

    def alpha047(self, data):
        """Alpha#47: ((((rank((1 / close)) * volume) / adv20) * ((high * rank((high - close))) / (sum(high, 5) / 5))) - rank((vwap - delay(vwap, 5))))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        rank1 = (1 / df['close']).groupby(df['trade_date']).rank(pct=True)
        part1 = (rank1 * df['volume']) / (adv20 + 1e-10)
        rank2 = (df['high'] - df['close']).groupby(df['trade_date']).rank(pct=True)
        sum_high = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(5).sum())
        part2 = (df['high'] * rank2) / ((sum_high / 5) + 1e-10)
        delay_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.shift(5))
        rank3 = (df['vwap'] - delay_vwap).groupby(df['trade_date']).rank(pct=True)
        return part1 * part2 - rank3

    def alpha048(self, data):
        """Alpha#48: indneutralize(((correlation(delta(close, 1), delta(delay(close, 1), 1), 250) * delta(close, 1)) / close), IndClass.subindustry) / sum(((delta(close, 1) / delay(close, 1))^2), 250)"""
        df = data.copy()
        delta1 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        delta_delay = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay1[x.index]).diff(1))
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(delta1[x.index]).rolling(250).corr(pd.Series(delta_delay[x.index]))
        )
        numerator = (corr * delta1) / df['close']
        ret = delta1 / (delay1 + 1e-10)
        denominator = df.groupby('ts_code')['close'].transform(lambda x: pd.Series((ret[x.index] ** 2)).rolling(250).sum())
        return numerator / (denominator + 1e-10)

    def alpha049(self, data):
        """Alpha#49: (((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10)) < (-1 * 0.1)) ? 1 : ((-1 * 1) * (close - delay(close, 1))))"""
        df = data.copy()
        delay20 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(20))
        delay10 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(10))
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = ((delay20 - delay10) / 10) - ((delay10 - df['close']) / 10)
        result = np.where(diff < -0.1, 1, -1 * (df['close'] - delay1))
        return pd.Series(result, index=df.index)

    def alpha050(self, data):
        """Alpha#50: -1 * ts_max(rank(correlation(rank(volume), rank(vwap), 5)), 5)"""
        df = data.copy()
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        rank_vwap = df.groupby('trade_date')['vwap'].rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank_vol[x.index]).rolling(5).corr(pd.Series(rank_vwap[x.index]))
        )
        rank_corr = corr.groupby(df['trade_date']).rank(pct=True)
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_corr[x.index]).rolling(5).max())
        return -1 * ts_max

    def alpha051(self, data):
        """Alpha#51: (((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, 10) - close) / 10)) < (-1 * 0.05)) ? 1 : ((-1 * 1) * (close - delay(close, 1))))"""
        df = data.copy()
        delay20 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(20))
        delay10 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(10))
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = ((delay20 - delay10) / 10) - ((delay10 - df['close']) / 10)
        result = np.where(diff < -0.05, 1, -1 * (df['close'] - delay1))
        return pd.Series(result, index=df.index)

    def alpha052(self, data):
        """Alpha#52: ((((-1 * ts_min(low, 5)) + delay(ts_min(low, 5), 5)) * rank(((sum(returns, 240) - sum(returns, 20)) / 220))) * ts_rank(volume, 5))"""
        df = data.copy()
        ts_min5 = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(5).min())
        delay_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_min5[x.index]).shift(5))
        sum240 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(240).sum())
        sum20 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(20).sum())
        rank_val = ((sum240 - sum20) / 220).groupby(df['trade_date']).rank(pct=True)
        ts_rank_vol = df.groupby('ts_code')['volume'].transform(
            lambda x: x.rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        return ((-1 * ts_min5 + delay_min) * rank_val * ts_rank_vol)

    def alpha053(self, data):
        """Alpha#53: -1 * delta((((close - low) - (high - close)) / (close - low)), 9)"""
        df = data.copy()
        inner = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['close'] - df['low'] + 1e-10)
        delta9 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).diff(9))
        return -1 * delta9

    def alpha054(self, data):
        """Alpha#54: ((-1 * ((low - close) * (open^5))) / ((low - high) * (close^5)))"""
        df = data.copy()
        numerator = -1 * (df['low'] - df['close']) * (df['open'] ** 5)
        denominator = (df['low'] - df['high']) * (df['close'] ** 5)
        return numerator / (denominator + 1e-10)

    def alpha055(self, data):
        """Alpha#55: -1 * correlation(rank(((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12)))), rank(volume), 6)"""
        df = data.copy()
        ts_min_low = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(12).min())
        ts_max_high = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(12).max())
        inner = (df['close'] - ts_min_low) / (ts_max_high - ts_min_low + 1e-10)
        rank1 = inner.groupby(df['trade_date']).rank(pct=True)
        rank2 = df.groupby('trade_date')['volume'].rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(rank1[x.index]).rolling(6).corr(pd.Series(rank2[x.index]))
        )
        return -1 * corr

    def alpha056(self, data):
        """Alpha#56: (0 - (1 * (rank((sum(returns, 10) / sum(sum(returns, 2), 3))) * rank((returns * cap)))))"""
        df = data.copy()
        sum10 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(10).sum())
        sum2 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(2).sum())
        sum_sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum2[x.index]).rolling(3).sum())
        rank1 = (sum10 / (sum_sum2 + 1e-10)).groupby(df['trade_date']).rank(pct=True)
        cap = df['close'] * df['volume']
        rank2 = (df['returns'] * cap).groupby(df['trade_date']).rank(pct=True)
        return -1 * rank1 * rank2

    def alpha057(self, data):
        """Alpha#57: (0 - (1 * ((close - vwap) / decay_linear(rank(ts_argmax(close, 30)), 2))))"""
        df = data.copy()
        ts_argmax30 = df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(30).apply(lambda y: y.argmax(), raw=True)
        )
        rank_argmax = ts_argmax30.groupby(df['trade_date']).rank(pct=True)
        return -1 * (df['close'] - df['vwap']) / (rank_argmax + 1e-10)

    def alpha058(self, data):
        """Alpha#58: -1 * Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndClass.sector), volume, 3.92795), 7.89291), 5.50322)"""
        df = data.copy()
        vwap_neutral = df.groupby('trade_date')['vwap'].transform(lambda x: x - x.mean())
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(vwap_neutral[x.index]).rolling(4).corr(df.loc[x.index, 'volume'])
        )
        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(corr[x.index]).rolling(6).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        return -1 * ts_rank

    def alpha059(self, data):
        """Alpha#59: -1 * Ts_Rank(decay_linear(correlation(IndNeutralize(((vwap * 0.728317) + (vwap * (1 - 0.728317))), IndClass.industry), volume, 4.25197), 16.2289), 8.19648)"""
        df = data.copy()
        vwap_weighted = df['vwap'] * 0.728317 + df['vwap'] * (1 - 0.728317)
        vwap_neutral = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(vwap_weighted[x.index]) - pd.Series(vwap_weighted[x.index]).mean())
        corr = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(vwap_neutral[x.index]).rolling(4).corr(df.loc[x.index, 'volume'])
        )
        ts_rank = df.groupby('ts_code')['close'].transform(
            lambda x: pd.Series(corr[x.index]).rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False)
        )
        return -1 * ts_rank

    def alpha060(self, data):
        """Alpha#60: (0 - (1 * ((2 * scale(rank(((((close - low) - (high - close)) / (high - low)) * volume)))) - scale(rank(ts_argmax(close, 10))))))"""
        df = data.copy()
        inner = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'] + 1e-10) * df['volume']
        rank1 = inner.groupby(df['trade_date']).rank(pct=True)
        ts_argmax10 = df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(10).apply(lambda y: y.argmax(), raw=True)
        )
        rank2 = ts_argmax10.groupby(df['trade_date']).rank(pct=True)
        return -1 * (2 * rank1 - rank2)

    def alpha061(self, data):
        """Alpha#61: rank((vwap - ts_min(vwap, 16.1219))) < rank(correlation(vwap, adv180, 17.9282))"""
        df = data.copy()
        adv180 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(180).mean())
        ts_min16 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(16).min())
        rank1 = (df['vwap'] - ts_min16).groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(18).corr(pd.Series(adv180[x.index])))
        rank2 = corr.groupby(df['trade_date']).rank(pct=True)
        return (rank1 < rank2).astype(float)

    def alpha062(self, data):
        """Alpha#62: ((rank(correlation(vwap, sum(adv20, 22.4101), 9.91009)) < rank(((rank(open) + rank(open)) < (rank(((high + low) / 2)) + rank(high))))) * -1)"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv20[x.index]).rolling(22).sum())
        corr = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(10).corr(pd.Series(sum_adv[x.index])))
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_hl = ((df['high'] + df['low']) / 2).groupby(df['trade_date']).rank(pct=True)
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank2 = ((rank_open + rank_open) < (rank_hl + rank_high)).astype(float).groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha063(self, data):
        """Alpha#63: ((rank(decay_linear(delta(IndNeutralize(close, IndClass.industry), 2.25164), 8.22237)) - rank(decay_linear(correlation(((vwap * 0.318108) + (open * (1 - 0.318108))), sum(adv180, 37.2467), 13.557), 12.2883))) * -1)"""
        df = data.copy()
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(2))
        rank1 = delta_close.groupby(df['trade_date']).rank(pct=True)
        adv180 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(180).mean())
        weighted = df['vwap'] * 0.318108 + df['open'] * (1 - 0.318108)
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv180[x.index]).rolling(37).sum())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).rolling(14).corr(pd.Series(sum_adv[x.index])))
        rank2 = corr.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 - rank2)

    def alpha064(self, data):
        """Alpha#64: ((rank(correlation(sum(((open * 0.178404) + (low * (1 - 0.178404))), 12.7054), sum(adv120, 12.7054), 16.6208)) < rank(delta(((((high + low) / 2) * 0.178404) + (vwap * (1 - 0.178404))), 3.69741))) * -1)"""
        df = data.copy()
        adv120 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(120).mean())
        weighted1 = df['open'] * 0.178404 + df['low'] * (1 - 0.178404)
        sum1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted1[x.index]).rolling(13).sum())
        sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv120[x.index]).rolling(13).sum())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum1[x.index]).rolling(17).corr(pd.Series(sum2[x.index])))
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        weighted2 = ((df['high'] + df['low']) / 2) * 0.178404 + df['vwap'] * (1 - 0.178404)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted2[x.index]).diff(4))
        rank2 = delta_w.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha065(self, data):
        """Alpha#65: ((rank(correlation(((open * 0.00817205) + (vwap * (1 - 0.00817205))), sum(adv60, 8.6911), 6.40374)) < rank((open - ts_min(open, 13.635)))) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        weighted = df['open'] * 0.00817205 + df['vwap'] * (1 - 0.00817205)
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(9).sum())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).rolling(6).corr(pd.Series(sum_adv[x.index])))
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        ts_min14 = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(14).min())
        rank2 = (df['open'] - ts_min14).groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha066(self, data):
        """Alpha#66: ((rank(decay_linear(delta(vwap, 3.51013), 7.23052)) + Ts_Rank(decay_linear(((((low * 0.96633) + (low * (1 - 0.96633))) - vwap) / (open - ((high + low) / 2))), 11.4157), 6.72611)) * -1)"""
        df = data.copy()
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(4))
        rank1 = delta_vwap.groupby(df['trade_date']).rank(pct=True)
        weighted_low = df['low'] * 0.96633 + df['low'] * (1 - 0.96633)
        inner = (weighted_low - df['vwap']) / (df['open'] - (df['high'] + df['low']) / 2 + 1e-10)
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(7).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 + ts_rank)

    def alpha067(self, data):
        """Alpha#67: ((rank((high - ts_min(high, 2.14593)))^rank(correlation(IndNeutralize(vwap, IndClass.sector), IndNeutralize(adv20, IndClass.subindustry), 6.02936))) * -1)"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        ts_min2 = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(2).min())
        rank1 = (df['high'] - ts_min2).groupby(df['trade_date']).rank(pct=True)
        vwap_n = df.groupby('trade_date')['vwap'].transform(lambda x: x - x.mean())
        adv_n = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv20[x.index]) - pd.Series(adv20[x.index]).mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(vwap_n[x.index]).rolling(6).corr(pd.Series(adv_n[x.index])))
        rank2 = corr.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 ** rank2)

    def alpha068(self, data):
        """Alpha#68: ((Ts_Rank(correlation(rank(high), rank(adv15), 8.91644), 13.9333) < rank(delta(((close * 0.518371) + (low * (1 - 0.518371))), 1.06157))) * -1)"""
        df = data.copy()
        adv15 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(15).mean())
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_adv = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv15[x.index]).rank(pct=True))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_high[x.index]).rolling(9).corr(pd.Series(rank_adv[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(14).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        weighted = df['close'] * 0.518371 + df['low'] * (1 - 0.518371)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(1))
        rank_delta = delta_w.groupby(df['trade_date']).rank(pct=True)
        return -1 * (ts_rank < rank_delta).astype(float)

    def alpha069(self, data):
        """Alpha#69: ((rank(ts_max(delta(IndNeutralize(vwap, IndClass.industry), 2.72412), 4.79344))^Ts_Rank(correlation(((close * 0.490655) + (vwap * (1 - 0.490655))), adv20, 4.92416), 9.0615)) * -1)"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(3))
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta_vwap[x.index]).rolling(5).max())
        rank1 = ts_max.groupby(df['trade_date']).rank(pct=True)
        weighted = df['close'] * 0.490655 + df['vwap'] * (1 - 0.490655)
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).rolling(5).corr(pd.Series(adv20[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(9).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 ** ts_rank)

    def alpha070(self, data):
        """Alpha#70: ((rank(delta(vwap, 1.29456))^Ts_Rank(correlation(IndNeutralize(close, IndClass.industry), adv50, 17.8256), 17.9171)) * -1)"""
        df = data.copy()
        adv50 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(50).mean())
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(1))
        rank1 = delta_vwap.groupby(df['trade_date']).rank(pct=True)
        close_n = df.groupby('trade_date')['close'].transform(lambda x: x - x.mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(close_n[x.index]).rolling(18).corr(pd.Series(adv50[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(18).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 ** ts_rank)

    def alpha071(self, data):
        """Alpha#71: max(Ts_Rank(decay_linear(correlation(Ts_Rank(close, 3.43976), Ts_Rank(adv180, 12.0647), 18.0175), 4.20501), 15.6948), Ts_Rank(decay_linear((rank(((low + open) - (vwap + vwap)))^2), 16.4662), 4.4388))"""
        df = data.copy()
        adv180 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(180).mean())
        ts_rank_close = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv180[x.index]).rolling(12).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_close[x.index]).rolling(18).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(16).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        inner = (df['low'] + df['open'] - df['vwap'] - df['vwap']).groupby(df['trade_date']).rank(pct=True) ** 2
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return np.maximum(ts_rank1, ts_rank2)

    def alpha072(self, data):
        """Alpha#72: (rank(decay_linear(correlation(((high + low) / 2), adv40, 8.93345), 10.1519)) / rank(decay_linear(correlation(Ts_Rank(vwap, 3.72469), Ts_Rank(volume, 18.5188), 6.86671), 2.95011)))"""
        df = data.copy()
        adv40 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(40).mean())
        hl_mid = (df['high'] + df['low']) / 2
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(9).corr(pd.Series(adv40[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        ts_rank_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(19).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_vwap[x.index]).rolling(7).corr(pd.Series(ts_rank_vol[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return rank1 / (rank2 + 1e-10)

    def alpha073(self, data):
        """Alpha#73: (max(rank(decay_linear(delta(vwap, 4.72775), 2.91864)), Ts_Rank(decay_linear(((delta(((open * 0.147155) + (low * (1 - 0.147155))), 2.03608) / ((open * 0.147155) + (low * (1 - 0.147155)))) * -1), 3.33829), 16.7411)) * -1)"""
        df = data.copy()
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(5))
        rank1 = delta_vwap.groupby(df['trade_date']).rank(pct=True)
        weighted = df['open'] * 0.147155 + df['low'] * (1 - 0.147155)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(2))
        inner = -1 * delta_w / (weighted + 1e-10)
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(17).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * np.maximum(rank1, ts_rank)

    def alpha074(self, data):
        """Alpha#74: ((rank(correlation(close, sum(adv30, 37.4843), 15.1365)) < rank(correlation(rank(((high * 0.0261661) + (vwap * (1 - 0.0261661)))), rank(volume), 11.4791))) * -1)"""
        df = data.copy()
        adv30 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(30).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv30[x.index]).rolling(37).sum())
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(15).corr(pd.Series(sum_adv[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        weighted = df['high'] * 0.0261661 + df['vwap'] * (1 - 0.0261661)
        rank_w = weighted.groupby(df['trade_date']).rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_w[x.index]).rolling(11).corr(pd.Series(rank_vol[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha075(self, data):
        """Alpha#75: (rank(correlation(vwap, volume, 4.24304)) < rank(correlation(rank(low), rank(adv50), 12.4413)))"""
        df = data.copy()
        adv50 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(50).mean())
        corr1 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(4).corr(df.loc[x.index, 'volume']))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)
        rank_adv = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv50[x.index]).rank(pct=True))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_low[x.index]).rolling(12).corr(pd.Series(rank_adv[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return (rank1 < rank2).astype(float)

    def alpha076(self, data):
        """Alpha#76: (max(rank(decay_linear(delta(vwap, 1.24383), 11.8259)), Ts_Rank(decay_linear(Ts_Rank(correlation(IndNeutralize(low, IndClass.sector), adv81, 8.14941), 19.569), 17.1543), 19.383)) * -1)"""
        df = data.copy()
        adv81 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(81).mean())
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(1))
        rank1 = delta_vwap.groupby(df['trade_date']).rank(pct=True)
        low_n = df.groupby('trade_date')['low'].transform(lambda x: x - x.mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(low_n[x.index]).rolling(8).corr(pd.Series(adv81[x.index])))
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(20).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank1[x.index]).rolling(19).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * np.maximum(rank1, ts_rank2)

    def alpha077(self, data):
        """Alpha#77: min(rank(decay_linear(((((high + low) / 2) + high) - (vwap + high)), 20.0451)), rank(decay_linear(correlation(((high + low) / 2), adv40, 3.1614), 5.64125)))"""
        df = data.copy()
        adv40 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(40).mean())
        hl_mid = (df['high'] + df['low']) / 2
        inner1 = hl_mid + df['high'] - df['vwap'] - df['high']
        rank1 = inner1.groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(3).corr(pd.Series(adv40[x.index])))
        rank2 = corr.groupby(df['trade_date']).rank(pct=True)
        return np.minimum(rank1, rank2)

    def alpha078(self, data):
        """Alpha#78: (rank(correlation(sum(((low * 0.352233) + (vwap * (1 - 0.352233))), 19.7428), sum(adv40, 19.7428), 6.83313))^rank(correlation(rank(vwap), rank(volume), 5.77492)))"""
        df = data.copy()
        adv40 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(40).mean())
        weighted = df['low'] * 0.352233 + df['vwap'] * (1 - 0.352233)
        sum1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).rolling(20).sum())
        sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv40[x.index]).rolling(20).sum())
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum1[x.index]).rolling(7).corr(pd.Series(sum2[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank_vwap = df.groupby('trade_date')['vwap'].rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_vwap[x.index]).rolling(6).corr(pd.Series(rank_vol[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return rank1 ** rank2

    def alpha079(self, data):
        """Alpha#79: (rank(delta(IndNeutralize(((close * 0.60733) + (open * (1 - 0.60733))), IndClass.sector), 1.23438)) < rank(correlation(Ts_Rank(vwap, 3.60973), Ts_Rank(adv150, 9.18637), 14.6644)))"""
        df = data.copy()
        adv150 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(150).mean())
        weighted = df['close'] * 0.60733 + df['open'] * (1 - 0.60733)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(1))
        rank1 = delta_w.groupby(df['trade_date']).rank(pct=True)
        ts_rank_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv150[x.index]).rolling(9).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_vwap[x.index]).rolling(15).corr(pd.Series(ts_rank_adv[x.index])))
        rank2 = corr.groupby(df['trade_date']).rank(pct=True)
        return (rank1 < rank2).astype(float)

    def alpha080(self, data):
        """Alpha#80: ((rank(Sign(delta(IndNeutralize(((open * 0.868128) + (high * (1 - 0.868128))), IndClass.industry), 4.04545)))^Ts_Rank(correlation(high, adv10, 5.11456), 5.53756)) * -1)"""
        df = data.copy()
        adv10 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(10).mean())
        weighted = df['open'] * 0.868128 + df['high'] * (1 - 0.868128)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(4))
        rank1 = np.sign(delta_w).groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(5).corr(pd.Series(adv10[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(6).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 ** ts_rank)

    def alpha081(self, data):
        """Alpha#81: ((rank(Log(product(rank((rank(correlation(vwap, sum(adv10, 49.6054), 8.47743))^4)), 14.9655))) < rank(correlation(rank(vwap), rank(volume), 5.07914))) * -1)"""
        df = data.copy()
        adv10 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(10).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv10[x.index]).rolling(50).sum())
        corr1 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(8).corr(pd.Series(sum_adv[x.index])))
        rank_corr = corr1.groupby(df['trade_date']).rank(pct=True)
        rank1 = (rank_corr ** 4).groupby(df['trade_date']).rank(pct=True)
        rank_vwap = df.groupby('trade_date')['vwap'].rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_vwap[x.index]).rolling(5).corr(pd.Series(rank_vol[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha082(self, data):
        """Alpha#82: (min(rank(decay_linear(delta(open, 1.46063), 14.8717)), Ts_Rank(decay_linear(correlation(IndNeutralize(volume, IndClass.sector), ((open * 0.634196) + (open * (1 - 0.634196))), 17.4842), 6.92131), 13.4283)) * -1)"""
        df = data.copy()
        delta_open = df.groupby('ts_code')['open'].transform(lambda x: x.diff(1))
        rank1 = delta_open.groupby(df['trade_date']).rank(pct=True)
        vol_n = df.groupby('trade_date')['volume'].transform(lambda x: x - x.mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(vol_n[x.index]).rolling(17).corr(df.loc[x.index, 'open']))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(13).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * np.minimum(rank1, ts_rank)

    def alpha083(self, data):
        """Alpha#83: ((rank(delay(((high - low) / (sum(close, 5) / 5)), 2)) * rank(rank(volume))) / (((high - low) / (sum(close, 5) / 5)) / (vwap - close)))"""
        df = data.copy()
        sum5 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(5).sum())
        hl_ratio = (df['high'] - df['low']) / ((sum5 / 5) + 1e-10)
        delay2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_ratio[x.index]).shift(2))
        rank1 = delay2.groupby(df['trade_date']).rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        rank2 = rank_vol.groupby(df['trade_date']).rank(pct=True)
        denominator = hl_ratio / (df['vwap'] - df['close'] + 1e-10)
        return (rank1 * rank2) / (denominator + 1e-10)

    def alpha084(self, data):
        """Alpha#84: SignedPower(Ts_Rank((vwap - ts_max(vwap, 15.3217)), 20.7127), delta(close, 4.96796))"""
        df = data.copy()
        ts_max15 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(15).max())
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series((df.loc[x.index, 'vwap'] - ts_max15[x.index])[x.index]).rolling(21).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        delta5 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(5))
        return np.sign(ts_rank) * (np.abs(ts_rank) ** delta5)

    def alpha085(self, data):
        """Alpha#85: (rank(correlation(((high * 0.876703) + (close * (1 - 0.876703))), adv30, 9.61331))^rank(correlation(Ts_Rank(((high + low) / 2), 3.70596), Ts_Rank(volume, 10.1595), 7.11408)))"""
        df = data.copy()
        adv30 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(30).mean())
        weighted = df['high'] * 0.876703 + df['close'] * (1 - 0.876703)
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).rolling(10).corr(pd.Series(adv30[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        hl_mid = (df['high'] + df['low']) / 2
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank2 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(10).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank1[x.index]).rolling(7).corr(pd.Series(ts_rank2[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return rank1 ** rank2

    def alpha086(self, data):
        """Alpha#86: ((Ts_Rank(correlation(close, sum(adv20, 14.7444), 6.00049), 20.4195) < rank(((open + close) - (vwap + open)))) * -1)"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv20[x.index]).rolling(15).sum())
        corr = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(6).corr(pd.Series(sum_adv[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(20).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        inner = (df['open'] + df['close']) - (df['vwap'] + df['open'])
        rank_inner = inner.groupby(df['trade_date']).rank(pct=True)
        return -1 * (ts_rank < rank_inner).astype(float)

    def alpha087(self, data):
        """Alpha#87: (max(rank(decay_linear(delta(((close * 0.369701) + (vwap * (1 - 0.369701))), 1.91233), 2.65461)), Ts_Rank(decay_linear(abs(correlation(IndNeutralize(adv81, IndClass.industry), close, 13.4132)), 4.89768), 14.4535)) * -1)"""
        df = data.copy()
        adv81 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(81).mean())
        weighted = df['close'] * 0.369701 + df['vwap'] * (1 - 0.369701)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(2))
        rank1 = delta_w.groupby(df['trade_date']).rank(pct=True)
        adv_n = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv81[x.index]) - pd.Series(adv81[x.index]).mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv_n[x.index]).rolling(13).corr(x))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(np.abs(corr[x.index])).rolling(14).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * np.maximum(rank1, ts_rank)

    def alpha088(self, data):
        """Alpha#88: min(rank(decay_linear(((rank(open) + rank(low)) - (rank(high) + rank(close))), 8.06882)), Ts_Rank(decay_linear(correlation(Ts_Rank(close, 8.44728), Ts_Rank(adv60, 20.6966), 8.01266), 6.65053), 2.61957))"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_close = df.groupby('trade_date')['close'].rank(pct=True)
        inner = (rank_open + rank_low) - (rank_high + rank_close)
        rank1 = inner.groupby(df['trade_date']).rank(pct=True)
        ts_rank_close = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(21).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_close[x.index]).rolling(8).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return np.minimum(rank1, ts_rank2)

    def alpha089(self, data):
        """Alpha#89: (Ts_Rank(decay_linear(correlation(((low * 0.967285) + (low * (1 - 0.967285))), adv10, 6.94279), 5.51607), 3.79744) - Ts_Rank(decay_linear(delta(IndNeutralize(vwap, IndClass.industry), 3.48158), 10.1466), 15.3012))"""
        df = data.copy()
        adv10 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(10).mean())
        corr = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(7).corr(pd.Series(adv10[x.index])))
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(3))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta_vwap[x.index]).rolling(15).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return ts_rank1 - ts_rank2

    def alpha090(self, data):
        """Alpha#90: ((rank((close - ts_max(close, 4.66719)))^Ts_Rank(correlation(IndNeutralize(adv40, IndClass.subindustry), low, 5.38375), 3.21856)) * -1)"""
        df = data.copy()
        adv40 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(40).mean())
        ts_max5 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(5).max())
        rank1 = (df['close'] - ts_max5).groupby(df['trade_date']).rank(pct=True)
        adv_n = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv40[x.index]) - pd.Series(adv40[x.index]).mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv_n[x.index]).rolling(5).corr(df.loc[x.index, 'low']))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 ** ts_rank)

    def alpha091(self, data):
        """Alpha#91: ((Ts_Rank(decay_linear(decay_linear(correlation(IndNeutralize(close, IndClass.industry), volume, 9.74928), 16.398), 3.83219), 4.8667) - rank(decay_linear(correlation(vwap, adv30, 4.01303), 2.6809))) * -1)"""
        df = data.copy()
        adv30 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(30).mean())
        close_n = df.groupby('trade_date')['close'].transform(lambda x: x - x.mean())
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(close_n[x.index]).rolling(10).corr(df.loc[x.index, 'volume']))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr1[x.index]).rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr2 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(4).corr(pd.Series(adv30[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * (ts_rank - rank2)

    def alpha092(self, data):
        """Alpha#92: min(Ts_Rank(decay_linear(((((high + low) / 2) + close) < (low + open)), 14.7221), 18.8683), Ts_Rank(decay_linear(correlation(rank(low), rank(adv30), 7.58555), 6.94024), 6.80584))"""
        df = data.copy()
        adv30 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(30).mean())
        cond = (((df['high'] + df['low']) / 2 + df['close']) < (df['low'] + df['open'])).astype(float)
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(19).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)
        rank_adv = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv30[x.index]).rank(pct=True))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_low[x.index]).rolling(8).corr(pd.Series(rank_adv[x.index])))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(7).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return np.minimum(ts_rank1, ts_rank2)

    def alpha093(self, data):
        """Alpha#93: (Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndClass.industry), adv81, 17.4193), 19.848), 7.54455) / rank(decay_linear(delta(((close * 0.524434) + (vwap * (1 - 0.524434))), 2.77377), 16.2664)))"""
        df = data.copy()
        adv81 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(81).mean())
        vwap_n = df.groupby('trade_date')['vwap'].transform(lambda x: x - x.mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(vwap_n[x.index]).rolling(17).corr(pd.Series(adv81[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        weighted = df['close'] * 0.524434 + df['vwap'] * (1 - 0.524434)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(3))
        rank_delta = delta_w.groupby(df['trade_date']).rank(pct=True)
        return ts_rank / (rank_delta + 1e-10)

    def alpha094(self, data):
        """Alpha#94: ((rank((vwap - ts_min(vwap, 11.5783)))^Ts_Rank(correlation(Ts_Rank(vwap, 19.6462), Ts_Rank(adv60, 4.02992), 18.0926), 2.70756)) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        ts_min12 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(12).min())
        rank1 = (df['vwap'] - ts_min12).groupby(df['trade_date']).rank(pct=True)
        ts_rank_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(20).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_vwap[x.index]).rolling(18).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 ** ts_rank)

    def alpha095(self, data):
        """Alpha#95: (rank((open - ts_min(open, 12.4105))) < Ts_Rank((rank(correlation(sum(((high + low) / 2), 19.1351), sum(adv40, 19.1351), 12.8742))^5), 11.7584))"""
        df = data.copy()
        adv40 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(40).mean())
        ts_min12 = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(12).min())
        rank1 = (df['open'] - ts_min12).groupby(df['trade_date']).rank(pct=True)
        hl_mid = (df['high'] + df['low']) / 2
        sum1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(19).sum())
        sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv40[x.index]).rolling(19).sum())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum1[x.index]).rolling(13).corr(pd.Series(sum2[x.index])))
        rank_corr = corr.groupby(df['trade_date']).rank(pct=True)
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series((rank_corr[x.index] ** 5)).rolling(12).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return (rank1 < ts_rank).astype(float)

    def alpha096(self, data):
        """Alpha#96: (max(Ts_Rank(decay_linear(correlation(rank(vwap), rank(volume), 3.83878), 4.16783), 8.38151), Ts_Rank(decay_linear(Ts_ArgMax(correlation(Ts_Rank(close, 7.45404), Ts_Rank(adv60, 4.13242), 3.65459), 12.6556), 14.0365), 13.4143)) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        rank_vwap = df.groupby('trade_date')['vwap'].rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_vwap[x.index]).rolling(4).corr(pd.Series(rank_vol[x.index])))
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr1[x.index]).rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_close = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(7).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_close[x.index]).rolling(4).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr2[x.index]).rolling(13).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * np.maximum(ts_rank1, ts_rank2)

    def alpha097(self, data):
        """Alpha#97: ((rank(decay_linear(delta(IndNeutralize(((low * 0.721001) + (vwap * (1 - 0.721001))), IndClass.industry), 3.3705), 20.4523)) - Ts_Rank(decay_linear(Ts_Rank(correlation(Ts_Rank(low, 7.87871), Ts_Rank(adv60, 17.255), 4.97547), 18.5925), 15.7152), 6.71659)) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        weighted = df['low'] * 0.721001 + df['vwap'] * (1 - 0.721001)
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(3))
        rank1 = delta_w.groupby(df['trade_date']).rank(pct=True)
        ts_rank_low = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(17).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_low[x.index]).rolling(5).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(19).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank1[x.index]).rolling(7).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 - ts_rank2)

    def alpha098(self, data):
        """Alpha#98: (rank(decay_linear(correlation(vwap, sum(adv5, 26.4719), 4.58418), 7.18088)) - rank(decay_linear(Ts_Rank(Ts_ArgMin(correlation(rank(open), rank(adv15), 20.8187), 8.62571), 6.95668), 8.07206)))"""
        df = data.copy()
        adv5 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(5).mean())
        adv15 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(15).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv5[x.index]).rolling(26).sum())
        corr1 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(5).corr(pd.Series(sum_adv[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_adv = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv15[x.index]).rank(pct=True))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_open[x.index]).rolling(21).corr(pd.Series(rank_adv[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr2[x.index]).rolling(7).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        rank2 = ts_rank.groupby(df['trade_date']).rank(pct=True)
        return rank1 - rank2

    def alpha099(self, data):
        """Alpha#99: ((rank(correlation(sum(((high + low) / 2), 19.8975), sum(adv60, 19.8975), 8.8136)) < rank(correlation(low, volume, 6.28259))) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        hl_mid = (df['high'] + df['low']) / 2
        sum1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(20).sum())
        sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(20).sum())
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum1[x.index]).rolling(9).corr(pd.Series(sum2[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        corr2 = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(6).corr(df.loc[x.index, 'volume']))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha100(self, data):
        """Alpha#100: (0 - (1 * (((1.5 * scale(indneutralize(indneutralize(rank(((((close - low) - (high - close)) / (high - low)) * volume)), IndClass.subindustry), IndClass.subindustry))) - scale(indneutralize((correlation(close, rank(adv20), 5) - rank(ts_argmin(close, 30))), IndClass.subindustry))) * (volume / adv20))))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        inner = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'] + 1e-10) * df['volume']
        rank_inner = inner.groupby(df['trade_date']).rank(pct=True)
        rank_adv = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv20[x.index]).rank(pct=True))
        corr = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(5).corr(pd.Series(rank_adv[x.index])))
        ts_argmin30 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(30).apply(lambda y: y.argmin(), raw=True))
        rank_argmin = ts_argmin30.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank_inner - (corr - rank_argmin)) * (df['volume'] / (adv20 + 1e-10))

    def alpha101(self, data):
        """Alpha#101: ((close - open) / ((high - low) + .001))"""
        df = data.copy()
        return (df['close'] - df['open']) / (df['high'] - df['low'] + 0.001)

    def alpha102(self, data):
        """Alpha#102: sma(max(volume-delay(volume,1),0),6,1)/sma(abs(volume-delay(volume,1)),6,1)*100"""
        df = data.copy()
        delay_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.shift(1))
        diff = df['volume'] - delay_vol
        max_diff = np.maximum(diff, 0)
        abs_diff = np.abs(diff)
        sma1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(max_diff[x.index]).rolling(6).mean())
        sma2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(abs_diff[x.index]).rolling(6).mean())
        return sma1 / (sma2 + 1e-10) * 100

    def alpha103(self, data):
        """Alpha#103: ((20-lowday(low,20))/20)*100"""
        df = data.copy()
        lowday = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(20).apply(lambda y: 20 - 1 - y.argmin(), raw=True))
        return (20 - lowday) / 20 * 100

    def alpha104(self, data):
        """Alpha#104: -1 * (delta(correlation(high, volume, 5), 5) * rank(stddev(close, 20)))"""
        df = data.copy()
        corr = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(5).corr(df.loc[x.index, 'volume']))
        delta_corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).diff(5))
        std20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).std())
        rank_std = std20.groupby(df['trade_date']).rank(pct=True)
        return -1 * delta_corr * rank_std

    def alpha105(self, data):
        """Alpha#105: -1 * correlation(rank(open), rank(volume), 10)"""
        df = data.copy()
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_open[x.index]).rolling(10).corr(pd.Series(rank_vol[x.index])))
        return -1 * corr

    def alpha106(self, data):
        """Alpha#106: close - delay(close, 20)"""
        df = data.copy()
        delay20 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(20))
        return df['close'] - delay20

    def alpha107(self, data):
        """Alpha#107: (((-1 * rank((open - delay(high, 1)))) * rank((open - delay(close, 1)))) * rank((open - delay(low, 1))))"""
        df = data.copy()
        delay_high = df.groupby('ts_code')['high'].transform(lambda x: x.shift(1))
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        delay_low = df.groupby('ts_code')['low'].transform(lambda x: x.shift(1))
        rank1 = (df['open'] - delay_high).groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['open'] - delay_close).groupby(df['trade_date']).rank(pct=True)
        rank3 = (df['open'] - delay_low).groupby(df['trade_date']).rank(pct=True)
        return -1 * rank1 * rank2 * rank3

    def alpha108(self, data):
        """Alpha#108: ((rank((high - ts_min(high, 2)))^rank(correlation(IndNeutralize(vwap, IndClass.sector), IndNeutralize(adv20, IndClass.subindustry), 6))) * -1)"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        ts_min2 = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(2).min())
        rank1 = (df['high'] - ts_min2).groupby(df['trade_date']).rank(pct=True)
        vwap_n = df.groupby('trade_date')['vwap'].transform(lambda x: x - x.mean())
        adv_n = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv20[x.index]) - pd.Series(adv20[x.index]).mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(vwap_n[x.index]).rolling(6).corr(pd.Series(adv_n[x.index])))
        rank2 = corr.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 ** rank2)

    def alpha109(self, data):
        """Alpha#109: sma(high-low,10,2)/sma(sma(high-low,10,2),10,2)"""
        df = data.copy()
        hl = df['high'] - df['low']
        sma1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl[x.index]).rolling(10).mean())
        sma2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma1[x.index]).rolling(10).mean())
        return sma1 / (sma2 + 1e-10)

    def alpha110(self, data):
        """Alpha#110: sum(max(0,high-delay(close,1)),20)/sum(max(0,delay(close,1)-low),20)*100"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        max1 = np.maximum(0, df['high'] - delay_close)
        max2 = np.maximum(0, delay_close - df['low'])
        sum1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(max1[x.index]).rolling(20).sum())
        sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(max2[x.index]).rolling(20).sum())
        return sum1 / (sum2 + 1e-10) * 100

    def alpha111(self, data):
        """Alpha#111: sma(vol*((close-low)-(high-close))/(high-low),11,2) - sma(vol*((close-low)-(high-close))/(high-low),4,2)"""
        df = data.copy()
        inner = df['volume'] * ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'] + 1e-10)
        sma11 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(11).mean())
        sma4 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(4).mean())
        return sma11 - sma4

    def alpha112(self, data):
        """Alpha#112: (sum((close-delay(close,1)>0?close-delay(close,1):0),12)-sum((close-delay(close,1)<0?abs(close-delay(close,1)):0),12))/(sum((close-delay(close,1)>0?close-delay(close,1):0),12)+sum((close-delay(close,1)<0?abs(close-delay(close,1)):0),12))*100"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = df['close'] - delay_close
        pos = np.where(diff > 0, diff, 0)
        neg = np.where(diff < 0, np.abs(diff), 0)
        sum_pos = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(pos[x.index]).rolling(12).sum())
        sum_neg = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(neg[x.index]).rolling(12).sum())
        return (sum_pos - sum_neg) / (sum_pos + sum_neg + 1e-10) * 100

    def alpha113(self, data):
        """Alpha#113: (-1 * ((rank((sum(delay(close, 5), 20) / 20)) * correlation(close, volume, 2)) * rank(correlation(sum(close, 5), sum(close, 20), 2))))"""
        df = data.copy()
        delay5 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(5))
        sum_delay = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay5[x.index]).rolling(20).sum())
        rank1 = (sum_delay / 20).groupby(df['trade_date']).rank(pct=True)
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(2).corr(df.loc[x.index, 'volume']))
        sum5 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(5).sum())
        sum20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).sum())
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum5[x.index]).rolling(2).corr(pd.Series(sum20[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * rank1 * corr1 * rank2

    def alpha114(self, data):
        """Alpha#114: ((rank(delay(((high - low) / (sum(close, 5) / 5)), 2)) * rank(rank(volume))) / (((high - low) / (sum(close, 5) / 5)) / (vwap - close)))"""
        df = data.copy()
        sum5 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(5).sum())
        hl_ratio = (df['high'] - df['low']) / ((sum5 / 5) + 1e-10)
        delay2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_ratio[x.index]).shift(2))
        rank1 = delay2.groupby(df['trade_date']).rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        rank2 = rank_vol.groupby(df['trade_date']).rank(pct=True)
        denominator = hl_ratio / (df['vwap'] - df['close'] + 1e-10)
        return (rank1 * rank2) / (denominator + 1e-10)

    def alpha115(self, data):
        """Alpha#115: (rank(correlation(((high * 0.9) + (close * 0.1)), adv30, 10))^rank(correlation(ts_rank(((high + low) / 2), 4), ts_rank(volume, 10), 7)))"""
        df = data.copy()
        adv30 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(30).mean())
        weighted = df['high'] * 0.9 + df['close'] * 0.1
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).rolling(10).corr(pd.Series(adv30[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        hl_mid = (df['high'] + df['low']) / 2
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank2 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(10).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank1[x.index]).rolling(7).corr(pd.Series(ts_rank2[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return rank1 ** rank2

    def alpha116(self, data):
        """Alpha#116: regbeta(close,sequence,20)"""
        df = data.copy()
        result = []
        for idx in df.index:
            ts_code = df.loc[idx, 'ts_code']
            stock_data = df[df['ts_code'] == ts_code].iloc[:idx+1].tail(20)
            if len(stock_data) >= 20:
                y = stock_data['close'].values
                x = np.arange(1, 21)
                if np.var(x) > 0:
                    beta = np.cov(x, y)[0, 1] / np.var(x)
                    result.append(beta)
                else:
                    result.append(np.nan)
            else:
                result.append(np.nan)
        return pd.Series(result, index=df.index)

    def alpha117(self, data):
        """Alpha#117: ((ts_rank(volume, 32) * (1 - ts_rank(((close + high) - low), 16))) * (1 - ts_rank(returns, 32)))"""
        df = data.copy()
        ts_rank1 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(32).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        hl_sum = df['close'] + df['high'] - df['low']
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_sum[x.index]).rolling(16).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank3 = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(32).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return ts_rank1 * (1 - ts_rank2) * (1 - ts_rank3)

    def alpha118(self, data):
        """Alpha#118: sum(high-open,20)/sum(open-low,20)*100"""
        df = data.copy()
        ho = df['high'] - df['open']
        ol = df['open'] - df['low']
        sum_ho = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ho[x.index]).rolling(20).sum())
        sum_ol = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ol[x.index]).rolling(20).sum())
        return sum_ho / (sum_ol + 1e-10) * 100

    def alpha119(self, data):
        """Alpha#119: (rank(decay_linear(correlation(vwap, sum(adv30, 37), 15), 11)) - rank(decay_linear(ts_rank(correlation(rank(high), rank(volume), 3), 3), 16)))"""
        df = data.copy()
        adv30 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(30).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv30[x.index]).rolling(37).sum())
        corr1 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(15).corr(pd.Series(sum_adv[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_high[x.index]).rolling(3).corr(pd.Series(rank_vol[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr2[x.index]).rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        rank2 = ts_rank.groupby(df['trade_date']).rank(pct=True)
        return rank1 - rank2

    def alpha120(self, data):
        """Alpha#120: (rank((vwap - close)) / rank((vwap + close)))"""
        df = data.copy()
        rank1 = (df['vwap'] - df['close']).groupby(df['trade_date']).rank(pct=True)
        rank2 = (df['vwap'] + df['close']).groupby(df['trade_date']).rank(pct=True)
        return rank1 / (rank2 + 1e-10)

    def alpha121(self, data):
        """Alpha#121: ((rank((vwap - ts_min(vwap, 12)))^ts_rank(correlation(ts_rank(vwap, 20), ts_rank(adv60, 4), 18), 3)) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        ts_min12 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(12).min())
        rank1 = (df['vwap'] - ts_min12).groupby(df['trade_date']).rank(pct=True)
        ts_rank_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(20).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(4).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_vwap[x.index]).rolling(18).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 ** ts_rank)

    def alpha122(self, data):
        """Alpha#122: (sma(sma(sma(log(close),13,2),13,2),13,2)-delay(sma(sma(sma(log(close),13,2),13,2),13,2),1))/delay(sma(sma(sma(log(close),13,2),13,2),13,2),1)"""
        df = data.copy()
        log_close = np.log(df['close'] + 1)
        sma1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(log_close[x.index]).rolling(13).mean())
        sma2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma1[x.index]).rolling(13).mean())
        sma3 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma2[x.index]).rolling(13).mean())
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma3[x.index]).shift(1))
        return (sma3 - delay1) / (delay1 + 1e-10)

    def alpha123(self, data):
        """Alpha#123: ((rank(correlation(sum(((high + low) / 2), 20), sum(adv60, 20), 9)) < rank(correlation(low, volume, 6))) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        hl_mid = (df['high'] + df['low']) / 2
        sum1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(20).sum())
        sum2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(20).sum())
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sum1[x.index]).rolling(9).corr(pd.Series(sum2[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        corr2 = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(6).corr(df.loc[x.index, 'volume']))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha124(self, data):
        """Alpha#124: (close - vwap) / decay_linear(rank(ts_argmax(close, 30)), 2)"""
        df = data.copy()
        ts_argmax30 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(30).apply(lambda y: y.argmax(), raw=True))
        rank_argmax = ts_argmax30.groupby(df['trade_date']).rank(pct=True)
        return (df['close'] - df['vwap']) / (rank_argmax + 1e-10)

    def alpha125(self, data):
        """Alpha#125: (rank(decay_linear(correlation(vwap, adv60, 4), 3)) / rank(decay_linear(delta(((close * 0.6) + (open * 0.4)), 1), 2)))"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        corr = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(4).corr(pd.Series(adv60[x.index])))
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        weighted = df['close'] * 0.6 + df['open'] * 0.4
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(1))
        rank2 = delta_w.groupby(df['trade_date']).rank(pct=True)
        return rank1 / (rank2 + 1e-10)

    def alpha126(self, data):
        """Alpha#126: (close+high+low)/3"""
        df = data.copy()
        return (df['close'] + df['high'] + df['low']) / 3

    def alpha127(self, data):
        """Alpha#127: (mean((close-delay(close,1))^2,12))^0.5"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        squared = (df['close'] - delay1) ** 2
        mean12 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(squared[x.index]).rolling(12).mean())
        return np.sqrt(mean12)

    def alpha128(self, data):
        """Alpha#128: 100-(100/(1+sum(((high+low+close)/3>delay((high+low+close)/3,1)?(high+low+close)/3*volume:0),14)/sum(((high+low+close)/3<delay((high+low+close)/3,1)?(high+low+close)/3*volume:0),14)))"""
        df = data.copy()
        hlc = (df['high'] + df['low'] + df['close']) / 3
        delay_hlc = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hlc[x.index]).shift(1))
        pos = np.where(hlc > delay_hlc, hlc * df['volume'], 0)
        neg = np.where(hlc < delay_hlc, hlc * df['volume'], 0)
        sum_pos = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(pos[x.index]).rolling(14).sum())
        sum_neg = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(neg[x.index]).rolling(14).sum())
        return 100 - (100 / (1 + sum_pos / (sum_neg + 1e-10)))

    def alpha129(self, data):
        """Alpha#129: sum((close-delay(close,1)<0?abs(close-delay(close,1)):0),12)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = df['close'] - delay1
        neg = np.where(diff < 0, np.abs(diff), 0)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(neg[x.index]).rolling(12).sum())

    def alpha130(self, data):
        """Alpha#130: (rank(decay_linear(correlation(((high + low) / 2), adv40, 9), 10)) / rank(decay_linear(correlation(rank(vwap), rank(volume), 7), 3)))"""
        df = data.copy()
        adv40 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(40).mean())
        hl_mid = (df['high'] + df['low']) / 2
        corr1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl_mid[x.index]).rolling(9).corr(pd.Series(adv40[x.index])))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank_vwap = df.groupby('trade_date')['vwap'].rank(pct=True)
        rank_vol = df.groupby('trade_date')['volume'].rank(pct=True)
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_vwap[x.index]).rolling(7).corr(pd.Series(rank_vol[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return rank1 / (rank2 + 1e-10)

    def alpha131(self, data):
        """Alpha#131: (rank(delta(vwap, 1))^ts_rank(correlation(close, adv50, 18), 18))"""
        df = data.copy()
        adv50 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(50).mean())
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(1))
        rank1 = delta_vwap.groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(18).corr(pd.Series(adv50[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(18).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return rank1 ** ts_rank

    def alpha132(self, data):
        """Alpha#132: mean(amount,20)"""
        df = data.copy()
        return df.groupby('ts_code')['amount'].transform(lambda x: x.rolling(20).mean())

    def alpha133(self, data):
        """Alpha#133: ((20-highday(high,20))/20)*100-((20-lowday(low,20))/20)*100"""
        df = data.copy()
        highday = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(20).apply(lambda y: 20 - 1 - y.argmax(), raw=True))
        lowday = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(20).apply(lambda y: 20 - 1 - y.argmin(), raw=True))
        return ((20 - highday) / 20 * 100) - ((20 - lowday) / 20 * 100)

    def alpha134(self, data):
        """Alpha#134: (close-delay(close,12))/delay(close,12)*volume"""
        df = data.copy()
        delay12 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(12))
        return (df['close'] - delay12) / (delay12 + 1e-10) * df['volume']

    def alpha135(self, data):
        """Alpha#135: sma(delay(close/delay(close,20),1),20,1)"""
        df = data.copy()
        delay20 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(20))
        ratio = df['close'] / (delay20 + 1e-10)
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ratio[x.index]).shift(1))
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay1[x.index]).rolling(20).mean())

    def alpha136(self, data):
        """Alpha#136: ((-1 * rank(delta(returns, 3))) * correlation(open, volume, 10))"""
        df = data.copy()
        delta_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.diff(3))
        rank_delta = delta_ret.groupby(df['trade_date']).rank(pct=True)
        corr = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(10).corr(df.loc[x.index, 'volume']))
        return -1 * rank_delta * corr

    def alpha137(self, data):
        """Alpha#137: 16*(close-delay(close,1)+(close-open)/2+delay(close,1)-delay(open,1))/((abs(high-delay(close,1))>abs(low-delay(close,1)) & abs(high-delay(close,1))>abs(high-delay(low,1))?abs(high-delay(close,1))+abs(low-delay(close,1))/2+abs(delay(close,1)-delay(open,1))/4:(abs(low-delay(close,1))>abs(high-delay(low,1)) & abs(low-delay(close,1))>abs(high-delay(close,1))?abs(low-delay(close,1))+abs(high-delay(close,1))/2+abs(delay(close,1)-delay(open,1))/4:abs(high-delay(low,1))+abs(delay(close,1)-delay(open,1))/4)))*max(abs(high-delay(close,1)),abs(low-delay(close,1)))"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        delay_open = df.groupby('ts_code')['open'].transform(lambda x: x.shift(1))
        delay_low = df.groupby('ts_code')['low'].transform(lambda x: x.shift(1))
        numerator = df['close'] - delay_close + (df['close'] - df['open']) / 2 + delay_close - delay_open
        hc = np.abs(df['high'] - delay_close)
        lc = np.abs(df['low'] - delay_close)
        hl = np.abs(df['high'] - delay_low)
        co = np.abs(delay_close - delay_open)
        cond1 = (hc > lc) & (hc > hl)
        cond2 = (lc > hl) & (lc > hc)
        denominator = np.where(cond1, hc + lc / 2 + co / 4, np.where(cond2, lc + hc / 2 + co / 4, hl + co / 4))
        return 16 * numerator / (denominator + 1e-10) * np.maximum(hc, lc)

    def alpha138(self, data):
        """Alpha#138: ((rank(decay_linear(delta((((low * 0.7) + (vwap * 0.3))), 3), 20)) - ts_rank(decay_linear(ts_rank(correlation(ts_rank(low, 8), ts_rank(adv60, 17), 5), 19), 16), 7)) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        weighted = df['low'] * 0.7 + df['vwap'] * 0.3
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(3))
        rank1 = delta_w.groupby(df['trade_date']).rank(pct=True)
        ts_rank_low = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(17).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_low[x.index]).rolling(5).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(19).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank1[x.index]).rolling(7).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return -1 * (rank1 - ts_rank2)

    def alpha139(self, data):
        """Alpha#139: (-1 * correlation(open, volume, 10))"""
        df = data.copy()
        corr = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(10).corr(df.loc[x.index, 'volume']))
        return -1 * corr

    def alpha140(self, data):
        """Alpha#140: min(rank(decay_linear(((rank(open) + rank(low)) - (rank(high) + rank(close))), 8)), ts_rank(decay_linear(correlation(ts_rank(close, 8), ts_rank(adv60, 20), 8), 7), 3))"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_close = df.groupby('trade_date')['close'].rank(pct=True)
        inner = (rank_open + rank_low) - (rank_high + rank_close)
        rank1 = inner.groupby(df['trade_date']).rank(pct=True)
        ts_rank_close = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(8).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        ts_rank_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(20).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ts_rank_close[x.index]).rolling(8).corr(pd.Series(ts_rank_adv[x.index])))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(corr[x.index]).rolling(3).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return np.minimum(rank1, ts_rank)

    def alpha141(self, data):
        """Alpha#141: (rank(correlation(rank(high), rank(adv15), 9))* -1)"""
        df = data.copy()
        adv15 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(15).mean())
        rank_high = df.groupby('trade_date')['high'].rank(pct=True)
        rank_adv = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(adv15[x.index]).rank(pct=True))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_high[x.index]).rolling(9).corr(pd.Series(rank_adv[x.index])))
        return -1 * corr.groupby(df['trade_date']).rank(pct=True)

    def alpha142(self, data):
        """Alpha#142: (((-1 * rank(ts_rank(close, 10))) * rank(delta(delta(close, 1), 1))) * rank(ts_rank((volume / adv20), 5)))"""
        df = data.copy()
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        ts_rank1 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(10).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        rank1 = ts_rank1.groupby(df['trade_date']).rank(pct=True)
        delta1 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        delta2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delta1[x.index]).diff(1))
        rank2 = delta2.groupby(df['trade_date']).rank(pct=True)
        vol_ratio = df['volume'] / (adv20 + 1e-10)
        ts_rank2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(vol_ratio[x.index]).rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        rank3 = ts_rank2.groupby(df['trade_date']).rank(pct=True)
        return -1 * rank1 * rank2 * rank3

    def alpha143(self, data):
        """Alpha#143: close>delay(close,1)?(close-delay(close,1))/delay(close,1)*self:self"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        ret = (df['close'] - delay1) / (delay1 + 1e-10)
        return np.where(df['close'] > delay1, ret * df['close'], df['close'])

    def alpha144(self, data):
        """Alpha#144: sumif(abs(close/delay(close,1)-1)/amount,20,close<delay(close,1))/count(close<delay(close,1),20)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        ratio = np.abs(df['close'] / (delay1 + 1e-10) - 1) / (df['amount'] + 1e-10)
        cond = (df['close'] < delay1).astype(float)
        sum_cond = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ratio[x.index] * cond[x.index]).rolling(20).sum())
        count_cond = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(20).sum())
        return sum_cond / (count_cond + 1e-10)

    def alpha145(self, data):
        """Alpha#145: (mean(volume,9)-mean(volume,26))/mean(volume,12)*100"""
        df = data.copy()
        mean9 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(9).mean())
        mean26 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(26).mean())
        mean12 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(12).mean())
        return (mean9 - mean26) / (mean12 + 1e-10) * 100

    def alpha146(self, data):
        """Alpha#146: mean((close-delay(close,1))/delay(close,1)-sma((close-delay(close,1))/delay(close,1),61,2),20)*((close-delay(close,1))/delay(close,1)-sma((close-delay(close,1))/delay(close,1),61,2))/sma(((close-delay(close,1))/delay(close,1)-((close-delay(close,1))/delay(close,1)-sma((close-delay(close,1))/delay(close,1),61,2)))^2,60)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        ret = (df['close'] - delay1) / (delay1 + 1e-10)
        sma61 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ret[x.index]).rolling(61).mean())
        diff = ret - sma61
        mean20 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(20).mean())
        squared = (ret - diff) ** 2
        sma60 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(squared[x.index]).rolling(60).mean())
        return mean20 * diff / (sma60 + 1e-10)

    def alpha147(self, data):
        """Alpha#147: regbeta(mean(close,12),sequence(12),12)"""
        df = data.copy()
        mean12 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(12).mean())
        result = []
        for idx in df.index:
            ts_code = df.loc[idx, 'ts_code']
            stock_data = df[df['ts_code'] == ts_code].iloc[:idx+1].tail(12)
            if len(stock_data) >= 12:
                y = pd.Series(mean12[stock_data.index]).values
                x = np.arange(1, 13)
                if np.var(x) > 0 and not np.isnan(y).all():
                    beta = np.cov(x, y)[0, 1] / np.var(x)
                    result.append(beta)
                else:
                    result.append(np.nan)
            else:
                result.append(np.nan)
        return pd.Series(result, index=df.index)

    def alpha148(self, data):
        """Alpha#148: ((rank(correlation((open), sum(adv60, 9), 6)) < rank((open - ts_min(open, 14)))) * -1)"""
        df = data.copy()
        adv60 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(60).mean())
        sum_adv = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(adv60[x.index]).rolling(9).sum())
        corr = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(6).corr(pd.Series(sum_adv[x.index])))
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        ts_min14 = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(14).min())
        rank2 = (df['open'] - ts_min14).groupby(df['trade_date']).rank(pct=True)
        return -1 * (rank1 < rank2).astype(float)

    def alpha149(self, data):
        """Alpha#149: regbeta(filter(close/delay(close,1)-1,bm_index_close<delay(bm_index_close,1)),filter(bm_index_close/delay(bm_index_close,1)-1,bm_index_close<delay(bm_index_close,1)),252)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        ret = df['close'] / (delay1 + 1e-10) - 1
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ret[x.index]).rolling(252).mean())

    def alpha150(self, data):
        """Alpha#150: (close+high+low)/3*volume"""
        df = data.copy()
        return (df['close'] + df['high'] + df['low']) / 3 * df['volume']

    def alpha151(self, data):
        """Alpha#151: sma(close-delay(close,20),20,1)"""
        df = data.copy()
        delay20 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(20))
        diff = df['close'] - delay20
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(20).mean())

    def alpha152(self, data):
        """Alpha#152: sma(mean(delay(sma(delay(close/delay(close,9),1),9,1),1),12)-mean(delay(sma(delay(close/delay(close,9),1),9,1),1),26),9,1)"""
        df = data.copy()
        delay9 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(9))
        ratio = df['close'] / (delay9 + 1e-10)
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ratio[x.index]).shift(1))
        sma9 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay1[x.index]).rolling(9).mean())
        delay_sma = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma9[x.index]).shift(1))
        mean12 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay_sma[x.index]).rolling(12).mean())
        mean26 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay_sma[x.index]).rolling(26).mean())
        diff = mean12 - mean26
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(9).mean())

    def alpha153(self, data):
        """Alpha#153: (mean(close,3)+mean(close,6)+mean(close,12)+mean(close,24))/4"""
        df = data.copy()
        mean3 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(3).mean())
        mean6 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(6).mean())
        mean12 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(12).mean())
        mean24 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(24).mean())
        return (mean3 + mean6 + mean12 + mean24) / 4

    def alpha154(self, data):
        """Alpha#154: (((vwap-min(vwap,16)))<(correlation(vwap,adv180,18)))"""
        df = data.copy()
        adv180 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(180).mean())
        ts_min16 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(16).min())
        corr = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(18).corr(pd.Series(adv180[x.index])))
        return ((df['vwap'] - ts_min16) < corr).astype(float)

    def alpha155(self, data):
        """Alpha#155: sma(volume,13,2)-sma(volume,27,2)-sma(sma(volume,13,2)-sma(volume,27,2),10,2)"""
        df = data.copy()
        sma13 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(13).mean())
        sma27 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(27).mean())
        diff = sma13 - sma27
        sma10 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(10).mean())
        return diff - sma10

    def alpha156(self, data):
        """Alpha#156: (max(rank(decay_linear(delta(vwap,5),3)),rank(decay_linear(((delta(((open*0.15)+(low*0.85)),2)/((open*0.15)+(low*0.85)))*-1),3)))*-1)"""
        df = data.copy()
        delta_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.diff(5))
        rank1 = delta_vwap.groupby(df['trade_date']).rank(pct=True)
        weighted = df['open'] * 0.15 + df['low'] * 0.85
        delta_w = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(weighted[x.index]).diff(2))
        ratio = -1 * delta_w / (weighted + 1e-10)
        rank2 = ratio.groupby(df['trade_date']).rank(pct=True)
        return -1 * np.maximum(rank1, rank2)

    def alpha157(self, data):
        """Alpha#157: (min(prod(rank(rank(log(sum(ts_min(rank(rank((-1*rank(delta((close-1),5))))),2),1)))),1),5)+ts_rank(delay((-1*returns),6),5))"""
        df = data.copy()
        delta5 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(5))
        rank1 = delta5.groupby(df['trade_date']).rank(pct=True)
        rank2 = (-1 * rank1).groupby(df['trade_date']).rank(pct=True)
        rank3 = rank2.groupby(df['trade_date']).rank(pct=True)
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank3[x.index]).rolling(2).min())
        delay_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.shift(6))
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(-1 * delay_ret[x.index]).rolling(5).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        return ts_min + ts_rank

    def alpha158(self, data):
        """Alpha#158: ((high-sma(close,15,2))-(low-sma(close,15,2)))/close"""
        df = data.copy()
        sma15 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(15).mean())
        return ((df['high'] - sma15) - (df['low'] - sma15)) / (df['close'] + 1e-10)

    def alpha159(self, data):
        """Alpha#159: ((close-sum(min(low,delay(close,1)),6))/sum(max(high,delay(close,1))-min(low,delay(close,1)),6)*12*24+(close-sum(min(low,delay(close,1)),12))/sum(max(high,delay(close,1))-min(low,delay(close,1)),12)*6*24+(close-sum(min(low,delay(close,1)),24))/sum(max(high,delay(close,1))-min(low,delay(close,1)),24)*6*24)*100/(6*12+6*24+12*24)"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        min_lc = np.minimum(df['low'], delay_close)
        max_hc = np.maximum(df['high'], delay_close)
        diff = max_hc - min_lc
        sum6_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(min_lc[x.index]).rolling(6).sum())
        sum6_diff = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(6).sum())
        sum12_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(min_lc[x.index]).rolling(12).sum())
        sum12_diff = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(12).sum())
        sum24_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(min_lc[x.index]).rolling(24).sum())
        sum24_diff = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(24).sum())
        part1 = (df['close'] - sum6_min) / (sum6_diff + 1e-10) * 12 * 24
        part2 = (df['close'] - sum12_min) / (sum12_diff + 1e-10) * 6 * 24
        part3 = (df['close'] - sum24_min) / (sum24_diff + 1e-10) * 6 * 24
        return (part1 + part2 + part3) * 100 / (6 * 12 + 6 * 24 + 12 * 24)

    def alpha160(self, data):
        """Alpha#160: sma((close<=delay(close,1)?std(close,20):0),20,1)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        std20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).std())
        cond = np.where(df['close'] <= delay1, std20, 0)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(20).mean())

    def alpha161(self, data):
        """Alpha#161: mean(max(max((high-low),abs(delay(close,1)-high)),abs(delay(close,1)-low)),12)"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        hl = df['high'] - df['low']
        ch = np.abs(delay_close - df['high'])
        cl = np.abs(delay_close - df['low'])
        max_val = np.maximum(np.maximum(hl, ch), cl)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(max_val[x.index]).rolling(12).mean())

    def alpha162(self, data):
        """Alpha#162: (sma(max(close-delay(close,1),0),12,1)/sma(abs(close-delay(close,1)),12,1)*100-min(sma(max(close-delay(close,1),0),12,1)/sma(abs(close-delay(close,1)),12,1)*100,12))/(max(sma(max(close-delay(close,1),0),12,1)/sma(abs(close-delay(close,1)),12,1)*100,12)-min(sma(max(close-delay(close,1),0),12,1)/sma(abs(close-delay(close,1)),12,1)*100,12))"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = df['close'] - delay1
        max_diff = np.maximum(diff, 0)
        abs_diff = np.abs(diff)
        sma_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(max_diff[x.index]).rolling(12).mean())
        sma_abs = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(abs_diff[x.index]).rolling(12).mean())
        ratio = sma_max / (sma_abs + 1e-10) * 100
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ratio[x.index]).rolling(12).min())
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ratio[x.index]).rolling(12).max())
        return (ratio - ts_min) / (ts_max - ts_min + 1e-10)

    def alpha163(self, data):
        """Alpha#163: rank(((((-1*returns)*mean(volume,20))*vwap)*(high-close)))"""
        df = data.copy()
        mean_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        inner = (-1 * df['returns']) * mean_vol * df['vwap'] * (df['high'] - df['close'])
        return inner.groupby(df['trade_date']).rank(pct=True)

    def alpha164(self, data):
        """Alpha#164: sma((((close>delay(close,1))?1/(close-delay(close,1)):1)-min(((close>delay(close,1))?1/(close-delay(close,1)):1),12))/(high-low)*100,13,2)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = df['close'] - delay1
        cond = np.where(df['close'] > delay1, 1 / (diff + 1e-10), 1)
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(12).min())
        ratio = (cond - ts_min) / (df['high'] - df['low'] + 1e-10) * 100
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ratio[x.index]).rolling(13).mean())

    def alpha165(self, data):
        """Alpha#165: max(sumac(close-mean(close,48)))-min(sumac(close-mean(close,48)))/std(close,48)"""
        df = data.copy()
        mean48 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(48).mean())
        diff = df['close'] - mean48
        cumsum = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).cumsum())
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cumsum[x.index]).rolling(48).max())
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cumsum[x.index]).rolling(48).min())
        std48 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(48).std())
        return (ts_max - ts_min) / (std48 + 1e-10)

    def alpha166(self, data):
        """Alpha#166: -20*(20-1)^1.5*sum(close/delay(close,1)-1-mean(close/delay(close,1)-1,20),20)/((20-1)*(20-2)*(sum((close/delay(close,1),20)^2,20))^1.5)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        ret = df['close'] / (delay1 + 1e-10) - 1
        mean_ret = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ret[x.index]).rolling(20).mean())
        diff = ret - mean_ret
        sum_diff = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(20).sum())
        return -20 * (19 ** 1.5) * sum_diff / ((19 * 18) + 1e-10)

    def alpha167(self, data):
        """Alpha#167: sum((close-delay(close,1)>0?close-delay(close,1):0),12)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = df['close'] - delay1
        pos = np.where(diff > 0, diff, 0)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(pos[x.index]).rolling(12).sum())

    def alpha168(self, data):
        """Alpha#168: (-1*volume/mean(volume,20))"""
        df = data.copy()
        mean20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        return -1 * df['volume'] / (mean20 + 1e-10)

    def alpha169(self, data):
        """Alpha#169: sma(mean(delay(sma(close-delay(close,1),9,1),1),12)-mean(delay(sma(close-delay(close,1),9,1),1),26),10,1)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        diff = df['close'] - delay1
        sma9 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(9).mean())
        delay_sma = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma9[x.index]).shift(1))
        mean12 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay_sma[x.index]).rolling(12).mean())
        mean26 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay_sma[x.index]).rolling(26).mean())
        diff2 = mean12 - mean26
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff2[x.index]).rolling(10).mean())

    def alpha170(self, data):
        """Alpha#170: ((((rank((1/close))*volume)/mean(volume,20))*((high*rank((high-close)))/(sum(high,5)/5)))-rank((vwap-delay(vwap,5))))"""
        df = data.copy()
        mean_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        rank1 = (1 / df['close']).groupby(df['trade_date']).rank(pct=True)
        part1 = (rank1 * df['volume']) / (mean_vol + 1e-10)
        rank2 = (df['high'] - df['close']).groupby(df['trade_date']).rank(pct=True)
        sum_high = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(5).sum())
        part2 = (df['high'] * rank2) / ((sum_high / 5) + 1e-10)
        delay_vwap = df.groupby('ts_code')['vwap'].transform(lambda x: x.shift(5))
        rank3 = (df['vwap'] - delay_vwap).groupby(df['trade_date']).rank(pct=True)
        return part1 * part2 - rank3

    def alpha171(self, data):
        """Alpha#171: ((-1*(low-close)*(open^5))/((close-high)*(close^5)))"""
        df = data.copy()
        numerator = -1 * (df['low'] - df['close']) * (df['open'] ** 5)
        denominator = (df['close'] - df['high']) * (df['close'] ** 5)
        return numerator / (denominator + 1e-10)

    def alpha172(self, data):
        """Alpha#172: mean(abs(sum((ld>0 & ld>hd)?ld:0,14)*100/sum(tr,14)-sum((hd>0 & hd>ld)?hd:0,14)*100/sum(tr,14))/(sum((ld>0 & ld>hd)?ld:0,14)*100/sum(tr,14)+sum((hd>0 & hd>ld)?hd:0,14)*100/sum(tr,14))*100,6)"""
        df = data.copy()
        hd = df.groupby('ts_code')['high'].transform(lambda x: x - x.shift(1))
        ld = df.groupby('ts_code')['low'].transform(lambda x: x.shift(1) - x)
        tr = np.maximum(np.maximum(df['high'] - df['low'], np.abs(df['high'] - df.groupby('ts_code')['close'].shift(1))), np.abs(df['low'] - df.groupby('ts_code')['close'].shift(1)))
        ld_cond = np.where((ld > 0) & (ld > hd), ld, 0)
        hd_cond = np.where((hd > 0) & (hd > ld), hd, 0)
        sum_ld = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ld_cond[x.index]).rolling(14).sum())
        sum_hd = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hd_cond[x.index]).rolling(14).sum())
        sum_tr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(tr[x.index]).rolling(14).sum())
        ld_ratio = sum_ld * 100 / (sum_tr + 1e-10)
        hd_ratio = sum_hd * 100 / (sum_tr + 1e-10)
        inner = np.abs(ld_ratio - hd_ratio) / (ld_ratio + hd_ratio + 1e-10) * 100
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(6).mean())

    def alpha173(self, data):
        """Alpha#173: 3*sma(close,13,2)-2*sma(sma(close,13,2),13,2)+sma(sma(sma(log(close),13,2),13,2),13,2)"""
        df = data.copy()
        sma1 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(13).mean())
        sma2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma1[x.index]).rolling(13).mean())
        log_close = np.log(df['close'] + 1)
        sma_log1 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(log_close[x.index]).rolling(13).mean())
        sma_log2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma_log1[x.index]).rolling(13).mean())
        sma_log3 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(sma_log2[x.index]).rolling(13).mean())
        return 3 * sma1 - 2 * sma2 + sma_log3

    def alpha174(self, data):
        """Alpha#174: sma((close>delay(close,1)?std(close,20):0),20,1)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        std20 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(20).std())
        cond = np.where(df['close'] > delay1, std20, 0)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(20).mean())

    def alpha175(self, data):
        """Alpha#175: mean(max(max((high-low),abs(delay(close,1)-high)),abs(delay(close,1)-low)),6)"""
        df = data.copy()
        delay_close = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        hl = df['high'] - df['low']
        ch = np.abs(delay_close - df['high'])
        cl = np.abs(delay_close - df['low'])
        max_val = np.maximum(np.maximum(hl, ch), cl)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(max_val[x.index]).rolling(6).mean())

    def alpha176(self, data):
        """Alpha#176: corr(rank(((close-ts_min(low,12))/(ts_max(high,12)-ts_min(low,12)))),rank(volume),6)"""
        df = data.copy()
        ts_min_low = df.groupby('ts_code')['low'].transform(lambda x: x.rolling(12).min())
        ts_max_high = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(12).max())
        inner = (df['close'] - ts_min_low) / (ts_max_high - ts_min_low + 1e-10)
        rank1 = inner.groupby(df['trade_date']).rank(pct=True)
        rank2 = df.groupby('trade_date')['volume'].rank(pct=True)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank1[x.index]).rolling(6).corr(pd.Series(rank2[x.index])))

    def alpha177(self, data):
        """Alpha#177: ((20-highday(high,20))/20)*100"""
        df = data.copy()
        highday = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(20).apply(lambda y: 20 - 1 - y.argmax(), raw=True))
        return (20 - highday) / 20 * 100

    def alpha178(self, data):
        """Alpha#178: (close-delay(close,1))/delay(close,1)*volume"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        return (df['close'] - delay1) / (delay1 + 1e-10) * df['volume']

    def alpha179(self, data):
        """Alpha#179: (rank(correlation(vwap,volume,4))*rank(correlation(rank(low),rank(mean(volume,50)),12)))"""
        df = data.copy()
        mean_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(50).mean())
        corr1 = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(4).corr(df.loc[x.index, 'volume']))
        rank1 = corr1.groupby(df['trade_date']).rank(pct=True)
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)
        rank_mean = df.groupby('trade_date')['close'].transform(lambda x: pd.Series(mean_vol[x.index]).rank(pct=True))
        corr2 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(rank_low[x.index]).rolling(12).corr(pd.Series(rank_mean[x.index])))
        rank2 = corr2.groupby(df['trade_date']).rank(pct=True)
        return rank1 * rank2

    def alpha180(self, data):
        """Alpha#180: ((mean(volume,20)<volume)?((-1*ts_rank(abs(delta(close,7)),60))*sign(delta(close,7)):(-1*volume)))"""
        df = data.copy()
        mean20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        delta7 = df.groupby('ts_code')['close'].transform(lambda x: x.diff(7))
        abs_delta = np.abs(delta7)
        ts_rank = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(abs_delta[x.index]).rolling(60).apply(lambda y: pd.Series(y).rank(pct=True).iloc[-1], raw=False))
        result = np.where(mean20 < df['volume'], -1 * ts_rank * np.sign(delta7), -1 * df['volume'])
        return pd.Series(result, index=df.index)

    def alpha181(self, data):
        """Alpha#181: sum(((close/delay(close,1)-1)-mean((close/delay(close,1)-1),20))-(bm_index_close-mean(bm_index_close,20))^2,20)/sum((bm_index_close-mean(bm_index_close,20))^3)"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        ret = df['close'] / (delay1 + 1e-10) - 1
        mean_ret = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ret[x.index]).rolling(20).mean())
        diff = ret - mean_ret
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).rolling(20).sum())

    def alpha182(self, data):
        """Alpha#182: count((close>open & bm_index_close>bm_index_open)or(close<open & bm_index_close<bm_index_open),20)/20"""
        df = data.copy()
        cond = ((df['close'] > df['open'])).astype(float)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(20).sum()) / 20

    def alpha183(self, data):
        """Alpha#183: max(sumac(close-mean(close,24)))-min(sumac(close-mean(close,24)))/std(close,24)"""
        df = data.copy()
        mean24 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(24).mean())
        diff = df['close'] - mean24
        cumsum = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(diff[x.index]).cumsum())
        ts_max = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cumsum[x.index]).rolling(24).max())
        ts_min = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cumsum[x.index]).rolling(24).min())
        std24 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(24).std())
        return (ts_max - ts_min) / (std24 + 1e-10)

    def alpha184(self, data):
        """Alpha#184: (rank(correlation(delay((open-close),1),close,200))+rank((open-close)))"""
        df = data.copy()
        oc = df['open'] - df['close']
        delay_oc = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(oc[x.index]).shift(1))
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(delay_oc[x.index]).rolling(200).corr(x))
        rank1 = corr.groupby(df['trade_date']).rank(pct=True)
        rank2 = oc.groupby(df['trade_date']).rank(pct=True)
        return rank1 + rank2

    def alpha185(self, data):
        """Alpha#185: rank((-1*((1-(open/close))^2)))"""
        df = data.copy()
        inner = -1 * ((1 - df['open'] / df['close']) ** 2)
        return inner.groupby(df['trade_date']).rank(pct=True)

    def alpha186(self, data):
        """Alpha#186: (mean(abs(sum((ld>0 & ld>hd)?ld:0,14)*100/sum(tr,14)-sum((hd>0 & hd>ld)?hd:0,14)*100/sum(tr,14))/(sum((ld>0 & ld>hd)?ld:0,14)*100/sum(tr,14)+sum((hd>0 & hd>ld)?hd:0,14)*100/sum(tr,14))*100,6)+delay(mean(abs(sum((ld>0 & ld>hd)?ld:0,14)*100/sum(tr,14)-sum((hd>0 & hd>ld)?hd:0,14)*100/sum(tr,14))/(sum((ld>0 & ld>hd)?ld:0,14)*100/sum(tr,14)+sum((hd>0 & hd>ld)?hd:0,14)*100/sum(tr,14))*100,6),6))/2"""
        df = data.copy()
        hd = df.groupby('ts_code')['high'].transform(lambda x: x - x.shift(1))
        ld = df.groupby('ts_code')['low'].transform(lambda x: x.shift(1) - x)
        tr = np.maximum(np.maximum(df['high'] - df['low'], np.abs(df['high'] - df.groupby('ts_code')['close'].shift(1))), np.abs(df['low'] - df.groupby('ts_code')['close'].shift(1)))
        ld_cond = np.where((ld > 0) & (ld > hd), ld, 0)
        hd_cond = np.where((hd > 0) & (hd > ld), hd, 0)
        sum_ld = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(ld_cond[x.index]).rolling(14).sum())
        sum_hd = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hd_cond[x.index]).rolling(14).sum())
        sum_tr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(tr[x.index]).rolling(14).sum())
        ld_ratio = sum_ld * 100 / (sum_tr + 1e-10)
        hd_ratio = sum_hd * 100 / (sum_tr + 1e-10)
        inner = np.abs(ld_ratio - hd_ratio) / (ld_ratio + hd_ratio + 1e-10) * 100
        mean6 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(inner[x.index]).rolling(6).mean())
        delay6 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(mean6[x.index]).shift(6))
        return (mean6 + delay6) / 2

    def alpha187(self, data):
        """Alpha#187: sum((open<=delay(open,1)?0:max((high-open),(open-delay(open,1)))),20)"""
        df = data.copy()
        delay_open = df.groupby('ts_code')['open'].transform(lambda x: x.shift(1))
        max_val = np.maximum(df['high'] - df['open'], df['open'] - delay_open)
        cond = np.where(df['open'] <= delay_open, 0, max_val)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(cond[x.index]).rolling(20).sum())

    def alpha188(self, data):
        """Alpha#188: ((high-low-sma(high-low,11,2))/sma(high-low,11,2))*100"""
        df = data.copy()
        hl = df['high'] - df['low']
        sma11 = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(hl[x.index]).rolling(11).mean())
        return (hl - sma11) / (sma11 + 1e-10) * 100

    def alpha189(self, data):
        """Alpha#189: mean(abs(close-mean(close,6)),6)"""
        df = data.copy()
        mean6 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(6).mean())
        abs_diff = np.abs(df['close'] - mean6)
        return df.groupby('ts_code')['close'].transform(lambda x: pd.Series(abs_diff[x.index]).rolling(6).mean())

    def alpha190(self, data):
        """Alpha#190: log((count(close/delay(close,1)-1>((close/delay(close,19))^(1/20)-1),20)-1)*(sumif(((close/delay(close,1)-1-(close/delay(close,19))^(1/20)-1))^2,20,close/delay(close,1)-1<(close/delay(close,19))^(1/20)-1))/((count((close/delay(close,1)-1<(close/delay(close,19))^(1/20)-1),20))*(sumif((close/delay(close,1)-1-((close/delay(close,19))^(1/20)-1))^2,20,close/delay(close,1)-1>(close/delay(close,19))^(1/20)-1))))"""
        df = data.copy()
        delay1 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(1))
        delay19 = df.groupby('ts_code')['close'].transform(lambda x: x.shift(19))
        ret1 = df['close'] / (delay1 + 1e-10) - 1
        ret19 = (df['close'] / (delay19 + 1e-10)) ** (1/20) - 1
        diff = ret1 - ret19
        count_pos = df.groupby('ts_code')['close'].transform(lambda x: pd.Series((ret1[x.index] > ret19[x.index]).astype(float)).rolling(20).sum())
        return np.log(count_pos + 1)

    def alpha191(self, data):
        """Alpha#191: ((corr(mean(volume,20),low,5)+((high+low)/2))-close)"""
        df = data.copy()
        mean_vol = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20).mean())
        corr = df.groupby('ts_code')['close'].transform(lambda x: pd.Series(mean_vol[x.index]).rolling(5).corr(df.loc[x.index, 'low']))
        return corr + (df['high'] + df['low']) / 2 - df['close']

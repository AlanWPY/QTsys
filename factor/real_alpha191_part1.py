"""
真实的Alpha191因子实现 - 基于WorldQuant研究
"""
import numpy as np
import pandas as pd
from factor.alpha191_operators import Alpha191Operators as ops

class RealAlpha191:
    """真实的Alpha191因子计算"""

    def __init__(self, data):
        """
        data: DataFrame with columns [ts_code, trade_date, open, high, low, close, volume, amount]
        """
        self.data = data.sort_values(['ts_code', 'trade_date'])

    def prepare_data(self):
        """准备计算所需的基础数据"""
        df = self.data.copy()
        df['returns'] = df.groupby('ts_code')['close'].pct_change()
        df['vwap'] = df['amount'] / (df['volume'] + 1e-10)
        return df

    def alpha001(self, df):
        """Alpha#1: rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5"""
        returns = df.groupby('ts_code')['returns'].transform(lambda x: x)
        close = df.groupby('ts_code')['close'].transform(lambda x: x)

        # (returns < 0) ? stddev(returns, 20) : close
        std_ret = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(20, min_periods=20).std())
        condition = returns < 0
        inner = pd.Series(np.where(condition, std_ret, close), index=df.index)

        # SignedPower(..., 2)
        signed_power = np.sign(inner) * (np.abs(inner) ** 2)

        # Ts_ArgMax(..., 5)
        ts_argmax = df.groupby('ts_code')[signed_power.name if hasattr(signed_power, 'name') else 0].transform(
            lambda x: pd.Series(signed_power[x.index]).rolling(5, min_periods=5).apply(
                lambda y: y.argmax() if len(y) == 5 else np.nan, raw=True
            )
        )

        # rank - 0.5
        result = ts_argmax.groupby(df['trade_date']).rank(pct=True) - 0.5
        return result

    def alpha002(self, df):
        """Alpha#2: -1 * correlation(rank(delta(log(volume), 2)), rank(((close-open)/open)), 6)"""
        # delta(log(volume), 2)
        log_vol = np.log(df['volume'] + 1)
        delta_log_vol = df.groupby('ts_code')[log_vol.name if hasattr(log_vol, 'name') else 0].transform(
            lambda x: pd.Series(log_vol[x.index]).diff(2)
        )

        # rank(delta(...))
        rank_delta = delta_log_vol.groupby(df['trade_date']).rank(pct=True)

        # (close-open)/open
        price_change = (df['close'] - df['open']) / df['open']

        # rank((close-open)/open)
        rank_price = price_change.groupby(df['trade_date']).rank(pct=True)

        # correlation(..., 6)
        corr = df.groupby('ts_code').apply(
            lambda x: pd.Series(rank_delta[x.index]).rolling(6, min_periods=6).corr(
                pd.Series(rank_price[x.index])
            )
        ).reset_index(level=0, drop=True)

        return -1 * corr

    def alpha003(self, df):
        """Alpha#3: -1 * correlation(rank(open), rank(volume), 10)"""
        rank_open = df.groupby('trade_date')['open'].rank(pct=True)
        rank_volume = df.groupby('trade_date')['volume'].rank(pct=True)

        corr = df.groupby('ts_code').apply(
            lambda x: pd.Series(rank_open[x.index]).rolling(10, min_periods=10).corr(
                pd.Series(rank_volume[x.index])
            )
        ).reset_index(level=0, drop=True)

        return -1 * corr

    def alpha004(self, df):
        """Alpha#4: -1 * Ts_Rank(rank(low), 9)"""
        rank_low = df.groupby('trade_date')['low'].rank(pct=True)

        ts_rank = df.groupby('ts_code')[rank_low.name if hasattr(rank_low, 'name') else 0].transform(
            lambda x: pd.Series(rank_low[x.index]).rolling(9, min_periods=9).apply(
                lambda y: pd.Series(y).rank(pct=True).iloc[-1] if len(y) == 9 else np.nan, raw=False
            )
        )

        return -1 * ts_rank

    def alpha005(self, df):
        """Alpha#5: rank((open - (sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vwap))))"""
        # sum(vwap, 10) / 10
        vwap_ma = df.groupby('ts_code')['vwap'].transform(lambda x: x.rolling(10, min_periods=10).sum() / 10)

        # rank(open - vwap_ma)
        rank1 = (df['open'] - vwap_ma).groupby(df['trade_date']).rank(pct=True)

        # rank(close - vwap)
        rank2 = (df['close'] - df['vwap']).groupby(df['trade_date']).rank(pct=True)

        return rank1 * (-1 * np.abs(rank2))

    def alpha006(self, df):
        """Alpha#6: -1 * correlation(open, volume, 10)"""
        corr = df.groupby('ts_code').apply(
            lambda x: x['open'].rolling(10, min_periods=10).corr(x['volume'])
        ).reset_index(level=0, drop=True)

        return -1 * corr

    def alpha007(self, df):
        """Alpha#7: ((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1 * 1))"""
        adv20 = df.groupby('ts_code')['volume'].transform(lambda x: x.rolling(20, min_periods=20).mean())
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(7))
        abs_delta = np.abs(delta_close)

        ts_rank = df.groupby('ts_code')[abs_delta.name if hasattr(abs_delta, 'name') else 0].transform(
            lambda x: pd.Series(abs_delta[x.index]).rolling(60, min_periods=60).apply(
                lambda y: pd.Series(y).rank(pct=True).iloc[-1] if len(y) == 60 else np.nan, raw=False
            )
        )

        sign_delta = np.sign(delta_close)
        result = np.where(adv20 < df['volume'], -1 * ts_rank * sign_delta, -1)

        return pd.Series(result, index=df.index)

    def alpha008(self, df):
        """Alpha#8: -1 * rank(((sum(open, 5) * sum(returns, 5)) - delay((sum(open, 5) * sum(returns, 5)), 10)))"""
        sum_open = df.groupby('ts_code')['open'].transform(lambda x: x.rolling(5, min_periods=5).sum())
        sum_returns = df.groupby('ts_code')['returns'].transform(lambda x: x.rolling(5, min_periods=5).sum())

        product = sum_open * sum_returns
        delay_product = df.groupby('ts_code')[product.name if hasattr(product, 'name') else 0].transform(
            lambda x: pd.Series(product[x.index]).shift(10)
        )

        result = (product - delay_product).groupby(df['trade_date']).rank(pct=True)
        return -1 * result

    def alpha009(self, df):
        """Alpha#9: ((0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : ((ts_max(delta(close, 1), 5) < 0) ? delta(close, 1) : (-1 * delta(close, 1))))"""
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        ts_min_delta = df.groupby('ts_code')[delta_close.name if hasattr(delta_close, 'name') else 0].transform(
            lambda x: pd.Series(delta_close[x.index]).rolling(5, min_periods=5).min()
        )
        ts_max_delta = df.groupby('ts_code')[delta_close.name if hasattr(delta_close, 'name') else 0].transform(
            lambda x: pd.Series(delta_close[x.index]).rolling(5, min_periods=5).max()
        )

        result = np.where(ts_min_delta > 0, delta_close,
                         np.where(ts_max_delta < 0, delta_close, -1 * delta_close))

        return pd.Series(result, index=df.index)

    def alpha010(self, df):
        """Alpha#10: rank(((0 < ts_min(delta(close, 1), 4)) ? delta(close, 1) : ((ts_max(delta(close, 1), 4) < 0) ? delta(close, 1) : (-1 * delta(close, 1)))))"""
        delta_close = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        ts_min_delta = df.groupby('ts_code')[delta_close.name if hasattr(delta_close, 'name') else 0].transform(
            lambda x: pd.Series(delta_close[x.index]).rolling(4, min_periods=4).min()
        )
        ts_max_delta = df.groupby('ts_code')[delta_close.name if hasattr(delta_close, 'name') else 0].transform(
            lambda x: pd.Series(delta_close[x.index]).rolling(4, min_periods=4).max()
        )

        inner = np.where(ts_min_delta > 0, delta_close,
                        np.where(ts_max_delta < 0, delta_close, -1 * delta_close))

        result = pd.Series(inner, index=df.index).groupby(df['trade_date']).rank(pct=True)
        return result

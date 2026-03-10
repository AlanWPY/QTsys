"""
Alpha191算子库 - 实现WorldQuant Alpha101/191的基础算子
"""
import numpy as np
import pandas as pd

class Alpha191Operators:
    """Alpha191基础算子"""

    @staticmethod
    def ts_sum(df, window):
        """时序求和"""
        return df.rolling(window=window, min_periods=window).sum()

    @staticmethod
    def ts_mean(df, window):
        """时序均值"""
        return df.rolling(window=window, min_periods=window).mean()

    @staticmethod
    def ts_std(df, window):
        """时序标准差"""
        return df.rolling(window=window, min_periods=window).std()

    @staticmethod
    def ts_max(df, window):
        """时序最大值"""
        return df.rolling(window=window, min_periods=window).max()

    @staticmethod
    def ts_min(df, window):
        """时序最小值"""
        return df.rolling(window=window, min_periods=window).min()

    @staticmethod
    def ts_argmax(df, window):
        """时序最大值位置"""
        return df.rolling(window=window, min_periods=window).apply(
            lambda x: x.argmax() if len(x) > 0 else np.nan, raw=True
        )

    @staticmethod
    def ts_argmin(df, window):
        """时序最小值位置"""
        return df.rolling(window=window, min_periods=window).apply(
            lambda x: x.argmin() if len(x) > 0 else np.nan, raw=True
        )

    @staticmethod
    def ts_rank(df, window):
        """时序排名（最新值在窗口内的排名）"""
        return df.rolling(window=window, min_periods=window).apply(
            lambda x: ((np.argsort(np.argsort(x))[-1] + 1) / len(x)) if len(x) > 0 else np.nan, raw=True
        )

    @staticmethod
    def delta(df, period):
        """差分"""
        return df.diff(period)

    @staticmethod
    def delay(df, period):
        """延迟"""
        return df.shift(period)

    @staticmethod
    def correlation(x, y, window):
        """滚动相关系数"""
        return x.rolling(window=window, min_periods=window).corr(y)

    @staticmethod
    def covariance(x, y, window):
        """滚动协方差"""
        return x.rolling(window=window, min_periods=window).cov(y)

    @staticmethod
    def rank(df):
        """截面排名（0-1标准化）"""
        return df.rank(pct=True)

    @staticmethod
    def scale(df, a=1):
        """缩放到和为a"""
        return df / df.abs().sum() * a

    @staticmethod
    def sign(df):
        """符号函数"""
        return np.sign(df)

    @staticmethod
    def log(df):
        """对数"""
        return np.log(df)

    @staticmethod
    def abs(df):
        """绝对值"""
        return np.abs(df)

    @staticmethod
    def power(df, exp):
        """幂运算"""
        return df ** exp

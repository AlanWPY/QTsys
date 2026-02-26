"""Tushare API 封装"""
import tushare as ts
import pandas as pd
from typing import Optional


class TushareClient:
    def __init__(self, token: str):
        self.token = token
        self.pro = ts.pro_api(token)

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日线行情数据"""
        df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date").reset_index(drop=True)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取每日指标(市值、换手率等)"""
        df = self.pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date").reset_index(drop=True)
        return df

    def get_trade_cal(self, start_date: str, end_date: str) -> list[str]:
        """获取交易日历"""
        df = self.pro.trade_cal(
            exchange="SSE", start_date=start_date, end_date=end_date, is_open="1"
        )
        if df is None or df.empty:
            return []
        return sorted(df["cal_date"].tolist())

    def get_stock_basic(self, ts_code: Optional[str] = None) -> pd.DataFrame:
        """获取股票基本信息"""
        kwargs = {"exchange": "", "list_status": "L"}
        if ts_code:
            kwargs["ts_code"] = ts_code
        df = self.pro.stock_basic(**kwargs, fields="ts_code,symbol,name,area,industry,list_date")
        return df if df is not None else pd.DataFrame()

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取指数日线(用于基准)"""
        df = self.pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date").reset_index(drop=True)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def get_adj_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取复权因子"""
        df = self.pro.adj_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date").reset_index(drop=True)
        return df

    def validate_token(self) -> bool:
        """验证token是否有效"""
        try:
            df = self.pro.trade_cal(exchange="SSE", start_date="20240101", end_date="20240102")
            return df is not None and not df.empty
        except Exception:
            return False

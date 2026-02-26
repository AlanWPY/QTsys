"""数据缓存层 - 本地文件缓存避免重复API调用"""
import os
import hashlib
import pandas as pd
from typing import Optional
from config import CACHE_DIR
from data.tushare_client import TushareClient


class DataCache:
    def __init__(self, client: TushareClient):
        self.client = client

    def _cache_path(self, key: str) -> str:
        h = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(CACHE_DIR, f"{h}.pkl")

    def _load(self, key: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(key)
        if os.path.exists(path):
            try:
                return pd.read_pickle(path)
            except Exception:
                os.remove(path)
        return None

    def _save(self, key: str, df: pd.DataFrame):
        if not df.empty:
            df.to_pickle(self._cache_path(key))

    def get_daily(self, ts_code: str, start_date: str, end_date: str, adj: str = "qfq") -> pd.DataFrame:
        key = f"daily_{ts_code}_{start_date}_{end_date}_{adj}"
        cached = self._load(key)
        if cached is not None:
            return cached
        df = self.client.get_daily(ts_code, start_date, end_date)
        if df.empty:
            return df
        if adj == "qfq":
            df = self._apply_adj(df, ts_code, start_date, end_date)
        self._save(key, df)
        return df

    def _apply_adj(self, df: pd.DataFrame, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """合并复权因子,计算前复权价格"""
        adj_df = self.client.get_adj_factor(ts_code, start_date, end_date)
        if adj_df.empty:
            return df
        # 将trade_date转为相同格式用于合并
        if "trade_date" in adj_df.columns:
            adj_df["trade_date"] = pd.to_datetime(adj_df["trade_date"])
        merged = df.merge(adj_df[["trade_date", "adj_factor"]], on="trade_date", how="left")
        if "adj_factor" not in merged.columns or merged["adj_factor"].isna().all():
            return df
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        # 前复权: 以最新日期的复权因子为基准
        latest_factor = merged["adj_factor"].iloc[-1]
        if latest_factor > 0:
            ratio = merged["adj_factor"] / latest_factor
            for col in ["open", "high", "low", "close"]:
                if col in merged.columns:
                    merged[col] = (merged[col] * ratio).round(2)
        return merged

    def get_trade_cal(self, start_date: str, end_date: str) -> list[str]:
        key = f"trade_cal_{start_date}_{end_date}"
        cached = self._load(key)
        if cached is not None:
            return cached["cal_date"].tolist()
        dates = self.client.get_trade_cal(start_date, end_date)
        if dates:
            df = pd.DataFrame({"cal_date": dates})
            self._save(key, df)
        return dates

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        key = f"index_daily_{ts_code}_{start_date}_{end_date}"
        cached = self._load(key)
        if cached is not None:
            return cached
        df = self.client.get_index_daily(ts_code, start_date, end_date)
        self._save(key, df)
        return df

    def clear_cache(self):
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".pkl"):
                os.remove(os.path.join(CACHE_DIR, f))

"""数据缓存层 - 本地文件缓存避免重复API调用"""
import os
import time
import hashlib
import pandas as pd
import numpy as np
from typing import Optional
from config import CACHE_DIR
from data.tushare_client import TushareClient
from logging_config import get_logger

logger = get_logger("qtsys.data.cache")

# TTL配置(秒)
TTL_HISTORICAL = 24 * 3600  # 历史数据: 24小时
TTL_INTRADAY = 300           # 当日数据: 5分钟


class DataCache:
    def __init__(self, client: TushareClient):
        self.client = client

    def _cache_path(self, key: str) -> str:
        h = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(CACHE_DIR, f"{h}.pkl")

    def _is_expired(self, path: str, is_today: bool = False) -> bool:
        """检查缓存是否过期"""
        if not os.path.exists(path):
            return True
        age = time.time() - os.path.getmtime(path)
        ttl = TTL_INTRADAY if is_today else TTL_HISTORICAL
        return age > ttl

    def _load(self, key: str, is_today: bool = False) -> Optional[pd.DataFrame]:
        path = self._cache_path(key)
        if self._is_expired(path, is_today):
            return None
        try:
            return pd.read_pickle(path)
        except Exception:
            logger.warning(f"缓存文件损坏, 已删除: {path}")
            os.remove(path)
        return None

    def _save(self, key: str, df: pd.DataFrame):
        if not df.empty:
            df.to_pickle(self._cache_path(key))

    def _validate(self, df: pd.DataFrame, ts_code: str = "") -> pd.DataFrame:
        """数据质量检查 - 过滤停牌日、检测异常收益率"""
        if df.empty:
            return df
        # 过滤停牌日 (OHLC全为0)
        ohlc_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
        if ohlc_cols:
            mask = (df[ohlc_cols] == 0).all(axis=1)
            n_suspended = mask.sum()
            if n_suspended > 0:
                logger.info(f"{ts_code} 过滤{n_suspended}个停牌日")
                df = df[~mask].reset_index(drop=True)
        # 检测异常收益率 (>22%, A股最大涨跌幅20%+缓冲)
        if "close" in df.columns and len(df) > 1:
            returns = df["close"].pct_change().abs()
            anomalies = returns[returns > 0.22]
            if len(anomalies) > 0:
                dates = df.loc[anomalies.index, "trade_date"].tolist()
                logger.warning(f"{ts_code} 检测到{len(anomalies)}个异常收益率: {dates[:5]}")
        return df

    def get_daily(self, ts_code: str, start_date: str, end_date: str, adj: str = "qfq") -> pd.DataFrame:
        """获取日线数据 - 支持增量更新"""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        is_today = end_date >= today

        key = f"daily_{ts_code}_{start_date}_{end_date}_{adj}"
        cached = self._load(key, is_today=is_today)
        if cached is not None:
            return cached

        # 尝试增量更新: 加载不带end_date的基础缓存
        base_key = f"daily_{ts_code}_{start_date}"
        base_path = self._cache_path(base_key)
        df = None
        if os.path.exists(base_path):
            try:
                base_df = pd.read_pickle(base_path)
                if not base_df.empty and "trade_date" in base_df.columns:
                    last_date = base_df["trade_date"].max()
                    if hasattr(last_date, "strftime"):
                        last_str = last_date.strftime("%Y%m%d")
                    else:
                        last_str = str(last_date)[:10].replace("-", "")
                    # 仅拉取增量
                    if last_str < end_date:
                        incr = self.client.get_daily(ts_code, last_str, end_date)
                        if not incr.empty:
                            df = pd.concat([base_df, incr]).drop_duplicates(
                                subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
                            logger.info(f"{ts_code} 增量更新: {last_str} -> {end_date}, +{len(incr)}条")
                    else:
                        df = base_df
            except Exception:
                logger.warning(f"{ts_code} 增量更新失败, 全量拉取")

        if df is None:
            df = self.client.get_daily(ts_code, start_date, end_date)

        if df.empty:
            return df
        if adj == "qfq":
            df = self._apply_adj(df, ts_code, start_date, end_date)
        df = self._validate(df, ts_code)
        self._save(key, df)
        # 同时保存基础缓存用于增量更新
        self._save(base_key, df)
        return df

    def _apply_adj(self, df: pd.DataFrame, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """合并复权因子,计算前复权价格"""
        adj_df = self.client.get_adj_factor(ts_code, start_date, end_date)
        if adj_df.empty:
            return df
        if "trade_date" in adj_df.columns:
            adj_df["trade_date"] = pd.to_datetime(adj_df["trade_date"])
        merged = df.merge(adj_df[["trade_date", "adj_factor"]], on="trade_date", how="left")
        if "adj_factor" not in merged.columns or merged["adj_factor"].isna().all():
            return df
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
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

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取每日指标数据(PE/PB/PS/市值/换手率等)"""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        is_today = end_date >= today

        key = f"daily_basic_{ts_code}_{start_date}_{end_date}"
        cached = self._load(key, is_today=is_today)
        if cached is not None:
            return cached

        df = self.client.get_daily_basic(ts_code, start_date, end_date)
        if df.empty:
            return df
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("trade_date").reset_index(drop=True)
        self._save(key, df)
        return df

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        key = f"index_daily_{ts_code}_{start_date}_{end_date}"
        cached = self._load(key)
        if cached is not None:
            return cached
        df = self.client.get_index_daily(ts_code, start_date, end_date)
        self._save(key, df)
        return df

    def clear_cache(self):
        count = 0
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".pkl"):
                os.remove(os.path.join(CACHE_DIR, f))
                count += 1
        logger.info(f"已清除{count}个缓存文件")

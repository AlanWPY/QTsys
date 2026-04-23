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
CACHE_SCHEMA_VERSION = "v2"


def make_mysql_conn(settings):
    """从 Settings 对象创建 PyMySQL 连接，失败返回 None"""
    if not getattr(settings, "use_mysql", 0) or not getattr(settings, "mysql_host", ""):
        return None
    try:
        import pymysql
        return pymysql.connect(
            host=settings.mysql_host, port=settings.mysql_port or 3306,
            user=settings.mysql_user, password=settings.mysql_password or "",
            database=settings.mysql_database or "qtsys",
            connect_timeout=5, charset="utf8mb4",
        )
    except Exception:
        return None


class DataCache:
    _MYSQL_TABLE_DDL = {
        "qtsys_daily_quotes": """
            CREATE TABLE IF NOT EXISTS qtsys_daily_quotes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                trade_date VARCHAR(10) NOT NULL,
                open DOUBLE NULL,
                high DOUBLE NULL,
                low DOUBLE NULL,
                close DOUBLE NULL,
                vol DOUBLE NULL,
                amount DOUBLE NULL,
                pct_chg DOUBLE NULL,
                UNIQUE KEY uk_qtsys_daily_quotes_code_date (ts_code, trade_date),
                KEY idx_qtsys_daily_quotes_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """.strip(),
        "qtsys_daily_quotes_qfq": """
            CREATE TABLE IF NOT EXISTS qtsys_daily_quotes_qfq (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                trade_date VARCHAR(10) NOT NULL,
                open DOUBLE NULL,
                high DOUBLE NULL,
                low DOUBLE NULL,
                close DOUBLE NULL,
                vol DOUBLE NULL,
                amount DOUBLE NULL,
                pct_chg DOUBLE NULL,
                UNIQUE KEY uk_qtsys_daily_quotes_qfq_code_date (ts_code, trade_date),
                KEY idx_qtsys_daily_quotes_qfq_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """.strip(),
        "qtsys_daily_basic": """
            CREATE TABLE IF NOT EXISTS qtsys_daily_basic (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                trade_date VARCHAR(10) NOT NULL,
                pe DOUBLE NULL,
                pb DOUBLE NULL,
                ps DOUBLE NULL,
                total_mv DOUBLE NULL,
                circ_mv DOUBLE NULL,
                turnover_rate DOUBLE NULL,
                UNIQUE KEY uk_qtsys_daily_basic_code_date (ts_code, trade_date),
                KEY idx_qtsys_daily_basic_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """.strip(),
        "qtsys_index_daily": """
            CREATE TABLE IF NOT EXISTS qtsys_index_daily (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                trade_date VARCHAR(10) NOT NULL,
                open DOUBLE NULL,
                high DOUBLE NULL,
                low DOUBLE NULL,
                close DOUBLE NULL,
                vol DOUBLE NULL,
                amount DOUBLE NULL,
                pct_chg DOUBLE NULL,
                UNIQUE KEY uk_qtsys_index_daily_code_date (ts_code, trade_date),
                KEY idx_qtsys_index_daily_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """.strip(),
    }

    def __init__(self, client: TushareClient, mysql_conn=None):
        self.client = client
        self.mysql = mysql_conn
        self._mysql_disabled_tables = set()
        if self.mysql:
            self._ensure_mysql_tables()

    def _ensure_mysql_schema_compat(self, cur):
        table_names = list(self._MYSQL_TABLE_DDL.keys())
        for table in table_names:
            try:
                cur.execute(f"SHOW COLUMNS FROM {table} LIKE 'trade_date'")
                row = cur.fetchone()
                if not row:
                    continue
                field_type = str(row[1]).lower() if len(row) > 1 else ""
                if field_type.startswith("varchar("):
                    try:
                        size = int(field_type.split("varchar(", 1)[1].split(")", 1)[0])
                    except Exception:
                        size = 0
                    if size >= 8:
                        continue
                cur.execute(f"ALTER TABLE {table} MODIFY COLUMN trade_date VARCHAR(10) NOT NULL")
            except Exception as exc:
                logger.warning(f"MySQL 表结构兼容修复失败 {table}: {exc}")

    def _ensure_mysql_tables(self):
        if not self.mysql:
            return
        try:
            cur = self.mysql.cursor()
            for ddl in self._MYSQL_TABLE_DDL.values():
                cur.execute(ddl)
            self._ensure_mysql_schema_compat(cur)
            self.mysql.commit()
            cur.close()
        except Exception as e:
            logger.warning(f"MySQL 初始化缓存表失败: {e}")

    def _mysql_read(self, table: str, ts_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从 MySQL 二级缓存读取指定区间数据。"""
        if not self.mysql or table in self._mysql_disabled_tables:
            return None
        try:
            cur = self.mysql.cursor()
            sql = f"SELECT * FROM {table} WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date"
            cur.execute(sql, (ts_code, start_date, end_date))
            rows = cur.fetchall()
            columns = [col[0] for col in cur.description] if cur.description else []
            cur.close()
            if not rows or not columns:
                return None
            df = pd.DataFrame(list(rows), columns=columns)
            if not df.empty and "trade_date" in df.columns:
                df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
                df = df.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
            return df if not df.empty else None
        except Exception as e:
            if "doesn't exist" in str(e) or "1146" in str(e):
                self._mysql_disabled_tables.add(table)
            logger.warning(f"MySQL 读取缓存失败 {table}: {e}")
            return None

    def _normalize_mysql_value(self, value, column: str):
        if value is None:
            return None
        if pd.isna(value):
            return None
        if column == "trade_date":
            try:
                return pd.to_datetime(value).strftime("%Y%m%d")
            except Exception:
                text = str(value).strip()
                digits = "".join(ch for ch in text if ch.isdigit())
                if len(digits) >= 8:
                    return digits[:8]
                return text[:10]
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                return value
        return value

    def _mysql_write(self, table: str, df: pd.DataFrame, cols: list):
        """写入 MySQL 二级缓存，使用 `REPLACE INTO` 覆盖。"""
        if not self.mysql or df.empty or table in self._mysql_disabled_tables:
            return
        try:
            cur = self.mysql.cursor()
            placeholders = ",".join(["%s"] * len(cols))
            sql = f"REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
            rows = [
                tuple(self._normalize_mysql_value(r[c], c) if c in r.index else None for c in cols)
                for _, r in df.iterrows()
            ]
            cur.executemany(sql, rows)
            self.mysql.commit()
            cur.close()
        except Exception as e:
            if "doesn't exist" in str(e) or "1146" in str(e):
                self._mysql_disabled_tables.add(table)
            logger.warning(f"MySQL 写入缓存失败 {table}: {e}")

    def _cache_path(self, key: str) -> str:
        h = hashlib.md5(f"{CACHE_SCHEMA_VERSION}:{key}".encode()).hexdigest()
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

    def _prepare_frame(self, df: Optional[pd.DataFrame], date_col: str = "trade_date", unique_cols: Optional[list[str]] = None) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        result = df.copy()
        if date_col in result.columns:
            result[date_col] = pd.to_datetime(result[date_col])
            result = result.sort_values(date_col)
        if unique_cols:
            result = result.drop_duplicates(subset=unique_cols, keep="last")
        return result.reset_index(drop=True)

    def _slice_frame(self, df: Optional[pd.DataFrame], start_date: str, end_date: str, date_col: str = "trade_date") -> pd.DataFrame:
        if df is None or df.empty or date_col not in df.columns:
            return pd.DataFrame()
        start_ts = pd.to_datetime(start_date)
        end_ts = pd.to_datetime(end_date)
        result = df[(df[date_col] >= start_ts) & (df[date_col] <= end_ts)].copy()
        return result.reset_index(drop=True)

    def _date_span(self, df: Optional[pd.DataFrame], date_col: str = "trade_date") -> tuple[Optional[str], Optional[str]]:
        if df is None or df.empty or date_col not in df.columns:
            return None, None
        prepared = self._prepare_frame(df, date_col=date_col)
        start = prepared[date_col].min()
        end = prepared[date_col].max()
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    def _missing_segments(self, df: Optional[pd.DataFrame], start_date: str, end_date: str, date_col: str = "trade_date") -> list[tuple[str, str]]:
        current_start, current_end = self._date_span(df, date_col=date_col)
        if not current_start or not current_end:
            return [(start_date, end_date)]
        segments = []
        if current_start > start_date:
            head_end = (pd.to_datetime(current_start) - pd.Timedelta(days=1)).strftime("%Y%m%d")
            if start_date <= head_end:
                segments.append((start_date, head_end))
        if current_end < end_date:
            tail_start = (pd.to_datetime(current_end) + pd.Timedelta(days=1)).strftime("%Y%m%d")
            if tail_start <= end_date:
                segments.append((tail_start, end_date))
        return segments

    def _missing_trade_segments(self, df: Optional[pd.DataFrame], start_date: str, end_date: str, date_col: str = "trade_date") -> list[tuple[str, str]]:
        expected_dates = self.get_trade_cal(start_date, end_date)
        if not expected_dates:
            return self._missing_segments(df, start_date, end_date, date_col=date_col)
        if df is None or df.empty or date_col not in df.columns:
            return [(expected_dates[0], expected_dates[-1])]
        prepared = self._prepare_frame(df, date_col=date_col)
        existing_dates = set(prepared[date_col].dt.strftime("%Y%m%d").tolist())
        missing = [date for date in expected_dates if date not in existing_dates]
        if not missing:
            return []
        expected_pos = {date: idx for idx, date in enumerate(expected_dates)}
        segments: list[tuple[str, str]] = []
        seg_start = missing[0]
        prev = missing[0]
        for date in missing[1:]:
            if expected_pos[date] != expected_pos[prev] + 1:
                segments.append((seg_start, prev))
                seg_start = date
            prev = date
        segments.append((seg_start, prev))
        return segments

    def _merge_frames(self, *frames: Optional[pd.DataFrame], date_col: str = "trade_date", unique_cols: Optional[list[str]] = None) -> pd.DataFrame:
        valid = [self._prepare_frame(frame, date_col=date_col, unique_cols=unique_cols) for frame in frames if frame is not None and not frame.empty]
        if not valid:
            return pd.DataFrame()
        merged = pd.concat(valid, ignore_index=True)
        return self._prepare_frame(merged, date_col=date_col, unique_cols=unique_cols)

    def get_daily(self, ts_code: str, start_date: str, end_date: str, adj: str = "qfq") -> pd.DataFrame:
        """按时间区间计算缺失片段，用于增量补数。"""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        is_today = end_date >= today

        key = f"daily_{ts_code}_{start_date}_{end_date}_{adj}"
        cached = self._load(key, is_today=is_today)
        if cached is not None:
            return cached

        if adj == "qfq":
            table = "qtsys_daily_quotes_qfq"
            base_key = f"daily_full_{ts_code}_{adj}"
            unique_cols = ["ts_code", "trade_date"]
            cached_full = self._load(base_key, is_today=is_today)
            mysql_df = self._prepare_frame(self._mysql_read(table, ts_code, start_date, end_date), unique_cols=unique_cols)
            base_df = self._prepare_frame(cached_full, unique_cols=unique_cols)
            merged = self._merge_frames(base_df, mysql_df, unique_cols=unique_cols)
            segments = self._missing_trade_segments(merged, start_date, end_date)
            fetched_segments = []
            for seg_start, seg_end in segments:
                seg_df = self.client.get_daily(ts_code, seg_start, seg_end, adj=adj)
                seg_df = self._prepare_frame(seg_df, unique_cols=unique_cols)
                if not seg_df.empty:
                    fetched_segments.append(seg_df)
                    logger.info(f"补齐前复权行情 {ts_code}: {seg_start} -> {seg_end}, {len(seg_df)} 条")
            merged = self._merge_frames(merged, *fetched_segments, unique_cols=unique_cols)
            requested = self._slice_frame(merged, start_date, end_date)
            if requested.empty:
                return requested
            requested = self._validate(requested, ts_code)
            self._save(key, requested)
            self._save(base_key, merged)
            for seg_df in fetched_segments:
                self._mysql_write(table, seg_df,
                    ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"])
            return requested

        table = "qtsys_daily_quotes"
        base_key = f"daily_full_{ts_code}_raw"
        unique_cols = ["ts_code", "trade_date"]
        cached_full = self._load(base_key, is_today=is_today)
        mysql_df = self._prepare_frame(self._mysql_read(table, ts_code, start_date, end_date), unique_cols=unique_cols)
        base_df = self._prepare_frame(cached_full, unique_cols=unique_cols)
        merged = self._merge_frames(base_df, mysql_df, unique_cols=unique_cols)
        segments = self._missing_trade_segments(merged, start_date, end_date)
        fetched_segments = []
        for seg_start, seg_end in segments:
            seg_df = self.client.get_daily(ts_code, seg_start, seg_end)
            seg_df = self._prepare_frame(seg_df, unique_cols=unique_cols)
            if not seg_df.empty:
                fetched_segments.append(seg_df)
                logger.info(f"补齐日线行情 {ts_code}: {seg_start} -> {seg_end}, {len(seg_df)} 条")
        merged = self._merge_frames(merged, *fetched_segments, unique_cols=unique_cols)
        requested = self._slice_frame(merged, start_date, end_date)
        if requested.empty:
            return requested
        requested = self._validate(requested, ts_code)
        self._save(key, requested)
        self._save(base_key, merged)
        for seg_df in fetched_segments:
            self._mysql_write(table, seg_df,
                ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"])
        return requested

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
            return cached["cal_date"].astype(str).tolist()
        base_key = "trade_cal_full"
        base_df = self._prepare_frame(self._load(base_key), date_col="cal_date", unique_cols=["cal_date"])
        segments = self._missing_segments(base_df, start_date, end_date, date_col="cal_date")
        fetched = []
        for seg_start, seg_end in segments:
            dates = self.client.get_trade_cal(seg_start, seg_end)
            if dates:
                fetched.append(pd.DataFrame({"cal_date": pd.to_datetime(dates)}))
        merged = self._merge_frames(base_df, *fetched, date_col="cal_date", unique_cols=["cal_date"])
        requested = self._slice_frame(merged, start_date, end_date, date_col="cal_date")
        if not requested.empty:
            requested["cal_date"] = requested["cal_date"].dt.strftime("%Y%m%d")
            self._save(key, requested)
            full_to_save = merged.copy()
            full_to_save["cal_date"] = full_to_save["cal_date"].dt.strftime("%Y%m%d")
            self._save(base_key, full_to_save)
            return requested["cal_date"].tolist()
        return []

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """统一行情读取入口：优先 L1 pickle，再读 L2 MySQL，最后回退到 API。"""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        is_today = end_date >= today

        key = f"daily_basic_{ts_code}_{start_date}_{end_date}"
        cached = self._load(key, is_today=is_today)
        if cached is not None:
            return cached

        mysql_df = self._mysql_read("qtsys_daily_basic", ts_code, start_date, end_date)
        if mysql_df is not None:
            self._save(key, mysql_df)
            return mysql_df

        df = self.client.get_daily_basic(ts_code, start_date, end_date)
        if df.empty:
            return df
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("trade_date").reset_index(drop=True)
        self._save(key, df)
        self._mysql_write("qtsys_daily_basic", df,
            ["ts_code", "trade_date", "pe", "pb", "ps", "total_mv", "circ_mv", "turnover_rate"])
        return df

    def get_index_weight(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        key = f"index_weight_{index_code}_{start_date}_{end_date}"
        cached = self._load(key)
        if cached is not None:
            return cached
        base_key = f"index_weight_full_{index_code}"
        base_df = self._prepare_frame(self._load(base_key), unique_cols=["trade_date", "con_code"])
        segments = self._missing_trade_segments(base_df, start_date, end_date)
        fetched = []
        for seg_start, seg_end in segments:
            seg_df = self.client.get_index_weight(index_code, seg_start, seg_end)
            seg_df = self._prepare_frame(seg_df, unique_cols=["trade_date", "con_code"])
            if not seg_df.empty:
                fetched.append(seg_df)
        merged = self._merge_frames(base_df, *fetched, unique_cols=["trade_date", "con_code"])
        requested = self._slice_frame(merged, start_date, end_date)
        if not requested.empty:
            self._save(key, requested)
            self._save(base_key, merged)
        return requested

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        key = f"index_daily_{ts_code}_{start_date}_{end_date}"
        cached = self._load(key)
        if cached is not None:
            return cached
        base_key = f"index_daily_full_{ts_code}"
        unique_cols = ["ts_code", "trade_date"]
        base_df = self._prepare_frame(self._load(base_key), unique_cols=unique_cols)
        mysql_df = self._prepare_frame(self._mysql_read("qtsys_index_daily", ts_code, start_date, end_date), unique_cols=unique_cols)
        merged = self._merge_frames(base_df, mysql_df, unique_cols=unique_cols)
        segments = self._missing_trade_segments(merged, start_date, end_date)
        fetched = []
        for seg_start, seg_end in segments:
            seg_df = self.client.get_index_daily(ts_code, seg_start, seg_end)
            seg_df = self._prepare_frame(seg_df, unique_cols=unique_cols)
            if not seg_df.empty:
                fetched.append(seg_df)
        merged = self._merge_frames(merged, *fetched, unique_cols=unique_cols)
        requested = self._slice_frame(merged, start_date, end_date)
        if not requested.empty:
            self._save(key, requested)
            self._save(base_key, merged)
            for seg_df in fetched:
                self._mysql_write("qtsys_index_daily", seg_df,
                    ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"])
        return requested

    def clear_cache(self):
        count = 0
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".pkl"):
                os.remove(os.path.join(CACHE_DIR, f))
                count += 1
        logger.info(f"已清除{count}个缓存文件")

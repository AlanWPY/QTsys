"""Tushare API 封装 - 带速率限制保护"""
import time
import threading
import warnings
import sys
for _optional_module in ("pyarrow", "numexpr", "bottleneck"):
    sys.modules.setdefault(_optional_module, None)
import tushare as ts
import pandas as pd
from typing import Optional
from logging_config import get_logger

logger = get_logger("qtsys.data.tushare")

warnings.filterwarnings("ignore", message="Series.fillna with 'method' is deprecated.*", category=FutureWarning)

# Tushare速率限制: ~170次/分钟
RATE_LIMIT_CALLS = 170
RATE_LIMIT_WINDOW = 60  # 秒


class TushareClient:
    _rate_lock = threading.Lock()
    _global_call_times: dict[str, list[float]] = {}

    def __init__(self, token: str):
        self.token = str(token or "").strip()
        if self.token:
            ts.set_token(self.token)
        self.pro = ts.pro_api(self.token)

    def _ensure_token(self):
        if self.token:
            ts.set_token(self.token)

    def _call_pro_bar(self, **kwargs):
        self._ensure_token()
        last_error = None
        for payload in ({**kwargs, "api": self.pro}, kwargs):
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message="Series.fillna with 'method' is deprecated.*",
                        category=FutureWarning,
                    )
                    return ts.pro_bar(**payload)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                if isinstance(exc, OSError) and "token" in message.lower():
                    self._ensure_token()
                if isinstance(exc, TypeError):
                    continue
                continue
        if last_error:
            raise last_error
        return pd.DataFrame()

    def _rate_limit(self):
        """速率限制保护 - 滑动窗口"""
        wait = 0.0
        with self._rate_lock:
            now = time.time()
            call_times = self._global_call_times.setdefault(self.token, [])
            call_times[:] = [t for t in call_times if now - t < RATE_LIMIT_WINDOW]
            if len(call_times) >= RATE_LIMIT_CALLS:
                wait = RATE_LIMIT_WINDOW - (now - call_times[0]) + 0.5
            if wait <= 0:
                call_times.append(now)

        if wait > 0:
            logger.warning(f"Tushare速率限制, 等待{wait:.1f}s")
            time.sleep(wait)
            with self._rate_lock:
                call_times = self._global_call_times.setdefault(self.token, [])
                now = time.time()
                call_times[:] = [t for t in call_times if now - t < RATE_LIMIT_WINDOW]
                call_times.append(now)

    def get_daily(self, ts_code: str, start_date: str, end_date: str, adj: Optional[str] = None) -> pd.DataFrame:
        """获取日线行情数据，`adj` 可选 `qfq` 或 `hfq`。"""
        self._rate_limit()
        self._ensure_token()
        try:
            if adj:
                df = self._call_pro_bar(
                    ts_code=ts_code,
                    adj=adj,
                    start_date=start_date,
                    end_date=end_date,
                    asset="E",
                    freq="D",
                )
            else:
                self._ensure_token()
                df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.sort_values("trade_date").reset_index(drop=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            return df
        except Exception:
            logger.exception(f"获取日线行情失败: {ts_code}, adj={adj or 'none'}")
            return pd.DataFrame()

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取每日指标(市值、换手率等)"""
        self._rate_limit()
        self._ensure_token()
        try:
            df = self.pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.sort_values("trade_date").reset_index(drop=True)
            return df
        except Exception:
            logger.exception(f"获取日指标失败: {ts_code}")
            return pd.DataFrame()

    def get_trade_cal(self, start_date: str, end_date: str) -> list[str]:
        """获取交易日历"""
        self._rate_limit()
        self._ensure_token()
        try:
            df = self.pro.trade_cal(
                exchange="SSE", start_date=start_date, end_date=end_date, is_open="1"
            )
            if df is None or df.empty:
                return []
            return sorted(df["cal_date"].tolist())
        except Exception:
            logger.exception("获取交易日历失败")
            return []

    def get_stock_basic(self, ts_code: Optional[str] = None) -> pd.DataFrame:
        """获取股票基本信息"""
        self._rate_limit()
        self._ensure_token()
        try:
            kwargs = {"exchange": "", "list_status": "L"}
            if ts_code:
                kwargs["ts_code"] = ts_code
            df = self.pro.stock_basic(**kwargs, fields="ts_code,symbol,name,area,industry,list_date")
            return df if df is not None else pd.DataFrame()
        except Exception:
            logger.exception("获取股票基本信息失败")
            return pd.DataFrame()

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取指数日线(用于基准)"""
        self._rate_limit()
        self._ensure_token()
        try:
            df = self.pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.sort_values("trade_date").reset_index(drop=True)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            return df
        except Exception:
            logger.exception(f"获取指数日线失败: {ts_code}")
            return pd.DataFrame()

    def get_adj_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取复权因子"""
        self._rate_limit()
        self._ensure_token()
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Series.fillna with 'method' is deprecated.*", category=FutureWarning)
                df = self.pro.adj_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.sort_values("trade_date").reset_index(drop=True)
            return df
        except Exception:
            logger.exception(f"获取复权因子失败: {ts_code}")
            return pd.DataFrame()

    def get_index_weight(self, index_code: str, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取指数成分股权重"""
        self._rate_limit()
        self._ensure_token()
        try:
            kwargs = {"index_code": index_code}
            if start_date:
                kwargs["start_date"] = start_date
            if end_date:
                kwargs["end_date"] = end_date
            df = self.pro.index_weight(**kwargs)
            if df is None or df.empty:
                return pd.DataFrame()
            return df
        except Exception:
            logger.exception(f"获取指数成分股失败: {index_code}")
            return pd.DataFrame()


    def get_fina_indicator(self, ts_code: str, period: str = "") -> pd.DataFrame:
        """获取财务指标(ROE/ROA/毛利率/资产负债率等)"""
        self._rate_limit()
        self._ensure_token()
        try:
            kwargs = {"ts_code": ts_code, "fields": "ts_code,ann_date,end_date,eps,roe,roa,debt_to_assets,grossprofit_margin,netprofit_margin,current_ratio,assets_turn,ocfps,ocf_to_or,bps,quick_ratio"}
            if period:
                kwargs["period"] = period
            df = self.pro.fina_indicator(**kwargs)
            if df is None or df.empty:
                return pd.DataFrame()
            return df.sort_values("end_date").reset_index(drop=True)
        except Exception:
            logger.warning(f"获取财务指标失败: {ts_code}")
            return pd.DataFrame()

    def get_income(self, ts_code: str, period: str = "") -> pd.DataFrame:
        """获取利润表(营收/净利润)"""
        self._rate_limit()
        self._ensure_token()
        try:
            kwargs = {"ts_code": ts_code, "fields": "ts_code,ann_date,end_date,revenue,n_income,operate_profit"}
            if period:
                kwargs["period"] = period
            df = self.pro.income(**kwargs)
            if df is None or df.empty:
                return pd.DataFrame()
            return df.sort_values("end_date").reset_index(drop=True)
        except Exception:
            logger.warning(f"获取利润表失败: {ts_code}")
            return pd.DataFrame()

    def get_stock_basic_full(self) -> pd.DataFrame:
        """获取全量A股基本信息(含行业/板块)"""
        self._rate_limit()
        self._ensure_token()
        try:
            df = self.pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,area,industry,market,list_date")
            if df is None or df.empty:
                return pd.DataFrame()
            return df
        except Exception:
            logger.warning("获取全量A股基本信息失败")
            return pd.DataFrame()

    def get_daily_basic_batch(self, trade_date: str, fields: str = "ts_code,pe,pb,total_mv,turnover_rate") -> pd.DataFrame:
        """批量获取全市场某日基础指标，自动向前找最近有效交易日（最多回退10天）"""
        from datetime import datetime, timedelta
        dt = datetime.strptime(str(trade_date)[:8], "%Y%m%d")
        for offset in range(10):
            check_date = (dt - timedelta(days=offset)).strftime("%Y%m%d")
            self._rate_limit()
            self._ensure_token()
            try:
                df = self.pro.daily_basic(ts_code="", trade_date=check_date, fields=fields)
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                logger.warning(f"get_daily_basic_batch {check_date}: {e}")
                continue
        return pd.DataFrame()

    def validate_token(self) -> bool:
        """验证token是否有效"""
        try:
            self._ensure_token()
            df = self.pro.trade_cal(exchange="SSE", start_date="20240101", end_date="20240102")
            return df is not None and not df.empty
        except Exception:
            logger.warning("Token验证失败")
            return False

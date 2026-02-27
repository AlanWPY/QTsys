"""数据质量监控模块 - 检测脏数据，避免垃圾进垃圾出"""
import numpy as np
import pandas as pd
from typing import Optional
from logging_config import get_logger

logger = get_logger("qtsys.data.quality")


def check_data_quality(
    cache,
    ts_codes: list[str],
    start_date: str,
    end_date: str,
) -> dict:
    """对股票池进行数据质量检测。

    Returns:
        {summary: {clean, warning, error, total}, per_stock: [...], recommendations: [...]}
    """
    trade_cal = cache.get_trade_cal(start_date, end_date)
    cal_set = set(trade_cal)
    total_trade_days = len(trade_cal)

    per_stock = []
    for code in ts_codes:
        issues = []
        try:
            df = cache.get_daily(code, start_date, end_date, adj="qfq")
        except Exception as e:
            per_stock.append({
                "ts_code": code, "status": "error",
                "issues": [{"type": "fetch_error", "severity": "error",
                            "dates": [], "desc": f"数据获取失败: {e}"}],
            })
            continue

        if df is None or df.empty:
            per_stock.append({
                "ts_code": code, "status": "error",
                "issues": [{"type": "no_data", "severity": "error",
                            "dates": [], "desc": "无可用数据"}],
            })
            continue

        issues.extend(_check_missing_dates(df, cal_set, total_trade_days))
        issues.extend(_check_suspension(df))
        issues.extend(_check_zero_volume(df))
        issues.extend(_check_extreme_returns(df))
        issues.extend(_check_price_sticking(df))
        issues.extend(_check_freshness(df, end_date))

        severities = [i["severity"] for i in issues]
        if "error" in severities:
            status = "error"
        elif "warning" in severities:
            status = "warning"
        else:
            status = "clean"
        per_stock.append({"ts_code": code, "status": status, "issues": issues})

    clean = sum(1 for s in per_stock if s["status"] == "clean")
    warning = sum(1 for s in per_stock if s["status"] == "warning")
    error = sum(1 for s in per_stock if s["status"] == "error")

    return {
        "summary": {"clean": clean, "warning": warning, "error": error, "total": len(ts_codes)},
        "per_stock": per_stock,
        "recommendations": _generate_recommendations(per_stock),
    }


def check_survivorship_bias(client, ts_codes: list[str]) -> list[dict]:
    """检测退市股票（幸存者偏差）。"""
    delisted = []
    try:
        df = client.get_stock_basic()
        if df is None or df.empty:
            return delisted
        listed_codes = set(df["ts_code"].tolist()) if "ts_code" in df.columns else set()
    except Exception:
        return delisted

    for code in ts_codes:
        if code not in listed_codes:
            delisted.append({
                "ts_code": code, "list_status": "D",
                "warning": "未找到该股票信息，可能已退市",
            })
    return delisted


# ---- 内部检测函数 ----

def _fmt_date(d):
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    return str(d)[:10].replace("-", "")


def _check_missing_dates(df, cal_set, total_trade_days):
    issues = []
    if "trade_date" not in df.columns or total_trade_days == 0:
        return issues
    df_dates = set(_fmt_date(d) for d in df["trade_date"])
    missing = cal_set - df_dates
    rate = len(missing) / total_trade_days
    if rate > 0.3:
        issues.append({"type": "missing_dates", "severity": "error",
                        "dates": sorted(list(missing))[:10],
                        "desc": f"缺失{len(missing)}个交易日({rate:.0%})，数据严重不完整"})
    elif rate > 0.05:
        issues.append({"type": "missing_dates", "severity": "warning",
                        "dates": sorted(list(missing))[:10],
                        "desc": f"缺失{len(missing)}个交易日({rate:.0%})"})
    return issues


def _check_suspension(df):
    issues = []
    ohlc = [c for c in ["open", "high", "low", "close"] if c in df.columns]
    if len(ohlc) < 4:
        return issues
    mask = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
    n = int(mask.sum())
    if n > 0:
        dates = [_fmt_date(d) for d in df.loc[mask, "trade_date"]][:10]
        sev = "warning" if n < 10 else "error"
        issues.append({"type": "suspension", "severity": sev,
                        "dates": dates, "desc": f"检测到{n}个疑似停牌日(OHLC相等)"})
    return issues


def _check_zero_volume(df):
    issues = []
    if "vol" not in df.columns or "close" not in df.columns:
        return issues
    price_change = df["close"].diff().abs() > 0
    zero_vol = df["vol"] == 0
    anomaly = price_change & zero_vol
    n = int(anomaly.sum())
    if n > 0:
        dates = [_fmt_date(d) for d in df.loc[anomaly, "trade_date"]][:10]
        issues.append({"type": "zero_volume", "severity": "warning",
                        "dates": dates, "desc": f"{n}天价格变动但成交量为0"})
    return issues


def _check_extreme_returns(df):
    issues = []
    if "close" not in df.columns or len(df) < 2:
        return issues
    returns = df["close"].pct_change().abs()
    extreme = returns > 0.11
    n = int(extreme.sum())
    if n > 0:
        dates = [_fmt_date(d) for d in df.loc[extreme, "trade_date"]][:10]
        sev = "warning" if n < 5 else "error"
        issues.append({"type": "extreme_return", "severity": sev,
                        "dates": dates, "desc": f"{n}天日收益率超过11%(超涨跌停限制)"})
    return issues


def _check_price_sticking(df, threshold=5):
    issues = []
    if "close" not in df.columns or len(df) < threshold:
        return issues
    close = df["close"].values
    streak = 1
    max_streak = 1
    for i in range(1, len(close)):
        if close[i] == close[i - 1]:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 1
    if max_streak >= threshold:
        issues.append({"type": "price_sticking", "severity": "warning",
                        "dates": [], "desc": f"连续{max_streak}天收盘价完全相同"})
    return issues


def _check_freshness(df, end_date):
    issues = []
    if "trade_date" not in df.columns or df.empty:
        return issues
    last = _fmt_date(df["trade_date"].iloc[-1])
    if last < end_date:
        gap = int(end_date) - int(last)
        if gap > 100:
            issues.append({"type": "stale_data", "severity": "error",
                            "dates": [last], "desc": f"最后数据日期{last}，距请求结束日期差距过大"})
        elif gap > 10:
            issues.append({"type": "stale_data", "severity": "warning",
                            "dates": [last], "desc": f"最后数据日期{last}，可能不够新鲜"})
    return issues


def _generate_recommendations(per_stock):
    recs = []
    error_codes = [s["ts_code"] for s in per_stock if s["status"] == "error"]
    if error_codes:
        recs.append(f"建议移除以下问题股票: {', '.join(error_codes[:10])}")
    warning_count = sum(1 for s in per_stock if s["status"] == "warning")
    if warning_count > 0:
        recs.append(f"{warning_count}只股票存在数据警告，建议检查后决定是否保留")
    return recs

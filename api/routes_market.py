"""看盘API接口 - K线图 + 形态匹配 (优化版)"""
import numpy as np
import datetime
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database.connection import get_db
from database.models import Settings
from data.tushare_client import TushareClient
from data.data_cache import DataCache

router = APIRouter(prefix="/api/market", tags=["market"])

# ===== 内存缓存: 股票名称映射 =====
_name_cache = {"data": {}, "ts": 0}
_NAME_CACHE_TTL = 600  # 10分钟


def _get_name_map(client: TushareClient) -> dict:
    """带内存缓存的股票名称映射"""
    import time
    now = time.time()
    if _name_cache["data"] and now - _name_cache["ts"] < _NAME_CACHE_TTL:
        return _name_cache["data"]
    try:
        basics = client.get_stock_basic()
        if not basics.empty:
            _name_cache["data"] = dict(zip(basics["ts_code"], basics["name"]))
            _name_cache["ts"] = now
    except Exception:
        pass
    return _name_cache["data"]


async def _get_cache(db: AsyncSession) -> DataCache:
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")
    from data.data_cache import make_mysql_conn
    return DataCache(TushareClient(settings.tushare_token), mysql_conn=make_mysql_conn(settings))


async def _get_client(db: AsyncSession) -> TushareClient:
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")
    return TushareClient(settings.tushare_token)


# ===== 辅助: DataFrame转K线记录 =====

def _df_to_records(df) -> list:
    records = []
    for _, r in df.iterrows():
        dt = r["trade_date"]
        records.append({
            "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
            "open": round(float(r["open"]), 2),
            "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2),
            "close": round(float(r["close"]), 2),
            "vol": round(float(r.get("vol", 0)), 2),
            "pct_chg": round(float(r.get("pct_chg", 0)), 2),
        })
    return records


# ===== K线数据 (优化: 用缓存名称) =====

@router.get("/kline")
async def get_kline(
    ts_code: str, limit: int = 120,
    db: AsyncSession = Depends(get_db),
):
    cache = await _get_cache(db)
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=limit * 3)).strftime("%Y%m%d")
    df = cache.get_daily(ts_code, start, end)
    if df.empty:
        return {"data": [], "name": "", "code": ts_code}
    df = df.tail(limit).reset_index(drop=True)
    # 用缓存获取名称, 不再单独调API
    client = await _get_client(db)
    name = _get_name_map(client).get(ts_code, "")
    return {"data": _df_to_records(df), "name": name, "code": ts_code}


# ===== 热门股票 (缩减到15只加速匹配) =====

POPULAR_STOCKS = [
    "000001.SZ", "000858.SZ", "600036.SH", "600519.SH",
    "601318.SH", "000333.SZ", "002415.SZ", "300750.SZ",
    "600030.SH", "000651.SZ", "601888.SH", "601398.SH",
    "600887.SH", "601899.SH", "600585.SH",
]


class PatternMatchRequest(BaseModel):
    curve: list[float]
    top_n: int = 6
    search_codes: list[str] = []
    start_date: str = ""      # 搜索起始日期 YYYYMMDD, 空则自动
    end_date: str = ""        # 搜索结束日期 YYYYMMDD, 空则今天
    mode: str = "pool"        # "pool" 匹配股票池, "single" 匹配单一股票
    target_code: str = ""     # mode=single时的目标股票代码


# ===== 形态匹配算法 (向量化优化) =====

def _normalize(arr: np.ndarray) -> np.ndarray:
    mn, mx = arr.min(), arr.max()
    if mx - mn < 1e-9:
        return np.zeros_like(arr)
    return (arr - mn) / (mx - mn)


def _resample(arr: np.ndarray, target_len: int) -> np.ndarray:
    if len(arr) == target_len:
        return arr
    return np.interp(
        np.linspace(0, 1, target_len),
        np.linspace(0, 1, len(arr)),
        arr,
    )


def _fast_corr(a: np.ndarray, b: np.ndarray) -> float:
    """快速皮尔逊相关, 纯numpy向量化"""
    n = len(a)
    if n < 3:
        return 0.0
    a_m = a - a.mean()
    b_m = b - b.mean()
    num = np.dot(a_m, b_m)
    den = np.sqrt(np.dot(a_m, a_m) * np.dot(b_m, b_m))
    if den < 1e-12:
        return 0.0
    return float(num / den)


def _find_best_match(closes: np.ndarray, user_curve: np.ndarray):
    """优化版滑动窗口: 更大步长 + 只用相关系数"""
    n = len(closes)
    base = len(user_curve)
    # 只用3个窗口尺寸, 步长更大
    window_sizes = [base, int(base * 0.7), int(base * 1.4)]
    window_sizes = list(set(w for w in window_sizes if 5 <= w <= n))
    if not window_sizes:
        return None
    user_norm = _normalize(user_curve)
    best_score, best_s, best_e = -1, 0, 0
    for ws in window_sizes:
        ref = _resample(user_norm, ws)
        step = max(1, ws // 3)  # 更大步长
        for i in range(0, n - ws + 1, step):
            seg = _normalize(closes[i:i + ws])
            sc = _fast_corr(ref, seg)
            if sc > best_score:
                best_score = sc
                best_s, best_e = i, i + ws
    return {"score": round(max(0, best_score), 4), "start": best_s, "end": best_e}


def _match_one_stock(args):
    """单只股票匹配 (用于线程池)"""
    code, cache, start, end, user_curve = args
    try:
        df = cache.get_daily(code, start, end)
        if df.empty or len(df) < 20:
            return None
        closes = df["close"].values.astype(float)
        match = _find_best_match(closes, user_curve)
        if not match or match["score"] < 0.3:
            return None
        s, e = match["start"], match["end"]
        seg_data = _df_to_records(df.iloc[s:e])
        return {
            "code": code, "score": match["score"],
            "start_date": seg_data[0]["date"] if seg_data else "",
            "end_date": seg_data[-1]["date"] if seg_data else "",
            "kline": seg_data,
        }
    except Exception:
        return None


@router.post("/pattern_match")
async def pattern_match(req: PatternMatchRequest, db: AsyncSession = Depends(get_db)):
    """形态匹配: 线程池并发搜索"""
    if len(req.curve) < 5:
        raise HTTPException(status_code=400, detail="曲线点数太少")

    user_curve = np.array(req.curve, dtype=float)
    cache = await _get_cache(db)
    client = await _get_client(db)
    name_map = _get_name_map(client)

    end = req.end_date if req.end_date else datetime.date.today().strftime("%Y%m%d")
    if req.start_date:
        start = req.start_date
    else:
        start = (datetime.datetime.strptime(end, "%Y%m%d") - datetime.timedelta(days=400)).strftime("%Y%m%d")

    if req.mode == "single" and req.target_code:
        codes = [req.target_code]
    else:
        codes = req.search_codes if req.search_codes else POPULAR_STOCKS

    # 线程池并发匹配
    args_list = [(code, cache, start, end, user_curve) for code in codes]
    with ThreadPoolExecutor(max_workers=6) as pool:
        raw = list(pool.map(_match_one_stock, args_list))

    results = []
    for r in raw:
        if r:
            r["name"] = name_map.get(r["code"], "")
            results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"matches": results[:req.top_n]}

"""数据查询接口"""
import asyncio
import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.connection import get_db
from database.models import Settings
from data.tushare_client import TushareClient
from data.data_cache import DataCache

router = APIRouter(prefix="/api/data", tags=["data"])

# 股票列表内存缓存 (TTL 30分钟)
_stock_cache = {"data": None, "token": None, "ts": 0}
_STOCK_CACHE_TTL = 1800  # 30 minutes


async def get_cache(db: AsyncSession) -> DataCache:
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")
    client = TushareClient(settings.tushare_token)
    return DataCache(client)


@router.get("/daily")
async def get_daily(ts_code: str, start_date: str, end_date: str, db: AsyncSession = Depends(get_db)):
    cache = await get_cache(db)
    df = await asyncio.to_thread(cache.get_daily, ts_code, start_date, end_date)
    if df.empty:
        return {"data": []}
    return {"data": df.to_dict(orient="records")}


@router.get("/trade_cal")
async def get_trade_cal(start_date: str, end_date: str, db: AsyncSession = Depends(get_db)):
    cache = await get_cache(db)
    dates = await asyncio.to_thread(cache.get_trade_cal, start_date, end_date)
    return {"data": dates}


@router.get("/stock_search")
async def stock_search(keyword: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    now = time.time()
    if (
        _stock_cache["data"] is not None
        and _stock_cache["token"] == settings.tushare_token
        and now - _stock_cache["ts"] < _STOCK_CACHE_TTL
    ):
        df = _stock_cache["data"]
    else:
        client = TushareClient(settings.tushare_token)
        df = await asyncio.to_thread(client.get_stock_basic)
        _stock_cache["data"] = df
        _stock_cache["token"] = settings.tushare_token
        _stock_cache["ts"] = now

    if df.empty:
        return {"data": []}
    mask = (
        df["ts_code"].str.contains(keyword, case=False, regex=False, na=False)
        | df["name"].str.contains(keyword, case=False, regex=False, na=False)
    )
    results = df[mask].head(20)
    return {"data": results.to_dict(orient="records")}


@router.post("/validate_token")
async def validate_token(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="未配置Token")
    client = TushareClient(settings.tushare_token)
    valid = await asyncio.to_thread(client.validate_token)
    return {"valid": valid}


@router.post("/clear_cache")
async def clear_cache(db: AsyncSession = Depends(get_db)):
    cache = await get_cache(db)
    cache.clear_cache()
    return {"message": "缓存已清除"}

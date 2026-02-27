"""数据质量检测接口"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database.connection import get_db
from database.models import Settings
from data.tushare_client import TushareClient
from data.data_cache import DataCache

router = APIRouter(prefix="/api/quality", tags=["quality"])


class QualityCheckRequest(BaseModel):
    universe: list[str]
    start_date: str
    end_date: str


@router.post("/check")
async def check_quality(req: QualityCheckRequest, db: AsyncSession = Depends(get_db)):
    """数据质量检测"""
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client)

    from data.quality import check_data_quality, check_survivorship_bias

    quality = await asyncio.to_thread(
        check_data_quality, cache, req.universe, req.start_date, req.end_date
    )
    delisted = await asyncio.to_thread(
        check_survivorship_bias, client, req.universe
    )
    quality["delisted"] = delisted

    return quality

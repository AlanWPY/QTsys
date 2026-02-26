"""设置接口"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database.models import Settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsSchema(BaseModel):
    tushare_token: Optional[str] = None
    default_cash: Optional[float] = None
    commission_rate: Optional[float] = None
    stamp_tax_rate: Optional[float] = None
    slippage: Optional[float] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None


class SettingsResponse(BaseModel):
    tushare_token: str = ""
    default_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.001
    slippage: float = 0.002
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""


async def get_or_create_settings(db: AsyncSession) -> Settings:
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = Settings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def _to_response(settings: Settings) -> SettingsResponse:
    return SettingsResponse(
        tushare_token=settings.tushare_token or "",
        default_cash=settings.default_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        llm_api_key=settings.llm_api_key or "",
        llm_base_url=settings.llm_base_url or "",
        llm_model=settings.llm_model or "",
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    settings = await get_or_create_settings(db)
    return _to_response(settings)


@router.put("", response_model=SettingsResponse)
async def update_settings(data: SettingsSchema, db: AsyncSession = Depends(get_db)):
    settings = await get_or_create_settings(db)
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(settings, field, value)
    await db.commit()
    await db.refresh(settings)
    return _to_response(settings)

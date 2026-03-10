"""设置相关服务。"""
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import build_mysql_url
from database.connection import init_external_db
from database.models import Settings


SECRET_FIELDS = {"tushare_token", "llm_api_key", "mysql_password"}


async def get_or_create_settings(db: AsyncSession) -> Settings:
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = Settings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def serialize_settings(settings: Settings) -> dict[str, Any]:
    return {
        "tushare_token": "",
        "default_cash": settings.default_cash,
        "commission_rate": settings.commission_rate,
        "stamp_tax_rate": settings.stamp_tax_rate,
        "slippage": settings.slippage,
        "llm_api_key": "",
        "llm_base_url": settings.llm_base_url or "",
        "llm_model": settings.llm_model or "",
        "mysql_host": settings.mysql_host or "",
        "mysql_port": settings.mysql_port or 3306,
        "mysql_user": settings.mysql_user or "",
        "mysql_password": "",
        "mysql_database": settings.mysql_database or "qtsys",
        "use_mysql": settings.use_mysql or 0,
        "has_tushare_token": bool(settings.tushare_token),
        "has_llm_api_key": bool(settings.llm_api_key),
        "has_mysql_password": bool(settings.mysql_password),
    }


def apply_settings_update(settings: Settings, payload: dict[str, Any]) -> Settings:
    for field, value in payload.items():
        if field in SECRET_FIELDS and value == "":
            continue
        setattr(settings, field, value)
    return settings


async def activate_mysql_cache(settings: Settings):
    url = build_mysql_url(
        settings.mysql_host,
        settings.mysql_port,
        settings.mysql_user,
        settings.mysql_password,
        settings.mysql_database,
    )
    await init_external_db(url)


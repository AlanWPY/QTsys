"""设置接口"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from logging_config import get_logger
from services.settings_service import (
    activate_mysql_cache,
    apply_settings_update,
    get_or_create_settings,
    serialize_settings,
)

logger = get_logger("qtsys.settings")

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
    mysql_host: Optional[str] = None
    mysql_port: Optional[int] = None
    mysql_user: Optional[str] = None
    mysql_password: Optional[str] = None
    mysql_database: Optional[str] = None
    use_mysql: Optional[int] = None


class SettingsResponse(BaseModel):
    default_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.001
    slippage: float = 0.002
    llm_base_url: str = ""
    llm_model: str = ""
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_database: str = "qtsys"
    use_mysql: int = 0
    tushare_token: str = ""
    llm_api_key: str = ""
    mysql_password: str = ""
    has_tushare_token: bool = False
    has_llm_api_key: bool = False
    has_mysql_password: bool = False


@router.get("", response_model=SettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    settings = await get_or_create_settings(db)
    return SettingsResponse(**serialize_settings(settings))


@router.put("", response_model=SettingsResponse)
async def update_settings(data: SettingsSchema, db: AsyncSession = Depends(get_db)):
    settings = await get_or_create_settings(db)
    apply_settings_update(settings, data.model_dump(exclude_none=True))
    await db.commit()
    await db.refresh(settings)
    return SettingsResponse(**serialize_settings(settings))


class MysqlTestRequest(BaseModel):
    host: str
    port: int = 3306
    user: str
    password: str
    database: str = "qtsys"


@router.post("/test_mysql")
async def test_mysql(req: MysqlTestRequest):
    """测试 MySQL 连接"""
    from config import build_mysql_url
    from sqlalchemy.ext.asyncio import create_async_engine
    url = build_mysql_url(req.host, req.port, req.user, req.password, req.database)
    tmp_engine = create_async_engine(url, echo=False)
    try:
        async with tmp_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"success": True, "message": "连接成功"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        await tmp_engine.dispose()


@router.post("/activate_mysql")
async def activate_mysql(db: AsyncSession = Depends(get_db)):
    """激活 MySQL 缓存：初始化表结构并保存配置。"""
    settings = await get_or_create_settings(db)
    if not settings.mysql_host or not settings.mysql_user:
        return {"success": False, "message": "请先配置 MySQL 连接信息"}
    try:
        await activate_mysql_cache(settings)
        settings.use_mysql = 1
        await db.commit()
        logger.info("MySQL 缓存已激活并初始化表结构")
        return {"success": True, "message": "MySQL 缓存已激活"}
    except Exception as e:
        logger.exception("激活 MySQL 缓存失败")
        return {"success": False, "message": str(e)}

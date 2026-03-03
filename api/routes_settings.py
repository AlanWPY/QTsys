"""设置接口"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database.models import Settings
from logging_config import get_logger

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
    tushare_token: str = ""
    default_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.001
    slippage: float = 0.002
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = "qtsys"
    use_mysql: int = 0


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
        mysql_host=settings.mysql_host or "",
        mysql_port=settings.mysql_port or 3306,
        mysql_user=settings.mysql_user or "",
        mysql_password=settings.mysql_password or "",
        mysql_database=settings.mysql_database or "qtsys",
        use_mysql=settings.use_mysql or 0,
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
    """激活 MySQL: 切换引擎 + 初始化表"""
    from config import build_mysql_url
    from database.connection import switch_to_mysql, init_db
    settings = await get_or_create_settings(db)
    if not settings.mysql_host or not settings.mysql_user:
        return {"success": False, "message": "请先配置 MySQL 连接信息"}
    url = build_mysql_url(
        settings.mysql_host, settings.mysql_port,
        settings.mysql_user, settings.mysql_password,
        settings.mysql_database,
    )
    try:
        switch_to_mysql(url)
        await init_db()
        settings.use_mysql = 1
        await db.commit()
        logger.info("MySQL 已激活并初始化表结构")
        return {"success": True, "message": "MySQL 已激活"}
    except Exception as e:
        logger.exception("激活 MySQL 失败")
        return {"success": False, "message": str(e)}

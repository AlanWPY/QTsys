"""设置接口。"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from config import build_mysql_url
from database.connection import get_db
from logging_config import get_logger
from services.llm_gateway import chat_complete_text, normalize_base_url
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


class MysqlTestRequest(BaseModel):
    host: str
    port: int = 3306
    user: str
    password: str
    database: str = "qtsys"


class LLMTestRequest(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""


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


@router.post("/test_mysql")
async def test_mysql(req: MysqlTestRequest):
    """测试 MySQL 连接。"""
    url = build_mysql_url(req.host, req.port, req.user, req.password, req.database)
    tmp_engine = create_async_engine(url, echo=False)
    try:
        async with tmp_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"success": True, "message": "MySQL 连接成功"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}
    finally:
        await tmp_engine.dispose()


@router.post("/activate_mysql")
async def activate_mysql(db: AsyncSession = Depends(get_db)):
    """启用 MySQL 缓存并初始化表结构。"""
    settings = await get_or_create_settings(db)
    if not settings.mysql_host or not settings.mysql_user:
        return {"success": False, "message": "请先配置 MySQL 连接信息"}

    try:
        await activate_mysql_cache(settings)
        settings.use_mysql = 1
        await db.commit()
        logger.info("MySQL 缓存已启用并初始化完成")
        return {"success": True, "message": "MySQL 缓存已启用"}
    except Exception as exc:
        logger.exception("启用 MySQL 缓存失败")
        return {"success": False, "message": str(exc)}


@router.post("/test_llm")
async def test_llm(req: LLMTestRequest, db: AsyncSession = Depends(get_db)):
    """测试 LLM 配置，可直接使用前端表单中的临时值。"""
    settings = await get_or_create_settings(db)
    api_key = (req.api_key or "").strip() or (settings.llm_api_key or "").strip()
    base_url = (req.base_url or "").strip() or (settings.llm_base_url or "").strip()
    model = (req.model or "").strip() or (settings.llm_model or "").strip()

    if not api_key or not base_url or not model:
        return {
            "success": False,
            "message": "请先填写并保存完整的 LLM 配置，或在当前表单中补全 API Key、接口地址、模型名称。",
        }

    try:
        result = await chat_complete_text(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            temperature=0.1,
            max_tokens=16,
        )
        return {
            "success": True,
            "message": "模型连接测试成功",
            "normalized_base_url": result["base_url"],
            "resolved_model": result["model"],
            "response_preview": (result["content"] or "")[:120],
            "available_models": result.get("available_models", [])[:20],
        }
    except Exception as exc:
        return {
            "success": False,
            "message": str(exc),
            "normalized_base_url": normalize_base_url(base_url),
        }

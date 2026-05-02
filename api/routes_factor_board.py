"""因子看板 API 路由。"""

from __future__ import annotations

from typing import Optional
import threading
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.tushare_client import TushareClient
from database.connection import get_db
from database.db_config import save_db_config, test_db_connection
from database.db_manager import DatabaseManager
from database.models import Settings, StockPool
from factor.alpha191_formulas import ALPHA191_INFO
from services.factor_board_service import (
    AnalysisCancelledError,
    get_factor_board_db_config,
    get_system_universes,
    run_factor_board_analysis,
)
from services.settings_service import get_or_create_settings

router = APIRouter(prefix="/api/factor_board", tags=["factor_board"])

analysis_status = {
    "running": False,
    "progress": 0,
    "total": 191,
    "current_factor": "",
    "message": "\u5f85\u542f\u52a8",
    "phase": "idle",
    "phase_label": "\u7b49\u5f85\u4efb\u52a1",
    "factor_completed": 0,
    "factor_total": 191,
    "market_total_codes": 0,
    "market_completed_codes": 0,
    "market_cached_codes": 0,
    "market_updated_codes": 0,
    "market_failed_codes": 0,
    "market_bundle_hit": False,
    "backtest_days": 365,
    "start_date": "",
    "end_date": "",
    "analysis_batch": "",
    "universe_type": "system",
    "universe_code": "000905.SH",
    "universe_name": "\u4e2d\u8bc1500",
    "custom_pool_id": None,
    "result_count": 0,
    "failed_factors": [],
    "logs": [],
    "batch_reused": False,
    "started_at": "",
    "updated_at": "",
    "last_error": "",
    "stop_requested": False,
}
analysis_status_lock = threading.Lock()
analysis_cancel_event = threading.Event()


class DBConfig(BaseModel):
    host: str
    port: int = 3306
    user: str
    password: str = ""
    database: str = "qtsys"
    has_password: Optional[bool] = False


class BacktestConfig(BaseModel):
    backtest_days: int


class AnalysisRequest(BaseModel):
    backtest_days: int = 365
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    universe_type: str = "system"
    universe_code: str = "000905.SH"
    universe_name: Optional[str] = None
    custom_pool_id: Optional[int] = None
    benchmark_code: Optional[str] = None
    reuse_existing: bool = True


class StockItemSchema(BaseModel):
    ts_code: str
    name: str = ""


class StockPoolSchema(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    stock_items: list[StockItemSchema] = Field(default_factory=list)


class StockPoolUpdateSchema(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = None
    stock_items: Optional[list[StockItemSchema]] = None


def serialize_pool(pool: StockPool) -> dict:
    items = pool.stock_items or []
    return {
        "id": pool.id,
        "name": pool.name,
        "description": pool.description or "",
        "pool_type": pool.pool_type or "custom",
        "index_code": pool.index_code or "",
        "stock_items": items,
        "stock_count": len(items),
        "created_at": pool.created_at.isoformat() if pool.created_at else "",
        "updated_at": pool.updated_at.isoformat() if pool.updated_at else "",
    }


async def require_tushare_client(db: AsyncSession) -> TushareClient:
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先在系统设置中配置 Tushare Token")
    return TushareClient(settings.tushare_token)


async def get_pool_or_404(db: AsyncSession, pool_id: int) -> StockPool:
    result = await db.execute(select(StockPool).where(StockPool.id == pool_id))
    pool = result.scalar_one_or_none()
    if not pool:
        raise HTTPException(status_code=404, detail="自定义股票池不存在")
    return pool


def _append_status_log_locked(message: str):
    text = str(message or "").strip()
    if not text:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    logs = list(analysis_status.get("logs") or [])
    if logs and logs[-1].endswith(text):
        return
    logs.append(f"[{timestamp}] {text}")
    analysis_status["logs"] = logs[-120:]


def _build_failed_factor_numbers(items: list[str]) -> list[int]:
    numbers: list[int] = []
    for item in items or []:
        text = str(item or "").strip()
        if "#" in text:
            text = text.split("#", 1)[1]
        try:
            numbers.append(int(text))
        except (TypeError, ValueError):
            continue
    return sorted(set(numbers))


def _update_status_factory():
    def _update_status(patch: dict):
        safe_patch = {k: v for k, v in patch.items() if v is not None}
        safe_patch["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with analysis_status_lock:
            analysis_status.update(safe_patch)
            if "message" in safe_patch:
                _append_status_log_locked(safe_patch["message"])
            if "message" in safe_patch:
                _append_status_log_locked(safe_patch["message"])
    return _update_status


def _launch_analysis(payload: dict):
    factor_numbers = payload.get("factor_numbers") or list(range(1, 192))
    total = len(factor_numbers)
    now_text = datetime.now().isoformat(timespec="seconds")
    analysis_cancel_event.clear()
    with analysis_status_lock:
        analysis_status.update({
            "running": True,
            "progress": 0,
            "total": total,
            "current_factor": "",
            "message": "任务已提交，正在初始化分析流程...",
            "phase": "queued",
            "phase_label": "任务排队",
            "factor_completed": 0,
            "factor_total": total,
            "analysis_batch": payload.get("analysis_batch") or "",
            "start_date": payload.get("start_date") or "",
            "end_date": payload.get("end_date") or "",
            "backtest_days": payload.get("backtest_days") or 365,
            "universe_type": payload.get("universe_type") or "system",
            "universe_code": payload.get("universe_code") or "000905.SH",
            "universe_name": payload.get("universe_name") or "中证500",
            "custom_pool_id": payload.get("custom_pool_id"),
            "market_total_codes": 0,
            "market_completed_codes": 0,
            "market_cached_codes": 0,
            "market_updated_codes": 0,
            "market_failed_codes": 0,
            "market_bundle_hit": False,
            "result_count": 0,
            "failed_factors": [],
            "batch_reused": False,
            "started_at": now_text,
            "updated_at": now_text,
            "last_error": "",
            "stop_requested": False,
            "logs": [],
        })
        _append_status_log_locked("任务已提交，等待执行")

    _update_status = _update_status_factory()

    def _run_task():
        try:
            run_factor_board_analysis(payload, _update_status, analysis_cancel_event)
            _update_status({"message": "分析完成", "phase": "completed", "phase_label": "分析完成"})
        except AnalysisCancelledError as exc:
            _update_status({
                "message": str(exc) or "分析已停止",
                "phase": "cancelled",
                "phase_label": "已停止",
                "last_error": "",
                "batch_reused": False,
            })
        except Exception as exc:
            _update_status({
                "message": f"分析失败: {exc}",
                "phase": "failed",
                "phase_label": "分析失败",
                "last_error": str(exc),
            })
        finally:
            with analysis_status_lock:
                analysis_status["running"] = False
                analysis_status["stop_requested"] = False
                analysis_status["updated_at"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_run_task, daemon=True).start()


@router.get("/db_config")
async def get_db_config():
    config = get_factor_board_db_config()
    return {
        "host": config.get("host", "localhost"),
        "port": config.get("port", 3306),
        "user": config.get("user", "root"),
        "password": "",
        "database": config.get("database", "qtsys"),
        "has_password": bool(config.get("password")),
    }


@router.post("/db_config")
async def update_db_config(config: DBConfig, db: AsyncSession = Depends(get_db)):
    current = get_factor_board_db_config()
    payload = config.model_dump()
    if not payload.get("password") and current.get("password"):
        payload["password"] = current["password"]
    payload.pop("has_password", None)

    success, message = test_db_connection(payload)
    if not success:
        raise HTTPException(status_code=400, detail=f"数据库连接测试失败: {message}")

    save_db_config(payload)
    settings = await get_or_create_settings(db)
    settings.mysql_host = payload["host"]
    settings.mysql_port = payload["port"]
    settings.mysql_user = payload["user"]
    settings.mysql_database = payload["database"]
    if payload.get("password"):
        settings.mysql_password = payload["password"]
    settings.use_mysql = 1
    await db.commit()
    return {"success": True, "message": "数据库配置已保存"}


@router.post("/test_connection")
async def test_connection(config: DBConfig):
    current = get_factor_board_db_config()
    payload = config.model_dump()
    if not payload.get("password") and current.get("password"):
        payload["password"] = current["password"]
    payload.pop("has_password", None)
    success, message = test_db_connection(payload)
    return {"success": success, "message": message}


@router.get("/analysis_status")
async def get_analysis_status():
    with analysis_status_lock:
        return dict(analysis_status)


@router.get("/universe_options")
async def get_universe_options(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StockPool).order_by(StockPool.updated_at.desc(), StockPool.id.desc()))
    custom_pools = [serialize_pool(pool) for pool in result.scalars().all()]
    return {
        "system_universes": get_system_universes(),
        "custom_pools": custom_pools,
    }


@router.get("/system_universe_members")
async def get_system_universe_members(
    code: str = Query("000905.SH"),
    db: AsyncSession = Depends(get_db),
):
    universe_code = str(code or "000905.SH").strip().upper()
    universe_map = {item["code"]: item for item in get_system_universes()}
    universe = universe_map.get(universe_code)
    if not universe:
        raise HTTPException(status_code=404, detail="系统股票池不存在")

    client = await require_tushare_client(db)
    end_date = datetime.now().strftime("%Y%m%d")
    index_weight = pd.DataFrame()
    for lookback_days in (45, 120, 365):
        start_date = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")
        candidate = client.get_index_weight(universe_code, start_date, end_date)
        if candidate is not None and not candidate.empty:
            index_weight = candidate
            break

    if index_weight is None or index_weight.empty:
        raise HTTPException(status_code=404, detail="未获取到系统股票池成分数据")

    frame = index_weight.copy()
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    frame["con_code"] = frame["con_code"].astype(str).str.strip().str.upper()
    frame["weight"] = pd.to_numeric(frame.get("weight", 0), errors="coerce").fillna(0.0)
    latest_trade_date = str(frame["trade_date"].max() or "")
    latest = frame[frame["trade_date"] == latest_trade_date].copy()
    latest = latest.sort_values(["weight", "con_code"], ascending=[False, True])
    stock_items = [
        {"ts_code": str(row.con_code).strip(), "name": ""}
        for row in latest.itertuples(index=False)
        if str(row.con_code).strip()
    ]

    return {
        "code": universe_code,
        "name": universe.get("name") or universe_code,
        "benchmark_code": universe.get("benchmark_code") or universe_code,
        "trade_date": latest_trade_date,
        "stock_items": stock_items,
        "stock_count": len(stock_items),
    }


@router.get("/custom_pools")
async def list_custom_pools(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StockPool).order_by(StockPool.updated_at.desc(), StockPool.id.desc()))
    return [serialize_pool(pool) for pool in result.scalars().all()]


@router.post("/custom_pools")
async def create_custom_pool(data: StockPoolSchema, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StockPool).where(StockPool.name == data.name.strip()))
    exists = result.scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="股票池名称已存在")
    items = []
    seen = set()
    for item in data.stock_items:
        code = item.ts_code.strip().upper()
        if not code or code in seen:
            continue
        items.append({"ts_code": code, "name": (item.name or "").strip()})
        seen.add(code)
    pool = StockPool(
        name=data.name.strip(),
        description=(data.description or "").strip(),
        pool_type="custom",
        stock_items=items,
    )
    db.add(pool)
    await db.commit()
    await db.refresh(pool)
    return serialize_pool(pool)


@router.put("/custom_pools/{pool_id}")
async def update_custom_pool(pool_id: int, data: StockPoolUpdateSchema, db: AsyncSession = Depends(get_db)):
    pool = await get_pool_or_404(db, pool_id)
    if data.name is not None:
        name = data.name.strip()
        result = await db.execute(select(StockPool).where(StockPool.name == name, StockPool.id != pool_id))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="股票池名称已存在")
        pool.name = name
    if data.description is not None:
        pool.description = data.description.strip()
    if data.stock_items is not None:
        items = []
        seen = set()
        for item in data.stock_items:
            code = item.ts_code.strip().upper()
            if not code or code in seen:
                continue
            items.append({"ts_code": code, "name": (item.name or "").strip()})
            seen.add(code)
        pool.stock_items = items
    await db.commit()
    await db.refresh(pool)
    return serialize_pool(pool)


@router.delete("/custom_pools/{pool_id}")
async def delete_custom_pool(pool_id: int, db: AsyncSession = Depends(get_db)):
    pool = await get_pool_or_404(db, pool_id)
    await db.delete(pool)
    await db.commit()
    return {"success": True}


@router.get("/stock_search")
async def stock_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    client = await require_tushare_client(db)
    basics = client.get_stock_basic()
    if basics is None or basics.empty:
        return []
    keyword = q.strip().lower()
    basics = basics.fillna("")
    mask = basics["ts_code"].astype(str).str.lower().str.contains(keyword, regex=False, na=False) | basics["name"].astype(str).str.lower().str.contains(keyword, regex=False, na=False)
    matched = basics.loc[mask, ["ts_code", "name", "industry", "list_date"]].head(limit)
    return matched.to_dict("records")


@router.get("/latest_results")
async def get_latest_results():
    try:
        db = DatabaseManager(get_factor_board_db_config())
        db.connect()
        db.init_tables()
        results = db.get_latest_analysis_results()
        db.close()
        return results.to_dict("records")
    except Exception as exc:
        with analysis_status_lock:
            analysis_status["message"] = f"因子看板数据库不可用：{exc}"
            analysis_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return []


@router.post("/stop_analysis")
async def stop_analysis():
    with analysis_status_lock:
        if not analysis_status["running"]:
            return {"success": True, "message": "当前没有正在运行的分析任务"}
        analysis_status["message"] = "已收到停止请求，正在结束当前分析任务..."
        analysis_status["phase"] = "stopping"
        analysis_status["phase_label"] = "正在停止"
        analysis_status["stop_requested"] = True
        analysis_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
    analysis_cancel_event.set()
    return {"success": True, "message": "已发送停止请求"}


@router.post("/start_analysis")
async def start_analysis(data: AnalysisRequest, db: AsyncSession = Depends(get_db)):
    with analysis_status_lock:
        if analysis_status["running"]:
            raise HTTPException(status_code=400, detail="\u5df2\u6709\u5206\u6790\u4efb\u52a1\u6b63\u5728\u8fd0\u884c")

    payload = data.model_dump()
    if payload["universe_type"] == "custom":
        if not payload.get("custom_pool_id"):
            raise HTTPException(status_code=400, detail="\u8bf7\u9009\u62e9\u81ea\u5b9a\u4e49\u80a1\u7968\u6c60")
        pool = await get_pool_or_404(db, payload["custom_pool_id"])
        payload["universe_name"] = pool.name
        payload["universe_code"] = f"custom:{pool.id}"
        payload["stock_items"] = pool.stock_items or []
    else:
        payload["custom_pool_id"] = None
        payload["stock_items"] = []

    def _update_status(patch: dict):
        safe_patch = {k: v for k, v in patch.items() if v is not None}
        safe_patch["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with analysis_status_lock:
            analysis_status.update(safe_patch)

    now_text = datetime.now().isoformat(timespec="seconds")
    analysis_cancel_event.clear()
    with analysis_status_lock:
        analysis_status.update({
            "running": True,
            "progress": 0,
            "total": 191,
            "current_factor": "",
            "message": "\u4efb\u52a1\u5df2\u63d0\u4ea4\uff0c\u6b63\u5728\u521d\u59cb\u5316\u5206\u6790\u6d41\u7a0b...",
            "phase": "queued",
            "phase_label": "\u4efb\u52a1\u6392\u961f",
            "factor_completed": 0,
            "factor_total": 191,
            "analysis_batch": "",
            "start_date": payload.get("start_date") or "",
            "end_date": payload.get("end_date") or "",
            "backtest_days": payload.get("backtest_days") or 365,
            "universe_type": payload.get("universe_type") or "system",
            "universe_code": payload.get("universe_code") or "000905.SH",
            "universe_name": payload.get("universe_name") or "\u4e2d\u8bc1500",
            "custom_pool_id": payload.get("custom_pool_id"),
            "market_total_codes": 0,
            "market_completed_codes": 0,
            "market_cached_codes": 0,
            "market_updated_codes": 0,
            "market_failed_codes": 0,
            "market_bundle_hit": False,
            "result_count": 0,
            "failed_factors": [],
            "batch_reused": False,
            "started_at": now_text,
            "updated_at": now_text,
            "last_error": "",
            "stop_requested": False,
            "logs": [],
        })
        _append_status_log_locked("任务已提交，等待执行")

    def _run_task():
        try:
            run_factor_board_analysis(payload, _update_status, analysis_cancel_event)
            _update_status({"message": "\u5206\u6790\u5b8c\u6210", "phase": "completed", "phase_label": "\u5206\u6790\u5b8c\u6210"})
        except AnalysisCancelledError as exc:
            _update_status({
                "message": str(exc),
                "phase": "cancelled",
                "phase_label": "\u5df2\u505c\u6b62",
                "last_error": "",
                "batch_reused": False,
            })
        except Exception as exc:
            _update_status({
                "message": f"\u5206\u6790\u5931\u8d25: {exc}",
                "phase": "failed",
                "phase_label": "\u5206\u6790\u5931\u8d25",
                "last_error": str(exc),
            })
        finally:
            with analysis_status_lock:
                analysis_status["running"] = False
                analysis_status["stop_requested"] = False
                analysis_status["updated_at"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_run_task, daemon=True).start()
    return {"message": "\u5206\u6790\u4efb\u52a1\u5df2\u542f\u52a8"}


@router.post("/retry_failed_factors")
async def retry_failed_factors(db: AsyncSession = Depends(get_db)):
    with analysis_status_lock:
        if analysis_status["running"]:
            raise HTTPException(status_code=400, detail="当前已有分析任务正在运行")
        failed_numbers = _build_failed_factor_numbers(analysis_status.get("failed_factors") or [])
        if not failed_numbers:
            raise HTTPException(status_code=400, detail="当前没有可重试的失败因子")
        payload = {
            "backtest_days": analysis_status.get("backtest_days") or 365,
            "start_date": analysis_status.get("start_date") or None,
            "end_date": analysis_status.get("end_date") or None,
            "universe_type": analysis_status.get("universe_type") or "system",
            "universe_code": analysis_status.get("universe_code") or "000905.SH",
            "universe_name": analysis_status.get("universe_name") or "中证500",
            "custom_pool_id": analysis_status.get("custom_pool_id"),
            "benchmark_code": analysis_status.get("universe_code") or "",
            "reuse_existing": False,
            "analysis_batch": analysis_status.get("analysis_batch") or "",
            "factor_numbers": failed_numbers,
        }

    if payload["universe_type"] == "custom":
        if not payload.get("custom_pool_id"):
            raise HTTPException(status_code=400, detail="自定义股票池不存在，请重新选择")
        pool = await get_pool_or_404(db, int(payload["custom_pool_id"]))
        payload["universe_name"] = pool.name
        payload["universe_code"] = f"custom:{pool.id}"
        payload["stock_items"] = pool.stock_items or []
        payload["benchmark_code"] = ""
    else:
        payload["custom_pool_id"] = None
        payload["stock_items"] = []

    now_text = datetime.now().isoformat(timespec="seconds")
    analysis_cancel_event.clear()
    with analysis_status_lock:
        analysis_status.update({
            "running": True,
            "progress": 0,
            "total": len(failed_numbers),
            "current_factor": "",
            "message": f"开始重试 {len(failed_numbers)} 个失败因子...",
            "phase": "queued",
            "phase_label": "失败重试",
            "factor_completed": 0,
            "factor_total": len(failed_numbers),
            "analysis_batch": payload.get("analysis_batch") or "",
            "start_date": payload.get("start_date") or "",
            "end_date": payload.get("end_date") or "",
            "backtest_days": payload.get("backtest_days") or 365,
            "universe_type": payload.get("universe_type") or "system",
            "universe_code": payload.get("universe_code") or "000905.SH",
            "universe_name": payload.get("universe_name") or "中证500",
            "custom_pool_id": payload.get("custom_pool_id"),
            "market_total_codes": 0,
            "market_completed_codes": 0,
            "market_cached_codes": 0,
            "market_updated_codes": 0,
            "market_failed_codes": 0,
            "market_bundle_hit": False,
            "result_count": 0,
            "failed_factors": [],
            "batch_reused": False,
            "started_at": now_text,
            "updated_at": now_text,
            "last_error": "",
            "stop_requested": False,
            "logs": [],
        })
        _append_status_log_locked(f"开始重试 {len(failed_numbers)} 个失败因子")

    def _update_status(patch: dict):
        safe_patch = {k: v for k, v in patch.items() if v is not None}
        safe_patch["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with analysis_status_lock:
            analysis_status.update(safe_patch)
            if "message" in safe_patch:
                _append_status_log_locked(safe_patch["message"])

    def _run_task():
        try:
            run_factor_board_analysis(payload, _update_status, analysis_cancel_event)
            _update_status({"message": "失败因子重试完成", "phase": "completed", "phase_label": "分析完成"})
        except AnalysisCancelledError as exc:
            _update_status({
                "message": str(exc),
                "phase": "cancelled",
                "phase_label": "已停止",
                "last_error": "",
                "batch_reused": False,
            })
        except Exception as exc:
            _update_status({
                "message": f"重试失败: {exc}",
                "phase": "failed",
                "phase_label": "分析失败",
                "last_error": str(exc),
            })
        finally:
            with analysis_status_lock:
                analysis_status["running"] = False
                analysis_status["stop_requested"] = False
                analysis_status["updated_at"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_run_task, daemon=True).start()
    return {"success": True, "message": f"已开始重试 {len(failed_numbers)} 个失败因子"}


@router.post("/set_backtest_days")
async def set_backtest_days(config: BacktestConfig):
    if config.backtest_days < 60 or config.backtest_days > 3650:
        raise HTTPException(status_code=400, detail="回测天数需在 60-3650 之间")
    analysis_status["backtest_days"] = config.backtest_days
    return {"success": True, "backtest_days": config.backtest_days}


@router.get("/factor/{factor_name}/details")
async def get_factor_details(factor_name: str):
    try:
        db = DatabaseManager(get_factor_board_db_config())
        db.connect()
        data = db.get_factor_daily_returns(factor_name)
        db.close()
        if len(data) > 0:
            data["trade_date"] = pd.to_datetime(data["trade_date"]).dt.strftime("%Y%m%d")
        return data.to_dict("records")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/factor/{factor_name}/holdings")
async def get_factor_holdings(factor_name: str, trade_date: Optional[str] = None):
    try:
        db = DatabaseManager(get_factor_board_db_config())
        db.connect()
        if trade_date is None:
            latest = db.get_factor_holdings(factor_name)
            db.close()
            if latest.empty:
                return []
            latest_date = str(pd.to_datetime(latest.iloc[0]["trade_date"]).strftime("%Y%m%d"))
            latest = latest[pd.to_datetime(latest["trade_date"]).dt.strftime("%Y%m%d") == latest_date]
            latest["trade_date"] = pd.to_datetime(latest["trade_date"]).dt.strftime("%Y%m%d")
            return latest.to_dict("records")
        holdings = db.get_factor_holdings(factor_name, trade_date)
        db.close()
        if len(holdings) > 0:
            holdings["trade_date"] = pd.to_datetime(holdings["trade_date"]).dt.strftime("%Y%m%d")
        return holdings.to_dict("records")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/factor/{factor_name}/snapshot")
async def get_factor_snapshot(factor_name: str):
    try:
        db = DatabaseManager(get_factor_board_db_config())
        db.connect()
        latest_result = db.get_factor_latest_result(factor_name)
        holdings = db.get_factor_holdings(factor_name)
        db.close()

        if latest_result and latest_result.get("latest_trade_date") is not None:
            latest_date = pd.to_datetime(latest_result["latest_trade_date"]).strftime("%Y%m%d")
            if not holdings.empty:
                holdings = holdings[pd.to_datetime(holdings["trade_date"]).dt.strftime("%Y%m%d") == latest_date]
        else:
            latest_date = ""
            if not holdings.empty:
                latest_date = pd.to_datetime(holdings.iloc[0]["trade_date"]).strftime("%Y%m%d")
                holdings = holdings[pd.to_datetime(holdings["trade_date"]).dt.strftime("%Y%m%d") == latest_date]

        grouped = {}
        if not holdings.empty:
            holdings["trade_date"] = pd.to_datetime(holdings["trade_date"]).dt.strftime("%Y%m%d")
            for quantile, group in holdings.groupby("quantile"):
                grouped[str(int(quantile))] = group.head(10).to_dict("records")

        return {
            "latest_result": latest_result,
            "latest_trade_date": latest_date,
            "holdings_by_quantile": grouped,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/factor/{factor_name}/info")
async def get_factor_info(factor_name: str):
    try:
        factor_num = int(factor_name.replace("Alpha#", ""))
        info = ALPHA191_INFO.get(factor_num, {
            "formula": "N/A",
            "description": "暂无说明",
        })
        db = DatabaseManager(get_factor_board_db_config())
        db.connect()
        latest_result = db.get_factor_latest_result(factor_name)
        db.close()
        return {**info, "latest_result": latest_result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

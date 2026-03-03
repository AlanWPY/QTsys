"""因子回测API接口"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database.connection import get_db
from database.models import Settings, Factor, FactorBacktestResult
from tasks.task_manager import TaskManager

router = APIRouter(prefix="/api/factor_backtest", tags=["factor_backtest"])


class SelectionBacktestRequest(BaseModel):
    expression: str
    universe: list[str]
    start_date: str
    end_date: str
    select_mode: str = "top"
    select_pct: float = 0.1
    rebalance_days: int = 5
    initial_cash: float = 1_000_000
    benchmark: str = "000300.SH"


class TechnicalBacktestRequest(BaseModel):
    expression: str
    ts_codes: list[str]
    start_date: str
    end_date: str
    open_threshold: float = 0.0
    close_threshold: float = 0.0
    open_condition: str = "gt"
    close_condition: str = "lt"
    initial_cash: float = 1_000_000


def _get_engine_and_cache(token: str, settings=None):
    from data.tushare_client import TushareClient
    from data.data_cache import DataCache, make_mysql_conn
    from factor.factor_engine import FactorEngine
    client = TushareClient(token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings) if settings else None)
    engine = FactorEngine(cache)
    return cache, engine


@router.post("/selection")
async def selection_backtest(req: SelectionBacktestRequest, db: AsyncSession = Depends(get_db)):
    """提交选股因子回测任务"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from factor.factor_backtest import run_selection_backtest
    cache, engine = _get_engine_and_cache(settings.tushare_token, settings)

    tm = TaskManager.get_instance()
    task_id = tm.submit(
        "factor_selection_backtest",
        run_selection_backtest,
        cache, engine, req.expression, req.universe,
        req.start_date, req.end_date,
        req.select_mode, req.select_pct,
        req.rebalance_days, req.initial_cash, req.benchmark,
    )
    return {"task_id": task_id}


@router.post("/technical")
async def technical_backtest(req: TechnicalBacktestRequest, db: AsyncSession = Depends(get_db)):
    """提交技术因子回测任务"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from factor.factor_backtest import run_technical_backtest
    cache, engine = _get_engine_and_cache(settings.tushare_token, settings)

    tm = TaskManager.get_instance()
    task_id = tm.submit(
        "factor_technical_backtest",
        run_technical_backtest,
        cache, engine, req.expression, req.ts_codes,
        req.start_date, req.end_date,
        req.open_threshold, req.close_threshold,
        req.open_condition, req.close_condition,
        req.initial_cash,
    )
    return {"task_id": task_id}


@router.get("/result/{task_id}")
async def get_backtest_result(task_id: str):
    """查询因子回测任务结果"""
    tm = TaskManager.get_instance()
    info = tm.get_task(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="任务不存在")
    return info.to_dict()

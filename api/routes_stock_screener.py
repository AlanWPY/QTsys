"""股票选股 API 路由"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from database.connection import get_db
from services.stock_screener_service import get_stock_pool_options, run_stock_screener

router = APIRouter(prefix="/api/screener", tags=["screener"])


class TechFactor(BaseModel):
    id: Optional[int] = None
    name: str = ""
    expression: str = ""
    direction: str = "high"  # "high" | "low"


class ScreenerRequest(BaseModel):
    pool_code: str = "000300.SH"
    fund_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    tech_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    tech_factors: list[TechFactor] = Field(default_factory=list)
    top_n: int = Field(default=50, ge=5, le=200)
    end_date: str = ""


@router.get("/options")
async def screener_options(db: AsyncSession = Depends(get_db)):
    """获取选股选项：股票池、因子目录"""
    from services.factor_catalog_service import load_factor_catalog
    from services.settings_service import get_or_create_settings
    options = get_stock_pool_options()
    try:
        factors = await load_factor_catalog(db)
        options["factors"] = factors
    except Exception:
        options["factors"] = []
    return options


@router.post("/run")
async def run_screener(req: ScreenerRequest, db: AsyncSession = Depends(get_db)):
    """执行选股，返回按综合评分排序的股票列表"""
    try:
        result = await run_stock_screener(req.model_dump(), db)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/kline/{ts_code}")
async def screener_kline(ts_code: str, limit: int = 250, db: AsyncSession = Depends(get_db)):
    """获取股票K线数据（供选股结果页面查看）"""
    from datetime import datetime, timedelta
    from services.settings_service import get_or_create_settings
    from data.tushare_client import TushareClient
    from data.data_cache import DataCache, make_mysql_conn
    from types import SimpleNamespace
    settings = await get_or_create_settings(db)
    token = (settings.tushare_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="请先配置 Tushare Token")
    client = TushareClient(token)
    cs = SimpleNamespace(
        use_mysql=getattr(settings, "use_mysql", 0),
        mysql_host=getattr(settings, "mysql_host", ""),
        mysql_port=getattr(settings, "mysql_port", 3306),
        mysql_user=getattr(settings, "mysql_user", ""),
        mysql_password=getattr(settings, "mysql_password", ""),
        mysql_database=getattr(settings, "mysql_database", "qtsys"),
    )
    cache = DataCache(client, mysql_conn=make_mysql_conn(cs))
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=limit * 2)).strftime("%Y%m%d")
    try:
        df = cache.get_daily(ts_code, start_date, end_date, adj="qfq")
        if df is None or df.empty:
            return {"candles": [], "ts_code": ts_code}
        df = df.sort_values("trade_date").tail(limit).reset_index(drop=True)
        candles = []
        for _, row in df.iterrows():
            dt = row["trade_date"]
            date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)[:10].replace("-", "")
            candles.append({
                "date": date_str,
                "open": round(float(row["open"]), 3) if row.get("open") is not None else None,
                "high": round(float(row["high"]), 3) if row.get("high") is not None else None,
                "low":  round(float(row["low"]),  3) if row.get("low")  is not None else None,
                "close":round(float(row["close"]),3) if row.get("close") is not None else None,
                "vol":  round(float(row["vol"]),  0) if row.get("vol")  is not None else None,
            })
        return {"candles": candles, "ts_code": ts_code}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if cache.mysql:
            try:
                cache.mysql.close()
            except Exception:
                pass

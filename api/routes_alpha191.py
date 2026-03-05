"""Alpha191批量测试API"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database.connection import get_db
from database.models import Settings
from factor.alpha191_templates import ALPHA191_FORMULAS

router = APIRouter(prefix="/api/alpha191", tags=["alpha191"])


class BatchTestRequest(BaseModel):
    factor_numbers: list[int]  # 要测试的因子编号列表
    universe: list[str]
    start_date: str
    end_date: str
    groups: int = 5
    forward_days: int = 5


@router.post("/batch_test")
async def batch_test_alpha191(req: BatchTestRequest, db: AsyncSession = Depends(get_db)):
    """批量测试Alpha191因子"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache, make_mysql_conn
    from factor.factor_engine import FactorEngine

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    results = []
    for num in req.factor_numbers:
        if num < 1 or num > 191:
            continue

        formula = ALPHA191_FORMULAS.get(num)
        if not formula:
            continue

        try:
            eval_result = await asyncio.to_thread(
                engine.evaluate, formula,
                req.universe, req.start_date, req.end_date, req.groups, req.forward_days
            )

            if "error" not in eval_result:
                results.append({
                    "factor_number": num,
                    "factor_name": f"Alpha#{num}",
                    "ic_mean": eval_result["metrics"]["ic_mean"],
                    "ic_ir": eval_result["metrics"]["ic_ir"],
                    "long_short_return": eval_result["metrics"]["long_short_return"],
                    "monotonicity": eval_result["metrics"]["monotonicity"],
                })
        except Exception as e:
            results.append({
                "factor_number": num,
                "factor_name": f"Alpha#{num}",
                "error": str(e)
            })

    # 按IC_IR排序
    results.sort(key=lambda x: x.get("ic_ir", -999), reverse=True)

    return {
        "total": len(results),
        "results": results
    }


@router.get("/list")
async def list_all_alpha191():
    """列出所有Alpha191因子"""
    return {
        "total": len(ALPHA191_FORMULAS),
        "factors": [
            {"number": num, "name": f"Alpha#{num}", "formula": formula[:100] + "..."}
            for num, formula in sorted(ALPHA191_FORMULAS.items())
        ]
    }

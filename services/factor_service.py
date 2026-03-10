"""因子服务。"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from database.models import Factor, FactorResult
from factor.factor_engine import FactorEngine
from factor.genetic import run_gp
from services.settings_service import get_or_create_settings


async def evaluate_factor_workflow(
    db: AsyncSession,
    *,
    factor_id: int,
    universe: list[str],
    start_date: str,
    end_date: str,
    groups: int,
    forward_days: int,
):
    factor_result = await db.execute(select(Factor).where(Factor.id == factor_id))
    factor = factor_result.scalar_one_or_none()
    if not factor:
        raise LookupError("因子不存在")

    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    eval_result = await asyncio.to_thread(
        engine.evaluate,
        factor.expression,
        universe,
        start_date,
        end_date,
        groups,
        forward_days,
    )
    if "error" in eval_result:
        raise ValueError(eval_result["error"])

    result = FactorResult(
        factor_id=factor.id,
        factor_name=factor.name,
        universe=",".join(universe),
        start_date=start_date,
        end_date=end_date,
        metrics=eval_result["metrics"],
        ic_series=eval_result["ic_series"],
        group_returns=eval_result["group_returns"],
        turnover_series=eval_result["turnover_series"],
        long_short_curve=eval_result["long_short_curve"],
    )
    db.add(result)
    await db.commit()
    return eval_result


async def mine_gp_workflow(
    db: AsyncSession,
    *,
    universe: list[str],
    start_date: str,
    end_date: str,
    pop_size: int,
    generations: int,
):
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    results = await asyncio.to_thread(
        run_gp,
        engine,
        universe,
        start_date,
        end_date,
        pop_size,
        generations,
    )

    saved = []
    for index, item in enumerate(results, start=1):
        name = f"GP因子_{index}"
        factor = Factor(
            name=name,
            description=f"遗传算法挖掘 (适应度 {item['fitness']})",
            expression=item["expression"],
            category="GP挖掘",
            source="gp",
        )
        db.add(factor)
        saved.append({"name": name, **item})
    await db.commit()
    return {"factors": saved}

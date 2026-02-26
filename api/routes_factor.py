"""因子挖掘API接口"""
import asyncio
import csv
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database.models import Settings, Factor, FactorResult
from factor.builtin_factors import BUILTIN_FACTORS

router = APIRouter(prefix="/api/factors", tags=["factors"])


class FactorCreate(BaseModel):
    name: str
    description: str = ""
    expression: str
    category: str = "custom"


class FactorEvalRequest(BaseModel):
    factor_id: int
    universe: list[str]
    start_date: str
    end_date: str
    groups: int = 5


class GPMineRequest(BaseModel):
    universe: list[str]
    start_date: str
    end_date: str
    pop_size: int = 30
    generations: int = 10


class LLMMineRequest(BaseModel):
    universe: list[str]
    start_date: str
    end_date: str
    count: int = 5
    hint: str = ""


# ===== 因子CRUD =====

@router.get("")
async def list_factors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Factor).order_by(Factor.created_at.desc()))
    factors = result.scalars().all()
    return [
        {
            "id": f.id, "name": f.name, "description": f.description,
            "expression": f.expression, "category": f.category,
            "source": f.source,
            "created_at": f.created_at.isoformat() if f.created_at else "",
        }
        for f in factors
    ]


@router.post("")
async def create_factor(data: FactorCreate, db: AsyncSession = Depends(get_db)):
    f = Factor(
        name=data.name, description=data.description,
        expression=data.expression, category=data.category, source="user",
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return {"id": f.id, "name": f.name}


@router.delete("/{factor_id}")
async def delete_factor(factor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Factor).where(Factor.id == factor_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="因子不存在")
    await db.delete(f)
    await db.commit()
    return {"message": "已删除"}


@router.post("/init_builtin")
async def init_builtin_factors(db: AsyncSession = Depends(get_db)):
    """初始化内置因子"""
    created = 0
    for name, info in BUILTIN_FACTORS.items():
        result = await db.execute(select(Factor).where(Factor.name == name))
        if result.scalar_one_or_none():
            continue
        f = Factor(
            name=name, description=info["description"],
            expression=info["expression"],
            category=info["category"], source="builtin",
        )
        db.add(f)
        created += 1
    await db.commit()
    return {"message": f"已创建{created}个内置因子", "created": created}


# ===== 因子评价 =====

@router.post("/evaluate")
async def evaluate_factor(req: FactorEvalRequest, db: AsyncSession = Depends(get_db)):
    """评价因子效果"""
    result = await db.execute(select(Factor).where(Factor.id == req.factor_id))
    factor = result.scalar_one_or_none()
    if not factor:
        raise HTTPException(status_code=404, detail="因子不存在")

    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache
    from factor.factor_engine import FactorEngine

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client)
    engine = FactorEngine(cache)

    eval_result = await asyncio.to_thread(
        engine.evaluate, factor.expression,
        req.universe, req.start_date, req.end_date, req.groups,
    )

    if "error" in eval_result:
        raise HTTPException(status_code=400, detail=eval_result["error"])

    # 保存结果
    fr = FactorResult(
        factor_id=factor.id, factor_name=factor.name,
        universe=",".join(req.universe),
        start_date=req.start_date, end_date=req.end_date,
        metrics=eval_result["metrics"],
        ic_series=eval_result["ic_series"],
        group_returns=eval_result["group_returns"],
        turnover_series=eval_result["turnover_series"],
        long_short_curve=eval_result["long_short_curve"],
    )
    db.add(fr)
    await db.commit()

    return eval_result


# ===== 遗传算法挖掘 =====

@router.post("/mine/gp")
async def mine_gp(req: GPMineRequest, db: AsyncSession = Depends(get_db)):
    """遗传算法因子挖掘"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache
    from factor.factor_engine import FactorEngine
    from factor.genetic import run_gp

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client)
    engine = FactorEngine(cache)

    results = await asyncio.to_thread(
        run_gp, engine, req.universe,
        req.start_date, req.end_date,
        req.pop_size, req.generations,
    )

    # 自动保存发现的因子
    saved = []
    for i, r in enumerate(results):
        name = f"GP因子_{i+1}"
        f = Factor(
            name=name, description=f"遗传算法挖掘 (适应度={r['fitness']})",
            expression=r["expression"],
            category="GP挖掘", source="gp",
        )
        db.add(f)
        saved.append({"name": name, **r})
    await db.commit()

    return {"factors": saved}


# ===== LLM挖掘 =====

@router.post("/mine/llm")
async def mine_llm(req: LLMMineRequest, db: AsyncSession = Depends(get_db)):
    """LLM因子挖掘"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=400, detail="请先配置设置")
    if not settings.llm_api_key or not settings.llm_base_url:
        raise HTTPException(status_code=400, detail="请先在设置页面配置LLM API Key和地址")

    from factor.llm_miner import call_llm, parse_llm_response, FACTOR_MINING_PROMPT

    extra = f"\n5. 额外提示: {req.hint}" if req.hint else ""
    prompt = FACTOR_MINING_PROMPT.format(count=req.count, extra_hint=extra)

    response = await call_llm(
        settings.llm_api_key, settings.llm_base_url,
        settings.llm_model or "gpt-3.5-turbo", prompt,
    )

    if not response:
        raise HTTPException(status_code=500, detail="LLM调用失败，请检查API配置")

    factors = parse_llm_response(response)
    if not factors:
        raise HTTPException(status_code=500, detail="LLM返回格式解析失败")

    # 保存到数据库
    saved = []
    for item in factors:
        f = Factor(
            name=item["name"], description=item["description"],
            expression=item["expression"],
            category="LLM挖掘", source="llm",
        )
        db.add(f)
        saved.append(item)
    await db.commit()

    return {"factors": saved}


# ===== 因子结果导出 =====

@router.get("/export/{result_id}")
async def export_factor_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """导出因子评估结果为CSV"""
    result = await db.execute(
        select(FactorResult).where(FactorResult.id == result_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="因子结果不存在")

    output = io.StringIO()

    # 写入指标摘要
    output.write("# 因子评价指标\n")
    metrics = r.metrics or {}
    for k, v in metrics.items():
        output.write(f"{k},{v}\n")
    output.write("\n")

    # 写入IC序列
    output.write("# IC序列\n")
    writer = csv.DictWriter(output, fieldnames=["date", "ic"])
    writer.writeheader()
    for row in (r.ic_series or []):
        writer.writerow(row)
    output.write("\n")

    # 写入分组收益
    output.write("# 分组收益\n")
    gr_fields = ["group", "label", "cumulative_return", "annual_return"]
    writer2 = csv.DictWriter(output, fieldnames=gr_fields)
    writer2.writeheader()
    for row in (r.group_returns or []):
        writer2.writerow({k: row.get(k, "") for k in gr_fields})
    output.write("\n")

    # 写入多空曲线
    output.write("# 多空曲线\n")
    writer3 = csv.DictWriter(output, fieldnames=["date", "value"])
    writer3.writeheader()
    for row in (r.long_short_curve or []):
        writer3.writerow(row)

    output.seek(0)
    filename = f"factor_{result_id}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
"""高级因子分析API - 相关性、组合优化、衰减、归因"""
import asyncio
import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database.connection import get_db
from database.models import Settings

router = APIRouter(prefix="/api/advanced_analysis", tags=["advanced_analysis"])


class CorrelationRequest(BaseModel):
    """因子相关性分析请求"""
    expressions: list[str]  # 因子表达式列表
    universe: list[str]
    start_date: str
    end_date: str


class CombineRequest(BaseModel):
    """因子组合优化请求"""
    expressions: list[str]
    weights: list[float] = None  # 如果为None则自动优化
    universe: list[str]
    start_date: str
    end_date: str


class DecayRequest(BaseModel):
    """因子衰减分析请求"""
    expression: str
    universe: list[str]
    start_date: str
    end_date: str
    window_days: int = 60  # 滚动窗口天数


@router.post("/correlation")
async def factor_correlation(req: CorrelationRequest, db: AsyncSession = Depends(get_db)):
    """因子相关性分析 - 计算多个因子之间的相关系数矩阵"""
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

    # 计算每个因子的值
    factor_values = {}
    for i, expr in enumerate(req.expressions):
        all_values = []
        for ts_code in req.universe:
            fv = await asyncio.to_thread(
                engine.compute_factor_values, expr, ts_code,
                req.start_date, req.end_date
            )
            if fv is not None:
                all_values.extend(fv.dropna().values)

        if all_values:
            factor_values[f"Factor_{i+1}"] = all_values

    # 计算相关性矩阵
    if len(factor_values) < 2:
        raise HTTPException(status_code=400, detail="至少需要2个有效因子")

    df = pd.DataFrame(dict([(k, pd.Series(v)) for k, v in factor_values.items()]))
    corr_matrix = df.corr()

    return {
        "correlation_matrix": corr_matrix.to_dict(),
        "summary": {
            "avg_correlation": float(corr_matrix.values[np.triu_indices_from(corr_matrix.values, k=1)].mean()),
            "max_correlation": float(corr_matrix.values[np.triu_indices_from(corr_matrix.values, k=1)].max()),
        }
    }


@router.post("/combine")
async def factor_combine(req: CombineRequest, db: AsyncSession = Depends(get_db)):
    """因子组合优化 - 使用IC加权或等权组合多个因子"""
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

    # 如果没有提供权重，使用IC加权
    if req.weights is None:
        ic_list = []
        for expr in req.expressions:
            result = await asyncio.to_thread(
                engine.evaluate, expr, req.universe,
                req.start_date, req.end_date, 5, 5
            )
            if "error" not in result:
                ic_list.append(abs(result["metrics"]["ic_mean"]))
            else:
                ic_list.append(0)

        total_ic = sum(ic_list)
        weights = [ic / total_ic if total_ic > 0 else 1/len(ic_list) for ic in ic_list]
    else:
        weights = req.weights

    # 构建组合因子表达式
    combined_expr = " + ".join([f"({expr}) * {w}" for expr, w in zip(req.expressions, weights)])

    # 评估组合因子
    result = await asyncio.to_thread(
        engine.evaluate, combined_expr, req.universe,
        req.start_date, req.end_date, 5, 5
    )

    return {
        "combined_expression": combined_expr,
        "weights": weights,
        "metrics": result.get("metrics", {}),
        "ic_series": result.get("ic_series", [])[:50]  # 只返回前50个点
    }


@router.post("/decay")
async def factor_decay(req: DecayRequest, db: AsyncSession = Depends(get_db)):
    """因子衰减分析 - 监控因子IC随时间的变化"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache, make_mysql_conn
    from factor.factor_engine import FactorEngine
    from datetime import datetime, timedelta

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    # 滚动窗口计算IC
    start = datetime.strptime(req.start_date, "%Y%m%d")
    end = datetime.strptime(req.end_date, "%Y%m%d")
    window = timedelta(days=req.window_days)

    decay_series = []
    current = start
    while current + window <= end:
        window_start = current.strftime("%Y%m%d")
        window_end = (current + window).strftime("%Y%m%d")

        result = await asyncio.to_thread(
            engine.evaluate, req.expression, req.universe,
            window_start, window_end, 5, 5
        )

        if "error" not in result:
            decay_series.append({
                "date": window_end,
                "ic_mean": result["metrics"]["ic_mean"],
                "ic_ir": result["metrics"]["ic_ir"]
            })

        current += timedelta(days=30)  # 每月滚动

    return {
        "decay_series": decay_series,
        "trend": "stable" if len(decay_series) > 1 and abs(decay_series[-1]["ic_mean"] - decay_series[0]["ic_mean"]) < 0.02 else "decaying"
    }


@router.post("/neutralize")
async def industry_neutralize(req: CorrelationRequest, db: AsyncSession = Depends(get_db)):
    """行业中性化 - 对因子进行行业中性化处理"""
    # 简化版：返回中性化后的因子表达式
    return {
        "neutralized_expressions": [f"indneutralize({expr})" for expr in req.expressions],
        "message": "已添加行业中性化算子"
    }


@router.post("/attribution")
async def factor_attribution(req: CombineRequest, db: AsyncSession = Depends(get_db)):
    """因子归因分析 - 分解组合因子的收益贡献"""
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

    # 计算每个因子的贡献
    attributions = []
    for i, expr in enumerate(req.expressions):
        result = await asyncio.to_thread(
            engine.evaluate, expr, req.universe,
            req.start_date, req.end_date, 5, 5
        )

        if "error" not in result:
            weight = req.weights[i] if req.weights else 1/len(req.expressions)
            contribution = result["metrics"]["long_short_return"] * weight

            attributions.append({
                "factor_index": i + 1,
                "weight": weight,
                "ic_mean": result["metrics"]["ic_mean"],
                "return": result["metrics"]["long_short_return"],
                "contribution": contribution
            })

    total_contribution = sum(a["contribution"] for a in attributions)

    return {
        "attributions": attributions,
        "total_return": total_contribution,
        "summary": {
            "best_factor": max(attributions, key=lambda x: x["contribution"])["factor_index"] if attributions else None,
            "worst_factor": min(attributions, key=lambda x: x["contribution"])["factor_index"] if attributions else None
        }
    }

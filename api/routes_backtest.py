"""回测运行接口"""
import asyncio
import csv
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import Any, Optional
from database.connection import get_db
from database.models import Settings, Strategy, BacktestResult, StockPool
from data.tushare_client import TushareClient
from data.data_cache import DataCache
from engine.backtest_engine import BacktestEngine
from strategy.strategy_loader import load_strategy
from services.backtest_service import resolve_backtest_universe, run_backtest_workflow
from services.factor_catalog_service import load_factor_catalog
from services.settings_service import get_or_create_settings
from services.factor_board_service import get_system_universes
from services.text_normalizer import normalize_text_payload, normalize_universe_label, repair_text

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/universe_options")
async def get_backtest_universe_options(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StockPool).order_by(StockPool.updated_at.desc(), StockPool.id.desc()))
    custom_pools = []
    for pool in result.scalars().all():
        items = pool.stock_items or []
        custom_pools.append({
            "id": pool.id,
            "name": pool.name,
            "description": pool.description or "",
            "pool_type": pool.pool_type or "custom",
            "index_code": pool.index_code or "",
            "stock_items": items,
            "stock_count": len(items),
        })
    unsupported_universes = [
        {
            "code": "NDX.US",
            "name": "纳斯达克100",
            "supported": False,
            "reason": "当前真实数据源尚未接入美股成分股与日线，暂不能用于真实股票池回测",
        },
        {
            "code": "SPX.US",
            "name": "标普500",
            "supported": False,
            "reason": "当前真实数据源尚未接入美股成分股与日线，暂不能用于真实股票池回测",
        },
    ]
    return {
        "system_universes": get_system_universes(),
        "custom_pools": custom_pools,
        "unsupported_universes": unsupported_universes,
    }


class BacktestRequest(BaseModel):
    strategy_id: int
    universe_type: Optional[str] = None
    universe_code: Optional[str] = None
    universe_name: Optional[str] = None
    custom_pool_id: Optional[int] = None
    stock_items: list[dict[str, Any]] = Field(default_factory=list)
    universe: list[str]  # 股票代码列表
    start_date: str       # YYYYMMDD
    end_date: str
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300.SH"
    max_position_pct: float = 0.25      # 单股最大仓位
    max_drawdown_limit: float = 0.0     # 回撤熔断 (0=不启用)


@router.post("/run")
async def run_backtest(req: BacktestRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await run_backtest_workflow(
            db,
            strategy_id=req.strategy_id,
            universe=req.universe,
            start_date=req.start_date,
            end_date=req.end_date,
            initial_cash=req.initial_cash,
            benchmark=req.benchmark,
            max_position_pct=req.max_position_pct,
            max_drawdown_limit=req.max_drawdown_limit,
            universe_type=req.universe_type,
            universe_code=req.universe_code,
            universe_name=req.universe_name,
            custom_pool_id=req.custom_pool_id,
            stock_items=req.stock_items,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/history")
async def list_results(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BacktestResult).order_by(BacktestResult.created_at.desc()).limit(50)
    )
    results = result.scalars().all()
    return [
        normalize_text_payload({
            "id": r.id,
            "strategy_name": repair_text(r.strategy_name),
            "start_date": r.start_date,
            "end_date": r.end_date,
            "universe": normalize_universe_label(r.universe),
            "initial_cash": r.initial_cash,
            "final_value": r.final_value,
            "metrics": r.metrics,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })
        for r in results
    ]


@router.get("/history/{result_id}")
async def get_result(result_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == result_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="结果不存在")
    return normalize_text_payload({
        "id": r.id,
        "strategy_name": r.strategy_name,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "universe": normalize_universe_label(r.universe),
        "initial_cash": r.initial_cash,
        "final_value": r.final_value,
        "metrics": r.metrics,
        "equity_curve": r.equity_curve,
        "trades": r.trades,
        "daily_returns": r.daily_returns,
        "benchmark_curve": r.benchmark_curve or [],
    })


# ===== 参数优化 =====

class OptimizeRequest(BaseModel):
    strategy_id: int
    universe: list[str]
    universe_type: Optional[str] = None
    universe_code: Optional[str] = None
    universe_name: Optional[str] = None
    custom_pool_id: Optional[int] = None
    stock_items: list[dict[str, Any]] = Field(default_factory=list)
    start_date: str
    end_date: str
    param_grid: dict[str, list[Any]]
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300.SH"


@router.post("/optimize")
async def optimize(req: OptimizeRequest, db: AsyncSession = Depends(get_db)):
    """网格搜索策略参数优化"""
    settings = await get_or_create_settings(db)
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    result = await db.execute(select(Strategy).where(Strategy.id == req.strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    from engine.optimizer import grid_search

    client = TushareClient(settings.tushare_token)
    from data.data_cache import make_mysql_conn
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    factor_catalog = await load_factor_catalog(db)
    resolved_universe = await resolve_backtest_universe(
        db,
        settings,
        universe=req.universe,
        universe_type=req.universe_type,
        universe_code=req.universe_code,
        universe_name=req.universe_name,
        custom_pool_id=req.custom_pool_id,
        stock_items=req.stock_items,
    )

    results = await asyncio.to_thread(
        grid_search,
        strategy_code=strategy.code,
        param_grid=req.param_grid,
        universe=resolved_universe["codes"],
        start_date=req.start_date,
        end_date=req.end_date,
        cache=cache,
        initial_cash=req.initial_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        benchmark=req.benchmark,
        factor_catalog=factor_catalog,
    )

    return {"results": results}


# ===== Walk-Forward滚动验证 =====

class WalkForwardRequest(BaseModel):
    strategy_id: int
    universe: list[str]
    universe_type: Optional[str] = None
    universe_code: Optional[str] = None
    universe_name: Optional[str] = None
    custom_pool_id: Optional[int] = None
    stock_items: list[dict[str, Any]] = Field(default_factory=list)
    start_date: str
    end_date: str
    param_grid: dict[str, list[Any]]
    train_days: int = 252
    test_days: int = 63
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300.SH"


@router.post("/walk_forward")
async def walk_forward_validate(req: WalkForwardRequest, db: AsyncSession = Depends(get_db)):
    """Walk-Forward滚动验证 - 检测策略过拟合"""
    settings = await get_or_create_settings(db)
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    result = await db.execute(select(Strategy).where(Strategy.id == req.strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    from engine.optimizer import walk_forward

    client = TushareClient(settings.tushare_token)
    from data.data_cache import make_mysql_conn
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    factor_catalog = await load_factor_catalog(db)
    resolved_universe = await resolve_backtest_universe(
        db,
        settings,
        universe=req.universe,
        universe_type=req.universe_type,
        universe_code=req.universe_code,
        universe_name=req.universe_name,
        custom_pool_id=req.custom_pool_id,
        stock_items=req.stock_items,
    )

    wf_result = await asyncio.to_thread(
        walk_forward,
        strategy_code=strategy.code,
        param_grid=req.param_grid,
        universe=resolved_universe["codes"],
        full_start=req.start_date,
        full_end=req.end_date,
        cache=cache,
        train_days=req.train_days,
        test_days=req.test_days,
        initial_cash=req.initial_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        benchmark=req.benchmark,
        factor_catalog=factor_catalog,
    )

    if "error" in wf_result:
        raise HTTPException(status_code=400, detail=wf_result["error"])

    return wf_result


# ===== 回测对比 =====

class CompareRequest(BaseModel):
    result_ids: list[int]


@router.post("/compare")
async def compare_results(req: CompareRequest, db: AsyncSession = Depends(get_db)):
    """对比多个回测结果"""
    if len(req.result_ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择2个结果进行对比")

    items = []
    for rid in req.result_ids:
        result = await db.execute(
            select(BacktestResult).where(BacktestResult.id == rid)
        )
        r = result.scalar_one_or_none()
        if not r:
            raise HTTPException(status_code=404, detail=f"结果 {rid} 不存在")
        items.append(r)

    # 构建对齐的净值曲线和指标对比
    curves = []
    metrics_list = []
    for r in items:
        eq = r.equity_curve or []
        if eq and r.initial_cash and r.initial_cash > 0:
            normalized = [
                {"date": p["date"], "value": round(p["value"] / r.initial_cash, 6)}
                for p in eq
            ]
        else:
            normalized = eq
        curves.append({
            "id": r.id,
            "strategy_name": repair_text(r.strategy_name),
            "equity_curve": normalized,
        })
        metrics_list.append({
            "id": r.id,
            "strategy_name": repair_text(r.strategy_name),
            "metrics": r.metrics,
            "final_value": r.final_value,
            "start_date": r.start_date,
            "end_date": r.end_date,
        })

    return normalize_text_payload({"curves": curves, "metrics": metrics_list})


# ===== CSV导出 =====

@router.get("/export/{result_id}")
async def export_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """导出回测结果为CSV"""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == result_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="结果不存在")

    output = io.StringIO()
    # 写入绩效指标
    output.write("# 绩效指标\n")
    metrics = r.metrics or {}
    for k, v in metrics.items():
        output.write(f"{k},{v}\n")
    output.write("\n")

    # 写入净值曲线
    output.write("# 净值曲线\n")
    writer = csv.DictWriter(output, fieldnames=["date", "value", "cash"])
    writer.writeheader()
    for row in (r.equity_curve or []):
        writer.writerow(row)
    output.write("\n")

    # 写入交易记录
    output.write("# 交易记录\n")
    if r.trades:
        trade_fields = ["date", "ts_code", "side", "amount", "price", "commission", "tax"]
        writer2 = csv.DictWriter(output, fieldnames=trade_fields)
        writer2.writeheader()
        for t in r.trades:
            writer2.writerow({k: t.get(k, "") for k in trade_fields})

    output.seek(0)
    filename = f"backtest_{result_id}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ===== 风险分析 =====

class RiskAnalysisRequest(BaseModel):
    result_id: int
    drawdown_threshold: float = 0.02


@router.post("/risk_analysis")
async def risk_analysis(req: RiskAnalysisRequest, db: AsyncSession = Depends(get_db)):
    """风险分析 - 回撤分解、风险度量、月度收益"""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == req.result_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="结果不存在")

    from engine.risk import analyze_drawdowns, compute_risk_metrics, compute_monthly_returns

    data = await asyncio.to_thread(
        _compute_risk, r.equity_curve, r.daily_returns, req.drawdown_threshold
    )
    return data


def _compute_risk(equity_curve, daily_returns, threshold):
    from engine.risk import analyze_drawdowns, compute_risk_metrics, compute_monthly_returns
    dd = analyze_drawdowns(equity_curve, threshold)
    risk = compute_risk_metrics(daily_returns)
    monthly = compute_monthly_returns(equity_curve)
    return {"drawdowns": dd, "risk_metrics": risk, "monthly_returns": monthly}


# ===== 归因分析 =====

class AttributionRequest(BaseModel):
    result_id: int


@router.post("/attribution")
async def compute_attribution(req: AttributionRequest, db: AsyncSession = Depends(get_db)):
    """归因分析 - 个股盈亏、行业归因、月度归因"""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == req.result_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="结果不存在")

    # 获取行业映射
    settings_result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_result.scalar_one_or_none()
    industry_map = {}
    if settings and settings.tushare_token:
        try:
            client = TushareClient(settings.tushare_token)
            df = await asyncio.to_thread(client.get_stock_basic)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    industry_map[row.get("ts_code", "")] = row.get("industry", "未知")
        except Exception:
            pass

    from engine.attribution import compute_stock_pnl, compute_sector_attribution, compute_monthly_attribution

    stock_pnl = await asyncio.to_thread(
        compute_stock_pnl, r.trades or [], r.initial_cash
    )
    sector_attr = await asyncio.to_thread(
        compute_sector_attribution, r.trades or [], industry_map,
        None, (r.metrics or {}).get("total_return", 0), r.initial_cash
    )
    monthly_attr = await asyncio.to_thread(
        compute_monthly_attribution, r.equity_curve or [], r.benchmark_curve or []
    )

    return {
        "stock_pnl": stock_pnl,
        "sector_attribution": sector_attr,
        "monthly_attribution": monthly_attr,
    }


# ===== 组合分析 =====

class PortfolioAnalysisRequest(BaseModel):
    result_ids: list[int]
    method: str = "max_sharpe"


@router.post("/portfolio_analysis")
async def portfolio_analysis(req: PortfolioAnalysisRequest, db: AsyncSession = Depends(get_db)):
    """组合分析 - 相关性、最优权重、组合回测"""
    if len(req.result_ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择2个结果")
    if len(req.result_ids) > 10:
        raise HTTPException(status_code=400, detail="最多选择10个结果")

    items = []
    for rid in req.result_ids:
        result = await db.execute(
            select(BacktestResult).where(BacktestResult.id == rid)
        )
        r = result.scalar_one_or_none()
        if not r:
            raise HTTPException(status_code=404, detail=f"结果 {rid} 不存在")
        items.append({
            "id": r.id,
            "strategy_name": repair_text(r.strategy_name),
            "daily_returns": r.daily_returns or [],
            "equity_curve": r.equity_curve or [],
            "initial_cash": r.initial_cash,
        })

    from engine.portfolio import analyze_correlations, compute_optimal_weights, simulate_portfolio

    corr = await asyncio.to_thread(analyze_correlations, items)
    weights_result = await asyncio.to_thread(
        compute_optimal_weights, items, req.method
    )
    weight_values = [w["weight"] for w in weights_result["weights"]]
    initial_cash = items[0]["initial_cash"] if items else 1_000_000.0
    portfolio = await asyncio.to_thread(
        simulate_portfolio, items, weight_values, initial_cash
    )

    return {
        "correlations": normalize_text_payload(corr),
        "weights": normalize_text_payload(weights_result),
        "portfolio": normalize_text_payload(portfolio),
    }

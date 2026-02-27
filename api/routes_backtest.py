"""回测运行接口"""
import asyncio
import csv
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Any
from database.connection import get_db
from database.models import Settings, Strategy, BacktestResult
from data.tushare_client import TushareClient
from data.data_cache import DataCache
from engine.backtest_engine import BacktestEngine
from strategy.strategy_loader import load_strategy

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    strategy_id: int
    universe: list[str]  # 股票代码列表
    start_date: str       # YYYYMMDD
    end_date: str
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300.SH"
    max_position_pct: float = 0.25      # 单股最大仓位
    max_drawdown_limit: float = 0.0     # 回撤熔断 (0=不启用)


@router.post("/run")
async def run_backtest(req: BacktestRequest, db: AsyncSession = Depends(get_db)):
    # 获取设置
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    # 获取策略
    result = await db.execute(select(Strategy).where(Strategy.id == req.strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    # 加载策略代码
    try:
        init_func, handle_func = load_strategy(strategy.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 创建回测引擎
    client = TushareClient(settings.tushare_token)
    cache = DataCache(client)
    engine = BacktestEngine(
        cache=cache,
        initial_cash=req.initial_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        max_position_pct=req.max_position_pct,
        max_drawdown_limit=req.max_drawdown_limit,
    )
    engine.universe = req.universe

    # 运行回测(在线程中执行避免阻塞事件循环)
    result_data = await asyncio.to_thread(
        engine.run,
        universe=req.universe,
        start_date=req.start_date,
        end_date=req.end_date,
        initialize_func=init_func,
        handle_data_func=handle_func,
        benchmark=req.benchmark,
    )

    if "error" in result_data:
        raise HTTPException(status_code=400, detail=result_data["error"])

    # 保存结果
    bt_result = BacktestResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        start_date=req.start_date,
        end_date=req.end_date,
        universe=",".join(req.universe),
        initial_cash=req.initial_cash,
        final_value=result_data["final_value"],
        metrics=result_data["metrics"],
        equity_curve=result_data["equity_curve"],
        trades=result_data["trades"],
        daily_returns=result_data["daily_returns"],
        benchmark_curve=result_data.get("benchmark_curve", []),
    )
    db.add(bt_result)
    await db.commit()
    await db.refresh(bt_result)

    return {
        "id": bt_result.id,
        "metrics": result_data["metrics"],
        "equity_curve": result_data["equity_curve"],
        "trades": result_data["trades"],
        "logs": result_data.get("logs", []),
        "final_value": result_data["final_value"],
        "benchmark_curve": result_data.get("benchmark_curve", []),
    }


@router.get("/history")
async def list_results(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BacktestResult).order_by(BacktestResult.created_at.desc()).limit(50)
    )
    results = result.scalars().all()
    return [
        {
            "id": r.id,
            "strategy_name": r.strategy_name,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "universe": r.universe,
            "initial_cash": r.initial_cash,
            "final_value": r.final_value,
            "metrics": r.metrics,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
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
    return {
        "id": r.id,
        "strategy_name": r.strategy_name,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "universe": r.universe,
        "initial_cash": r.initial_cash,
        "final_value": r.final_value,
        "metrics": r.metrics,
        "equity_curve": r.equity_curve,
        "trades": r.trades,
        "daily_returns": r.daily_returns,
        "benchmark_curve": r.benchmark_curve or [],
    }


# ===== 参数优化 =====

class OptimizeRequest(BaseModel):
    strategy_id: int
    universe: list[str]
    start_date: str
    end_date: str
    param_grid: dict[str, list[Any]]
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300.SH"


@router.post("/optimize")
async def optimize(req: OptimizeRequest, db: AsyncSession = Depends(get_db)):
    """网格搜索策略参数优化"""
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    result = await db.execute(select(Strategy).where(Strategy.id == req.strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    from engine.optimizer import grid_search

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client)

    results = await asyncio.to_thread(
        grid_search,
        strategy_code=strategy.code,
        param_grid=req.param_grid,
        universe=req.universe,
        start_date=req.start_date,
        end_date=req.end_date,
        cache=cache,
        initial_cash=req.initial_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        benchmark=req.benchmark,
    )

    return {"results": results}


# ===== Walk-Forward滚动验证 =====

class WalkForwardRequest(BaseModel):
    strategy_id: int
    universe: list[str]
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
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    result = await db.execute(select(Strategy).where(Strategy.id == req.strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    from engine.optimizer import walk_forward

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client)

    wf_result = await asyncio.to_thread(
        walk_forward,
        strategy_code=strategy.code,
        param_grid=req.param_grid,
        universe=req.universe,
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
            "strategy_name": r.strategy_name,
            "equity_curve": normalized,
        })
        metrics_list.append({
            "id": r.id,
            "strategy_name": r.strategy_name,
            "metrics": r.metrics,
            "final_value": r.final_value,
            "start_date": r.start_date,
            "end_date": r.end_date,
        })

    return {"curves": curves, "metrics": metrics_list}


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
            "strategy_name": r.strategy_name,
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
        "correlations": corr,
        "weights": weights_result,
        "portfolio": portfolio,
    }

"""回测服务。"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Strategy, BacktestResult
from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from engine.backtest_engine import BacktestEngine
from services.settings_service import get_or_create_settings
from strategy.strategy_loader import load_strategy


async def run_backtest_workflow(
    db: AsyncSession,
    *,
    strategy_id: int,
    universe: list[str],
    start_date: str,
    end_date: str,
    initial_cash: float,
    benchmark: str,
    max_position_pct: float,
    max_drawdown_limit: float,
):
    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise LookupError("策略不存在")

    init_func, handle_func = load_strategy(strategy.code)

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = BacktestEngine(
        cache=cache,
        initial_cash=initial_cash,
        commission_rate=settings.commission_rate,
        stamp_tax_rate=settings.stamp_tax_rate,
        slippage=settings.slippage,
        max_position_pct=max_position_pct,
        max_drawdown_limit=max_drawdown_limit,
    )
    engine.universe = universe

    result_data = await asyncio.to_thread(
        engine.run,
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        initialize_func=init_func,
        handle_data_func=handle_func,
        benchmark=benchmark,
    )
    if "error" in result_data:
        raise ValueError(result_data["error"])

    bt_result = BacktestResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        start_date=start_date,
        end_date=end_date,
        universe=",".join(universe),
        initial_cash=initial_cash,
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


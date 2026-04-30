"""因子服务。"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from database.models import Factor, FactorResult, Strategy
from factor.factor_engine import FactorEngine
from factor.genetic import run_gp
from services.backtest_service import resolve_backtest_universe
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


def _compact_date(value: str) -> str:
    return str(value or "").replace("-", "").strip()[:8]


def _lookback_start(end_date: str, days: int = 420) -> str:
    try:
        dt = datetime.strptime(_compact_date(end_date), "%Y%m%d")
    except Exception:
        dt = datetime.now()
    return (dt - timedelta(days=days)).strftime("%Y%m%d")


def build_factor_strategy_code(factor_id: int, factor_name: str, direction: str = "top") -> str:
    safe_name = str(factor_name or f"Factor {factor_id}").replace('"""', "")
    reverse = "True" if direction != "bottom" else "False"
    return f'''def initialize(context):
    """基于因子库因子自动生成的选股策略：{safe_name}"""
    context.factor_ref = "id:{factor_id}"
    context.max_positions = 10
    context.rebalance_days = 5
    context.factor_direction_high_is_better = {reverse}

def handle_data(context):
    if not hasattr(context, "day_count"):
        context.day_count = 0
    context.day_count += 1
    if context.day_count % context.rebalance_days != 1:
        return

    scores = []
    for ts_code in context.universe:
        value = context.get_factor(context.factor_ref, ts_code, default=None)
        if value is None:
            continue
        try:
            value = float(value)
        except Exception:
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            scores.append((ts_code, value))

    if not scores:
        return

    scores.sort(key=lambda item: item[1], reverse=context.factor_direction_high_is_better)
    selected = [code for code, _ in scores[:max(1, int(context.max_positions))]]
    target_weight = 1.0 / max(len(selected), 1)

    for ts_code in list(context.positions.keys()):
        if ts_code not in selected:
            context.order_target_percent(ts_code, 0)

    for ts_code in selected:
        context.order_target_percent(ts_code, target_weight)
'''


async def create_strategy_from_factor_workflow(
    db: AsyncSession,
    *,
    factor_id: int,
    direction: str = "top",
) -> dict:
    factor_result = await db.execute(select(Factor).where(Factor.id == factor_id))
    factor = factor_result.scalar_one_or_none()
    if not factor:
        raise LookupError("因子不存在")

    name = f"因子策略_{factor.id}_{factor.name}"[:100]
    code = build_factor_strategy_code(factor.id, factor.name, direction=direction)
    description = f"由因子库自动生成，使用因子 {factor.name} ({factor.expression}) 进行截面排序选股。"
    existing_result = await db.execute(select(Strategy).where(Strategy.name == name))
    strategy = existing_result.scalar_one_or_none()
    if strategy:
        strategy.description = description
        strategy.code = code
        strategy.updated_at = datetime.utcnow()
    else:
        strategy = Strategy(
            name=name,
            description=description,
            code=code,
        )
        db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return {
        "id": strategy.id,
        "name": strategy.name,
        "factor_id": factor.id,
        "factor_name": factor.name,
    }


async def rank_factor_cross_section_workflow(
    db: AsyncSession,
    *,
    factor_id: int,
    trade_date: str,
    universe_type: str,
    universe_code: str,
    custom_pool_id: int | None,
    n: int,
) -> dict:
    factor_result = await db.execute(select(Factor).where(Factor.id == factor_id))
    factor = factor_result.scalar_one_or_none()
    if not factor:
        raise LookupError("因子不存在")

    settings = await get_or_create_settings(db)
    if not settings.tushare_token:
        raise ValueError("请先配置 Tushare Token")

    resolved = await resolve_backtest_universe(
        db,
        settings,
        universe_type=universe_type or "system",
        universe_code=universe_code or "000300.SH",
        custom_pool_id=custom_pool_id,
    )
    codes = list(resolved.get("codes") or [])[:800]
    if not codes:
        raise ValueError("股票池为空")

    date_text = _compact_date(trade_date) or datetime.now().strftime("%Y%m%d")
    start_date = _lookback_start(date_text)
    n = max(1, min(int(n or 10), 100))

    client = TushareClient(settings.tushare_token)
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    def _compute():
        name_map = {}
        try:
            stock_basic = client.get_stock_basic()
            if stock_basic is not None and not stock_basic.empty:
                for row in stock_basic.to_dict("records"):
                    name_map[str(row.get("ts_code") or "").upper()] = str(row.get("name") or "")
        except Exception:
            name_map = {}

        rows = []
        for code in codes:
            try:
                series = engine.compute_factor_values(factor.expression, code, start_date, date_text)
            except Exception:
                continue
            if series is None or series.dropna().empty:
                continue
            valid = series.dropna()
            valid = valid[valid.index <= datetime.strptime(date_text, "%Y%m%d")]
            if valid.empty:
                continue
            value = float(valid.iloc[-1])
            if value != value:
                continue
            rows.append({
                "ts_code": code,
                "name": name_map.get(str(code).upper(), ""),
                "factor_value": round(value, 8),
                "factor_date": valid.index[-1].strftime("%Y%m%d") if hasattr(valid.index[-1], "strftime") else str(valid.index[-1]),
            })
        rows.sort(key=lambda item: item["factor_value"], reverse=True)
        return rows

    rows = await asyncio.to_thread(_compute)
    return {
        "factor": {
            "id": factor.id,
            "name": factor.name,
            "expression": factor.expression,
        },
        "trade_date": date_text,
        "universe": resolved,
        "total_count": len(rows),
        "top": rows[:n],
        "bottom": list(reversed(rows[-n:])) if rows else [],
    }

"""策略管理与 AI 策略助手接口。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from database.models import Strategy
from services.strategy_ai_service import STRATEGY_AI_SKILLS, generate_strategy_with_ai
from strategy.builtin.bollinger_breakout import BOLLINGER_BREAKOUT_CODE
from strategy.builtin.ma_cross import MA_CROSS_CODE
from strategy.builtin.macd_signal import MACD_SIGNAL_CODE
from strategy.builtin.mean_reversion import MEAN_REVERSION_CODE
from strategy.builtin.momentum import MOMENTUM_CODE
from strategy.builtin.multi_factor import MULTI_FACTOR_CODE
from strategy.builtin.rsi_reversion import RSI_REVERSION_CODE
from strategy.builtin.turtle_trading import TURTLE_TRADING_CODE
from strategy.builtin.volume_price import VOLUME_PRICE_CODE


router = APIRouter(prefix="/api/strategies", tags=["strategies"])

BUILTIN_STRATEGIES = [
    {
        "name": "MA 双均线",
        "description": "经典双均线趋势策略：短期均线上穿长期均线买入，下穿卖出。",
        "code": MA_CROSS_CODE,
    },
    {
        "name": "RSI 超买超卖",
        "description": "RSI 反转策略：RSI<30 视为超卖买入，RSI>70 视为超买卖出。",
        "code": RSI_REVERSION_CODE,
    },
    {
        "name": "MACD 信号",
        "description": "MACD 金叉买入、死叉卖出，适用于中短期趋势跟随。",
        "code": MACD_SIGNAL_CODE,
    },
    {
        "name": "布林带突破",
        "description": "价格向上突破上轨时买入，跌破中轨或回撤时退出。",
        "code": BOLLINGER_BREAKOUT_CODE,
    },
    {
        "name": "动量策略",
        "description": "基于过去 N 日收益率判断动量方向，延续强势上涨信号。",
        "code": MOMENTUM_CODE,
    },
    {
        "name": "海龟交易",
        "description": "结合唐奇安通道和 ATR 风险控制的经典趋势策略。",
        "code": TURTLE_TRADING_CODE,
    },
    {
        "name": "量价共振",
        "description": "放量突破均线时买入，缩量跌破关键位置时卖出。",
        "code": VOLUME_PRICE_CODE,
    },
    {
        "name": "多因子评分",
        "description": "融合动量、波动率和成交量变化的综合评分策略。",
        "code": MULTI_FACTOR_CODE,
    },
    {
        "name": "均值回归",
        "description": "利用价格对均值的偏离程度，在极端状态下做反转交易。",
        "code": MEAN_REVERSION_CODE,
    },
]


class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    code: str


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None


class StrategyChatMessage(BaseModel):
    role: str = Field(default="user")
    content: str


class CurrentStrategyPayload(BaseModel):
    name: str = ""
    description: str = ""
    code: str = ""


class StrategyAIGenerateRequest(BaseModel):
    messages: list[StrategyChatMessage] = Field(default_factory=list)
    current_strategy: Optional[CurrentStrategyPayload] = None
    include_market_context: bool = True
    save: bool = False


def _strategy_to_dict(strategy: Strategy) -> dict[str, Any]:
    return {
        "id": strategy.id,
        "name": strategy.name,
        "description": strategy.description or "",
        "code": strategy.code,
        "created_at": strategy.created_at.isoformat() if strategy.created_at else "",
        "updated_at": strategy.updated_at.isoformat() if strategy.updated_at else "",
    }


@router.get("/ai/skills")
async def get_strategy_ai_skills():
    return {"skills": STRATEGY_AI_SKILLS}


@router.post("/ai/generate")
async def generate_strategy_ai(
    req: StrategyAIGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await generate_strategy_with_ai(
            db,
            messages=[message.model_dump() for message in req.messages],
            current_strategy=req.current_strategy.model_dump() if req.current_strategy else None,
            include_market_context=req.include_market_context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI 策略生成失败：{exc}") from exc

    saved_strategy = None
    if req.save:
        strategy_payload = result["strategy"]
        strategy = Strategy(
            name=strategy_payload["name"],
            description=strategy_payload["description"],
            code=strategy_payload["code"],
        )
        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)
        saved_strategy = {"id": strategy.id, "name": strategy.name}

    return {
        **result,
        "saved_strategy": saved_strategy,
        "message": "AI 策略生成成功",
    }


@router.get("")
async def list_strategies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).order_by(Strategy.updated_at.desc()))
    return [_strategy_to_dict(item) for item in result.scalars().all()]


@router.get("/{strategy_id}")
async def get_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")
    return _strategy_to_dict(strategy)


@router.post("")
async def create_strategy(data: StrategyCreate, db: AsyncSession = Depends(get_db)):
    strategy = Strategy(name=data.name, description=data.description, code=data.code)
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return {"id": strategy.id, "name": strategy.name}


@router.put("/{strategy_id}")
async def update_strategy(
    strategy_id: int,
    data: StrategyUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(strategy, field, value)
    await db.commit()
    await db.refresh(strategy)
    return {"id": strategy.id, "name": strategy.name}


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="策略不存在")

    await db.delete(strategy)
    await db.commit()
    return {"message": "已删除"}


@router.post("/init_builtin")
async def init_builtin(db: AsyncSession = Depends(get_db)):
    created = 0
    skipped = 0
    for item in BUILTIN_STRATEGIES:
        exists = await db.execute(select(Strategy).where(Strategy.name == item["name"]))
        if exists.scalar_one_or_none():
            skipped += 1
            continue
        db.add(
            Strategy(
                name=item["name"],
                description=item["description"],
                code=item["code"],
            )
        )
        created += 1

    await db.commit()
    return {
        "message": f"已创建 {created} 个内置策略，跳过 {skipped} 个已存在策略",
        "created": created,
        "skipped": skipped,
    }

"""策略CRUD接口"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database.models import Strategy
from strategy.builtin.ma_cross import MA_CROSS_CODE
from strategy.builtin.rsi_reversion import RSI_REVERSION_CODE
from strategy.builtin.macd_signal import MACD_SIGNAL_CODE
from strategy.builtin.bollinger_breakout import BOLLINGER_BREAKOUT_CODE
from strategy.builtin.momentum import MOMENTUM_CODE
from strategy.builtin.turtle_trading import TURTLE_TRADING_CODE
from strategy.builtin.volume_price import VOLUME_PRICE_CODE
from strategy.builtin.multi_factor import MULTI_FACTOR_CODE
from strategy.builtin.mean_reversion import MEAN_REVERSION_CODE

BUILTIN_STRATEGIES = [
    {"name": "MA双均线交叉", "description": "经典双均线交叉策略: 短期均线上穿长期均线买入, 下穿卖出", "code": MA_CROSS_CODE},
    {"name": "RSI超买超卖", "description": "RSI指标策略: RSI<30超卖买入, RSI>70超买卖出", "code": RSI_REVERSION_CODE},
    {"name": "MACD信号", "description": "MACD指标策略: MACD金叉买入, 死叉卖出", "code": MACD_SIGNAL_CODE},
    {"name": "布林带突破", "description": "布林带策略: 突破上轨买入, 跌破中轨卖出", "code": BOLLINGER_BREAKOUT_CODE},
    {"name": "动量策略", "description": "动量策略: N日收益率为正买入, 为负卖出", "code": MOMENTUM_CODE},
    {"name": "海龟交易", "description": "海龟交易策略: 唐奇安通道突破+ATR仓位管理", "code": TURTLE_TRADING_CODE},
    {"name": "量价策略", "description": "量价策略: 放量突破均线买入, 缩量跌破卖出", "code": VOLUME_PRICE_CODE},
    {"name": "多因子", "description": "多因子策略: 动量+波动率+成交量综合评分", "code": MULTI_FACTOR_CODE},
    {"name": "均值回归", "description": "均值回归策略: Z-score偏离均值反向交易", "code": MEAN_REVERSION_CODE},
]

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    code: str


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None


class StrategyResponse(BaseModel):
    id: int
    name: str
    description: str
    code: str
    created_at: str
    updated_at: str


@router.get("")
async def list_strategies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).order_by(Strategy.updated_at.desc()))
    strategies = result.scalars().all()
    return [
        {
            "id": s.id, "name": s.name, "description": s.description,
            "code": s.code,
            "created_at": s.created_at.isoformat() if s.created_at else "",
            "updated_at": s.updated_at.isoformat() if s.updated_at else "",
        }
        for s in strategies
    ]


@router.get("/{strategy_id}")
async def get_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "code": s.code,
        "created_at": s.created_at.isoformat() if s.created_at else "",
        "updated_at": s.updated_at.isoformat() if s.updated_at else "",
    }


@router.post("")
async def create_strategy(data: StrategyCreate, db: AsyncSession = Depends(get_db)):
    s = Strategy(name=data.name, description=data.description, code=data.code)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return {"id": s.id, "name": s.name}


@router.put("/{strategy_id}")
async def update_strategy(strategy_id: int, data: StrategyUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="策略不存在")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    await db.commit()
    await db.refresh(s)
    return {"id": s.id, "name": s.name}


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="策略不存在")
    await db.delete(s)
    await db.commit()
    return {"message": "已删除"}


@router.post("/init_builtin")
async def init_builtin(db: AsyncSession = Depends(get_db)):
    """初始化所有内置策略"""
    created = 0
    skipped = 0
    for item in BUILTIN_STRATEGIES:
        result = await db.execute(select(Strategy).where(Strategy.name == item["name"]))
        if result.scalar_one_or_none():
            skipped += 1
            continue
        s = Strategy(name=item["name"], description=item["description"], code=item["code"])
        db.add(s)
        created += 1
    await db.commit()
    return {"message": f"已创建{created}个内置策略, 跳过{skipped}个已存在策略", "created": created, "skipped": skipped}

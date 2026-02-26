"""回测上下文对象 - Portfolio, Position, Order, Context"""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass
class Position:
    ts_code: str
    amount: int = 0           # 总持仓
    available: int = 0        # 可卖数量(T+1)
    avg_cost: float = 0.0     # 平均成本
    last_price: float = 0.0   # 最新价格

    @property
    def market_value(self) -> float:
        return self.amount * self.last_price

    @property
    def profit(self) -> float:
        if self.amount == 0:
            return 0.0
        return (self.last_price - self.avg_cost) * self.amount

    @property
    def profit_pct(self) -> float:
        if self.avg_cost == 0:
            return 0.0
        return (self.last_price - self.avg_cost) / self.avg_cost


@dataclass
class Order:
    ts_code: str
    side: OrderSide
    amount: int
    price: Optional[float] = None  # None表示市价单
    status: OrderStatus = OrderStatus.PENDING
    filled_price: float = 0.0
    filled_amount: int = 0
    commission: float = 0.0
    tax: float = 0.0
    created_date: str = ""
    filled_date: str = ""
    reason: str = ""

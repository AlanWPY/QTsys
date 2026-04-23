"""回测上下文对象 - Portfolio, Position, Order, HistoryWindow."""
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

import numpy as np
import pandas as pd


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
    amount: int = 0
    available: int = 0
    avg_cost: float = 0.0
    last_price: float = 0.0

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

    def __bool__(self) -> bool:
        return self.amount > 0

    def __int__(self) -> int:
        return int(self.amount)

    def __float__(self) -> float:
        return float(self.amount)

    def __neg__(self) -> int:
        return -self.amount

    def __pos__(self) -> int:
        return self.amount

    def __mul__(self, other):
        return self.amount * other

    def __rmul__(self, other):
        return other * self.amount

    def __truediv__(self, other):
        return self.amount / other

    def __eq__(self, other) -> bool:
        if isinstance(other, Position):
            return self.amount == other.amount
        return self.amount == other

    def __lt__(self, other) -> bool:
        return self.amount < (other.amount if isinstance(other, Position) else other)

    def __le__(self, other) -> bool:
        return self.amount <= (other.amount if isinstance(other, Position) else other)

    def __gt__(self, other) -> bool:
        return self.amount > (other.amount if isinstance(other, Position) else other)

    def __ge__(self, other) -> bool:
        return self.amount >= (other.amount if isinstance(other, Position) else other)

    def __getitem__(self, key: str):
        mapping = {
            "amount": self.amount,
            "available": self.available,
            "avg_cost": self.avg_cost,
            "cost": self.avg_cost,
            "last_price": self.last_price,
            "price": self.last_price,
            "market_value": self.market_value,
            "value": self.market_value,
            "profit": self.profit,
            "profit_pct": self.profit_pct,
        }
        if key not in mapping:
            raise KeyError(key)
        return mapping[key]


@dataclass
class Order:
    ts_code: str
    side: OrderSide
    amount: int
    price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_price: float = 0.0
    filled_amount: int = 0
    commission: float = 0.0
    tax: float = 0.0
    created_date: str = ""
    filled_date: str = ""
    reason: str = ""


class _HistoryILoc:
    def __init__(self, parent: "HistoryWindow"):
        self.parent = parent

    def __getitem__(self, item):
        result = self.parent.to_series().iloc[item]
        if isinstance(result, pd.Series):
            return HistoryWindow(result.tolist(), name=self.parent.name)
        return result


class HistoryWindow:
    def __init__(self, values: Optional[Iterable] = None, name: str = ""):
        self._values = list(values or [])
        self.name = name

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __bool__(self) -> bool:
        return len(self._values) > 0

    def __getitem__(self, item):
        result = self._values[item]
        if isinstance(item, slice):
            return HistoryWindow(result, name=self.name)
        return result

    def __array__(self, dtype=None):
        return np.asarray(self._values, dtype=dtype)

    def __repr__(self) -> str:
        return f"HistoryWindow({self._values!r})"

    @property
    def empty(self) -> bool:
        return not self._values

    @property
    def values(self):
        return np.asarray(self._values)

    @property
    def iloc(self):
        return _HistoryILoc(self)

    def tolist(self) -> list:
        return list(self._values)

    def to_series(self) -> pd.Series:
        return pd.Series(self._values, dtype=float if self._values else None, name=self.name)

    def tail(self, n: int) -> "HistoryWindow":
        return HistoryWindow(self._values[-n:], name=self.name)

    def head(self, n: int) -> "HistoryWindow":
        return HistoryWindow(self._values[:n], name=self.name)

    def mean(self) -> float:
        return float(np.mean(self._values)) if self._values else 0.0

    def std(self, ddof: int = 1) -> float:
        if len(self._values) <= ddof:
            return 0.0
        return float(np.std(self._values, ddof=ddof))

    def min(self):
        return min(self._values) if self._values else None

    def max(self):
        return max(self._values) if self._values else None

    def sum(self):
        return sum(self._values)

    def pct_change(self) -> "HistoryWindow":
        series = self.to_series().pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        return HistoryWindow(series.tolist(), name=self.name)

    def __getattr__(self, item):
        attr = getattr(self.to_series(), item)
        if callable(attr):
            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                if isinstance(result, pd.Series):
                    return HistoryWindow(result.tolist(), name=self.name)
                return result
            return wrapper
        return attr

"""模拟券商 - 订单执行、滑点、手续费、涨跌停"""
import math
from typing import Optional
from engine.context import Order, OrderSide, OrderStatus, Position


class Broker:
    def __init__(
        self,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.001,
        slippage: float = 0.002,
        volume_limit: float = 0.25,
        min_commission: float = 5.0,
    ):
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.slippage = slippage
        self.volume_limit = volume_limit
        self.min_commission = min_commission

    def check_limit(self, pre_close: float, ts_code: str = "", name: str = "") -> tuple[float, float]:
        """计算涨跌停价格 - 根据股票类型区分涨跌停幅度
        创业板(300xxx)/科创板(688xxx): 20%
        ST股: 5%
        其他: 10%
        """
        code = ts_code.split(".")[0] if ts_code else ""
        is_st = "ST" in name.upper() if name else False

        if is_st:
            pct = 0.05
        elif code.startswith("300") or code.startswith("688"):
            pct = 0.20
        else:
            pct = 0.10

        limit_up = round(pre_close * (1 + pct), 2)
        limit_down = round(pre_close * (1 - pct), 2)
        return limit_up, limit_down

    def apply_slippage(self, price: float, side: OrderSide) -> float:
        """应用滑点"""
        if side == OrderSide.BUY:
            return round(price * (1 + self.slippage), 2)
        else:
            return round(price * (1 - self.slippage), 2)

    def calc_commission(self, amount: int, price: float) -> float:
        """计算佣金"""
        commission = amount * price * self.commission_rate
        return max(commission, self.min_commission)

    def calc_tax(self, amount: int, price: float, side: OrderSide) -> float:
        """计算印花税(仅卖出)"""
        if side == OrderSide.SELL:
            return amount * price * self.stamp_tax_rate
        return 0.0

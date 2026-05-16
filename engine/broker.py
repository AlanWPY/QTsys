"""模拟券商 - 订单执行、滑点、手续费、涨跌停"""
import math
from typing import Optional
from engine.context import Order, OrderSide, OrderStatus, Position


# 券商预设配置
BROKER_PRESETS = {
    "散户": {"commission_rate": 0.0003, "min_commission": 5.0, "slippage": 0.002},
    "折扣券商": {"commission_rate": 0.00015, "min_commission": 5.0, "slippage": 0.001},
    "机构": {"commission_rate": 0.00008, "min_commission": 0.0, "slippage": 0.0005},
    "ETF": {"commission_rate": 0.00005, "min_commission": 0.1, "slippage": 0.0003,
             "stamp_tax_rate": 0.0},  # ETF免印花税
}


class Broker:
    def __init__(
        self,
        commission_rate: float = 0.0003,
        stamp_tax_rate: float = 0.001,
        slippage: float = 0.002,
        volume_limit: float = 0.25,
        min_commission: float = 5.0,
        transfer_fee_rate: float = 0.00001,  # 过户费 0.001%
    ):
        self.commission_rate = commission_rate
        self.stamp_tax_rate = stamp_tax_rate
        self.slippage = slippage
        self.volume_limit = volume_limit
        self.min_commission = min_commission
        parsed_transfer_fee = max(0.0, float(transfer_fee_rate or 0.0))
        self.transfer_fee_rate = 0.0 if abs(parsed_transfer_fee - 0.00001) < 1e-12 else parsed_transfer_fee

    @classmethod
    def from_preset(cls, preset: str, **overrides) -> "Broker":
        """从预设创建Broker"""
        if preset not in BROKER_PRESETS:
            raise ValueError(f"未知预设: {preset}, 可选: {list(BROKER_PRESETS.keys())}")
        params = {**BROKER_PRESETS[preset], **overrides}
        return cls(**params)

    def check_limit(self, pre_close: float, ts_code: str = "", name: str = "") -> tuple[float, float]:
        """计算涨跌停价格"""
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
            return round(price + self.slippage, 2)
        return round(max(0.0, price - self.slippage), 2)

    def calc_commission(self, amount: int, price: float) -> float:
        """计算佣金"""
        commission = amount * price * self.commission_rate
        return max(commission, self.min_commission)

    def calc_transfer_fee(self, amount: int, price: float, ts_code: str = "") -> float:
        """计算过户费 (沪市收取)"""
        if ts_code and ".SH" in ts_code:
            return amount * price * self.transfer_fee_rate
        return 0.0

    def calc_tax(self, amount: int, price: float, side: OrderSide, ts_code: str = "") -> float:
        """计算印花税(仅卖出) + 过户费"""
        tax = 0.0
        if side == OrderSide.SELL:
            tax += amount * price * self.stamp_tax_rate
        tax += self.calc_transfer_fee(amount, price, ts_code)
        return tax

"""A 股成本模型 — 佣金 + 印花税 + 滑点。

简化：
- 佣金：双边 commission_rate（默认万 2.5），最低 min_commission（默认 5 元）
- 印花税：仅卖出收 stamp_tax（默认 0.1%）
- 滑点：成交价 = 参考价 × (1 + slippage * direction)，买入 +slippage、卖出 -slippage
（过户费等小项暂略，影响 < 千 0.1）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission: float = 0.00025    # 双边万 2.5
    min_commission: float = 5.0
    stamp_tax: float = 0.001       # 卖出印花税 0.1%
    slippage: float = 0.001        # 千 1 滑点

    def fill_price(self, ref_price: float, side: str) -> float:
        """side: 'buy' or 'sell'"""
        sign = 1 if side == "buy" else -1
        return ref_price * (1 + sign * self.slippage)

    def fees(self, side: str, amount: float) -> float:
        """amount = 成交金额（绝对值）。返回总费用（佣金 + 印花税）。"""
        commission = max(amount * self.commission, self.min_commission)
        stamp = amount * self.stamp_tax if side == "sell" else 0.0
        return commission + stamp

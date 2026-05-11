"""验证成本模型：佣金、最低佣金、印花税、滑点。"""

from __future__ import annotations

from alphaforge.runtime.costs import CostModel


def test_default_buy_no_stamp_tax():
    cm = CostModel()
    fees = cm.fees(side="buy", amount=100_000)
    # 佣金 25 元，无印花税
    assert abs(fees - 25.0) < 1e-6


def test_default_sell_has_stamp_tax():
    cm = CostModel()
    fees = cm.fees(side="sell", amount=100_000)
    # 佣金 25 + 印花税 100 = 125
    assert abs(fees - 125.0) < 1e-6


def test_min_commission():
    cm = CostModel()
    fees = cm.fees(side="buy", amount=1_000)  # 0.25 < 5
    assert fees == 5.0


def test_slippage_buy_higher():
    cm = CostModel(slippage=0.001)
    assert cm.fill_price(10.0, "buy") > 10.0
    assert abs(cm.fill_price(10.0, "buy") - 10.01) < 1e-9


def test_slippage_sell_lower():
    cm = CostModel(slippage=0.001)
    assert cm.fill_price(10.0, "sell") < 10.0
    assert abs(cm.fill_price(10.0, "sell") - 9.99) < 1e-9

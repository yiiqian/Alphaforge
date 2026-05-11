"""验证 A 股交易约束：T+1 / 涨跌停 / 停牌。"""

from __future__ import annotations

from alphaforge.runtime.constraints import T1Lock, can_buy, can_sell


def test_can_buy_blocked_by_limit_up(mini_bundle):
    data, _, _, days, _ = mini_bundle
    d4 = days[3]
    ok, reason = can_buy(data, d4, "A.SZ")  # A 涨停
    assert not ok
    assert reason == "limit_up"


def test_can_buy_blocked_by_suspension(mini_bundle):
    data, _, _, days, _ = mini_bundle
    d4 = days[3]
    ok, reason = can_buy(data, d4, "C.SZ")  # C 停牌
    assert not ok
    assert reason == "suspended"


def test_can_buy_normal(mini_bundle):
    data, _, _, days, _ = mini_bundle
    ok, reason = can_buy(data, days[0], "A.SZ")
    assert ok and reason == ""


def test_can_sell_blocked_by_limit_down(mini_bundle):
    data, _, _, days, _ = mini_bundle
    d4 = days[3]
    ok, reason, sellable = can_sell(data, d4, "B.SZ", holding_qty=1000, locked_qty=0)
    assert not ok
    assert reason == "limit_down"
    assert sellable == 0


def test_can_sell_blocked_by_suspension(mini_bundle):
    data, _, _, days, _ = mini_bundle
    d4 = days[3]
    ok, reason, _ = can_sell(data, d4, "C.SZ", holding_qty=1000, locked_qty=0)
    assert not ok
    assert reason == "suspended"


def test_can_sell_t1_full_lock(mini_bundle):
    """全部数量都被 T+1 锁定时，不可卖。"""
    data, _, _, days, _ = mini_bundle
    ok, reason, sellable = can_sell(data, days[0], "A.SZ", holding_qty=1000, locked_qty=1000)
    assert not ok
    assert reason == "t1_locked"
    assert sellable == 0


def test_can_sell_partial_lock(mini_bundle):
    """部分锁定 — 应允许卖出未锁部分。"""
    data, _, _, days, _ = mini_bundle
    ok, reason, sellable = can_sell(data, days[0], "A.SZ", holding_qty=1000, locked_qty=400)
    assert ok and reason == ""
    assert sellable == 600


def test_t1_lock_ledger():
    """T1Lock 簿记：当日加入 → 同日仍锁；越日后查询当日 → 0。"""
    import pandas as pd

    lock = T1Lock()
    d1 = pd.Timestamp("2024-01-02")
    d2 = pd.Timestamp("2024-01-03")
    lock.add("A.SZ", d1, 500)

    # d1 当日 = 仍锁定
    assert lock.locked_qty("A.SZ", d1) == 500
    # d2 查 d1 锁定 = 没有 d2 当天买入的锁定
    assert lock.locked_qty("A.SZ", d2) == 0


def test_universe_excludes_suspended(mini_bundle):
    _, universe, _, days, _ = mini_bundle
    d4 = days[3]
    pool = universe.tradable(d4)
    assert "C.SZ" not in pool
    assert "A.SZ" in pool
    assert "B.SZ" in pool

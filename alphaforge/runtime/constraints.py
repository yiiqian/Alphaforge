"""A 股交易约束 — T+1 / 涨跌停 / 停牌。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from alphaforge.data.api import DataAPI


@dataclass
class T1Lock:
    """T+1 锁定簿：当日买入次日才可卖。

    内部维护 {ts_code: [(buy_date, qty), ...]}，每天 close 时调用 unlock(date) 解冻
    昨日及更早买入的部分。
    """

    locks: dict[str, list[tuple[pd.Timestamp, int]]] = field(default_factory=dict)

    def add(self, ts_code: str, date: pd.Timestamp, qty: int) -> None:
        if qty <= 0:
            return
        self.locks.setdefault(ts_code, []).append((date, qty))

    def locked_qty(self, ts_code: str, date: pd.Timestamp) -> int:
        """date 当天仍被锁定（即买入日 == date）的数量。"""
        rows = self.locks.get(ts_code, [])
        return sum(qty for d, qty in rows if d == date)

    def cleanup(self, before: pd.Timestamp) -> None:
        """清理 before 之前的记录，控制内存。"""
        for code in list(self.locks):
            self.locks[code] = [(d, q) for d, q in self.locks[code] if d >= before]
            if not self.locks[code]:
                del self.locks[code]


def can_buy(data: DataAPI, date: pd.Timestamp, ts_code: str) -> tuple[bool, str]:
    """是否可在 date 买入 ts_code。返回 (allowed, reason_if_not)。"""
    if data.is_suspended(date, ts_code):
        return False, "suspended"
    if data.is_limit_up(date, ts_code):
        return False, "limit_up"
    return True, ""


def can_sell(
    data: DataAPI,
    date: pd.Timestamp,
    ts_code: str,
    holding_qty: int,
    locked_qty: int,
) -> tuple[bool, str, int]:
    """是否可在 date 卖出 ts_code。返回 (allowed, reason, sellable_qty)。"""
    sellable = max(holding_qty - locked_qty, 0)
    if sellable <= 0:
        return False, "t1_locked", 0
    if data.is_suspended(date, ts_code):
        return False, "suspended", 0
    if data.is_limit_down(date, ts_code):
        return False, "limit_down", 0
    return True, "", sellable

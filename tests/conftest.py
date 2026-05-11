"""Pytest 通用 fixture — 构造一个迷你"可控"的合成数据，方便断言 A 股约束。"""

from __future__ import annotations

import pandas as pd
import pytest

from alphaforge.data.api import _SynthCalendar, _SynthData, _SynthUniverse


@pytest.fixture
def mini_bundle():
    """3 只股票 × 10 个交易日的迷你数据集。

    精心构造：
      - 第 4 天（idx=3）：A 涨停（pct +10%）；B 跌停（-10%）；C 停牌
      - 第 5 天（idx=4）：所有股票正常
    用于直接验证 can_buy / can_sell / 回测撮合行为。
    """
    days = pd.bdate_range("2024-01-02", periods=10)
    codes = ["A.SZ", "B.SZ", "C.SZ"]

    base = pd.DataFrame(10.0, index=days, columns=codes)
    pct_chg = pd.DataFrame(0.0, index=days, columns=codes)
    limit_status = pd.DataFrame(0, index=days, columns=codes)
    is_suspended = pd.DataFrame(False, index=days, columns=codes)

    # 第 4 天的特殊情况
    pct_chg.iloc[3] = [0.10, -0.10, 0.0]
    limit_status.iloc[3] = [1, -1, 0]
    is_suspended.iloc[3, 2] = True  # C 当日停牌

    # 用 close 反推一条线性价格序列
    close = base.copy()
    pre_close = base.shift(1).fillna(base)

    panels = {
        "open":  base,
        "high":  base * 1.01,
        "low":   base * 0.99,
        "close": close,
        "vol":   pd.DataFrame(1e6, index=days, columns=codes),
        "pre_close":    pre_close,
        "pct_chg":      pct_chg,
        "limit_status": limit_status,
        "is_suspended": is_suspended,
    }

    data = _SynthData(panels)
    universe = _SynthUniverse(codes, data, list_dates={c: days[0] for c in codes})
    cal = _SynthCalendar(days)
    return data, universe, cal, days, codes

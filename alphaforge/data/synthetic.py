"""合成 A 股行情生成器 — 用于 demo / 单测，让回测引擎在 M1 数据层落地前就能端到端跑。

特点：
- 内置中国 A 股交易日历（周末 + 主要节假日近似）
- 支持随机停牌 / 偶发涨跌停
- 价格走几何布朗运动，相关性可调
- 月初指数成分调整
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alphaforge.data.api import (
    DataAPI,
    UniverseAPI,
    _SynthCalendar,
    _SynthData,
    _SynthUniverse,
)

CHINESE_HOLIDAYS_2022_2025 = {
    # 简化版：只列出元旦/春节/清明/劳动/端午/国庆等主要长假近似
    # 真实回测请用 Tushare trade_cal
    "2022-01-03", "2022-01-31", "2022-02-01", "2022-02-02", "2022-02-03",
    "2022-02-04", "2022-04-04", "2022-04-05", "2022-05-02", "2022-05-03",
    "2022-05-04", "2022-06-03", "2022-09-12", "2022-10-03", "2022-10-04",
    "2022-10-05", "2022-10-06", "2022-10-07",
    "2023-01-02", "2023-01-23", "2023-01-24", "2023-01-25", "2023-01-26",
    "2023-01-27", "2023-04-05", "2023-05-01", "2023-05-02", "2023-05-03",
    "2023-06-22", "2023-06-23", "2023-09-29", "2023-10-02", "2023-10-03",
    "2023-10-04", "2023-10-05", "2023-10-06",
    "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14",
    "2024-02-15", "2024-02-16", "2024-04-04", "2024-04-05", "2024-05-01",
    "2024-05-02", "2024-05-03", "2024-06-10", "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04", "2025-04-04", "2025-05-01", "2025-05-02",
    "2025-05-05", "2025-06-02", "2025-10-01", "2025-10-02", "2025-10-03",
    "2025-10-06", "2025-10-07", "2025-10-08",
}


def _trade_days(start: str, end: str) -> pd.DatetimeIndex:
    bdays = pd.bdate_range(start, end)
    holidays = pd.to_datetime(list(CHINESE_HOLIDAYS_2022_2025))
    return bdays.difference(holidays)


def make_synthetic_bundle(
    start: str = "2022-01-01",
    end: str = "2024-12-31",
    n_stocks: int = 50,
    seed: int = 42,
) -> tuple[DataAPI, UniverseAPI, _SynthCalendar]:
    """生成一份完整的合成 A 股行情包。

    Returns:
        (data_api, universe_api, calendar_api)
    """
    rng = np.random.default_rng(seed)
    days = _trade_days(start, end)
    codes = [f"{i+1:06d}.SZ" if i % 2 == 0 else f"{i+600001:06d}.SH" for i in range(n_stocks)]

    n = len(days)

    # 几何布朗运动：年化漂移 8%, 年化波动 30%
    mu = 0.08 / 252
    sigma = 0.30 / np.sqrt(252)
    log_returns = rng.normal(mu, sigma, size=(n, n_stocks))

    # 加点市场公共因子，让股票有一定相关性
    market = rng.normal(mu, sigma * 0.6, size=n)
    log_returns = log_returns * 0.7 + market[:, None] * 0.3

    log_prices = np.cumsum(log_returns, axis=0) + np.log(rng.uniform(5, 50, n_stocks))
    close = np.exp(log_prices)

    # OHLV 由 close 派生
    open_ = close * (1 + rng.normal(0, 0.005, size=close.shape))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, size=close.shape)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, size=close.shape)))
    vol = rng.uniform(1e6, 1e8, size=close.shape)
    pre_close = np.vstack([close[:1], close[:-1]])

    pct_chg = (close - pre_close) / pre_close
    # 涨跌停：±10%（主板模拟，简化处理）
    limit_up = pct_chg > 0.099
    limit_down = pct_chg < -0.099
    limit_status = np.where(limit_up, 1, np.where(limit_down, -1, 0))

    # 随机停牌：每只股票每年约 2 个交易日
    suspend_prob = 2 / 252
    is_suspended = rng.random(close.shape) < suspend_prob

    panels = {
        "open":  pd.DataFrame(open_, index=days, columns=codes),
        "high":  pd.DataFrame(high,  index=days, columns=codes),
        "low":   pd.DataFrame(low,   index=days, columns=codes),
        "close": pd.DataFrame(close, index=days, columns=codes),
        "vol":   pd.DataFrame(vol,   index=days, columns=codes),
        "pre_close":    pd.DataFrame(pre_close, index=days, columns=codes),
        "pct_chg":      pd.DataFrame(pct_chg, index=days, columns=codes),
        "limit_status": pd.DataFrame(limit_status, index=days, columns=codes),
        "is_suspended": pd.DataFrame(is_suspended, index=days, columns=codes),
    }

    data_api = _SynthData(panels)
    list_dates = {c: days[0] for c in codes}
    universe_api = _SynthUniverse(codes, data_api, list_dates=list_dates)
    cal_api = _SynthCalendar(days)
    return data_api, universe_api, cal_api

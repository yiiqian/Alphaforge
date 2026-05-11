"""M1 数据层单测 —— 用伪造 client 写出 parquet，再用 ParquetBundle 读回来验证。

不依赖真实 Tushare 网络。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from alphaforge.data.parquet_store import (
    ParquetCalendar,
    ParquetData,
    ParquetUniverse,
    load_tushare_bundle,
)
from alphaforge.data.updater import (
    DataLakePaths,
    update_daily_panel,
    update_indices,
    update_stock_basic,
    update_suspend,
    update_trade_cal,
)


class _FakeClient:
    """模拟 Tushare 客户端 —— 返回固定的 3 只股票 × 5 个交易日数据。"""

    DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"])
    CODES = ["000001.SZ", "600000.SH", "300001.SZ"]

    def trade_cal(self, start, end, exchange="SSE"):
        all_days = pd.date_range("2024-01-01", "2024-01-15")
        is_open = [(d in self.DATES) for d in all_days]
        return pd.DataFrame({
            "exchange": exchange,
            "cal_date": all_days,
            "is_open": [int(x) for x in is_open],
            "pretrade_date": [pd.NaT] * len(all_days),
        })

    def stock_basic(self, list_status="ALL"):
        return pd.DataFrame({
            "ts_code": self.CODES,
            "symbol": ["000001", "600000", "300001"],
            "name": ["平安银行", "浦发银行", "特锐德"],
            "area": ["深圳", "上海", "青岛"],
            "industry": ["银行", "银行", "电气设备"],
            "market": ["主板", "主板", "创业板"],
            "list_date": pd.to_datetime(["2010-01-01", "1999-11-10", "2009-10-30"]),
            "delist_date": [pd.NaT, pd.NaT, pd.NaT],
            "list_status": "L",
        })

    def daily(self, trade_date=None, **kwargs):
        d = pd.to_datetime(trade_date)
        if d not in self.DATES:
            return pd.DataFrame()
        rows = []
        for i, code in enumerate(self.CODES):
            base = 10.0 + i
            rows.append({
                "ts_code": code,
                "trade_date": d,
                "open": base, "high": base * 1.02, "low": base * 0.98,
                "close": base * 1.01, "pre_close": base,
                "change": base * 0.01, "pct_chg": 1.0,
                "vol": 1e6, "amount": 1e7,
            })
        return pd.DataFrame(rows)

    def adj_factor(self, trade_date=None, **kwargs):
        d = pd.to_datetime(trade_date)
        if d not in self.DATES:
            return pd.DataFrame()
        return pd.DataFrame({
            "ts_code": self.CODES,
            "trade_date": [d] * len(self.CODES),
            "adj_factor": [1.0] * len(self.CODES),
        })

    def stk_limit(self, trade_date=None, **kwargs):
        d = pd.to_datetime(trade_date)
        if d not in self.DATES:
            return pd.DataFrame()
        return pd.DataFrame({
            "ts_code": self.CODES,
            "trade_date": [d] * len(self.CODES),
            "up_limit": [11.0, 12.1, 13.2],
            "down_limit": [9.0, 9.9, 10.8],
        })

    def suspend_d(self, ts_code=None, start=None, end=None, suspend_type="S"):
        # 模拟 600000.SH 在 2024-01-04 停牌
        return pd.DataFrame({
            "ts_code": ["600000.SH"],
            "trade_date": pd.to_datetime(["2024-01-04"]),
            "suspend_timing": [None],
            "suspend_type": ["S"],
        })

    def index_daily(self, ts_code, start=None, end=None):
        return pd.DataFrame({
            "ts_code": [ts_code] * len(self.DATES),
            "trade_date": list(self.DATES),
            "close": [3000 + i * 10 for i in range(len(self.DATES))],
            "open": [2990 + i * 10 for i in range(len(self.DATES))],
            "high": [3010 + i * 10 for i in range(len(self.DATES))],
            "low": [2980 + i * 10 for i in range(len(self.DATES))],
            "vol": [1e8] * len(self.DATES),
        })


@pytest.fixture
def populated_lake(tmp_path: Path):
    """跑一遍 updater，把伪造数据写进 tmp parquet 数据湖。"""
    paths = DataLakePaths(tmp_path)
    paths.ensure_dirs()
    client = _FakeClient()

    cal = update_trade_cal(client, paths, start="2024-01-01", end="2024-01-15")
    update_stock_basic(client, paths)
    for panel in ("daily", "adj_factor", "stk_limit"):
        update_daily_panel(client, paths, cal, start="2024-01-01", end="2024-01-15", panel=panel)
    update_suspend(client, paths, start="2024-01-01", end="2024-01-15")
    update_indices(client, paths, start="2024-01-01", end="2024-01-15", codes=["000300.SH"])
    return paths


def test_calendar_reads_back(populated_lake):
    cal = ParquetCalendar(populated_lake)
    days = cal.all_trade_days()
    assert len(days) == 5
    assert cal.is_trade_day(pd.Timestamp("2024-01-02"))
    assert not cal.is_trade_day(pd.Timestamp("2024-01-06"))  # 周末


def test_data_bars_qfq(populated_lake):
    data = ParquetData(populated_lake)
    df = data.bars(start=pd.Timestamp("2024-01-01"), end=pd.Timestamp("2024-01-15"))
    assert len(df) == 5 * 3
    assert set(df["ts_code"].unique()) == {"000001.SZ", "600000.SH", "300001.SZ"}
    assert "close" in df.columns


def test_daily_table_has_limit_status(populated_lake):
    data = ParquetData(populated_lake)
    table = data.daily_table(pd.Timestamp("2024-01-02"))
    assert not table.empty
    assert "limit_status" in table.columns
    assert "is_suspended" in table.columns


def test_suspend_detected(populated_lake):
    data = ParquetData(populated_lake)
    assert data.is_suspended(pd.Timestamp("2024-01-04"), "600000.SH")
    assert not data.is_suspended(pd.Timestamp("2024-01-04"), "000001.SZ")


def test_universe_filters_suspend(populated_lake):
    data = ParquetData(populated_lake)
    universe = ParquetUniverse(populated_lake, data)
    pool = universe.tradable(pd.Timestamp("2024-01-04"))
    assert "600000.SH" not in pool          # 当日停牌
    assert "000001.SZ" in pool


def test_load_tushare_bundle_factory(populated_lake):
    data, universe, calendar = load_tushare_bundle(
        start=pd.Timestamp("2024-01-01"),
        end=pd.Timestamp("2024-01-15"),
        root=populated_lake.root,
    )
    assert calendar.all_trade_days().shape[0] == 5
    assert len(universe.all_stocks()) == 3


def test_incremental_cursor(populated_lake, tmp_path):
    """重复跑一遍 update_daily_panel，应基于游标跳过已写入的天。"""
    client = _FakeClient()
    cal = ParquetCalendar(populated_lake).all_trade_days()
    cal_df = pd.DataFrame({"cal_date": cal, "is_open": 1})
    n = update_daily_panel(
        client, populated_lake, cal_df,
        start="2024-01-01", end="2024-01-15", panel="daily",
    )
    assert n == 0  # 没有新增的天

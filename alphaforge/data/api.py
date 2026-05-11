"""数据访问 API 层 — 策略与回测引擎只依赖这些接口，不直接读 parquet。

实现可被替换：
- SyntheticBundle:   合成数据（demo / 单测）
- ParquetBundle:     真实 Tushare 落地的 Parquet（M1 上线后启用）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence

import pandas as pd


class CalendarAPI(ABC):
    """交易日历。"""

    @abstractmethod
    def all_trade_days(self) -> pd.DatetimeIndex: ...

    @abstractmethod
    def is_trade_day(self, date: pd.Timestamp) -> bool: ...

    def trade_days(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
        idx = self.all_trade_days()
        mask = (idx >= start) & (idx <= end)
        return idx[mask]

    def prev(self, date: pd.Timestamp, n: int = 1) -> pd.Timestamp:
        idx = self.all_trade_days()
        loc = idx.searchsorted(date, side="right") - 1
        loc -= n - 1
        if loc < 0:
            raise IndexError(f"No trade day {n} steps before {date}")
        return idx[loc]

    def next(self, date: pd.Timestamp, n: int = 1) -> pd.Timestamp:
        idx = self.all_trade_days()
        loc = idx.searchsorted(date, side="left")
        if loc < len(idx) and idx[loc] == date:
            loc += n
        else:
            loc += n - 1
        if loc >= len(idx):
            raise IndexError(f"No trade day {n} steps after {date}")
        return idx[loc]


class DataAPI(ABC):
    """行情数据访问 — 全部 PIT 安全（不会泄露 date 之后的数据）。"""

    @abstractmethod
    def bars(
        self,
        codes: Sequence[str] | None = None,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        adj: str = "qfq",
        fields: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """返回长表：[trade_date, ts_code, open, high, low, close, vol, amount, ...]"""

    @abstractmethod
    def daily_table(self, date: pd.Timestamp, adj: str = "qfq") -> pd.DataFrame:
        """单日横截面：index=ts_code, 列含 close/open/high/low/vol/amount/limit_status/is_suspended"""

    @abstractmethod
    def is_limit_up(self, date: pd.Timestamp, ts_code: str) -> bool: ...

    @abstractmethod
    def is_limit_down(self, date: pd.Timestamp, ts_code: str) -> bool: ...

    @abstractmethod
    def is_suspended(self, date: pd.Timestamp, ts_code: str) -> bool: ...


class UniverseAPI(ABC):
    """选股池服务。"""

    @abstractmethod
    def all_stocks(self) -> list[str]: ...

    @abstractmethod
    def tradable(self, date: pd.Timestamp) -> list[str]:
        """date 当日可交易的股票（剔除停牌 / ST / 次新 / 退市）。"""


class FactorAPI(ABC):
    """因子库读取 — M2 之后实现。当前留接口占位。"""

    @abstractmethod
    def get(
        self,
        name: str,
        date: pd.Timestamp,
        codes: Iterable[str] | None = None,
        mode: str = "zscore",
    ) -> pd.Series: ...


# ----- 合成实现：用于 demo 与单测 -----


class _SynthCalendar(CalendarAPI):
    def __init__(self, days: pd.DatetimeIndex) -> None:
        self._days = days

    def all_trade_days(self) -> pd.DatetimeIndex:
        return self._days

    def is_trade_day(self, date: pd.Timestamp) -> bool:
        return date in self._days


class _SynthData(DataAPI):
    """合成数据 DataAPI — 接收一个 pivoted 字典 { 'close': DF[date,code], 'open': ..., 'is_suspended': ..., 'limit_status': ... }"""

    def __init__(self, panels: dict[str, pd.DataFrame]) -> None:
        self.panels = panels

    def bars(
        self,
        codes: Sequence[str] | None = None,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        adj: str = "qfq",
        fields: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        out = []
        for fld in (fields or ["open", "high", "low", "close", "vol"]):
            if fld not in self.panels:
                continue
            df = self.panels[fld].copy()
            if start is not None:
                df = df.loc[df.index >= start]
            if end is not None:
                df = df.loc[df.index <= end]
            if codes is not None:
                df = df.loc[:, [c for c in codes if c in df.columns]]
            stk = df.stack().rename(fld).reset_index()
            stk.columns = ["trade_date", "ts_code", fld]
            out.append(stk.set_index(["trade_date", "ts_code"]))
        if not out:
            return pd.DataFrame()
        merged = pd.concat(out, axis=1).reset_index()
        return merged.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def daily_table(self, date: pd.Timestamp, adj: str = "qfq") -> pd.DataFrame:
        out = {}
        for k, df in self.panels.items():
            if date in df.index:
                out[k] = df.loc[date]
        return pd.DataFrame(out)

    def is_limit_up(self, date: pd.Timestamp, ts_code: str) -> bool:
        ls = self.panels.get("limit_status")
        if ls is None or date not in ls.index or ts_code not in ls.columns:
            return False
        return ls.loc[date, ts_code] == 1

    def is_limit_down(self, date: pd.Timestamp, ts_code: str) -> bool:
        ls = self.panels.get("limit_status")
        if ls is None or date not in ls.index or ts_code not in ls.columns:
            return False
        return ls.loc[date, ts_code] == -1

    def is_suspended(self, date: pd.Timestamp, ts_code: str) -> bool:
        sp = self.panels.get("is_suspended")
        if sp is None or date not in sp.index or ts_code not in sp.columns:
            return False
        return bool(sp.loc[date, ts_code])


class _SynthUniverse(UniverseAPI):
    def __init__(self, all_stocks: list[str], data: DataAPI, list_dates: dict[str, pd.Timestamp] | None = None) -> None:
        self._stocks = all_stocks
        self._data = data
        self._list_dates = list_dates or {}

    def all_stocks(self) -> list[str]:
        return list(self._stocks)

    def tradable(self, date: pd.Timestamp) -> list[str]:
        out = []
        for code in self._stocks:
            if self._data.is_suspended(date, code):
                continue
            ld = self._list_dates.get(code)
            if ld is not None and (date - ld).days < 60:  # 剔除次新（上市 < 60 天）
                continue
            out.append(code)
        return out

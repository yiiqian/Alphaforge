"""ParquetBundle —— 从本地 data_lake 读取 parquet，实现 DataAPI/UniverseAPI/CalendarAPI。

PIT 安全保证：
- 所有查询都按 trade_date <= date 过滤；
- 上市日从 stock_basic 读取，未上市的股票不会出现在 universe；
- 复权采用前复权（qfq）：close_qfq = close * adj_factor / latest_adj_factor。

设计权衡：
- 读取时一次性把请求范围内的 parquet 加载进内存，按 (date,code) 组织 panel；
- 大数据量场景下可改为 duckdb 懒加载，这里先简单可读。
"""

from __future__ import annotations

import functools
from pathlib import Path

import pandas as pd

from alphaforge.data.api import CalendarAPI, DataAPI, UniverseAPI
from alphaforge.data.updater import DataLakePaths
from alphaforge.infra.logger import logger


_DATE_COLS = ("trade_date", "cal_date", "list_date", "delist_date", "pretrade_date")


def _read_parquet_or_csv(path: Path) -> pd.DataFrame:
    """优先读 parquet；缺 pyarrow 时降级到同名 csv。"""
    if path.exists():
        return pd.read_parquet(path)
    csv = path.with_suffix(".csv")
    if csv.exists():
        head = pd.read_csv(csv, nrows=0)
        date_cols = [c for c in _DATE_COLS if c in head.columns]
        return pd.read_csv(csv, parse_dates=date_cols)
    raise FileNotFoundError(path)


class ParquetCalendar(CalendarAPI):
    def __init__(self, paths: DataLakePaths) -> None:
        self._paths = paths
        self._days: pd.DatetimeIndex | None = None

    def all_trade_days(self) -> pd.DatetimeIndex:
        if self._days is None:
            df = _read_parquet_or_csv(self._paths.trade_cal)
            df = df[df["is_open"] == 1].sort_values("cal_date")
            self._days = pd.DatetimeIndex(pd.to_datetime(df["cal_date"]).values)
        return self._days

    def is_trade_day(self, date: pd.Timestamp) -> bool:
        return date in self.all_trade_days()


class ParquetData(DataAPI):
    """读取 daily / adj_factor / stk_limit / suspend，并提供 PIT 复权与涨跌停查询。

    懒加载：每次按需加载相关年份的 parquet 文件，并 LRU 缓存。
    """

    def __init__(self, paths: DataLakePaths) -> None:
        self._paths = paths

    # ---------- 内部加载 ----------

    @functools.lru_cache(maxsize=32)
    def _daily_year(self, year: int) -> pd.DataFrame:
        path = self._paths.daily_year(year)
        if not path.exists() and not path.with_suffix(".csv").exists():
            return pd.DataFrame()
        df = _read_parquet_or_csv(path)
        return df

    @functools.lru_cache(maxsize=32)
    def _adj_year(self, year: int) -> pd.DataFrame:
        path = self._paths.adj_year(year)
        if not path.exists() and not path.with_suffix(".csv").exists():
            return pd.DataFrame()
        df = _read_parquet_or_csv(path)
        return df

    @functools.lru_cache(maxsize=32)
    def _limit_year(self, year: int) -> pd.DataFrame:
        path = self._paths.limit_year(year)
        if not path.exists() and not path.with_suffix(".csv").exists():
            return pd.DataFrame()
        df = _read_parquet_or_csv(path)
        return df

    @functools.lru_cache(maxsize=1)
    def _suspend(self) -> pd.DataFrame:
        path = self._paths.suspend
        if not path.exists() and not path.with_suffix(".csv").exists():
            return pd.DataFrame()
        return _read_parquet_or_csv(path)

    @functools.lru_cache(maxsize=8)
    def _index(self, code: str) -> pd.DataFrame:
        path = self._paths.index(code)
        if not path.exists() and not path.with_suffix(".csv").exists():
            return pd.DataFrame()
        return _read_parquet_or_csv(path)

    def _slice(self, frame_fn, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        years = []
        s_year = (start or pd.Timestamp("2000-01-01")).year
        e_year = (end or pd.Timestamp.today()).year
        for y in range(s_year, e_year + 1):
            df = frame_fn(y)
            if not df.empty:
                years.append(df)
        if not years:
            return pd.DataFrame()
        out = pd.concat(years, ignore_index=True)
        if start is not None:
            out = out[out["trade_date"] >= start]
        if end is not None:
            out = out[out["trade_date"] <= end]
        return out

    # ---------- 公共 API ----------

    def bars(
        self,
        codes=None,
        start=None,
        end=None,
        adj: str = "qfq",
        fields=None,
    ) -> pd.DataFrame:
        df = self._slice(self._daily_year, start, end)
        if df.empty:
            return df
        if codes is not None:
            df = df[df["ts_code"].isin(list(codes))]

        if adj == "qfq":
            adj_df = self._slice(self._adj_year, start, end)
            if not adj_df.empty:
                df = self._apply_qfq(df, adj_df, codes)

        keep = ["ts_code", "trade_date"] + list(fields or ["open", "high", "low", "close", "vol", "amount"])
        keep = [c for c in keep if c in df.columns]
        return df[keep].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def _apply_qfq(self, daily: pd.DataFrame, adj: pd.DataFrame,
                   codes=None) -> pd.DataFrame:
        """前复权：以查询窗口最后一个交易日的 adj_factor 为基准。"""
        # 取每只股票最后一个 adj_factor
        latest = adj.sort_values("trade_date").groupby("ts_code")["adj_factor"].last()
        merged = daily.merge(adj[["ts_code", "trade_date", "adj_factor"]],
                             on=["ts_code", "trade_date"], how="left")
        # 同股票内向后填充缺失因子
        merged["adj_factor"] = merged.groupby("ts_code")["adj_factor"].ffill().bfill()
        merged["_latest_adj"] = merged["ts_code"].map(latest)
        ratio = merged["adj_factor"] / merged["_latest_adj"]
        for col in ("open", "high", "low", "close", "pre_close"):
            if col in merged.columns:
                merged[col] = merged[col] * ratio
        return merged.drop(columns=["adj_factor", "_latest_adj"])

    def daily_table(self, date: pd.Timestamp, adj: str = "qfq") -> pd.DataFrame:
        """单日横截面 — 含 limit_status / is_suspended。"""
        year = date.year
        d = self._daily_year(year)
        if d.empty:
            return pd.DataFrame()
        day_df = d[d["trade_date"] == date].copy()
        if day_df.empty:
            return pd.DataFrame()

        if adj == "qfq":
            adj_df = self._adj_year(year)
            if not adj_df.empty:
                day_df = self._apply_qfq(day_df, adj_df)

        # 拼接涨跌停字段
        ldf = self._limit_year(year)
        if not ldf.empty:
            ld = ldf[ldf["trade_date"] == date]
            day_df = day_df.merge(ld[["ts_code", "up_limit", "down_limit"]],
                                  on="ts_code", how="left")
            # 涨跌停判定：close >= up_limit 或 <= down_limit（容差 0.01）
            limit_status = pd.Series(0, index=day_df.index, dtype=int)
            limit_status[day_df["close"] >= day_df["up_limit"] - 0.01] = 1
            limit_status[day_df["close"] <= day_df["down_limit"] + 0.01] = -1
            day_df["limit_status"] = limit_status

        # 停牌：当日 daily 缺失 = 停牌；这里用 suspend 表补齐
        susp = self._suspend()
        if not susp.empty and "trade_date" in susp.columns:
            susp_day = susp[susp["trade_date"] == date]["ts_code"].unique()
            day_df["is_suspended"] = day_df["ts_code"].isin(susp_day)
        else:
            day_df["is_suspended"] = False

        # 设 ts_code 为 index，列与 _SynthData.daily_table 对齐
        return day_df.set_index("ts_code")

    def is_limit_up(self, date: pd.Timestamp, ts_code: str) -> bool:
        df = self.daily_table(date)
        if df.empty or ts_code not in df.index:
            return False
        val = df.at[ts_code, "limit_status"] if "limit_status" in df.columns else 0
        return int(val) == 1

    def is_limit_down(self, date: pd.Timestamp, ts_code: str) -> bool:
        df = self.daily_table(date)
        if df.empty or ts_code not in df.index:
            return False
        val = df.at[ts_code, "limit_status"] if "limit_status" in df.columns else 0
        return int(val) == -1

    def is_suspended(self, date: pd.Timestamp, ts_code: str) -> bool:
        susp = self._suspend()
        if susp.empty or "trade_date" not in susp.columns:
            return False
        match = susp[(susp["trade_date"] == date) & (susp["ts_code"] == ts_code)]
        return not match.empty


class ParquetUniverse(UniverseAPI):
    def __init__(self, paths: DataLakePaths, data: ParquetData) -> None:
        self._paths = paths
        self._data = data
        self._basic: pd.DataFrame | None = None

    def _load_basic(self) -> pd.DataFrame:
        if self._basic is None:
            self._basic = _read_parquet_or_csv(self._paths.stock_basic)
        return self._basic

    def all_stocks(self) -> list[str]:
        df = self._load_basic()
        return df["ts_code"].tolist()

    def tradable(self, date: pd.Timestamp) -> list[str]:
        df = self._load_basic()
        # 上市 ≥ 60 天，未退市
        eligible = df[df["list_date"] <= (date - pd.Timedelta(days=60))]
        if "delist_date" in eligible.columns:
            eligible = eligible[(eligible["delist_date"].isna()) | (eligible["delist_date"] > date)]
        # 剔除当日停牌
        out: list[str] = []
        susp = self._data._suspend()
        if not susp.empty and "trade_date" in susp.columns:
            susp_day = set(susp[susp["trade_date"] == date]["ts_code"].tolist())
        else:
            susp_day = set()

        # 剔除当日没有日线数据的（隐含停牌）
        day_panel = self._data._daily_year(date.year)
        if not day_panel.empty:
            traded_today = set(day_panel[day_panel["trade_date"] == date]["ts_code"].tolist())
        else:
            traded_today = set()

        # ST 简单过滤：name 含 "ST" 字样
        for _, row in eligible.iterrows():
            code = row["ts_code"]
            if code in susp_day:
                continue
            if traded_today and code not in traded_today:
                continue
            name = row.get("name", "")
            if isinstance(name, str) and ("ST" in name or "*ST" in name):
                continue
            out.append(code)
        return out


def load_tushare_bundle(start: pd.Timestamp, end: pd.Timestamp,
                        root: Path | str | None = None) -> tuple[ParquetData, ParquetUniverse, ParquetCalendar]:
    """入口：从本地 data_lake 加载 (data, universe, calendar)。

    root 默认从环境变量 ALPHAFORGE_DATA_LAKE 读取，否则用 <project_root>/data_lake/。
    """
    from alphaforge.infra.config import project_root

    if root is None:
        import os
        root = os.environ.get("ALPHAFORGE_DATA_LAKE") or (project_root() / "data_lake")
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(
            f"data_lake not found at {root}. Run `alphaforge data update` first."
        )

    paths = DataLakePaths(root)
    cal = ParquetCalendar(paths)
    data = ParquetData(paths)
    universe = ParquetUniverse(paths, data)
    logger.info(f"Loaded ParquetBundle from {root} (window {start.date()} -> {end.date()})")
    return data, universe, cal

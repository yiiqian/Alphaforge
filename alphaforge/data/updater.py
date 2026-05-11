"""Tushare → 本地 parquet 数据湖增量更新器。

布局：
    data_lake/
        trade_cal.parquet                  # 全量交易日历（每次 update 全刷）
        stock_basic.parquet                # 股票基本面（每次 update 全刷）
        daily/<year>.parquet               # 不复权日线，按年分片
        adj_factor/<year>.parquet          # 复权因子，按年分片
        stk_limit/<year>.parquet           # 涨跌停价
        suspend.parquet                    # 停牌事件（数据量小，单文件）
        index_daily/<code>.parquet         # 基准指数（按代码一文件）
        _meta.json                         # 增量游标 {"daily_last_date": "20240501", ...}

设计要点：
- 大表（daily/adj_factor/stk_limit）按"交易日逐日拉"，因为 Tushare 单次返回上限是 6000 行，
  而每个交易日 ~5500 只股票刚好一页能取完，最稳妥。
- 小表（trade_cal/stock_basic/suspend/index_daily）每次 update 全量刷新，开销可忽略。
- 增量游标存最后成功写入的 trade_date；下次从游标 +1 个交易日继续。
- 写入采用"先 append 到内存、再覆盖整个年份分片"，避免多次小 I/O。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from alphaforge.data.tushare_client import TushareClient
from alphaforge.infra.logger import logger

# ---- 默认基准指数列表 ----
DEFAULT_INDICES = ["000300.SH", "000905.SH", "000852.SH", "000001.SH"]


@dataclass
class DataLakePaths:
    root: Path

    @property
    def trade_cal(self) -> Path:
        return self.root / "trade_cal.parquet"

    @property
    def stock_basic(self) -> Path:
        return self.root / "stock_basic.parquet"

    @property
    def suspend(self) -> Path:
        return self.root / "suspend.parquet"

    @property
    def meta(self) -> Path:
        return self.root / "_meta.json"

    def daily_year(self, year: int) -> Path:
        return self.root / "daily" / f"{year}.parquet"

    def adj_year(self, year: int) -> Path:
        return self.root / "adj_factor" / f"{year}.parquet"

    def limit_year(self, year: int) -> Path:
        return self.root / "stk_limit" / f"{year}.parquet"

    def index(self, code: str) -> Path:
        return self.root / "index_daily" / f"{code}.parquet"

    def ensure_dirs(self) -> None:
        for sub in ("daily", "adj_factor", "stk_limit", "index_daily"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)


def _read_meta(paths: DataLakePaths) -> dict:
    if not paths.meta.exists():
        return {}
    try:
        return json.loads(paths.meta.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(paths: DataLakePaths, meta: dict) -> None:
    paths.meta.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")


def _read_year(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception as e:
            logger.warning(f"read_parquet failed for {path}: {e}")
    return pd.DataFrame()


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path, index=False)
    except Exception:
        # pyarrow 缺失时降级为 csv
        df.to_csv(path.with_suffix(".csv"), index=False)


def update_trade_cal(client: TushareClient, paths: DataLakePaths,
                     start: str = "2010-01-01", end: str | None = None) -> pd.DataFrame:
    """全量刷新交易日历。"""
    end = end or pd.Timestamp.today().strftime("%Y%m%d")
    logger.info(f"Fetching trade_cal {start} -> {end}")
    df = client.trade_cal(start, end)
    _write_parquet(df, paths.trade_cal)
    return df


def update_stock_basic(client: TushareClient, paths: DataLakePaths) -> pd.DataFrame:
    """全量刷新股票基本面（含已退市）。"""
    logger.info("Fetching stock_basic (L + D + P)")
    df = client.stock_basic(list_status="ALL")
    _write_parquet(df, paths.stock_basic)
    return df


def _trade_dates_in(cal_df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    if cal_df.empty:
        return []
    df = cal_df[cal_df["is_open"] == 1]
    df = df[(df["cal_date"] >= start) & (df["cal_date"] <= end)]
    return list(df["cal_date"].sort_values())


def update_daily_panel(
    client: TushareClient,
    paths: DataLakePaths,
    cal_df: pd.DataFrame,
    *,
    start: str,
    end: str,
    panel: str,  # "daily" | "adj_factor" | "stk_limit"
) -> int:
    """按交易日逐日拉取大表，分年写入 parquet。返回写入的天数。"""

    api_map = {
        "daily": client.daily,
        "adj_factor": client.adj_factor,
        "stk_limit": client.stk_limit,
    }
    path_map = {
        "daily": paths.daily_year,
        "adj_factor": paths.adj_year,
        "stk_limit": paths.limit_year,
    }
    fn = api_map[panel]
    path_fn = path_map[panel]

    meta = _read_meta(paths)
    cursor_key = f"{panel}_last_date"
    cursor = meta.get(cursor_key)
    user_start = pd.Timestamp(start)
    if cursor:
        # 从游标的下一交易日开始
        cursor_ts = pd.Timestamp(cursor)
        user_start = max(user_start, cursor_ts + pd.Timedelta(days=1))

    end_ts = pd.Timestamp(end)
    dates = _trade_dates_in(cal_df, user_start, end_ts)
    if not dates:
        logger.info(f"[{panel}] up to date (cursor={cursor}, requested {start}->{end})")
        return 0

    logger.info(f"[{panel}] fetching {len(dates)} trade days: {dates[0].date()} -> {dates[-1].date()}")

    by_year: dict[int, list[pd.DataFrame]] = {}
    for i, d in enumerate(dates, 1):
        ds = d.strftime("%Y%m%d")
        df = fn(trade_date=ds)
        if df is None or df.empty:
            logger.debug(f"[{panel}] {ds} empty")
            continue
        by_year.setdefault(d.year, []).append(df)
        if i % 50 == 0:
            logger.info(f"[{panel}] progress {i}/{len(dates)}")

    written = 0
    for year, frames in by_year.items():
        new_df = pd.concat(frames, ignore_index=True)
        existing = _read_year(path_fn(year))
        if not existing.empty:
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
        else:
            combined = new_df
        combined = combined.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        _write_parquet(combined, path_fn(year))
        written += len(new_df)

    # 更新游标
    meta[cursor_key] = dates[-1].strftime("%Y%m%d")
    _write_meta(paths, meta)
    logger.info(f"[{panel}] wrote {written} rows, cursor -> {meta[cursor_key]}")
    return len(dates)


def update_suspend(client: TushareClient, paths: DataLakePaths,
                   start: str, end: str) -> pd.DataFrame:
    """全量刷新停牌事件（数据量小，直接覆盖）。"""
    logger.info(f"Fetching suspend events {start} -> {end}")
    df = client.suspend_d(start=start, end=end, suspend_type="S")
    _write_parquet(df, paths.suspend)
    return df


def update_indices(client: TushareClient, paths: DataLakePaths,
                   start: str, end: str, codes: list[str] = DEFAULT_INDICES) -> None:
    """更新基准指数日线（每个代码独立文件，全量覆盖）。"""
    for code in codes:
        logger.info(f"Fetching index_daily {code} {start} -> {end}")
        df = client.index_daily(ts_code=code, start=start, end=end)
        if df.empty:
            logger.warning(f"index_daily {code} returned empty")
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        _write_parquet(df, paths.index(code))


def update_all(
    root: Path | str,
    start: str = "2018-01-01",
    end: str | None = None,
    *,
    indices: list[str] = DEFAULT_INDICES,
    client: TushareClient | None = None,
) -> dict:
    """一键更新：日历 + 基本面 + 日线 + 复权 + 涨跌停 + 停牌 + 基准。"""
    client = client or TushareClient.from_env()
    paths = DataLakePaths(Path(root))
    paths.ensure_dirs()
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")

    cal_df = update_trade_cal(client, paths, start=start, end=end)
    update_stock_basic(client, paths)

    summary: dict = {}
    for panel in ("daily", "adj_factor", "stk_limit"):
        summary[panel] = update_daily_panel(
            client, paths, cal_df, start=start, end=end, panel=panel
        )

    update_suspend(client, paths, start=start, end=end)
    update_indices(client, paths, start=start, end=end, codes=indices)

    logger.info(f"data_lake updated at {paths.root} | summary={summary}")
    return summary

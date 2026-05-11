"""Tushare Pro API 薄封装 — 带重试 / 限速 / 字段标准化。

约定：
- 所有方法返回 pd.DataFrame，列名与 Tushare 保持一致（`trade_date` 为 datetime64）。
- 调用频率受积分限制：默认每分钟 ≤ 500 次请求，自动节流。
- 网络/限频错误自动重试（指数退避，最多 3 次）。

Tushare 字段参考：
    https://tushare.pro/document/2
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import pandas as pd

from alphaforge.infra.logger import logger


@dataclass
class TushareConfig:
    """Tushare 客户端配置。"""

    token: str | None = None
    rate_per_minute: int = 480              # 留点 buffer
    max_retries: int = 3
    retry_backoff: float = 2.0              # 指数退避基数
    timeout: int = 30


class TushareClient:
    """Tushare Pro API 封装。

    用法:
        client = TushareClient.from_env()
        df = client.daily(trade_date="20240101")
    """

    def __init__(self, config: TushareConfig | None = None) -> None:
        self.cfg = config or TushareConfig()
        if self.cfg.token is None:
            self.cfg.token = os.getenv("TUSHARE_TOKEN")
        if not self.cfg.token:
            raise RuntimeError(
                "TUSHARE_TOKEN not set. Put it in .env (TUSHARE_TOKEN=xxx) "
                "or export it as an environment variable."
            )

        try:
            import tushare as ts
        except ImportError as e:
            raise ImportError(
                "tushare not installed. `uv add tushare` or `pip install tushare`."
            ) from e

        ts.set_token(self.cfg.token)
        self._pro = ts.pro_api()
        self._call_times: deque[float] = deque(maxlen=self.cfg.rate_per_minute)

    @classmethod
    def from_env(cls) -> "TushareClient":
        return cls()

    # --------- 内部：限速 + 重试 ---------

    def _throttle(self) -> None:
        now = time.monotonic()
        if len(self._call_times) >= self.cfg.rate_per_minute:
            earliest = self._call_times[0]
            elapsed = now - earliest
            if elapsed < 60.0:
                sleep = 60.0 - elapsed + 0.1
                logger.debug(f"Tushare rate-limit, sleeping {sleep:.1f}s")
                time.sleep(sleep)
        self._call_times.append(time.monotonic())

    def _call(self, api_name: str, **kwargs: Any) -> pd.DataFrame:
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            self._throttle()
            try:
                fn = getattr(self._pro, api_name)
                df = fn(**kwargs)
                if df is None:
                    return pd.DataFrame()
                return df
            except Exception as e:  # noqa: BLE001
                last_exc = e
                wait = self.cfg.retry_backoff ** attempt
                logger.warning(
                    f"Tushare {api_name} failed (attempt {attempt + 1}/"
                    f"{self.cfg.max_retries}): {e}; retrying in {wait:.1f}s"
                )
                time.sleep(wait)
        raise RuntimeError(f"Tushare {api_name} failed after {self.cfg.max_retries} retries") from last_exc

    # --------- 业务方法 ---------

    def trade_cal(self, start: str, end: str, exchange: str = "SSE") -> pd.DataFrame:
        """交易日历。返回 [cal_date, is_open, pretrade_date]。"""
        df = self._call(
            "trade_cal",
            exchange=exchange,
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
        )
        if df.empty:
            return df
        df["cal_date"] = pd.to_datetime(df["cal_date"])
        if "pretrade_date" in df.columns:
            df["pretrade_date"] = pd.to_datetime(df["pretrade_date"])
        return df.sort_values("cal_date").reset_index(drop=True)

    def stock_basic(self, list_status: str = "L") -> pd.DataFrame:
        """股票基本面信息。
        list_status: L=上市 D=退市 P=暂停。这里取全量（L+D+P）需多次调用合并。
        """
        frames = []
        for status in ([list_status] if list_status != "ALL" else ["L", "D", "P"]):
            df = self._call(
                "stock_basic",
                exchange="",
                list_status=status,
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date",
            )
            if not df.empty:
                df["list_status"] = status
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out["list_date"] = pd.to_datetime(out["list_date"], errors="coerce")
        if "delist_date" in out.columns:
            out["delist_date"] = pd.to_datetime(out["delist_date"], errors="coerce")
        return out

    def daily(self, trade_date: str | None = None, ts_code: str | None = None,
              start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """日线行情（不复权）。返回 [ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount]。"""
        params: dict[str, Any] = {}
        if trade_date:
            params["trade_date"] = trade_date.replace("-", "")
        if ts_code:
            params["ts_code"] = ts_code
        if start:
            params["start_date"] = start.replace("-", "")
        if end:
            params["end_date"] = end.replace("-", "")
        df = self._call("daily", **params)
        if not df.empty:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def adj_factor(self, trade_date: str | None = None, ts_code: str | None = None,
                   start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """复权因子。"""
        params: dict[str, Any] = {}
        if trade_date:
            params["trade_date"] = trade_date.replace("-", "")
        if ts_code:
            params["ts_code"] = ts_code
        if start:
            params["start_date"] = start.replace("-", "")
        if end:
            params["end_date"] = end.replace("-", "")
        df = self._call("adj_factor", **params)
        if not df.empty:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def stk_limit(self, trade_date: str | None = None, ts_code: str | None = None,
                  start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """涨跌停价格。返回 [ts_code, trade_date, up_limit, down_limit]。"""
        params: dict[str, Any] = {}
        if trade_date:
            params["trade_date"] = trade_date.replace("-", "")
        if ts_code:
            params["ts_code"] = ts_code
        if start:
            params["start_date"] = start.replace("-", "")
        if end:
            params["end_date"] = end.replace("-", "")
        df = self._call("stk_limit", **params)
        if not df.empty:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def suspend_d(self, ts_code: str | None = None,
                  start: str | None = None, end: str | None = None,
                  suspend_type: str = "S") -> pd.DataFrame:
        """每日停牌事件。suspend_type: S=停牌 R=复牌。"""
        params: dict[str, Any] = {"suspend_type": suspend_type}
        if ts_code:
            params["ts_code"] = ts_code
        if start:
            params["start_date"] = start.replace("-", "")
        if end:
            params["end_date"] = end.replace("-", "")
        df = self._call("suspend_d", **params)
        if not df.empty and "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def index_daily(self, ts_code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """指数日线。常用基准：000300.SH（沪深300）/ 000905.SH（中证500）/ 000852.SH（中证1000）。"""
        params: dict[str, Any] = {"ts_code": ts_code}
        if start:
            params["start_date"] = start.replace("-", "")
        if end:
            params["end_date"] = end.replace("-", "")
        df = self._call("index_daily", **params)
        if not df.empty:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

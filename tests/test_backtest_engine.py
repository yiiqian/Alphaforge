"""端到端回测烟测：跑一个最简策略，验证输出文件齐备 + 关键不变量成立。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from alphaforge.data.synthetic import make_synthetic_bundle
from alphaforge.runtime.backtest import BacktestConfig, run_backtest
from alphaforge.runtime.costs import CostModel
from alphaforge.strategy.base import BaseStrategy, StrategyContext


class _BuyHoldFirstThree(BaseStrategy):
    """买入并持有前 3 只可交易股票，等权。月度调仓但因目标固定，几乎无换手。"""

    name = "test_buy_hold_first_three"
    rebalance = "M"
    benchmark = "000300.SH"

    def select(self, date: pd.Timestamp, ctx: StrategyContext) -> pd.DataFrame:
        pool = ctx.universe.tradable(date)[:3]
        if not pool:
            return pd.DataFrame(columns=["ts_code", "weight"])
        return pd.DataFrame({"ts_code": pool, "weight": [1.0 / len(pool)] * len(pool)})


@pytest.fixture
def small_bundle():
    return make_synthetic_bundle(start="2023-01-01", end="2023-06-30", n_stocks=10, seed=1)


def test_backtest_outputs(tmp_path: Path, small_bundle):
    data, universe, calendar = small_bundle
    cfg = BacktestConfig(
        start=pd.Timestamp("2023-01-01"),
        end=pd.Timestamp("2023-06-30"),
        init_cash=1_000_000,
        benchmark=None,
        cost=CostModel(),
    )
    out = tmp_path / "run1"
    metrics = run_backtest(_BuyHoldFirstThree(), data, universe, calendar, cfg, out)

    # 输出文件齐（parquet 或 csv 任一）
    for stem in ["nav", "positions", "trades"]:
        assert (out / f"{stem}.parquet").exists() or (out / f"{stem}.csv").exists(), stem
    assert (out / "metrics.json").exists()

    # 关键 metric
    assert metrics["n_days"] > 50
    assert "total_return" in metrics
    assert "max_drawdown" in metrics
    assert metrics["max_drawdown"] <= 0  # 回撤总是 ≤ 0


def test_backtest_t1_no_same_day_sell(tmp_path: Path, small_bundle):
    """同一调仓日应该只有买入或只有卖出 — 当日新买入的不能在同一日被卖。
    更直接：trades.parquet 中同一 (date, ts_code) 不应同时出现 buy 与 sell。"""
    data, universe, calendar = small_bundle
    cfg = BacktestConfig(
        start=pd.Timestamp("2023-01-01"),
        end=pd.Timestamp("2023-06-30"),
        init_cash=1_000_000, benchmark=None, cost=CostModel(),
    )
    out = tmp_path / "run2"
    run_backtest(_BuyHoldFirstThree(), data, universe, calendar, cfg, out)
    from alphaforge.runtime.backtest import _try_read_table
    trades = _try_read_table(out / "trades")
    if trades.empty:
        return
    pivot = trades.groupby(["date", "ts_code"])["side"].nunique()
    assert (pivot == 1).all(), "同一日同一股票出现了 buy + sell（违反 T+1）"


def test_backtest_cash_never_negative(tmp_path: Path, small_bundle):
    """每日 cash 不应为负数。"""
    data, universe, calendar = small_bundle
    cfg = BacktestConfig(
        start=pd.Timestamp("2023-01-01"),
        end=pd.Timestamp("2023-06-30"),
        init_cash=1_000_000, benchmark=None, cost=CostModel(),
    )
    out = tmp_path / "run3"
    run_backtest(_BuyHoldFirstThree(), data, universe, calendar, cfg, out)
    from alphaforge.runtime.backtest import _try_read_table
    nav = _try_read_table(out / "nav")
    assert (nav["cash"] >= -1e-3).all(), f"cash went negative: min={nav['cash'].min()}"

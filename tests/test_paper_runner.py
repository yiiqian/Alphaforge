"""PaperRunner 端到端烟测 — 用合成 bundle 模拟连续多日 paper 跑。

验证：
  1) 第 1 天（决策日）→ 写出明天要执行的 buy 信号
  2) 第 2 天 → settle 第 1 天的信号 → 持仓出现
  3) 多次 run 同一天是幂等的
  4) NAV 表行数 == 跑过的天数
  5) cash 永远 ≥ 0
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from alphaforge.data.synthetic import make_synthetic_bundle
from alphaforge.runtime.costs import CostModel
from alphaforge.runtime.paper import PaperRunner
from alphaforge.runtime.paper_state import PaperState
from alphaforge.strategy.base import BaseStrategy, StrategyContext


class _BuyHoldThree(BaseStrategy):
    name = "test_paper_buy_hold"
    rebalance = "D"               # 每天调仓，方便测试
    benchmark = "000300.SH"

    def select(self, date: pd.Timestamp, ctx: StrategyContext) -> pd.DataFrame:
        pool = ctx.universe.tradable(date)[:3]
        if not pool:
            return pd.DataFrame(columns=["ts_code", "weight"])
        return pd.DataFrame({
            "ts_code": pool,
            "weight": [1 / 3] * len(pool),
        })


@pytest.fixture
def bundle():
    return make_synthetic_bundle(start="2023-01-03", end="2023-02-28", n_stocks=8, seed=42)


def _setup(tmp_path: Path, bundle):
    data, universe, calendar = bundle
    state = PaperState(tmp_path / "acct.sqlite")
    state.init_account(init_cash=1_000_000, strategy="t", account="t")
    runner = PaperRunner(
        strategy=_BuyHoldThree(), data=data, universe=universe, calendar=calendar,
        state=state, cost=CostModel(),
    )
    return runner, state, calendar


def test_paper_first_day_writes_signals(tmp_path: Path, bundle):
    runner, state, calendar = _setup(tmp_path, bundle)
    days = calendar.trade_days(pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-10"))
    res = runner.run(days[0])
    assert res.rebalance is True
    # 第一天没持仓，所以应该只有 buy 信号
    assert len(res.buys) > 0
    assert len(res.sells) == 0
    # 信号写到第二天
    next_day = calendar.next(days[0])
    sig = state.signals_on(next_day)
    assert not sig.empty
    assert (sig["executed"] == 0).all()
    # 当天没有持仓
    assert state.positions().empty


def test_paper_second_day_settles_to_positions(tmp_path: Path, bundle):
    runner, state, calendar = _setup(tmp_path, bundle)
    days = calendar.trade_days(pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-10"))
    runner.run(days[0])              # 第 1 天：发信号
    res2 = runner.run(days[1])       # 第 2 天：撮合 + 新决策
    # 撮合至少有 1 笔成交
    assert res2.settled["n_buy"] >= 1
    # 持仓出现
    pos = state.positions()
    assert not pos.empty
    # 第 1 天的信号 executed=1 了
    sig = state.signals_on(days[1])
    if not sig.empty:
        assert (sig["executed"] == 1).any()


def test_paper_run_idempotent(tmp_path: Path, bundle):
    """同一天 run 两次：第二次应该不会重复 settle，也不会双倍下单。"""
    runner, state, calendar = _setup(tmp_path, bundle)
    days = calendar.trade_days(pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-10"))
    runner.run(days[0])
    runner.run(days[1])
    pos1 = state.positions().copy()

    runner.run(days[1])              # 第二次跑同一天
    pos2 = state.positions()
    # 持仓数量不变（不会被双倍买入）
    assert pos1.set_index("ts_code")["qty"].equals(pos2.set_index("ts_code")["qty"])


def test_paper_nav_matches_run_count(tmp_path: Path, bundle):
    runner, state, calendar = _setup(tmp_path, bundle)
    days = calendar.trade_days(pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-20"))
    for d in days[:5]:
        runner.run(d)
    nav = state.nav_curve()
    assert len(nav) == 5
    assert nav["cash"].min() >= -1e-3, f"cash went negative: {nav['cash'].min()}"


def test_paper_rejects_non_trade_day(tmp_path: Path, bundle):
    runner, _state, _calendar = _setup(tmp_path, bundle)
    # 2023-01-01 是周日，肯定不是交易日
    with pytest.raises(RuntimeError, match="not a trade day|No daily data"):
        runner.run(pd.Timestamp("2023-01-01"))


def test_paper_format_run_result_renders(tmp_path: Path, bundle):
    from alphaforge.runtime.paper import format_run_result
    runner, _state, calendar = _setup(tmp_path, bundle)
    days = calendar.trade_days(pd.Timestamp("2023-01-03"), pd.Timestamp("2023-01-10"))
    res = runner.run(days[0])
    text = format_run_result(res, account_name="test_acct")
    assert "Paper run" in text
    assert "test_acct" in text
    assert "[nav]" in text

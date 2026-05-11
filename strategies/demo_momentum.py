"""Demo 策略：N 日动量 Top K，等权月度调仓。

把这个文件丢在 strategies/ 目录下就会被自动发现。
启动后用：
    alphaforge backtest run --strategy demo_momentum --config configs/run/demo_momentum.yaml
"""

from __future__ import annotations

import pandas as pd

from alphaforge.strategy.base import BaseStrategy, StrategyContext


class DemoMomentumStrategy(BaseStrategy):
    name = "demo_momentum"
    description = "N 日动量 Top K，等权，月度调仓 — 平台 demo"
    rebalance = "M"
    benchmark = "000300.SH"

    def select(self, date: pd.Timestamp, ctx: StrategyContext) -> pd.DataFrame:
        top_n: int = int(ctx.params.get("top_n", 10))
        lookback: int = int(ctx.params.get("lookback", 120))

        pool = ctx.universe.tradable(date)
        if not pool:
            return pd.DataFrame(columns=["ts_code", "weight", "score"])

        try:
            start = ctx.calendar.prev(date, n=lookback)
        except IndexError:
            return pd.DataFrame(columns=["ts_code", "weight", "score"])

        bars = ctx.data.bars(codes=pool, start=start, end=date, fields=["close"])
        if bars.empty:
            return pd.DataFrame(columns=["ts_code", "weight", "score"])

        wide = bars.pivot(index="trade_date", columns="ts_code", values="close")
        wide = wide.dropna(axis=1, thresh=int(lookback * 0.8))  # 数据稀疏的剔除
        if wide.empty:
            return pd.DataFrame(columns=["ts_code", "weight", "score"])

        first = wide.ffill().iloc[0]
        last = wide.ffill().iloc[-1]
        momentum = (last / first) - 1.0
        top = momentum.nlargest(top_n).dropna()
        if top.empty:
            return pd.DataFrame(columns=["ts_code", "weight", "score"])

        return pd.DataFrame({
            "ts_code": top.index,
            "weight": 1.0 / len(top),
            "score": top.values,
        })

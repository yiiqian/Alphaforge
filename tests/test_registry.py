"""验证策略自动发现 + 注册。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from alphaforge.strategy.base import BaseStrategy, StrategyContext
from alphaforge.strategy.registry import StrategyNotFound, StrategyRegistry


def test_discover_demo_strategy(tmp_path: Path):
    StrategyRegistry.reset()
    proj_root = Path(__file__).resolve().parent.parent
    StrategyRegistry.discover(proj_root / "strategies", force=True)
    items = {it["name"] for it in StrategyRegistry.list()}
    assert "demo_momentum" in items


def test_get_unknown_raises():
    StrategyRegistry.reset()
    with __import__("pytest").raises(StrategyNotFound):
        StrategyRegistry.get("does_not_exist")


def test_register_custom_class():
    class MyStrat(BaseStrategy):
        name = "my_test_strategy"

        def select(self, date, ctx: StrategyContext) -> pd.DataFrame:
            return pd.DataFrame({"ts_code": [], "weight": []})

    StrategyRegistry.reset()
    StrategyRegistry.register(MyStrat)
    assert StrategyRegistry.get("my_test_strategy") is MyStrat

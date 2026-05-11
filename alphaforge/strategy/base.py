"""BaseStrategy & StrategyContext — 用户策略只需继承 BaseStrategy。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from alphaforge.data.api import CalendarAPI, DataAPI, FactorAPI, UniverseAPI


@dataclass
class StrategyContext:
    """平台注入给策略的运行时上下文 — 数据访问的唯一入口。

    所有 API 都对调用 `select(date, ctx)` 时的 date 做了 PIT 截断，
    策略代码无法读到未来数据 —— 从机制上防未来函数。
    """

    data: "DataAPI"
    universe: "UniverseAPI"
    calendar: "CalendarAPI"
    factors: "FactorAPI | None" = None
    params: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """A 股选股策略基类。一个子类 = 一个策略。

    Attributes:
        name:        策略唯一标识（CLI / 配置中按 name 选）
        description: 一行说明
        rebalance:   调仓频率：D / W / M / Q
        benchmark:   基准代码
    """

    name: str = "unnamed"
    description: str = ""
    rebalance: str = "M"
    benchmark: str = "000300.SH"

    def setup(self, ctx: StrategyContext) -> None:  # noqa: B027
        """运行前钩子 — 一次性预计算（缓存、加载模型等）。可选。"""
        return None

    @abstractmethod
    def select(self, date: pd.Timestamp, ctx: StrategyContext) -> pd.DataFrame:
        """在调仓日 `date` 选股并给出权重。

        Returns:
            DataFrame，列必须包含：
              - ts_code: str    股票代码
              - weight:  float  目标权重（和应 ≤ 1.0）
            其它列（score / reason 等）会被报告层透传展示。
        """

    def teardown(self, ctx: StrategyContext) -> None:  # noqa: B027
        """运行后钩子 — 持久化模型、清理资源。可选。"""
        return None

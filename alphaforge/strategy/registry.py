"""StrategyRegistry — 扫描 strategies/ 目录，自动注册所有 BaseStrategy 子类。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from alphaforge.infra.logger import logger
from alphaforge.strategy.base import BaseStrategy


class StrategyNotFound(KeyError):
    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(
            f"Strategy {name!r} not found. "
            f"Available: {available if available else '(none registered)'}"
        )


class StrategyRegistry:
    _registry: dict[str, type[BaseStrategy]] = {}
    _discovered: bool = False

    @classmethod
    def register(cls, strategy_cls: type[BaseStrategy]) -> None:
        if not isinstance(strategy_cls, type) or not issubclass(strategy_cls, BaseStrategy):
            return
        if strategy_cls is BaseStrategy:
            return
        name = strategy_cls.name
        if name in (None, "", "unnamed"):
            logger.warning(
                f"Skipping {strategy_cls.__module__}.{strategy_cls.__name__}: "
                "must override class attribute `name`"
            )
            return
        existing = cls._registry.get(name)
        if existing and existing is not strategy_cls:
            logger.warning(
                f"Strategy name conflict for {name!r}: "
                f"{existing.__module__} -> {strategy_cls.__module__} (overriding)"
            )
        cls._registry[name] = strategy_cls
        logger.debug(f"Registered strategy: {name} ({strategy_cls.__module__})")

    @classmethod
    def discover(cls, root: str | Path = "strategies", force: bool = False) -> None:
        """递归 import 给定目录下所有 .py 文件，触发 __init_subclass__ 注册。"""
        if cls._discovered and not force:
            return

        root = Path(root)
        if not root.exists():
            logger.warning(f"Strategies directory not found: {root}")
            cls._discovered = True
            return

        for py in sorted(root.rglob("*.py")):
            if py.name.startswith("_"):
                continue
            module_name = f"_alphaforge_user_strategies.{py.stem}"
            spec = importlib.util.spec_from_file_location(module_name, py)
            if spec is None or spec.loader is None:
                logger.warning(f"Cannot import strategy file: {py}")
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                logger.exception(f"Failed to import {py}: {e}")
                continue
            for obj in vars(module).values():
                if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                    cls.register(obj)

        cls._discovered = True

    @classmethod
    def get(cls, name: str) -> type[BaseStrategy]:
        cls.discover()
        if name not in cls._registry:
            raise StrategyNotFound(name, available=sorted(cls._registry))
        return cls._registry[name]

    @classmethod
    def list(cls) -> list[dict]:
        cls.discover()
        return [
            {
                "name": s.name,
                "description": s.description,
                "rebalance": s.rebalance,
                "benchmark": s.benchmark,
                "module": s.__module__,
            }
            for s in cls._registry.values()
        ]

    @classmethod
    def reset(cls) -> None:
        """主要用于测试。"""
        cls._registry.clear()
        cls._discovered = False

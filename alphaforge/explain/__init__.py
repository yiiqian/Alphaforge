"""LLM 策略解释层 — 把 paper-trading 的调仓清单 + 持仓 + 净值喂给 LLM，
拿到一段中文解释，附带在通知文本里。

约定：
    Explainer.explain(payload: dict, **kw) -> str
    payload 形如：
        {"date": "2026-05-12", "strategy": "demo_momentum",
         "buys": [...], "sells": [...], "positions": [...], "nav": {...}}

调用失败一律退化为预设兜底文本（不抛异常）。
"""

from alphaforge.explain.base import Explainer, build_explainer
from alphaforge.explain.prompt import build_payload, build_prompt

__all__ = ["Explainer", "build_explainer", "build_payload", "build_prompt"]

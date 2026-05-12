"""Explainer ABC + factory。

工厂入口 build_explainer(cfg) → Explainer。
cfg 形如：
    provider: deepseek            # deepseek / claude / openai / null
    model: deepseek-chat          # 各家自己的模型名
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "..."               # OpenAI 兼容时填
    max_tokens: 600
    temperature: 0.3
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from alphaforge.infra.logger import logger


class Explainer(ABC):
    name: str = "abstract"

    @abstractmethod
    def explain(self, payload: dict, *, fallback: str = "") -> str:
        """生成中文解释。失败时返回 fallback 或空串（不抛）。"""


class _NullExplainer(Explainer):
    name = "null"

    def explain(self, payload: dict, *, fallback: str = "") -> str:
        return fallback or "(未配置 LLM，跳过自动解释)"


def _expand_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def build_explainer(cfg: dict | None) -> Explainer:
    """根据配置 dict 构建 Explainer。cfg=None / provider=null → 静默。"""
    if not cfg:
        return _NullExplainer()
    cfg = {k: _expand_env(v) for k, v in cfg.items()}
    provider = (cfg.get("provider") or "null").lower()

    if provider == "null":
        return _NullExplainer()

    if provider == "deepseek":
        from alphaforge.explain.deepseek import DeepSeekExplainer
        return DeepSeekExplainer(
            api_key=cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", ""),
            model=cfg.get("model", "deepseek-chat"),
            base_url=cfg.get("base_url", "https://api.deepseek.com/v1"),
            max_tokens=int(cfg.get("max_tokens", 700)),
            temperature=float(cfg.get("temperature", 0.3)),
        )

    if provider in ("claude", "anthropic"):
        from alphaforge.explain.claude import ClaudeExplainer
        return ClaudeExplainer(
            api_key=cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", ""),
            model=cfg.get("model", "claude-sonnet-4-5"),
            base_url=cfg.get("base_url", "https://api.anthropic.com"),
            max_tokens=int(cfg.get("max_tokens", 700)),
            temperature=float(cfg.get("temperature", 0.3)),
        )

    if provider in ("openai", "openai-compat", "compat"):
        from alphaforge.explain.openai_compat import OpenAICompatExplainer
        return OpenAICompatExplainer(
            api_key=cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
            model=cfg.get("model", "gpt-4o-mini"),
            base_url=cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            max_tokens=int(cfg.get("max_tokens", 700)),
            temperature=float(cfg.get("temperature", 0.3)),
        )

    logger.warning(f"Unknown explainer provider: {provider}; falling back to null")
    return _NullExplainer()

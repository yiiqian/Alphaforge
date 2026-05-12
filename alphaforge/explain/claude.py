"""Anthropic Claude adapter — /v1/messages API。

文档：https://docs.anthropic.com/en/api/messages

环境变量：ANTHROPIC_API_KEY
默认模型：claude-sonnet-4-5
"""

from __future__ import annotations

from alphaforge.explain._http import safe_post_json
from alphaforge.explain.base import Explainer
from alphaforge.explain.prompt import SYSTEM_PROMPT, build_prompt, fallback_text
from alphaforge.infra.logger import logger


class ClaudeExplainer(Explainer):
    name = "claude"

    def __init__(self, api_key: str, *, model: str = "claude-sonnet-4-5",
                 base_url: str = "https://api.anthropic.com",
                 max_tokens: int = 700, temperature: float = 0.3,
                 timeout: float = 30.0,
                 anthropic_version: str = "2023-06-01") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.anthropic_version = anthropic_version

    def explain(self, payload: dict, *, fallback: str = "") -> str:
        if not self.api_key:
            logger.warning("[claude] ANTHROPIC_API_KEY 未配置，使用兜底文本")
            return fallback or fallback_text(payload)

        body = {
            "model": self.model,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": build_prompt(payload)},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "Content-Type": "application/json",
        }
        resp = safe_post_json(
            f"{self.base_url}/v1/messages",
            headers=headers, body=body, timeout=self.timeout, tag="claude",
        )
        if not resp:
            return fallback or fallback_text(payload)
        try:
            blocks = resp.get("content", [])
            text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            content = "\n".join(p for p in text_parts if p).strip()
        except Exception:
            logger.warning(f"[claude] unexpected response shape: {str(resp)[:200]}")
            return fallback or fallback_text(payload)
        return content or (fallback or fallback_text(payload))

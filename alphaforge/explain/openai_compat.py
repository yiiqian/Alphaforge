"""OpenAI 兼容 chat/completions adapter — 通义千问 / Kimi / 智谱 / Ollama 都走这里。

环境变量：OPENAI_API_KEY + OPENAI_BASE_URL
"""

from __future__ import annotations

from alphaforge.explain._http import safe_post_json
from alphaforge.explain.base import Explainer
from alphaforge.explain.prompt import SYSTEM_PROMPT, build_prompt, fallback_text
from alphaforge.infra.logger import logger


class OpenAICompatExplainer(Explainer):
    name = "openai-compat"

    def __init__(self, api_key: str, *, model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com/v1",
                 max_tokens: int = 700, temperature: float = 0.3,
                 timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    def explain(self, payload: dict, *, fallback: str = "") -> str:
        if not self.api_key:
            logger.warning("[openai-compat] api_key 未配置，使用兜底文本")
            return fallback or fallback_text(payload)

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(payload)},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = safe_post_json(
            f"{self.base_url}/chat/completions",
            headers=headers, body=body, timeout=self.timeout, tag="openai-compat",
        )
        if not resp:
            return fallback or fallback_text(payload)
        try:
            content = resp["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            logger.warning(f"[openai-compat] unexpected response shape: {str(resp)[:200]}")
            return fallback or fallback_text(payload)
        return content or (fallback or fallback_text(payload))

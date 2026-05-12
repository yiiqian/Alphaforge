"""DeepSeek LLM adapter（OpenAI 兼容 chat/completions）。

文档：https://api-docs.deepseek.com/

环境变量：DEEPSEEK_API_KEY
默认模型：deepseek-chat（非推理）；推理用 deepseek-reasoner。
"""

from __future__ import annotations

from alphaforge.explain._http import safe_post_json
from alphaforge.explain.base import Explainer
from alphaforge.explain.prompt import SYSTEM_PROMPT, build_prompt, fallback_text
from alphaforge.infra.logger import logger


class DeepSeekExplainer(Explainer):
    name = "deepseek"

    def __init__(self, api_key: str, *, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com/v1",
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
            logger.warning("[deepseek] DEEPSEEK_API_KEY 未配置，使用兜底文本")
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
            headers=headers, body=body, timeout=self.timeout, tag="deepseek",
        )
        if not resp:
            return fallback or fallback_text(payload)
        try:
            content = resp["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            logger.warning(f"[deepseek] unexpected response shape: {str(resp)[:200]}")
            return fallback or fallback_text(payload)
        return content or (fallback or fallback_text(payload))

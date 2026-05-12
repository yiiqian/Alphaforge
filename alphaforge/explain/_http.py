"""共享 HTTP POST 助手 — 各 LLM adapter 共用。

使用 stdlib 的 urllib，不引额外依赖。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from alphaforge.infra.logger import logger


def post_json(url: str, *, headers: dict, body: dict, timeout: float = 30.0) -> dict:
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw_body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    return json.loads(text)


def safe_post_json(url: str, *, headers: dict, body: dict, timeout: float = 30.0,
                   tag: str = "llm") -> dict | None:
    try:
        return post_json(url, headers=headers, body=body, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "ignore")
        except Exception:
            err_body = ""
        logger.warning(f"[{tag}] HTTP {e.code}: {err_body[:300]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[{tag}] request failed: {e}")
    return None

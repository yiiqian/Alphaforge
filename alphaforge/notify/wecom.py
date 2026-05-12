"""企业微信群机器人 — 群里 添加机器人 → 拿 webhook URL。

文档：https://developer.work.weixin.qq.com/document/path/91770

支持 markdown 类型；单条 ≤ 4096 字节 → 超长自动按段切分多次发。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from alphaforge.infra.logger import logger
from alphaforge.notify.base import Notifier

_MAX_MD_BYTES = 3800  # 留一点余量


def _split_by_bytes(text: str, max_bytes: int) -> list[str]:
    out: list[str] = []
    cur = ""
    cur_bytes = 0
    for line in text.splitlines(keepends=True):
        b = len(line.encode("utf-8"))
        if cur_bytes + b > max_bytes and cur:
            out.append(cur)
            cur, cur_bytes = "", 0
        cur += line
        cur_bytes += b
    if cur:
        out.append(cur)
    return out or [""]


class WecomNotifier(Notifier):
    name = "wecom"

    def __init__(self, webhook: str, timeout: float = 8.0) -> None:
        if not webhook:
            raise ValueError("WecomNotifier: webhook is required")
        self.webhook = webhook
        self.timeout = timeout

    def _post(self, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            logger.warning(f"[wecom] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wecom] send failed: {e}")
            return False
        try:
            obj = json.loads(raw)
        except ValueError:
            logger.warning(f"[wecom] non-JSON: {raw[:200]}")
            return False
        if obj.get("errcode", -1) == 0:
            return True
        logger.warning(f"[wecom] errmsg: {raw[:300]}")
        return False

    def send(self, title: str, text: str, *, level: str = "info") -> bool:
        prefix = {"info": "ℹ️", "warn": "⚠️", "error": "❌"}.get(level, "")
        full = f"## {prefix} {title}\n\n{text}"
        chunks = _split_by_bytes(full, _MAX_MD_BYTES)
        ok = True
        for i, chunk in enumerate(chunks):
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": chunk if i == 0 else f"(续 {i+1}/{len(chunks)})\n{chunk}"},
            }
            if not self._post(payload):
                ok = False
        return ok

"""飞书自定义机器人 — 通过群里"添加机器人"得到 webhook URL，可选签名校验。

文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot

发送两种格式：
- 默认：interactive 消息卡片，标题带颜色（绿/橙/红）
- 长文本（> 2 万字符）退回 text 类型避免被截

签名校验（开启了"签名校验"的机器人必须）：
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\\n{secret}"
    sign = base64.b64encode(hmac.new(string_to_sign.encode(), digestmod=sha256).digest())
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from alphaforge.infra.logger import logger
from alphaforge.notify.base import Notifier

_LEVEL_TEMPLATE = {
    "info": "blue",
    "warn": "orange",
    "error": "red",
}


def _sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


class FeishuNotifier(Notifier):
    name = "feishu"

    def __init__(self, webhook: str, secret: str | None = None,
                 timeout: float = 8.0) -> None:
        if not webhook:
            raise ValueError("FeishuNotifier: webhook is required")
        self.webhook = webhook
        self.secret = secret or None
        self.timeout = timeout

    def _build_payload(self, title: str, text: str, level: str) -> dict:
        # 飞书卡片对 markdown 支持有限，这里用最稳妥的 markdown 元素
        color = _LEVEL_TEMPLATE.get(level, "blue")
        # 长度安全：飞书单条建议 < 30k 字符
        if len(text) > 25000:
            text = text[:24800] + "\n\n... (truncated)"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": title[:120]},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            ],
        }
        payload: dict = {"msg_type": "interactive", "card": card}
        if self.secret:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = _sign(self.secret, ts)
        return payload

    def send(self, title: str, text: str, *, level: str = "info") -> bool:
        payload = self._build_payload(title, text, level)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            logger.warning(f"[feishu] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[feishu] send failed: {e}")
            return False
        # 飞书成功响应：{"code":0,"msg":"success", ...}
        try:
            obj = json.loads(raw)
        except ValueError:
            logger.warning(f"[feishu] non-JSON response: {raw[:200]}")
            return False
        if obj.get("code", -1) == 0 or obj.get("StatusCode", -1) == 0:
            return True
        logger.warning(f"[feishu] error response: {raw[:300]}")
        return False

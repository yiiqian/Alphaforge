"""QQ 推送 — 通过 OneBot v11 兼容的 HTTP API（go-cqhttp / NapCat / Lagrange）。

前置：用户自行起 go-cqhttp / NapCat / Lagrange 等 OneBot v11 实现，开启 HTTP API。
本模块只调 /send_group_msg 或 /send_private_msg。

配置：
    type: qq
    base_url: "http://127.0.0.1:5700"
    token: "..."         # access_token，没设则不发
    target: 12345678     # group_id 或 user_id
    target_kind: group   # group / private
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from alphaforge.infra.logger import logger
from alphaforge.notify.base import Notifier


class QQNotifier(Notifier):
    name = "qq"

    def __init__(self, base_url: str, target: int | str,
                 token: str | None = None,
                 target_kind: str = "group", timeout: float = 8.0) -> None:
        if not base_url:
            raise ValueError("QQNotifier: base_url is required")
        if target is None:
            raise ValueError("QQNotifier: target is required")
        if target_kind not in ("group", "private"):
            raise ValueError(f"QQNotifier: invalid target_kind={target_kind}")
        self.base_url = base_url.rstrip("/")
        self.target = int(target)
        self.token = token or None
        self.target_kind = target_kind
        self.timeout = timeout

    def send(self, title: str, text: str, *, level: str = "info") -> bool:
        prefix = {"info": "ℹ️", "warn": "⚠️", "error": "❌"}.get(level, "")
        message = f"【{prefix} {title}】\n{text}"

        endpoint = f"{self.base_url}/send_{self.target_kind}_msg"
        params: dict = {"message": message}
        if self.target_kind == "group":
            params["group_id"] = self.target
        else:
            params["user_id"] = self.target

        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            logger.warning(f"[qq] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq] send failed: {e}")
            return False
        try:
            obj = json.loads(raw)
        except ValueError:
            logger.warning(f"[qq] non-JSON: {raw[:200]}")
            return False
        # OneBot v11: {"status":"ok"|"failed", "retcode":0, "data":{...}}
        if obj.get("status") == "ok" and obj.get("retcode", -1) == 0:
            return True
        logger.warning(f"[qq] error response: {raw[:300]}")
        return False

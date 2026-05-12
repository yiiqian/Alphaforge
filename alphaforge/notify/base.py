"""Notifier ABC + factory。

工厂入口 build_notifier(cfg) → Notifier。
cfg 形如：
    type: feishu             # feishu / wecom / qq / multi / null
    webhook: "${FEISHU_WEBHOOK}"     # 各家通用字段
    secret: "..."                    # 飞书签名校验（可选）
    base_url: "http://..."           # QQ go-cqhttp HTTP API 地址
    token: "..."                     # QQ HTTP API access_token
    target: 12345                    # QQ group_id 或 user_id
    target_kind: group               # group / private
    # multi 模式：嵌套
    children:
      - { type: feishu, webhook: ... }
      - { type: wecom, webhook: ... }
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from alphaforge.infra.logger import logger


class NotifyError(RuntimeError):
    """通知发送失败 — Notifier.send 内部捕获并返回 False，外部一般用不到。"""


class Notifier(ABC):
    """通知抽象。所有 adapter 都用这一接口。"""

    name: str = "abstract"

    @abstractmethod
    def send(self, title: str, text: str, *, level: str = "info") -> bool:
        """发送一条通知。失败返回 False（不抛异常）。"""

    def test(self) -> bool:
        """Smoke test：发一条 'hello from alphaforge' 验证联通性。"""
        return self.send(
            title="Alphaforge 通知测试",
            text="如果你看到这条消息，说明通知通道已配通 ✅",
            level="info",
        )


class _NullNotifier(Notifier):
    name = "null"

    def send(self, title: str, text: str, *, level: str = "info") -> bool:  # noqa: D401
        logger.info(f"[notify-null] {level.upper()} {title}\n{text}")
        return True


class _MultiNotifier(Notifier):
    """同时往多个通道发；任一成功即认为成功，所有失败返回 False。"""

    name = "multi"

    def __init__(self, children: list[Notifier]) -> None:
        self.children = children

    def send(self, title: str, text: str, *, level: str = "info") -> bool:
        ok = False
        for ch in self.children:
            if ch.send(title, text, level=level):
                ok = True
        return ok


def _expand_env(value: Any) -> Any:
    """${VAR} → env value；其它原样返回。"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def build_notifier(cfg: dict | None) -> Notifier:
    """根据配置 dict 构建 Notifier。cfg=None 或 type=null → 静默 logger。"""
    if not cfg:
        return _NullNotifier()
    cfg = {k: _expand_env(v) for k, v in cfg.items()}
    typ = (cfg.get("type") or "null").lower()

    if typ == "null":
        return _NullNotifier()

    if typ == "multi":
        children_cfg = cfg.get("children") or []
        children = [build_notifier(c) for c in children_cfg]
        return _MultiNotifier(children)

    if typ == "feishu":
        from alphaforge.notify.feishu import FeishuNotifier
        return FeishuNotifier(
            webhook=cfg["webhook"],
            secret=cfg.get("secret"),
        )

    if typ in ("wecom", "wechat", "qywx"):
        from alphaforge.notify.wecom import WecomNotifier
        return WecomNotifier(webhook=cfg["webhook"])

    if typ == "qq":
        from alphaforge.notify.qq import QQNotifier
        return QQNotifier(
            base_url=cfg["base_url"],
            token=cfg.get("token"),
            target=cfg["target"],
            target_kind=cfg.get("target_kind", "group"),
        )

    raise ValueError(f"Unknown notifier type: {typ}")

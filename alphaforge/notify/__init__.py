"""通知层 — 把 paper-trading 的每日清单推送到飞书 / 企业微信 / QQ。

约定：
  Notifier.send(title, text, *, level="info") -> bool
  level: "info" | "warn" | "error"  — 仅影响标题前缀和飞书的卡片颜色。

任何 send 调用失败都不会抛异常 → 返回 False，调用方（scheduler）日志记录后继续。
"""

from alphaforge.notify.base import Notifier, NotifyError, build_notifier

__all__ = ["Notifier", "NotifyError", "build_notifier"]

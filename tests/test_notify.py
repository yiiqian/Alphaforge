"""Notify 层测试 — 全部用 mock HTTP，不真发出去。"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from alphaforge.notify.base import (
    Notifier,
    _MultiNotifier,
    _NullNotifier,
    build_notifier,
)
from alphaforge.notify.feishu import FeishuNotifier, _sign
from alphaforge.notify.qq import QQNotifier
from alphaforge.notify.wecom import WecomNotifier


# ---------------- factory ----------------

def test_build_notifier_null_when_empty():
    assert isinstance(build_notifier(None), _NullNotifier)
    assert isinstance(build_notifier({}), _NullNotifier)
    assert isinstance(build_notifier({"type": "null"}), _NullNotifier)


def test_build_notifier_feishu():
    n = build_notifier({"type": "feishu", "webhook": "https://example.com/hook"})
    assert isinstance(n, FeishuNotifier)
    assert n.webhook == "https://example.com/hook"
    assert n.secret is None


def test_build_notifier_feishu_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_WEBHOOK", "https://x.example/y")
    n = build_notifier({"type": "feishu", "webhook": "${MY_WEBHOOK}"})
    assert isinstance(n, FeishuNotifier)
    assert n.webhook == "https://x.example/y"


def test_build_notifier_wecom():
    n = build_notifier({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x"})
    assert isinstance(n, WecomNotifier)


def test_build_notifier_qq():
    n = build_notifier({
        "type": "qq",
        "base_url": "http://127.0.0.1:5700",
        "target": 12345,
        "target_kind": "group",
    })
    assert isinstance(n, QQNotifier)
    assert n.target == 12345


def test_build_notifier_multi():
    cfg = {
        "type": "multi",
        "children": [
            {"type": "null"},
            {"type": "null"},
        ],
    }
    n = build_notifier(cfg)
    assert isinstance(n, _MultiNotifier)
    assert len(n.children) == 2


def test_build_notifier_unknown_raises():
    with pytest.raises(ValueError):
        build_notifier({"type": "no-such-channel"})


# ---------------- Null ----------------

def test_null_notifier_send_and_test():
    n = _NullNotifier()
    assert n.send("t", "body", level="info") is True
    assert n.test() is True


# ---------------- Multi ----------------

class _StubNotifier(Notifier):
    name = "stub"

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple] = []

    def send(self, title: str, text: str, *, level: str = "info") -> bool:
        self.calls.append((title, text, level))
        return self.ok


def test_multi_any_success():
    a, b = _StubNotifier(True), _StubNotifier(False)
    m = _MultiNotifier([a, b])
    assert m.send("t", "x") is True
    assert a.calls and b.calls


def test_multi_all_fail():
    a, b = _StubNotifier(False), _StubNotifier(False)
    m = _MultiNotifier([a, b])
    assert m.send("t", "x") is False


# ---------------- Feishu ----------------

def _fake_resp(json_obj: dict, status: int = 200):
    raw = json.dumps(json_obj).encode("utf-8")

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *args):
            return False

        def read(self_inner):
            return raw

        status_code = status

    return _Resp()


def test_feishu_sign_deterministic():
    s1 = _sign("secret", "1700000000")
    s2 = _sign("secret", "1700000000")
    assert s1 == s2 and len(s1) > 10


def test_feishu_send_success_no_secret():
    n = FeishuNotifier(webhook="https://example.com/hook")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"code": 0, "msg": "success"})) as mock_open:
        ok = n.send("Title", "body")
    assert ok is True
    # capture the request body to verify shape
    req = mock_open.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert body["msg_type"] == "interactive"
    assert "card" in body
    assert "sign" not in body


def test_feishu_send_with_secret_adds_signature():
    n = FeishuNotifier(webhook="https://example.com/hook", secret="abc")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"code": 0})) as mock_open:
        n.send("T", "x")
    body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
    assert "timestamp" in body and "sign" in body


def test_feishu_send_error_response_returns_false():
    n = FeishuNotifier(webhook="https://example.com/hook")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"code": 19001, "msg": "bad"})):
        assert n.send("T", "x") is False


def test_feishu_truncates_long_text():
    n = FeishuNotifier(webhook="https://example.com/hook")
    long_text = "x" * 30000
    payload = n._build_payload("t", long_text, "info")
    content = payload["card"]["elements"][0]["text"]["content"]
    assert len(content) < 30000
    assert "truncated" in content


# ---------------- Wecom ----------------

def test_wecom_send_success():
    n = WecomNotifier(webhook="https://qyapi.weixin.qq.com/foo")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"errcode": 0})) as mock_open:
        ok = n.send("T", "hello")
    assert ok is True
    body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
    assert body["msgtype"] == "markdown"


def test_wecom_send_error_returns_false():
    n = WecomNotifier(webhook="https://qyapi.weixin.qq.com/foo")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"errcode": 40001, "errmsg": "bad"})):
        assert n.send("T", "x") is False


# ---------------- QQ ----------------

def test_qq_send_group():
    n = QQNotifier(base_url="http://127.0.0.1:5700", target=12345, target_kind="group")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"status": "ok", "retcode": 0})) as mock_open:
        ok = n.send("Title", "body")
    assert ok is True
    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/send_group_msg")
    body = json.loads(req.data.decode("utf-8"))
    assert body["group_id"] == 12345
    assert "Title" in body["message"]


def test_qq_send_private_with_token():
    n = QQNotifier(base_url="http://127.0.0.1:5700/", target=10001,
                   target_kind="private", token="tok")
    with patch("urllib.request.urlopen", return_value=_fake_resp({"status": "ok", "retcode": 0})) as mock_open:
        n.send("T", "x")
    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/send_private_msg")
    assert req.headers.get("Authorization") == "Bearer tok"
    body = json.loads(req.data.decode("utf-8"))
    assert body["user_id"] == 10001


def test_qq_validates_target_kind():
    with pytest.raises(ValueError):
        QQNotifier(base_url="http://x", target=1, target_kind="weird")

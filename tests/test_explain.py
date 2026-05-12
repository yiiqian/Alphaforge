"""Explain 层测试 — LLM 调用全部 mock，不真发请求。"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from alphaforge.explain.base import (
    Explainer,
    _NullExplainer,
    build_explainer,
)
from alphaforge.explain.claude import ClaudeExplainer
from alphaforge.explain.deepseek import DeepSeekExplainer
from alphaforge.explain.openai_compat import OpenAICompatExplainer
from alphaforge.explain.prompt import (
    SYSTEM_PROMPT,
    build_payload,
    build_prompt,
    fallback_text,
)
from alphaforge.runtime.paper import PaperRunResult


# ---------------- factory ----------------

def test_build_explainer_null_when_empty():
    assert isinstance(build_explainer(None), _NullExplainer)
    assert isinstance(build_explainer({}), _NullExplainer)
    assert isinstance(build_explainer({"provider": "null"}), _NullExplainer)


def test_build_explainer_deepseek():
    e = build_explainer({"provider": "deepseek", "api_key": "sk-x"})
    assert isinstance(e, DeepSeekExplainer)
    assert e.api_key == "sk-x"


def test_build_explainer_claude():
    e = build_explainer({"provider": "claude", "api_key": "k", "model": "claude-sonnet-4-5"})
    assert isinstance(e, ClaudeExplainer)


def test_build_explainer_openai_compat():
    e = build_explainer({
        "provider": "openai",
        "api_key": "sk-y",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    })
    assert isinstance(e, OpenAICompatExplainer)
    assert e.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_build_explainer_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    e = build_explainer({"provider": "deepseek", "api_key": "${MY_KEY}"})
    assert e.api_key == "sk-from-env"


def test_build_explainer_unknown_falls_back_to_null():
    assert isinstance(build_explainer({"provider": "no-such"}), _NullExplainer)


# ---------------- payload / prompt ----------------

def _sample_result() -> PaperRunResult:
    res = PaperRunResult(date=pd.Timestamp("2026-05-13"))
    res.rebalance = True
    res.buys = [{"ts_code": "000001.SZ", "qty": 1000, "ref_price": 12.345, "fees": 5.0, "reason": "rebalance"}]
    res.sells = [{"ts_code": "600519.SH", "qty": 100, "ref_price": 1701.5, "fees": 5.0, "reason": "rebalance"}]
    res.skipped = [{"ts_code": "000002.SZ", "side": "buy", "reason": "limit_up"}]
    res.settled = {"n_buy": 0, "n_sell": 0, "cash_delta": 0.0}
    res.nav = {"cash": 100000.0, "market_value": 900000.0, "equity": 1_000_000.0, "n_positions": 8}
    res.next_trade_day = pd.Timestamp("2026-05-14")
    return res


def test_build_payload_shape():
    res = _sample_result()
    payload = build_payload(
        res,
        strategy_name="demo_momentum",
        strategy_desc="12个月动量",
        rebalance_freq="monthly",
        positions_snapshot=[{"ts_code": "000001.SZ", "qty": 1000}],
        benchmark="000300.SH",
    )
    assert payload["date"] == "2026-05-13"
    assert payload["strategy"] == "demo_momentum"
    assert payload["rebalance_today"] is True
    assert payload["n_buys"] == 1 and payload["n_sells"] == 1
    assert payload["nav"]["equity"] == 1_000_000.0
    assert payload["next_trade_day"] == "2026-05-14"
    assert payload["benchmark"] == "000300.SH"
    assert payload["positions"] == [{"ts_code": "000001.SZ", "qty": 1000}]


def test_build_payload_trims_long_lists():
    res = PaperRunResult(date=pd.Timestamp("2026-05-13"))
    res.buys = [
        {"ts_code": f"{i:06d}.SZ", "qty": 100, "ref_price": 10.0, "fees": 1.0, "reason": "x"}
        for i in range(50)
    ]
    res.skipped = [{"ts_code": f"x{i}", "reason": "r"} for i in range(30)]
    payload = build_payload(res, strategy_name="s")
    assert len(payload["buys"]) == 15
    assert payload["n_buys"] == 50
    assert len(payload["skipped"]) == 10


def test_build_prompt_contains_payload_json():
    payload = {"date": "2026-05-13", "n_buys": 3}
    p = build_prompt(payload)
    assert "2026-05-13" in p
    assert "n_buys" in p
    assert "json" in p.lower()


def test_fallback_text_basic():
    payload = {
        "date": "2026-05-13", "rebalance_today": True,
        "n_buys": 2, "n_sells": 1,
        "nav": {"n_positions": 8, "equity": 1_000_000.0},
    }
    text = fallback_text(payload)
    assert "2026-05-13" in text
    assert "买 2" in text and "卖 1" in text
    assert "持仓 8" in text


def test_fallback_text_no_rebalance():
    payload = {"date": "2026-05-13", "rebalance_today": False, "n_buys": 0, "n_sells": 0,
               "nav": {"n_positions": 5, "equity": 999.0}}
    text = fallback_text(payload)
    assert "未调仓" in text


def test_system_prompt_has_constraints():
    # Smoke: 关键约束词必须保留
    assert "中文" in SYSTEM_PROMPT
    assert "禁止" in SYSTEM_PROMPT


# ---------------- adapters: explain() with mocked HTTP ----------------

def test_null_explainer_returns_fallback():
    e = _NullExplainer()
    assert e.explain({}, fallback="abc") == "abc"
    out = e.explain({})
    assert "未配置" in out


def test_deepseek_no_key_returns_fallback():
    e = DeepSeekExplainer(api_key="")
    out = e.explain({"date": "x", "rebalance_today": False, "n_buys": 0, "n_sells": 0,
                     "nav": {"n_positions": 0, "equity": 0.0}})
    assert "未调仓" in out  # fallback_text path


def test_deepseek_explain_success():
    e = DeepSeekExplainer(api_key="sk-x")
    fake = {"choices": [{"message": {"content": "今日调仓：买1只。"}}]}
    with patch("alphaforge.explain.deepseek.safe_post_json", return_value=fake) as mock:
        out = e.explain({"date": "2026-05-13", "n_buys": 1, "n_sells": 0, "rebalance_today": True,
                         "nav": {"n_positions": 1, "equity": 100.0}})
    assert "今日调仓" in out
    # 校验请求 url 与 body shape
    args, kwargs = mock.call_args
    assert args[0].endswith("/chat/completions")
    body = kwargs["body"]
    assert body["model"] == "deepseek-chat"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"


def test_deepseek_explain_http_failure_falls_back():
    e = DeepSeekExplainer(api_key="sk-x")
    payload = {"date": "2026-05-13", "rebalance_today": False, "n_buys": 0, "n_sells": 0,
               "nav": {"n_positions": 0, "equity": 0.0}}
    with patch("alphaforge.explain.deepseek.safe_post_json", return_value=None):
        out = e.explain(payload)
    assert "未调仓" in out


def test_deepseek_explain_malformed_response_falls_back():
    e = DeepSeekExplainer(api_key="sk-x")
    payload = {"date": "2026-05-13", "rebalance_today": False, "n_buys": 0, "n_sells": 0,
               "nav": {"n_positions": 0, "equity": 0.0}}
    with patch("alphaforge.explain.deepseek.safe_post_json", return_value={"weird": "shape"}):
        out = e.explain(payload, fallback="FB")
    assert out == "FB"


def test_claude_explain_success():
    e = ClaudeExplainer(api_key="k")
    fake = {"content": [{"type": "text", "text": "调仓说明……"}]}
    with patch("alphaforge.explain.claude.safe_post_json", return_value=fake) as mock:
        out = e.explain({"date": "x", "n_buys": 0, "n_sells": 0, "rebalance_today": False,
                         "nav": {"n_positions": 0, "equity": 0.0}})
    assert "调仓说明" in out
    args, kwargs = mock.call_args
    assert kwargs["headers"]["x-api-key"] == "k"
    assert kwargs["headers"]["anthropic-version"]
    assert "system" in kwargs["body"]


def test_claude_no_key_returns_fallback():
    e = ClaudeExplainer(api_key="")
    out = e.explain({"date": "x", "rebalance_today": False, "n_buys": 0, "n_sells": 0,
                     "nav": {"n_positions": 0, "equity": 0.0}}, fallback="FB")
    assert out == "FB"


def test_openai_compat_explain_success():
    e = OpenAICompatExplainer(api_key="k", base_url="https://x.example/v1", model="qwen-plus")
    fake = {"choices": [{"message": {"content": "OK"}}]}
    with patch("alphaforge.explain.openai_compat.safe_post_json", return_value=fake) as mock:
        out = e.explain({"date": "x", "n_buys": 0, "n_sells": 0, "rebalance_today": False,
                         "nav": {"n_positions": 0, "equity": 0.0}})
    assert out == "OK"
    args, kwargs = mock.call_args
    assert args[0] == "https://x.example/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer k"

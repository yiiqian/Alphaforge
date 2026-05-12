"""把 PaperRunResult + 一些上下文打包成 LLM 可读的 payload + prompt。

设计原则：
  1) 只塞"必要事实" — 日期、策略、买卖清单、当前持仓、NAV、跳过原因；
  2) Prompt 强约束：用中文 / 不超过 200 字 / 不许编造代码 / 必须基于事实；
  3) 不送任何 PII。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alphaforge.runtime.paper import PaperRunResult


SYSTEM_PROMPT = (
    "你是 A 股量化策略的运行说明助手。"
    "你只能基于用户提供的事实写解释，不可虚构股票代码、价格或宏观信息；"
    "用简体中文，控制在 200 字以内，分两段："
    "（1）今日组合发生了什么变动（买/卖几只、调仓 vs 不调仓）；"
    "（2）当前组合的特征与净值简评。"
    "禁止给投资建议、禁止预测涨跌，只描述事实和策略逻辑。"
)


def build_payload(
    result: "PaperRunResult",
    *,
    strategy_name: str,
    strategy_desc: str = "",
    rebalance_freq: str = "",
    positions_snapshot: list[dict] | None = None,
    benchmark: str | None = None,
) -> dict[str, Any]:
    """把 PaperRunResult 浓缩成 LLM 友好的 payload。"""

    def _trim(rows: list[dict], k: int = 15) -> list[dict]:
        return [
            {"ts_code": r["ts_code"], "qty": r["qty"], "ref_price": round(r["ref_price"], 3)}
            for r in rows[:k]
        ]

    return {
        "date": str(result.date.date()),
        "strategy": strategy_name,
        "strategy_description": strategy_desc,
        "rebalance_freq": rebalance_freq,
        "rebalance_today": bool(result.rebalance),
        "buys": _trim(result.buys),
        "sells": _trim(result.sells),
        "n_buys": len(result.buys),
        "n_sells": len(result.sells),
        "skipped": [
            {"ts_code": x["ts_code"], "reason": x["reason"]}
            for x in (result.skipped or [])[:10]
        ],
        "settled": result.settled or {},
        "nav": result.nav or {},
        "positions": [
            {"ts_code": p.get("ts_code"), "qty": int(p.get("qty", 0))}
            for p in (positions_snapshot or [])[:20]
        ],
        "benchmark": benchmark,
        "next_trade_day": str(result.next_trade_day.date()) if result.next_trade_day else None,
    }


def build_prompt(payload: dict[str, Any]) -> str:
    """生成给 LLM 的 user prompt。"""
    import json as _json
    return (
        "下面是今日 A 股 paper-trading 的事实数据（JSON）：\n\n"
        f"```json\n{_json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        "请按系统指令的格式与字数要求生成中文解释。"
    )


def fallback_text(payload: dict[str, Any]) -> str:
    """LLM 不可用时的兜底文本。"""
    nav = payload.get("nav") or {}
    parts = [
        f"今日（{payload.get('date')}）{'有调仓' if payload.get('rebalance_today') else '未调仓'}：",
        f"买 {payload.get('n_buys', 0)} 只 / 卖 {payload.get('n_sells', 0)} 只。",
        f"持仓 {nav.get('n_positions', 0)} 只，净值 {nav.get('equity', 0):,.2f}。",
    ]
    return " ".join(parts)

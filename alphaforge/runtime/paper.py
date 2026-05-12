"""PaperRunner — 单日 paper-trading 跑（信号生成器）。

一次 run(today) 做四件事：
  1) 校验：today 是交易日 + 当日数据已就绪（否则拒绝跑）
  2) 撮合：把今天 executed=0 的信号按"今天开盘价"撮合并落库（T-1 决策今天执行）
  3) 决策：如果 today 是调仓日，调 strategy.select(today, ctx) → 转为 buy/sell 信号，
           写入 signals 表（date = 下一交易日，executed=0），等明天开盘撮合
  4) 快照：用 today 收盘价计算 NAV / 持仓快照写入 nav 表

设计要点：
- 与 backtest.py 共用 100% 的约束（T+1 / 涨跌停 / 停牌）和成本模型 CostModel
- "调仓日" 判定使用 PaperState meta 里的 last_rebalance_date（而不是单跑实例的 prev）
- "T+1 锁" 在 settle 阶段加锁，PaperRunner 不直接维护，状态全在 SQLite
- 任何写都在 PaperState 的事务里完成；崩溃后再跑同一天会用 INSERT OR REPLACE 幂等覆盖
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from alphaforge.data.api import CalendarAPI, DataAPI, UniverseAPI
from alphaforge.infra.logger import logger
from alphaforge.runtime.backtest import _is_rebalance_day, _round_lot
from alphaforge.runtime.constraints import can_buy, can_sell
from alphaforge.runtime.costs import CostModel
from alphaforge.runtime.paper_state import PaperState
from alphaforge.strategy.base import BaseStrategy, StrategyContext


@dataclass
class PaperRunResult:
    """单日跑的结果摘要 — CLI 与 scheduler 都用这个结构展示/通知。"""

    date: pd.Timestamp
    settled: dict = field(default_factory=dict)              # settle 概况
    rebalance: bool = False
    targets: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    buys: list[dict] = field(default_factory=list)
    sells: list[dict] = field(default_factory=list)
    nav: dict = field(default_factory=dict)                  # {cash, market_value, equity, n_positions}
    skipped: list[dict] = field(default_factory=list)        # 因约束被跳过的目标 [{ts_code, reason}]
    next_trade_day: pd.Timestamp | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "date": str(self.date.date()),
            "settled": self.settled,
            "rebalance": self.rebalance,
            "n_buys": len(self.buys),
            "n_sells": len(self.sells),
            "nav": self.nav,
            "skipped": self.skipped,
            "next_trade_day": str(self.next_trade_day.date()) if self.next_trade_day else None,
        }
        return out


class PaperRunner:
    """单日 paper-trading 跑。无状态，每次 run() 都会从 SQLite 加载账户。"""

    def __init__(
        self,
        strategy: BaseStrategy,
        data: DataAPI,
        universe: UniverseAPI,
        calendar: CalendarAPI,
        state: PaperState,
        *,
        cost: CostModel | None = None,
    ) -> None:
        self.strategy = strategy
        self.data = data
        self.universe = universe
        self.calendar = calendar
        self.state = state
        self.cost = cost or CostModel()

    # ---------- 校验 ----------

    def _ensure_data_ready(self, date: pd.Timestamp) -> pd.DataFrame:
        """校验：当天必须是交易日 + 必须有日线数据。"""
        if not self.calendar.is_trade_day(date):
            raise RuntimeError(
                f"{date.date()} is not a trade day. Paper run aborted."
            )
        try:
            day_table = self.data.daily_table(date)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Failed to load daily_table({date.date()}): {e}. "
                f"Run `alphaforge data update` first."
            ) from e
        if day_table is None or day_table.empty:
            raise RuntimeError(
                f"No daily data for {date.date()}. Refusing to run "
                f"(数据未就绪，请先 `alphaforge data update`)."
            )
        return day_table

    # ---------- 决策 → 信号 ----------

    def _is_rebalance_today(self, date: pd.Timestamp) -> bool:
        meta = self.state.meta()
        last_str = meta.get("last_rebalance_date")
        last = pd.Timestamp(last_str) if last_str else None
        return _is_rebalance_day(date, last, self.strategy.rebalance)

    def _generate_signals(
        self,
        decision_date: pd.Timestamp,
        execute_date: pd.Timestamp,
        ctx: StrategyContext,
        account,
    ) -> tuple[pd.DataFrame, list[dict], list[dict], list[dict]]:
        """决策日 == decision_date；预计将在 execute_date 开盘撮合。

        用 decision_date 当日的 close 估值（PIT 安全），
        用 decision_date 的 close 作为 ref_price（实际成交价 = 明日 open，会在 settle 时覆盖）。
        """
        skipped: list[dict] = []

        try:
            targets = self.strategy.select(decision_date, ctx)
        except Exception:
            logger.exception(f"strategy.select({decision_date.date()}) failed")
            return pd.DataFrame(), [], [], skipped

        if targets is None or targets.empty:
            return pd.DataFrame(), [], [], skipped
        if "ts_code" not in targets.columns or "weight" not in targets.columns:
            raise ValueError("strategy.select() must return columns ['ts_code', 'weight']")

        # 权重归一化
        w = targets["weight"].astype(float)
        if w.sum() > 1.0001:
            targets = targets.assign(weight=w / w.sum())

        today_table = self.data.daily_table(decision_date)
        closes = today_table.get("close", pd.Series(dtype=float))

        # 当前 equity（用今日 close）
        equity = account.equity(closes.reindex(account.positions.keys()).fillna(0.0))
        tgt_w = targets.set_index("ts_code")["weight"].astype(float)
        tgt_value = tgt_w * equity

        buys: list[dict] = []
        sells: list[dict] = []

        # 1) 卖
        for code, pos in list(account.positions.items()):
            if pos.qty == 0:
                continue
            target_qty = 0
            if code in tgt_value.index and code in closes.index and pd.notna(closes[code]):
                px = float(closes[code])
                target_qty = _round_lot(int(tgt_value[code] // px)) if px > 0 else 0
            delta = target_qty - pos.qty
            if delta < 0:
                sell_target = -delta
                # T+1 锁仅看 decision_date == buy_date 那一档；execute_date 时此锁已失效。
                # 真实运行中 decision_date == today（15:30），如果今天有买入，T+1 锁=今天那笔买入，
                # 而 execute_date = 明天，明天可以卖。所以 paper 这里允许卖出全部持仓。
                # 但仍保留校验：execute_date 当日是否会停牌/跌停，由 settle 阶段最终判定。
                ok, reason, sellable = can_sell(
                    self.data, decision_date, code, pos.qty, locked_qty=0
                )
                sell_qty = _round_lot(min(sell_target, sellable)) if ok else 0
                if sell_qty > 0:
                    ref_px = float(closes.get(code, 0.0))
                    fill_px = self.cost.fill_price(ref_px, "sell")
                    fees = self.cost.fees("sell", sell_qty * fill_px)
                    sells.append({
                        "ts_code": code, "qty": int(sell_qty),
                        "ref_price": ref_px, "fees": float(fees),
                        "reason": "rebalance",
                    })
                else:
                    skipped.append({"ts_code": code, "side": "sell", "reason": reason or "n/a"})

        # 2) 买
        buy_orders: list[tuple[str, int, float]] = []
        for code, tgt_v in tgt_value.items():
            if code not in closes.index or pd.isna(closes[code]) or closes[code] <= 0:
                skipped.append({"ts_code": code, "side": "buy", "reason": "no_close_price"})
                continue
            ref_px = float(closes[code])
            cur_qty = account.positions.get(code).qty if code in account.positions else 0
            target_qty = _round_lot(int(tgt_v // ref_px))
            delta = target_qty - cur_qty
            if delta > 0:
                ok, reason = can_buy(self.data, decision_date, code)
                if not ok:
                    skipped.append({"ts_code": code, "side": "buy", "reason": reason})
                    continue
                buy_orders.append((code, delta, ref_px))

        # 现金：当前 cash + 今天卖的预估回笼
        cash_after_sells = account.cash + sum(
            s["qty"] * s["ref_price"] - s["fees"] for s in sells
        )
        if buy_orders:
            cash_needed = sum(
                qty * self.cost.fill_price(px, "buy") * (1 + self.cost.commission)
                for _, qty, px in buy_orders
            )
            scale = 1.0
            if cash_needed > cash_after_sells:
                scale = max(cash_after_sells / cash_needed * 0.99, 0.0)
            for code, qty, ref_px in buy_orders:
                final_qty = _round_lot(int(qty * scale))
                if final_qty <= 0:
                    continue
                fill_px = self.cost.fill_price(ref_px, "buy")
                fees = self.cost.fees("buy", final_qty * fill_px)
                buys.append({
                    "ts_code": code, "qty": int(final_qty),
                    "ref_price": ref_px, "fees": float(fees),
                    "reason": "rebalance",
                })

        return targets, buys, sells, skipped

    # ---------- 主入口 ----------

    def run(self, today: pd.Timestamp) -> PaperRunResult:
        """执行 today 的一次 paper 跑。

        若 today 不是交易日 / 数据未就绪 → 抛 RuntimeError。
        """
        today = pd.Timestamp(today).normalize()
        result = PaperRunResult(date=today)

        # 0) 校验
        day_table = self._ensure_data_ready(today)
        opens = day_table.get("open", pd.Series(dtype=float))
        closes = day_table.get("close", pd.Series(dtype=float))

        # 1) 撮合 today 的 pending 信号（昨天决策、今天 open 撮合）
        # PaperState.settle 的 actual_prices 直接用作成交价。这里给"开盘价"，
        # 不在外部叠加滑点（buy/sell 用同一价 → 与 backtest 引擎按 open 撮合的语义一致）。
        actual_prices = {
            code: float(px) for code, px in opens.items() if pd.notna(px)
        } if opens is not None and not opens.empty else None
        result.settled = self.state.settle(today, actual_prices=actual_prices)

        # 2) 加载账户（settle 完之后的状态）
        ctx = StrategyContext(
            data=self.data,
            universe=self.universe,
            calendar=self.calendar,
            params=dict(getattr(self.strategy, "params", {}) or {}),
        )
        if not hasattr(self.strategy, "params") or self.strategy.params is None:
            self.strategy.params = ctx.params
        try:
            self.strategy.setup(ctx)
        except Exception:
            logger.exception("strategy.setup failed; continuing without setup")

        account = self.state.load_account()

        # 3) 决策（仅在调仓日）
        is_reb = self._is_rebalance_today(today)
        result.rebalance = is_reb
        next_day = None
        if is_reb:
            try:
                next_day = self.calendar.next(today, n=1)
            except IndexError:
                next_day = None
            if next_day is None:
                logger.warning(
                    f"No next trade day after {today.date()}; cannot dispatch new signals."
                )
            else:
                targets, buys, sells, skipped = self._generate_signals(
                    decision_date=today,
                    execute_date=next_day,
                    ctx=ctx,
                    account=account,
                )
                result.targets = targets
                result.buys = buys
                result.sells = sells
                result.skipped = skipped
                result.next_trade_day = next_day

                # 写入 next_day 的待执行信号
                if buys or sells:
                    self.state.write_signals(next_day, buys, sells)

                # 更新调仓游标
                with self.state._conn() as cx:
                    cx.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        ("last_rebalance_date", today.strftime("%Y-%m-%d")),
                    )

        # 4) NAV 快照（用 today close）
        # settle 之后账户状态已变；重新加载一遍最新持仓
        account = self.state.load_account()
        mv = account.market_value(closes) if closes is not None else 0.0
        n_pos = sum(1 for p in account.positions.values() if p.qty > 0)
        self.state.append_nav(today, account.cash, mv, n_pos)
        result.nav = {
            "cash": float(account.cash),
            "market_value": float(mv),
            "equity": float(account.cash + mv),
            "n_positions": int(n_pos),
        }

        try:
            self.strategy.teardown(ctx)
        except Exception:
            logger.exception("strategy.teardown failed (ignored)")

        return result


# ---------- 渲染 ----------

def format_run_result(res: PaperRunResult, account_name: str = "default") -> str:
    """把 PaperRunResult 渲染为可读文本（CLI 输出 & Bot 通知都用这个）。"""

    lines: list[str] = []
    lines.append(f"=== Paper run @ {res.date.date()} (account={account_name}) ===")

    # settle 概况
    s = res.settled or {}
    lines.append(
        f"[settle] today's pending signals filled: "
        f"buy={s.get('n_buy', 0)}, sell={s.get('n_sell', 0)}, "
        f"cash_delta={s.get('cash_delta', 0):.2f}"
    )

    # 决策
    if res.rebalance:
        lines.append(
            f"[decision] rebalance day. "
            f"{len(res.buys)} buys / {len(res.sells)} sells dispatched for "
            f"{res.next_trade_day.date() if res.next_trade_day else '(N/A)'} open."
        )
        if res.buys:
            lines.append("  BUY:")
            for b in res.buys[:30]:
                lines.append(
                    f"    {b['ts_code']:>10}  qty={b['qty']:>6}  ref={b['ref_price']:.3f}  fees={b['fees']:.2f}"
                )
            if len(res.buys) > 30:
                lines.append(f"    ... and {len(res.buys)-30} more")
        if res.sells:
            lines.append("  SELL:")
            for s_ in res.sells[:30]:
                lines.append(
                    f"    {s_['ts_code']:>10}  qty={s_['qty']:>6}  ref={s_['ref_price']:.3f}  fees={s_['fees']:.2f}"
                )
            if len(res.sells) > 30:
                lines.append(f"    ... and {len(res.sells)-30} more")
        if res.skipped:
            lines.append(f"  SKIPPED ({len(res.skipped)}): "
                         + ", ".join(f"{x['ts_code']}({x['reason']})" for x in res.skipped[:10])
                         + (" ..." if len(res.skipped) > 10 else ""))
    else:
        lines.append("[decision] not a rebalance day; no new signals.")

    # NAV
    n = res.nav or {}
    lines.append(
        f"[nav] cash={n.get('cash', 0):,.2f}  mv={n.get('market_value', 0):,.2f}  "
        f"equity={n.get('equity', 0):,.2f}  positions={n.get('n_positions', 0)}"
    )

    # 占位：策略解释 — M5 才接 LLM；目前留空白行让 paper bot 渲染
    lines.append("[explain] (auto-explanation slot — wired up in M5)")
    return "\n".join(lines)

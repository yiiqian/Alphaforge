"""自研轻量回测引擎 — A 股约束 + 目标权重撮合。

核心循环：
    for date in trade_days:
        if 是调仓日（按 strategy.rebalance）:
            targets = strategy.select(date, ctx)            # 用收盘前可见的数据
            把 targets 排进"下一交易日开盘"待执行队列
        if 有待执行队列 and date 是开盘执行日:
            按 open 价撮合（受涨跌停 / 停牌 / 现金 / T+1 约束）
        日内 mark-to-market（按 close）

输出：
    runs/<run_id>/
        nav.parquet         # 每日净值
        positions.parquet   # 每日持仓
        trades.parquet      # 每笔成交
        metrics.json        # 标准指标
        config.snapshot.yaml
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from alphaforge.data.api import CalendarAPI, DataAPI, UniverseAPI
from alphaforge.infra.logger import logger
from alphaforge.runtime.constraints import T1Lock, can_buy, can_sell
from alphaforge.runtime.costs import CostModel
from alphaforge.strategy.base import BaseStrategy, StrategyContext

LOT = 100  # A 股一手 = 100 股


def _save_table(df: pd.DataFrame, base_path: Path) -> None:
    """优先保存为 parquet（pyarrow 可用时），缺依赖则降级为 csv。"""
    try:
        df.to_parquet(base_path.with_suffix(".parquet"), index=False)
    except Exception:
        df.to_csv(base_path.with_suffix(".csv"), index=False)


def _try_read_table(base_path: Path) -> pd.DataFrame:
    pq = base_path.with_suffix(".parquet")
    csv = base_path.with_suffix(".csv")
    if pq.exists():
        return pd.read_parquet(pq)
    if csv.exists():
        return pd.read_csv(csv)
    raise FileNotFoundError(base_path)


@dataclass
class BacktestConfig:
    start: pd.Timestamp
    end: pd.Timestamp
    init_cash: float = 1_000_000.0
    benchmark: str | None = None
    cost: CostModel = field(default_factory=CostModel)


@dataclass
class Position:
    qty: int = 0
    avg_cost: float = 0.0


@dataclass
class Trade:
    date: pd.Timestamp
    ts_code: str
    side: str          # buy / sell
    qty: int
    price: float       # 成交均价（含滑点）
    amount: float      # qty * price
    fees: float
    reason: str = ""


@dataclass
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    t1: T1Lock = field(default_factory=T1Lock)

    def market_value(self, prices: pd.Series) -> float:
        mv = 0.0
        for code, pos in self.positions.items():
            if pos.qty == 0:
                continue
            px = prices.get(code, np.nan)
            if not np.isnan(px):
                mv += pos.qty * px
        return mv

    def equity(self, prices: pd.Series) -> float:
        return self.cash + self.market_value(prices)


def _is_rebalance_day(today: pd.Timestamp, prev: pd.Timestamp | None, freq: str) -> bool:
    """
    freq:
      D — 每个交易日
      W — 周首次交易日（与上一交易日不同 ISO 周）
      M — 月首次交易日
      Q — 季首次交易日
    """
    if prev is None:
        return True
    if freq == "D":
        return True
    if freq == "W":
        return today.isocalendar().week != prev.isocalendar().week or today.year != prev.year
    if freq == "M":
        return today.month != prev.month or today.year != prev.year
    if freq == "Q":
        return ((today.month - 1) // 3) != ((prev.month - 1) // 3) or today.year != prev.year
    raise ValueError(f"Unknown rebalance freq: {freq}")


def _round_lot(qty: int) -> int:
    """A 股一手 = 100 股，取整到 LOT 的整数倍（向下）。"""
    return (qty // LOT) * LOT


def run_backtest(
    strategy: BaseStrategy,
    data: DataAPI,
    universe: UniverseAPI,
    calendar: CalendarAPI,
    cfg: BacktestConfig,
    out_dir: Path,
    *,
    config_snapshot: dict | None = None,
) -> dict:
    """运行回测。out_dir 会被创建（不存在的话）。返回标准 metrics dict。"""

    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = StrategyContext(
        data=data,
        universe=universe,
        calendar=calendar,
        params=dict(getattr(strategy, "params", {}) or {}),
    )
    # 让策略实例上能直接访问 self.params（约定）
    if not hasattr(strategy, "params") or strategy.params is None:
        strategy.params = ctx.params

    strategy.setup(ctx)

    days = calendar.trade_days(cfg.start, cfg.end)
    if len(days) < 2:
        raise ValueError("Backtest period must cover at least 2 trade days.")

    account = Account(cash=cfg.init_cash)
    trades: list[Trade] = []
    nav_records: list[dict] = []
    pos_records: list[dict] = []

    pending_targets: pd.DataFrame | None = None  # 下一开盘日要执行的目标权重
    prev_rebalance_day: pd.Timestamp | None = None

    benchmark_prices: dict[pd.Timestamp, float] = {}

    bench_panel = None
    if cfg.benchmark:
        # 基准：直接读 bars。如果 universe 里没有基准代码，引擎仍会忽略并继续。
        try:
            bdf = data.bars(codes=[cfg.benchmark], start=cfg.start, end=cfg.end, fields=["close"])
            if not bdf.empty:
                bench_panel = bdf.set_index(["trade_date", "ts_code"])["close"].unstack()
        except Exception:
            bench_panel = None

    for i, date in enumerate(days):
        # 1) 执行待执行的目标权重单（在今日 open）
        if pending_targets is not None and not pending_targets.empty:
            today_table = data.daily_table(date)
            opens = today_table.get("open", pd.Series(dtype=float))
            closes = today_table.get("close", pd.Series(dtype=float))

            # 已有持仓的 close 估值
            ref_prices = closes.combine_first(opens) if not opens.empty else closes

            equity = account.equity(ref_prices.reindex(account.positions.keys()).fillna(0.0))

            # 预计算目标权重 → 目标市值
            tgt_w = pending_targets.set_index("ts_code")["weight"].astype(float)
            tgt_value = tgt_w * equity

            # 统一按 open 价撮合（带滑点）
            # 1a) 先卖（释放现金）
            held = list(account.positions.keys())
            for code in held:
                pos = account.positions.get(code)
                if pos is None or pos.qty == 0:
                    continue
                target_qty_raw = 0
                if code in tgt_value.index and code in opens.index and not math.isnan(opens[code]):
                    px = opens[code]
                    target_qty_raw = _round_lot(int(tgt_value[code] // px)) if px > 0 else 0
                delta = target_qty_raw - pos.qty
                if delta < 0:
                    sell_qty_target = -delta
                    locked = account.t1.locked_qty(code, date)
                    ok, reason, sellable = can_sell(data, date, code, pos.qty, locked)
                    sell_qty = min(sell_qty_target, sellable) if ok else 0
                    sell_qty = _round_lot(sell_qty)
                    if sell_qty > 0 and code in opens.index:
                        ref_px = float(opens[code])
                        fill_px = cfg.cost.fill_price(ref_px, "sell")
                        amount = sell_qty * fill_px
                        fees = cfg.cost.fees("sell", amount)
                        account.cash += amount - fees
                        pos.qty -= sell_qty
                        trades.append(Trade(
                            date=date, ts_code=code, side="sell",
                            qty=sell_qty, price=fill_px, amount=amount,
                            fees=fees, reason=reason or "rebalance",
                        ))
                        if pos.qty == 0:
                            account.positions.pop(code, None)
                    elif sell_qty_target > 0 and not ok:
                        logger.debug(f"{date.date()} cannot sell {code} ({reason})")

            # 1b) 再买
            buy_orders: list[tuple[str, int, float]] = []  # code, qty, ref_px
            for code, tgt_v in tgt_value.items():
                if code not in opens.index or math.isnan(opens[code]) or opens[code] <= 0:
                    continue
                ref_px = float(opens[code])
                cur_qty = account.positions.get(code, Position()).qty
                target_qty_raw = _round_lot(int(tgt_v // ref_px))
                delta = target_qty_raw - cur_qty
                if delta > 0:
                    ok, reason = can_buy(data, date, code)
                    if not ok:
                        logger.debug(f"{date.date()} cannot buy {code} ({reason})")
                        continue
                    buy_orders.append((code, delta, ref_px))

            # 现金不足时按比例缩放买单
            if buy_orders:
                cash_needed = sum(
                    qty * cfg.cost.fill_price(px, "buy") * (1 + cfg.cost.commission)
                    for _, qty, px in buy_orders
                )
                scale = 1.0
                if cash_needed > account.cash:
                    scale = max(account.cash / cash_needed * 0.99, 0.0)
                for code, qty, ref_px in buy_orders:
                    final_qty = _round_lot(int(qty * scale))
                    if final_qty <= 0:
                        continue
                    fill_px = cfg.cost.fill_price(ref_px, "buy")
                    amount = final_qty * fill_px
                    fees = cfg.cost.fees("buy", amount)
                    cash_required = amount + fees
                    if cash_required > account.cash:
                        # 再降一档
                        max_qty = _round_lot(
                            int((account.cash * 0.999 - cfg.cost.min_commission) / fill_px)
                        )
                        final_qty = max(0, max_qty)
                        if final_qty <= 0:
                            continue
                        amount = final_qty * fill_px
                        fees = cfg.cost.fees("buy", amount)
                    account.cash -= amount + fees
                    pos = account.positions.setdefault(code, Position())
                    new_total = pos.qty + final_qty
                    pos.avg_cost = (
                        (pos.avg_cost * pos.qty + fill_px * final_qty) / new_total
                        if new_total > 0 else 0.0
                    )
                    pos.qty = new_total
                    account.t1.add(code, date, final_qty)
                    trades.append(Trade(
                        date=date, ts_code=code, side="buy",
                        qty=final_qty, price=fill_px, amount=amount,
                        fees=fees, reason="rebalance",
                    ))

            pending_targets = None

        # 2) 收盘后：是否调仓日？
        is_reb = _is_rebalance_day(date, prev_rebalance_day, strategy.rebalance)
        if is_reb:
            try:
                targets = strategy.select(date, ctx)
            except Exception as e:
                logger.exception(f"strategy.select() failed on {date.date()}: {e}")
                targets = pd.DataFrame(columns=["ts_code", "weight"])

            if targets is not None and not targets.empty:
                if "ts_code" not in targets.columns or "weight" not in targets.columns:
                    raise ValueError("strategy.select() must return columns ['ts_code', 'weight']")
                # 权重归一化到 ≤ 1
                w = targets["weight"].astype(float)
                if w.sum() > 1.0001:
                    targets = targets.assign(weight=w / w.sum())
                pending_targets = targets
            prev_rebalance_day = date

        # 3) 收盘 mark-to-market
        closes = data.daily_table(date).get("close", pd.Series(dtype=float))
        equity = account.equity(closes.reindex(account.positions.keys()).fillna(0.0))
        nav_records.append({
            "trade_date": date,
            "cash": account.cash,
            "market_value": account.market_value(closes.reindex(account.positions.keys()).fillna(0.0)),
            "equity": equity,
            "n_positions": sum(1 for p in account.positions.values() if p.qty > 0),
        })
        for code, pos in account.positions.items():
            if pos.qty == 0:
                continue
            px = float(closes.get(code, np.nan)) if code in closes.index else float("nan")
            pos_records.append({
                "trade_date": date, "ts_code": code,
                "qty": pos.qty, "avg_cost": pos.avg_cost,
                "close": px, "value": pos.qty * px if not math.isnan(px) else 0.0,
            })

        # 4) 释放 T+1 锁定（第二天就能卖了）
        if i + 1 < len(days):
            account.t1.cleanup(before=days[i + 1])

        # 5) 基准价
        if bench_panel is not None and date in bench_panel.index:
            benchmark_prices[date] = float(bench_panel.loc[date, cfg.benchmark])

    strategy.teardown(ctx)

    # ---- 输出 ----
    nav_df = pd.DataFrame(nav_records)
    nav_df["nav"] = nav_df["equity"] / cfg.init_cash
    nav_df["ret"] = nav_df["nav"].pct_change().fillna(0.0)

    if benchmark_prices:
        bench_s = pd.Series(benchmark_prices).reindex(nav_df["trade_date"])
        bench_s = bench_s.ffill()
        nav_df["bench_nav"] = (bench_s / bench_s.iloc[0]).values
        nav_df["bench_ret"] = nav_df["bench_nav"].pct_change().fillna(0.0)
        nav_df["excess_ret"] = nav_df["ret"] - nav_df["bench_ret"]

    pos_df = pd.DataFrame(pos_records)
    trades_df = pd.DataFrame([asdict(t) for t in trades])

    _save_table(nav_df, out_dir / "nav")
    _save_table(pos_df, out_dir / "positions")
    _save_table(trades_df, out_dir / "trades")

    metrics = compute_metrics(nav_df)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    if config_snapshot:
        (out_dir / "config.snapshot.yaml").write_text(
            yaml.safe_dump(config_snapshot, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    return metrics


def compute_metrics(nav_df: pd.DataFrame) -> dict:
    """标准绩效指标。"""
    if nav_df.empty:
        return {}
    nav = nav_df["nav"].astype(float)
    ret = nav_df["ret"].astype(float)
    n = len(nav)
    total_ret = float(nav.iloc[-1] - 1.0)
    ann_ret = float((1 + total_ret) ** (252 / max(n, 1)) - 1)
    ann_vol = float(ret.std(ddof=0) * np.sqrt(252))
    sharpe = float(ann_ret / ann_vol) if ann_vol > 0 else 0.0
    cummax = nav.cummax()
    drawdown = (nav - cummax) / cummax
    max_dd = float(drawdown.min())

    # 胜率：日度收益 > 0 的占比（粗略指标）
    win_rate = float((ret > 0).mean())

    out = {
        "n_days": n,
        "total_return": total_ret,
        "annual_return": ann_ret,
        "annual_volatility": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate_daily": win_rate,
        "final_nav": float(nav.iloc[-1]),
    }
    if "bench_nav" in nav_df.columns:
        bench = nav_df["bench_nav"].astype(float)
        bench_ret = nav_df["bench_ret"].astype(float)
        bench_total = float(bench.iloc[-1] - 1.0)
        bench_ann = float((1 + bench_total) ** (252 / max(n, 1)) - 1)
        excess = nav_df["excess_ret"].astype(float)
        te = float(excess.std(ddof=0) * np.sqrt(252))
        ir = float((excess.mean() * 252) / te) if te > 0 else 0.0
        out.update({
            "benchmark_total_return": bench_total,
            "benchmark_annual_return": bench_ann,
            "alpha_total": total_ret - bench_total,
            "tracking_error": te,
            "information_ratio": ir,
        })
    return out

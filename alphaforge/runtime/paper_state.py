"""PaperState — 纸面交易账户状态机 + SQLite 持久化。

每个 (account_name) 对应一个 SQLite 文件，结构：
  meta(key, value)               配置元信息：created_at / init_cash / strategy_name 等
  positions(ts_code, qty, avg_cost, last_update_date)
  t1_locks(ts_code, buy_date, qty)
  signals(date, ts_code, side, qty, ref_price, fees, reason, executed)
  nav(date, cash, market_value, equity, n_positions, daily_ret)

设计要点：
- 一切金额用 REAL（够用，不引入 Decimal 复杂度）
- 日期统一用 ISO 字符串 'YYYY-MM-DD' 入库；in-memory 用 pd.Timestamp
- T+1 锁：当日 add，T+1 自动 cleanup
- 提供 `apply_signals()` 接口，把 PaperRunner 算出的买卖清单合并进账户
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from alphaforge.runtime.backtest import Position
from alphaforge.runtime.constraints import T1Lock


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    ts_code          TEXT PRIMARY KEY,
    qty              INTEGER NOT NULL,
    avg_cost         REAL    NOT NULL,
    last_update_date TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS t1_locks (
    ts_code   TEXT NOT NULL,
    buy_date  TEXT NOT NULL,
    qty       INTEGER NOT NULL,
    PRIMARY KEY (ts_code, buy_date)
);

CREATE TABLE IF NOT EXISTS signals (
    date        TEXT NOT NULL,
    ts_code     TEXT NOT NULL,
    side        TEXT NOT NULL,    -- buy / sell
    qty         INTEGER NOT NULL,
    ref_price   REAL    NOT NULL,
    fees        REAL    NOT NULL,
    reason      TEXT,
    executed    INTEGER DEFAULT 0,    -- 0=信号 1=已撮合（次日 settle 后置 1）
    PRIMARY KEY (date, ts_code, side)
);

CREATE TABLE IF NOT EXISTS nav (
    date         TEXT PRIMARY KEY,
    cash         REAL    NOT NULL,
    market_value REAL    NOT NULL,
    equity       REAL    NOT NULL,
    n_positions  INTEGER NOT NULL,
    daily_ret    REAL    NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
CREATE INDEX IF NOT EXISTS idx_nav_date     ON nav(date);
"""


@dataclass
class PaperAccount:
    """运行时账户视图（从 SQLite 加载）。"""
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    t1: T1Lock = field(default_factory=T1Lock)

    def market_value(self, prices: pd.Series) -> float:
        mv = 0.0
        for code, pos in self.positions.items():
            if pos.qty == 0:
                continue
            px = prices.get(code) if hasattr(prices, "get") else None
            if px is not None and pd.notna(px):
                mv += pos.qty * px
        return mv

    def equity(self, prices: pd.Series) -> float:
        return self.cash + self.market_value(prices)


class PaperState:
    """SQLite 包装。所有写操作都在事务里。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as cx:
            cx.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        cx = sqlite3.connect(str(self.db_path))
        cx.row_factory = sqlite3.Row
        try:
            yield cx
            cx.commit()
        except Exception:
            cx.rollback()
            raise
        finally:
            cx.close()

    # ---------- meta ----------

    def init_account(self, init_cash: float, strategy: str, account: str,
                     **extra) -> None:
        with self._conn() as cx:
            existing = cx.execute("SELECT value FROM meta WHERE key='init_cash'").fetchone()
            if existing:
                return
            data = {
                "init_cash": str(init_cash),
                "strategy": strategy,
                "account": account,
                "created_at": pd.Timestamp.now().isoformat(timespec="seconds"),
                **{k: json.dumps(v) for k, v in extra.items()},
            }
            cx.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                list(data.items()),
            )

    def is_initialized(self) -> bool:
        with self._conn() as cx:
            row = cx.execute("SELECT 1 FROM meta WHERE key='init_cash'").fetchone()
        return row is not None

    def meta(self) -> dict[str, str]:
        with self._conn() as cx:
            rows = cx.execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def get_init_cash(self) -> float:
        return float(self.meta().get("init_cash", "0"))

    # ---------- account view ----------

    def load_account(self) -> PaperAccount:
        """从 SQLite 重建运行时账户。"""
        with self._conn() as cx:
            # cash = init_cash - 累积签名净支出（buy = -amount-fees, sell = +amount-fees）
            init_cash = self.get_init_cash()
            row = cx.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN side='buy'  THEN -(qty*ref_price + fees) END), 0) AS buy_flow,
                    COALESCE(SUM(CASE WHEN side='sell' THEN (qty*ref_price - fees) END), 0) AS sell_flow
                FROM signals
                WHERE executed = 1
            """).fetchone()
            cash = init_cash + (row["buy_flow"] or 0) + (row["sell_flow"] or 0)

            positions: dict[str, Position] = {}
            for r in cx.execute("SELECT ts_code, qty, avg_cost FROM positions WHERE qty > 0"):
                positions[r["ts_code"]] = Position(qty=int(r["qty"]), avg_cost=float(r["avg_cost"]))

            t1 = T1Lock()
            for r in cx.execute("SELECT ts_code, buy_date, qty FROM t1_locks"):
                t1.add(r["ts_code"], pd.Timestamp(r["buy_date"]), int(r["qty"]))

        return PaperAccount(cash=cash, positions=positions, t1=t1)

    # ---------- 写信号 ----------

    def write_signals(self, date: pd.Timestamp,
                      buys: list[dict], sells: list[dict]) -> None:
        """写入今天的调仓信号（executed=0）。

        每条 dict 形如 {ts_code, qty, ref_price, fees, reason}.
        """
        ds = date.strftime("%Y-%m-%d")
        with self._conn() as cx:
            # 同一日期重复跑 → 先清空当天信号
            cx.execute("DELETE FROM signals WHERE date=? AND executed=0", (ds,))
            rows = []
            for s in buys:
                rows.append((ds, s["ts_code"], "buy", int(s["qty"]),
                             float(s["ref_price"]), float(s["fees"]),
                             s.get("reason", ""), 0))
            for s in sells:
                rows.append((ds, s["ts_code"], "sell", int(s["qty"]),
                             float(s["ref_price"]), float(s["fees"]),
                             s.get("reason", ""), 0))
            if rows:
                cx.executemany(
                    "INSERT OR REPLACE INTO signals(date, ts_code, side, qty, ref_price, fees, reason, executed) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )

    # ---------- 撮合（settle）----------

    def settle(self, date: pd.Timestamp,
               actual_prices: dict[str, float] | None = None) -> dict:
        """把 date 当天 executed=0 的信号按"实际价"撮合。

        actual_prices: {ts_code: price}. 没传则用信号里的 ref_price。
        返回 dict 概况：{n_buy, n_sell, cash_delta, ...}
        """
        ds = date.strftime("%Y-%m-%d")
        n_buy = n_sell = 0
        cash_delta = 0.0

        with self._conn() as cx:
            sigs = cx.execute(
                "SELECT * FROM signals WHERE date=? AND executed=0", (ds,)
            ).fetchall()

            for sig in sigs:
                code = sig["ts_code"]
                side = sig["side"]
                qty = int(sig["qty"])
                price = float((actual_prices or {}).get(code, sig["ref_price"]))
                fees = float(sig["fees"])
                amount = qty * price

                if side == "buy":
                    # 更新 positions（avg_cost 加权）
                    row = cx.execute("SELECT qty, avg_cost FROM positions WHERE ts_code=?", (code,)).fetchone()
                    cur_qty = int(row["qty"]) if row else 0
                    cur_avg = float(row["avg_cost"]) if row else 0.0
                    new_qty = cur_qty + qty
                    new_avg = ((cur_avg * cur_qty) + (price * qty)) / new_qty if new_qty else 0.0
                    cx.execute(
                        "INSERT OR REPLACE INTO positions(ts_code, qty, avg_cost, last_update_date) "
                        "VALUES (?, ?, ?, ?)",
                        (code, new_qty, new_avg, ds),
                    )
                    # T+1 锁
                    cx.execute(
                        "INSERT OR REPLACE INTO t1_locks(ts_code, buy_date, qty) VALUES (?, ?, ?)",
                        (code, ds, qty),
                    )
                    cash_delta -= amount + fees
                    n_buy += 1
                elif side == "sell":
                    row = cx.execute("SELECT qty, avg_cost FROM positions WHERE ts_code=?", (code,)).fetchone()
                    cur_qty = int(row["qty"]) if row else 0
                    new_qty = cur_qty - qty
                    if new_qty < 0:
                        # 防御性：实际撮合卖量不能超过持仓
                        qty = cur_qty
                        new_qty = 0
                        amount = qty * price
                    if new_qty > 0:
                        cx.execute("UPDATE positions SET qty=?, last_update_date=? WHERE ts_code=?",
                                   (new_qty, ds, code))
                    else:
                        cx.execute("DELETE FROM positions WHERE ts_code=?", (code,))
                    cash_delta += amount - fees
                    n_sell += 1

                # 更新实际成交 price + 标记 executed
                cx.execute(
                    "UPDATE signals SET ref_price=?, executed=1 WHERE date=? AND ts_code=? AND side=?",
                    (price, ds, code, side),
                )

            # 清理过期 T+1 锁（保留 buy_date >= date 的）
            cx.execute("DELETE FROM t1_locks WHERE buy_date < ?", (ds,))

        return {"n_buy": n_buy, "n_sell": n_sell, "cash_delta": cash_delta}

    # ---------- nav 快照 ----------

    def append_nav(self, date: pd.Timestamp, cash: float,
                   market_value: float, n_positions: int) -> None:
        ds = date.strftime("%Y-%m-%d")
        equity = cash + market_value
        with self._conn() as cx:
            prev = cx.execute(
                "SELECT equity FROM nav WHERE date < ? ORDER BY date DESC LIMIT 1", (ds,)
            ).fetchone()
            prev_eq = float(prev["equity"]) if prev else self.get_init_cash() or equity
            daily_ret = (equity - prev_eq) / prev_eq if prev_eq else 0.0
            cx.execute(
                "INSERT OR REPLACE INTO nav(date, cash, market_value, equity, n_positions, daily_ret) "
                "VALUES (?,?,?,?,?,?)",
                (ds, cash, market_value, equity, n_positions, daily_ret),
            )

    # ---------- 查询 ----------

    def signals_on(self, date: pd.Timestamp) -> pd.DataFrame:
        ds = date.strftime("%Y-%m-%d")
        with self._conn() as cx:
            return pd.read_sql_query(
                "SELECT * FROM signals WHERE date=? ORDER BY side, ts_code", cx, params=(ds,)
            )

    def positions(self) -> pd.DataFrame:
        with self._conn() as cx:
            return pd.read_sql_query(
                "SELECT * FROM positions WHERE qty > 0 ORDER BY ts_code", cx
            )

    def nav_curve(self) -> pd.DataFrame:
        with self._conn() as cx:
            df = pd.read_sql_query("SELECT * FROM nav ORDER BY date", cx)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

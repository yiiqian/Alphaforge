# Paper Trading 模块（M4）

> **定位**：信号生成器，不是自动下单。每天 15:30（A 股收盘后）跑一次策略，把"明天开盘要买/卖什么"写入账户库；不连券商，不真实下单。用户根据信号自己手工下单。

## 设计目标

1. **可重复**：每天能在固定时间自动产出当日调仓清单 + 持仓快照 + 净值；
2. **可解释**：每条信号都带 `reason`，M5 已接 LLM（DeepSeek / Claude / OpenAI 兼容）自动生成"为什么买/卖"的中文解释，详见 [docs/notifications.md](notifications.md)；
3. **强一致**：状态全部落在 SQLite（每个账户一个 `.sqlite` 文件），同一天重跑幂等；
4. **共用约束**：T+1 / 涨跌停 / 停牌过滤、滑点、佣金印花税 100% 复用回测引擎的代码（`runtime/constraints.py`、`runtime/costs.py`）。

## 架构

```
┌─────────────┐  cron 15:30  ┌────────────────┐  生成信号  ┌───────────────┐
│ APScheduler │─────────────▶│  PaperRunner   │──────────▶│  PaperState   │
└─────────────┘              │   .run(today)  │           │  (SQLite)     │
                             │                │  撮合昨日  └───────────────┘
                             │                │◀──────────         │
                             │                │  写 NAV  ──────────┘
                             └────────────────┘
                                     │ 输出
                                     ▼
                       调仓清单 + 持仓快照 + 净值（控制台 / 通知）
```

每次 `PaperRunner.run(today)` 做四件事：

1. **校验**：今天必须是交易日，且 `daily_table(today)` 有数据；否则拒绝跑（避免在数据未就绪时输出垃圾）。
2. **撮合**：把 `signals` 表里 `date=today, executed=0` 的所有信号按今天的 **open** 价撮合，更新 `positions` / `t1_locks` / 现金流。
3. **决策**：如果 today 是调仓日（按 `strategy.rebalance` 判断），调 `strategy.select(today, ctx)` 生成目标权重，转换为 `buy/sell` 信号写入下一交易日的 `signals` 表（`executed=0`），等明天 open 撮合。
4. **快照**：用 today 的 close 计算市值与净值，写入 `nav` 表。

## SQLite Schema

每账户一个 `paper_accounts/<account>.sqlite`：

| 表           | 用途                                       |
|--------------|--------------------------------------------|
| `meta`       | `init_cash` / `strategy` / `last_rebalance_date` 等元信息 |
| `positions`  | 当前持仓 `(ts_code, qty, avg_cost, last_update_date)` |
| `t1_locks`   | T+1 锁定簿 `(ts_code, buy_date, qty)`        |
| `signals`    | 历史信号 `(date, ts_code, side, qty, ref_price, fees, reason, executed)` |
| `nav`        | 每日净值 `(date, cash, market_value, equity, n_positions, daily_ret)` |

> 现金值不直接存表，而是从 `signals.executed=1` 的累计净流入流出 + `init_cash` 推算 → 单一信息源，避免账目错位。

## 用法

### 1) 准备配置

```yaml
# configs/run/demo_momentum.paper.yaml
strategy: demo_momentum
account: demo_momentum
init_cash: 1_000_000
data_source: tushare
costs: { commission: 0.00025, min_commission: 5.0, stamp_tax: 0.001, slippage: 0.001 }
params: { top_n: 10, lookback: 120 }
schedule:
  cron: "30 15 * * 1-5"
  backfill: true
```

### 2) 手动跑一次（适合排错 / 补跑历史日）

```bash
uv run alphaforge paper run \
    --config configs/run/demo_momentum.paper.yaml \
    --account demo_momentum
```

也可以补跑指定日期：

```bash
uv run alphaforge paper run --config <path> --date 2026-05-12
```

输出形如：

```
=== Paper run @ 2026-05-12 (account=demo_momentum) ===
[settle] today's pending signals filled: buy=8, sell=2, cash_delta=-453210.50
[decision] rebalance day. 7 buys / 3 sells dispatched for 2026-05-13 open.
  BUY:
    600519.SH  qty=  100  ref=1789.500  fees=44.74
    ...
  SELL:
    000333.SZ  qty=  500  ref= 75.300  fees= 9.41
    ...
[nav] cash=234,567.89  mv=812,345.67  equity=1,046,913.56  positions=10
[explain]
  今日为月度调仓日，根据 12 月动量截面排序新增买入 3 只、卖出 2 只...
  （配置 LLM 后由 DeepSeek / Claude 自动生成，详见 docs/notifications.md）
```

### 3) 查看账户状态

```bash
uv run alphaforge paper status --config <path>
```

显示当前持仓、最近 5 天 NAV、元信息。

### 4) 启动守护进程（自动每日跑）

```bash
uv run alphaforge paper schedule --config <path>
```

每个工作日 15:30（Asia/Shanghai）自动调用一次 `paper run`。Ctrl-C 退出。如果当前已晚于 15:30 且今天还没跑，会立刻补跑一次（`backfill=true` 时）。

生产环境建议套 `nohup` / `systemd` / `tmux`：

```bash
nohup uv run alphaforge paper schedule --config <path> >> paper.log 2>&1 &
```

## 与回测引擎的差异

| 维度       | 回测                       | Paper trading                 |
|------------|----------------------------|-------------------------------|
| 触发       | 一次跑全程                 | 一天一次                      |
| 状态       | 内存（`Account` dataclass）| SQLite（`PaperState`）        |
| 撮合时点   | 决策日的 *次日* open       | 同上（决策日把信号写到 next_day） |
| 交易约束   | T+1 / 涨跌停 / 停牌        | 同上（共用 `constraints.py`） |
| 成本       | `CostModel`                | 同上                          |
| 数据源     | synthetic 或 tushare        | 仅 tushare（synthetic 没意义）|
| 输出       | parquet 三件套             | 控制台 + SQLite 永久快照       |
| 失败处理   | raise                      | 单日 raise，守护进程不挂掉    |

## 安全与边界

- **不真实下单**。这是个信号生成器，券商对接是 M6+ 才考虑的事。
- **数据未就绪时拒绝跑**：今天 daily 没拉下来 → 直接抛错（避免输出虚假信号）。
- **同一天重跑幂等**：`signals` 用 `(date, ts_code, side)` 主键 + `INSERT OR REPLACE`，重复跑不会双倍下单。
- **崩溃恢复**：所有写在事务里，崩溃后下次 run 会从最近的一致状态续跑。

## 后续扩展

- ✅ M5 通知 + LLM 解释 — 已交付，见 [docs/notifications.md](notifications.md)。
- M6 券商对接（实盘下单）— 待规划，预计接 QMT / EasyTrader / 模拟盘。
- 多账户并存（已支持 — 不同 `--account` 即不同 sqlite 文件），方便对比策略。

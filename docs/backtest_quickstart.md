# 回测模块快速上手

## 1. 装依赖

推荐 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd Alphaforge
uv sync
```

或用 pip：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. 列出策略

```bash
uv run alphaforge strategy list
```

应看到自带的 `demo_momentum`。

## 3. 用合成数据跑回测（不需要 Tushare token）

```bash
uv run alphaforge backtest run \
    --strategy demo_momentum \
    --config configs/run/demo_momentum.yaml \
    --data synthetic
```

输出落在 `runs/backtest__demo_momentum__<timestamp>/`，包含：

```
metrics.json            # 标准指标
nav.parquet             # 每日净值
positions.parquet       # 每日持仓
trades.parquet          # 每笔成交（含 buy/sell, qty, price, fees）
config.snapshot.yaml    # 本次跑的完整参数（可复现）
```

## 4. 用真实 Tushare 数据跑（M1 数据层完成后）

```bash
echo "TUSHARE_TOKEN=你的token" > .env
uv run alphaforge data update          # 增量更新数据湖
uv run alphaforge backtest run --strategy demo_momentum --config ... --data tushare
```

> M1 数据层尚未实现 — `--data tushare` 会报 `ImportError: tushare_bundle`。先用 `--data synthetic`。

## 5. 写自己的策略

把 `.py` 文件丢到 `strategies/`：

```python
# strategies/my_low_pe.py
from alphaforge.strategy.base import BaseStrategy, StrategyContext
import pandas as pd

class MyLowPE(BaseStrategy):
    name = "my_low_pe"
    description = "我的最低 PE 策略"
    rebalance = "M"

    def select(self, date, ctx: StrategyContext) -> pd.DataFrame:
        pool = ctx.universe.tradable(date)
        # ... 你的选股逻辑（M2 因子层上线后可用 ctx.factors）...
        return pd.DataFrame({"ts_code": [...], "weight": [...]})
```

重启 CLI 即可：

```bash
uv run alphaforge strategy list                    # 应看到 my_low_pe
uv run alphaforge backtest run --strategy my_low_pe ...
```

## 6. A 股约束实现说明

- **T+1**：每笔买入进入 `T1Lock`，当日不可卖；次日开盘自动解冻
- **涨停**：`is_limit_up == True` 的股票当日不接受买单，跳过
- **跌停**：`is_limit_down == True` 的股票当日不接受卖单，跳过
- **停牌**：从可交易池剔除（`UniverseAPI.tradable` 已过滤）
- **撮合时机**：调仓日策略输出目标权重 → **下一交易日 open 价**撮合（避免未来函数）
- **一手 = 100 股**：所有数量向下取整到 100 的倍数
- **现金不足**：买单按比例缩放，保证 cash ≥ 0

## 7. 跑测试

```bash
uv run pytest -v
```

覆盖：
- `test_constraints.py` — 涨停 / 跌停 / 停牌 / T+1 锁定行为
- `test_costs.py` — 佣金 / 最低佣金 / 印花税 / 滑点
- `test_registry.py` — 策略自动发现 + 注册
- `test_backtest_engine.py` — 端到端：输出齐 + T+1 不变量 + cash ≥ 0

# Alphaforge

A-share quantitative stock-picking platform — pluggable strategies, backtest & paper trading.

> 详细架构与规划见 [docs/architecture.md](docs/architecture.md)

## 快速开始

```bash
# 1) 安装 uv（一次性）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) 装依赖
uv sync

# 3) 配置 Tushare token（M1 数据层会用到）
echo "TUSHARE_TOKEN=你的token" > .env

# 4) 列出已注册的策略
uv run alphaforge strategy list

# 5) 跑回测（先用合成数据 demo）
uv run alphaforge backtest run --strategy demo_momentum --config configs/run/demo_momentum.yaml
```

## 当前进度

- [x] M0 项目骨架
- [x] M1 数据层（Tushare → parquet 数据湖；含权限分级降级）
- [x] M3a 策略框架（BaseStrategy / Registry / Context）
- [x] M3b 回测引擎（自研轻量，含 T+1 / 涨跌停 / 停牌 / 成本）
- [x] M3c Demo 策略 + CLI
- [x] M4 Paper trading（信号生成器 + APScheduler 守护进程，详见 [docs/paper_trading.md](docs/paper_trading.md)）
- [x] M5 通知 + LLM 策略解释（飞书 / 企微 / QQ + DeepSeek / Claude / OpenAI 兼容，详见 [docs/notifications.md](docs/notifications.md)）

## 写一个策略

把 `.py` 文件丢到 `strategies/` 目录，继承 `BaseStrategy` 并实现 `select(date, ctx)`：

```python
# strategies/my_strategy.py
from alphaforge.strategy.base import BaseStrategy, StrategyContext
import pandas as pd

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    description = "我的第一个策略"
    rebalance = "M"     # M=月  W=周  D=日

    def select(self, date, ctx: StrategyContext) -> pd.DataFrame:
        pool = ctx.universe.tradable(date)
        # ... 你的选股逻辑 ...
        return pd.DataFrame({"ts_code": [...], "weight": [...]})
```

启动后会被自动发现，CLI 用 `--strategy my_strategy` 选中。

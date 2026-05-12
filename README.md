# Alphaforge

A 股量化选股平台 — 可插拔策略 / 回测 / 纸面交易（信号生成）/ 飞书推送 + LLM 解释。

> 详细架构与规划见 [docs/architecture.md](docs/architecture.md)

## 当前进度

- [x] M0 项目骨架（uv + pyproject）
- [x] M1 数据层（Tushare → parquet 数据湖；含权限分级降级）
- [x] M3a 策略框架（BaseStrategy / Registry / Context）
- [x] M3b 回测引擎（自研轻量，含 T+1 / 涨跌停 / 停牌 / 成本）
- [x] M3c Demo 策略 + CLI
- [x] M4 Paper trading（信号生成器 + APScheduler 守护进程，详见 [docs/paper_trading.md](docs/paper_trading.md)）
- [x] M5 通知 + LLM 策略解释（飞书 / 企微 / QQ + DeepSeek / Claude / OpenAI 兼容，详见 [docs/notifications.md](docs/notifications.md)）
- [ ] M6 券商对接（实盘下单，待规划）

---

## 一、装环境（5 分钟，只需一次）

```bash
# 1) 安装 uv（Python 包管理；如已装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) 拉代码 + 装依赖
git clone https://github.com/<you>/alphaforge.git && cd alphaforge
uv sync

# 3) 复制环境变量模板（秘密不进 git）
cp .env.example .env
```

打开 `.env`，按需填以下变量（**只有 `TUSHARE_TOKEN` 是必须的**，其它按需）：

| 变量 | 用途 | 必填？ |
|---|---|---|
| `TUSHARE_TOKEN` | M1 拉行情数据 | ✅ 必填（[免费注册 tushare.pro](https://tushare.pro/register)） |
| `FEISHU_WEBHOOK` | 飞书机器人推送 | 想自动推送时填 |
| `FEISHU_SIGN_SECRET` | 飞书签名校验 | 仅当机器人开了签名校验 |
| `WECOM_WEBHOOK` | 企业微信群推送 | 想推到企微时填 |
| `QQ_ACCESS_TOKEN` | OneBot v11 access_token | 想推到 QQ 时填 |
| `DEEPSEEK_API_KEY` | DeepSeek LLM 解释 | 想要中文策略解释时填 |
| `ANTHROPIC_API_KEY` | Claude LLM | 用 Claude 时填 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容 LLM | 用通义/Kimi/Ollama 时填 |

`.env` 已被 `.gitignore` 忽略，绝不会提交。

---

## 二、3 个核心场景，从快到慢

### 场景 1️⃣：纯本地试一下（不要 token，1 分钟）

用合成数据跑一遍回测，验证整套链路：

```bash
uv run alphaforge strategy list                    # 看有哪些策略
uv run alphaforge backtest run \
    --strategy demo_momentum \
    --config configs/run/demo_momentum.yaml        # data 默认 synthetic
```

跑完会把 NAV 曲线、成交流水、metrics 写到 `runs/backtest__demo_momentum__<时间戳>/`。

### 场景 2️⃣：用真实 A 股数据回测（5–10 分钟）

填好 `TUSHARE_TOKEN` 后：

```bash
# 1) 拉数据（首次约 2–5 分钟，之后增量秒级）
uv run alphaforge data update --start 2020-01-01

# 2) 看数据湖状态
uv run alphaforge data status

# 3) 跑真实数据回测
uv run alphaforge backtest run \
    --strategy demo_momentum \
    --config configs/run/demo_momentum.yaml \
    --data tushare
```

> 不同 Tushare 积分等级能拉的接口不同（详见 [docs/data_layer.md](docs/data_layer.md)）。
> 平台会自动降级跳过没权限的接口，不会硬挂。

### 场景 3️⃣：每天 15:30 自动出调仓单 + 推飞书（生产用法）

1. **填 `.env`**：至少加 `FEISHU_WEBHOOK` 和 `DEEPSEEK_API_KEY`。
2. **拉飞书机器人**：群右上角 → 设置 → 群机器人 → 添加 → 自定义机器人，
   复制 webhook URL。建议勾上"签名校验"，把 secret 也填进 `.env`。
3. **测通道**：
   ```bash
   uv run alphaforge notify test --config configs/run/demo_momentum.paper.yaml
   ```
   群里收到 "Alphaforge 通知测试 ✅" 即成功。
4. **手动跑一次（带推送）**：
   ```bash
   uv run alphaforge paper run \
       --config configs/run/demo_momentum.paper.yaml \
       --notify
   ```
5. **守护进程（每个工作日 15:30 自动跑）**：
   ```bash
   nohup uv run alphaforge paper schedule \
       --config configs/run/demo_momentum.paper.yaml \
       >> paper.log 2>&1 &
   ```
   Ctrl-C 退出；中途 15:30 已过且今天没跑会自动补一次。

> Paper trading 是**信号生成器**，不真实下单。每天给你"明天开盘要买/卖什么"的清单 + 持仓 + 净值 + LLM 解释，你自己手工下单。详见 [docs/paper_trading.md](docs/paper_trading.md) 与 [docs/notifications.md](docs/notifications.md)。

---

## 三、CLI 速查表

```
alphaforge strategy list                    列出策略
alphaforge strategy info <name>             查看某策略元信息

alphaforge data update [--start ... --end ...]  拉/增量更新行情
alphaforge data status                      查看数据湖

alphaforge backtest run --strategy <s> --config <yaml> [--data synthetic|tushare]

alphaforge paper run     --config <yaml> [--notify] [--date YYYY-MM-DD]
alphaforge paper status  --config <yaml>
alphaforge paper schedule --config <yaml>   守护进程
alphaforge paper explain --config <yaml>    重渲染 LLM 解释（不重跑策略，调试 prompt 用）

alphaforge notify test   --config <yaml>    给当前通道发一条测试消息
```

---

## 四、配置文件长什么样

回测用 `configs/run/demo_momentum.yaml`（极简）：

```yaml
strategy: demo_momentum
period: { start: 2022-01-01, end: 2024-12-31 }
init_cash: 1_000_000
benchmark: "000300.SH"
costs: { commission: 0.00025, stamp_tax: 0.001, slippage: 0.001 }
params: { top_n: 10, lookback: 120 }
```

Paper + 推送用 `configs/run/demo_momentum.paper.yaml`（已预填，照抄即可）：

```yaml
strategy: demo_momentum
account: demo_momentum
init_cash: 1_000_000
benchmark: "000300.SH"
data_source: tushare
costs: { commission: 0.00025, stamp_tax: 0.001, slippage: 0.001 }
params: { top_n: 10, lookback: 120 }
schedule: { cron: "30 15 * * 1-5", backfill: true }

notify:
  type: feishu
  webhook: "${FEISHU_WEBHOOK}"
  # secret: "${FEISHU_SIGN_SECRET}"        # 开了签名校验就取消注释

llm:
  provider: deepseek
  model: deepseek-chat
  api_key: "${DEEPSEEK_API_KEY}"
```

`${VAR}` 占位符会从 `.env` 读取。想换企微/QQ/Claude/通义/Kimi/Ollama，看
[docs/notifications.md](docs/notifications.md) 里的完整模板。

---

## 五、写自己的策略

把 `.py` 文件丢到 `strategies/` 目录即可被自动发现：

```python
# strategies/my_strategy.py
from alphaforge.strategy.base import BaseStrategy, StrategyContext
import pandas as pd

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    description = "我的第一个策略"
    rebalance = "M"     # M=月  W=周  D=日
    benchmark = "000300.SH"

    def select(self, date, ctx: StrategyContext) -> pd.DataFrame:
        pool = ctx.universe.tradable(date)              # 当日可交易票
        # ... 你的选股逻辑 ...
        return pd.DataFrame({"ts_code": [...], "weight": [...]})
```

CLI 跑：

```bash
uv run alphaforge strategy list                       # 应该看到 my_strategy
uv run alphaforge backtest run --strategy my_strategy --config <你的 yaml>
```

策略框架细节见 [docs/strategy_framework.md](docs/strategy_framework.md)。

---

## 六、目录结构

```
alphaforge/
├── data/        Tushare 数据层 + 数据湖
├── strategy/    策略基类 + 注册器
├── runtime/     回测引擎 + 约束 + 成本 + paper trading + scheduler
├── notify/      飞书 / 企微 / QQ adapter
├── explain/     DeepSeek / Claude / OpenAI 兼容 LLM adapter
└── cli.py       Click CLI 入口
strategies/      用户策略 .py 丢这里
configs/         策略配置 yaml
docs/            文档
tests/           pytest
data_lake/       本地 parquet 数据湖（gitignore）
paper_accounts/  paper-trading SQLite（gitignore）
runs/            回测产物（gitignore）
```

---

## 七、常见问题

**Q：跑 paper 报 "No daily data for YYYY-MM-DD"？**
A：今日数据还没拉。先 `alphaforge data update`，或加 `--date 上一个交易日` 补跑历史。

**Q：飞书消息发不出去？**
A：(1) 检查 `.env` 里的 `FEISHU_WEBHOOK` 是不是完整 URL；(2) 机器人开了签名校验时
`FEISHU_SIGN_SECRET` 必须填；(3) `notify test` 看终端报错（多半被频控或签名错）。

**Q：DeepSeek key 没填会怎样？**
A：自动 fallback 到一行模板文本（`今日(YYYY-MM-DD)有/未调仓...`），不影响主流程。

**Q：Tushare 积分不够拉某个接口？**
A：平台自动跳过该接口并打 warning，不会挂。详见 [docs/data_layer.md](docs/data_layer.md) 的权限分级表。

---

## 八、跑测试

```bash
uv run pytest                     # 全套
uv run pytest tests/test_paper_runner.py -v   # paper-trading 单元测试
uv run pytest tests/test_notify.py tests/test_explain.py -v   # M5
```

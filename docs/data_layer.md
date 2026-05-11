# M1 数据层：Tushare → Parquet 数据湖

## 1. 准备 Tushare token

1. 注册 [Tushare Pro](https://tushare.pro/)
2. 取你的 token（个人主页 → 接口 token）
3. 落到项目根的 `.env`：

```bash
echo "TUSHARE_TOKEN=你的token" > .env
```

> 部分接口（`daily`、`adj_factor`、`stk_limit`、`suspend_d`）需要 ≥ 2000 积分。新账号可以做任务或捐赠几块钱拿到。

## 2. 拉取数据

```bash
uv run alphaforge data update --start 2018-01-01
```

第一次跑：会按交易日逐日拉取（≈ 252 个交易日/年），3 年数据大概 5-10 分钟。

之后每天再跑一次：游标记录在 `data_lake/_meta.json`，只会拉新增的那几个交易日，秒级完成。

也可以指定参数：

```bash
uv run alphaforge data update \
    --start 2020-01-01 \
    --end 2024-12-31 \
    --indices 000300.SH,000905.SH \
    --root /path/to/data_lake
```

## 3. 检查数据湖状态

```bash
uv run alphaforge data status
```

会显示：
- 各表的最后更新游标（如 `daily_last_date: 20241231`）
- 文件数量与磁盘占用

## 4. 数据湖布局

```
data_lake/
├── trade_cal.parquet              # 交易日历（每次 update 全刷）
├── stock_basic.parquet            # 股票基本面（含已退市；每次 update 全刷）
├── suspend.parquet                # 停牌事件（小表，全刷）
├── _meta.json                     # 增量游标
├── daily/
│   ├── 2018.parquet               # 不复权日线，按年分片
│   ├── 2019.parquet
│   └── ...
├── adj_factor/                    # 复权因子，按年分片
├── stk_limit/                     # 涨跌停价，按年分片
└── index_daily/
    ├── 000300.SH.parquet          # 沪深 300
    ├── 000905.SH.parquet
    └── ...
```

字段以 Tushare 原始字段为准（参考 [Tushare 文档](https://tushare.pro/document/2)）。

## 5. 用真实数据跑回测

```bash
uv run alphaforge backtest run \
    --strategy demo_momentum \
    --config configs/run/demo_momentum.yaml \
    --data tushare
```

CLI 会自动从 `data_lake/` 加载，构造 `ParquetBundle`，传给回测引擎。

## 6. 关键设计取舍

### 6.1 增量游标 vs 全量重拉

`daily / adj_factor / stk_limit` 三个大表每次 update 时按"上次成功的最后一天 + 1"开始拉，
游标存在 `_meta.json` 里。如果某次拉到一半失败，重跑会从中断点继续。

如果想强制全量重拉，删掉 `_meta.json` 即可（已写入的 parquet 会被合并去重）。

### 6.2 PIT 安全（Point-in-Time）

`ParquetBundle` 的所有查询都按 `trade_date <= date` 过滤；
`tradable(date)` 还会检查 `list_date + 60 ≤ date`（剔除次新）和 `delist_date > date`（剔除已退市）。

这样策略代码无法"看到未来"，回测结果可信。

### 6.3 前复权（qfq）

`bars(adj="qfq")` 会自动把 close/open/high/low/pre_close 乘以
`adj_factor / latest_adj_factor`，得到以"查询窗口最后一天"为基准的前复权价。

如果策略需要原始未复权价，传 `adj="raw"`（暂未实现，回测引擎默认 qfq）。

### 6.4 涨跌停判定

依据 `stk_limit.up_limit / down_limit` 与当日收盘价比较（容差 0.01 元）：
- `close >= up_limit - 0.01` → 涨停
- `close <= down_limit + 0.01` → 跌停

注意：科创板/创业板涨跌幅是 ±20%，主板 ±10%，ST ±5%。`stk_limit` 表已经按板块给出正确的限价，引擎不需要自己判断。

### 6.5 停牌识别

两层信息合并：
1. `suspend.parquet`（Tushare suspend_d 接口）—— 显式停牌事件
2. 当日 `daily` 表里没有该股票的行 —— 隐含停牌

`is_suspended()` 与 `tradable()` 都会综合考虑这两路。

## 7. 字段映射速查

| 引擎使用的字段 | 来源 |
|---|---|
| `open / high / low / close / vol / amount` | `daily.parquet` |
| `pre_close / pct_chg` | `daily.parquet` |
| `adj_factor` | `adj_factor.parquet` |
| `up_limit / down_limit / limit_status` | `stk_limit.parquet` + 计算 |
| `is_suspended` | `suspend.parquet` ∪ daily 缺失 |
| `list_date / delist_date / name` | `stock_basic.parquet` |

## 8. 跑测试

M1 的单测使用伪造客户端，不会请求真实 Tushare：

```bash
uv run pytest tests/test_tushare_bundle.py -v
```

覆盖：
- 交易日历读写
- 日线 + 复权因子合并 + qfq
- 单日横截面带 `limit_status / is_suspended`
- 停牌识别与 universe 过滤
- 增量游标（重复跑不重复拉）

## 9. 常见问题

**Q: `tushare not installed` 报错？**
A: `uv add tushare` 或 `pip install tushare`。库没列在默认依赖里是为了让没 token 的同学也能跑合成 demo。

**Q: 拉了一半被 Tushare 限频怎么办？**
A: 客户端会自动节流（每分钟 ≤ 480 次）+ 指数退避重试。如果还失败，过几分钟重跑命令，会从游标续传。

**Q: 想加新字段（比如基本面）怎么办？**
A: 在 `tushare_client.py` 加新接口方法 → `updater.py` 加对应 `update_xxx` → `parquet_store.py` 在 `daily_table` 或新方法里读取。基本面（`fina_indicator`, `daily_basic`）的接入是 M2 因子层的工作。

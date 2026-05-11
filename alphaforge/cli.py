"""Alphaforge CLI 入口。

子命令：
    strategy list              列出已注册策略
    strategy info NAME         查看某策略元信息
    data update                拉取 Tushare 数据到本地 parquet
    data status                查看数据湖状态
    backtest run               跑回测
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import click
import pandas as pd
from rich.console import Console
from rich.table import Table

from alphaforge.infra.config import load_dotenv, load_yaml, project_root
from alphaforge.infra.logger import logger
from alphaforge.runtime.backtest import BacktestConfig, run_backtest
from alphaforge.runtime.costs import CostModel
from alphaforge.strategy.registry import StrategyRegistry, StrategyNotFound

console = Console()


@click.group()
def main() -> None:
    """Alphaforge — A-share quant platform."""
    load_dotenv(project_root() / ".env")


# ---------------- strategy ----------------

@main.group()
def strategy() -> None:
    """策略管理。"""


@strategy.command("list")
def strategy_list() -> None:
    """列出所有已发现的策略。"""
    StrategyRegistry.discover(project_root() / "strategies")
    items = StrategyRegistry.list()
    if not items:
        console.print("[yellow]No strategies registered. Drop a .py file in strategies/[/]")
        return
    table = Table(title="Registered Strategies")
    table.add_column("name", style="cyan")
    table.add_column("description")
    table.add_column("rebalance", style="green")
    table.add_column("benchmark")
    table.add_column("module", style="dim")
    for it in items:
        table.add_row(it["name"], it["description"], it["rebalance"], it["benchmark"], it["module"])
    console.print(table)


@strategy.command("info")
@click.argument("name")
def strategy_info(name: str) -> None:
    """查看某策略详细元信息。"""
    StrategyRegistry.discover(project_root() / "strategies")
    try:
        cls = StrategyRegistry.get(name)
    except StrategyNotFound as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)
    console.print(f"[cyan]name[/]        : {cls.name}")
    console.print(f"[cyan]description[/] : {cls.description}")
    console.print(f"[cyan]rebalance[/]   : {cls.rebalance}")
    console.print(f"[cyan]benchmark[/]   : {cls.benchmark}")
    console.print(f"[cyan]module[/]      : {cls.__module__}")
    console.print(f"[cyan]docstring[/]   :\n{cls.__doc__ or '(none)'}")


# ---------------- data ----------------

@main.group()
def data() -> None:
    """数据湖管理：拉取 Tushare 行情到本地 parquet。"""


@data.command("update")
@click.option("--start", default="2018-01-01", help="拉取起始日（默认 2018-01-01）")
@click.option("--end", default=None, help="拉取截止日（默认今日）")
@click.option("--root", default=None,
              help="数据湖根目录（默认 <project_root>/data_lake/，或读环境变量 ALPHAFORGE_DATA_LAKE）")
@click.option("--indices", default="000300.SH,000905.SH,000852.SH,000001.SH",
              help="基准指数列表（逗号分隔）")
def data_update(start: str, end: str | None, root: str | None, indices: str) -> None:
    """从 Tushare 增量更新本地数据湖。

    第一次跑会比较慢（按日逐次拉，一年 ~252 个交易日，约 2-5 分钟）。
    后续每天增量只拉新增的几个交易日，秒级完成。
    """
    import os
    from alphaforge.data.updater import update_all

    if root is None:
        root = os.environ.get("ALPHAFORGE_DATA_LAKE") or str(project_root() / "data_lake")

    if not os.environ.get("TUSHARE_TOKEN"):
        console.print(
            "[red]TUSHARE_TOKEN not set.[/] "
            "Put it in .env (TUSHARE_TOKEN=xxx) or `export TUSHARE_TOKEN=...`"
        )
        raise SystemExit(1)

    idx_list = [c.strip() for c in indices.split(",") if c.strip()]
    console.print(f"[cyan]data_lake[/] = {root}")
    console.print(f"[cyan]window[/]    = {start} -> {end or 'today'}")
    console.print(f"[cyan]indices[/]   = {idx_list}")

    summary = update_all(root=Path(root), start=start, end=end, indices=idx_list)

    table = Table(title="Update Summary")
    table.add_column("panel", style="cyan")
    table.add_column("days written", justify="right")
    for k, v in summary.items():
        table.add_row(k, str(v))
    console.print(table)
    console.print(f"[green]Done.[/] data_lake at {root}")


@data.command("status")
@click.option("--root", default=None, help="数据湖根目录")
def data_status(root: str | None) -> None:
    """查看数据湖游标 + 文件统计。"""
    import json as _json
    import os
    from alphaforge.data.updater import DataLakePaths

    if root is None:
        root = os.environ.get("ALPHAFORGE_DATA_LAKE") or str(project_root() / "data_lake")

    paths = DataLakePaths(Path(root))
    if not paths.root.exists():
        console.print(f"[red]No data_lake at {root}.[/] Run `alphaforge data update` first.")
        raise SystemExit(1)

    console.print(f"[cyan]root:[/] {paths.root}")
    if paths.meta.exists():
        meta = _json.loads(paths.meta.read_text(encoding="utf-8"))
        for k, v in meta.items():
            console.print(f"  {k}: {v}")
    else:
        console.print("[yellow]No _meta.json (never updated).[/]")

    table = Table(title="Files")
    table.add_column("file")
    table.add_column("size", justify="right")
    for sub in ("trade_cal.parquet", "stock_basic.parquet", "suspend.parquet"):
        p = paths.root / sub
        size = p.stat().st_size if p.exists() else 0
        table.add_row(str(p.relative_to(paths.root)), f"{size/1024:.1f} KB" if size else "-")
    for sub in ("daily", "adj_factor", "stk_limit", "index_daily"):
        d = paths.root / sub
        if d.exists():
            n = sum(1 for _ in d.glob("*.parquet"))
            total = sum(p.stat().st_size for p in d.glob("*.parquet"))
            table.add_row(f"{sub}/", f"{n} files, {total/1024/1024:.1f} MB")
    console.print(table)


# ---------------- backtest ----------------

@main.group()
def backtest() -> None:
    """回测命令。"""


@backtest.command("run")
@click.option("--strategy", "strategy_name", required=True, help="策略名（见 strategy list）")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True),
              help="回测配置 YAML")
@click.option("--data", "data_source", default="synthetic",
              type=click.Choice(["synthetic", "tushare"]),
              help="数据源：synthetic（合成，默认）/ tushare（真实，需 token）")
@click.option("--out", "out_dir", default=None,
              help="输出目录，默认 runs/backtest__<strategy>__<timestamp>/")
def backtest_run(strategy_name: str, config_path: str, data_source: str, out_dir: str | None) -> None:
    """跑回测。"""

    cfg_dict = load_yaml(config_path)
    if cfg_dict.get("strategy") and cfg_dict["strategy"] != strategy_name:
        logger.warning(
            f"--strategy={strategy_name} != config strategy={cfg_dict['strategy']}, "
            "using --strategy."
        )

    StrategyRegistry.discover(project_root() / "strategies")
    try:
        strategy_cls = StrategyRegistry.get(strategy_name)
    except StrategyNotFound as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1)

    strategy = strategy_cls()
    strategy.params = dict(cfg_dict.get("params") or {})

    period = cfg_dict.get("period") or {}
    start = pd.Timestamp(period.get("start") or "2022-01-01")
    end_str = period.get("end") or ""
    end = pd.Timestamp(end_str) if end_str else pd.Timestamp.today().normalize()

    cost = CostModel(**(cfg_dict.get("costs") or {}))
    cfg = BacktestConfig(
        start=start,
        end=end,
        init_cash=float(cfg_dict.get("init_cash", 1_000_000)),
        benchmark=cfg_dict.get("benchmark") or strategy.benchmark,
        cost=cost,
    )

    # ----- 数据源 -----
    if data_source == "synthetic":
        from alphaforge.data.synthetic import make_synthetic_bundle
        logger.info("Loading synthetic data bundle (for demo only)...")
        data, universe, calendar = make_synthetic_bundle(
            start=str(start.date()),
            end=str(end.date()),
            n_stocks=int(cfg_dict.get("synthetic_n_stocks", 50)),
        )
    elif data_source == "tushare":
        from alphaforge.data.tushare_bundle import load_tushare_bundle
        logger.info("Loading Tushare data bundle...")
        data, universe, calendar = load_tushare_bundle(start=start, end=end)
    else:
        raise click.UsageError(f"Unknown data source: {data_source}")

    # ----- 输出目录 -----
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if out_dir is None:
        out_dir = project_root() / "runs" / f"backtest__{strategy_name}__{timestamp}"
    else:
        out_dir = Path(out_dir)

    logger.info(f"Running backtest: strategy={strategy_name}  out={out_dir}")
    metrics = run_backtest(
        strategy=strategy,
        data=data,
        universe=universe,
        calendar=calendar,
        cfg=cfg,
        out_dir=out_dir,
        config_snapshot={
            "strategy": strategy_name,
            "data_source": data_source,
            "config": cfg_dict,
        },
    )

    # ----- 打印 metrics -----
    console.print()
    table = Table(title=f"Backtest Result · {strategy_name}")
    table.add_column("metric", style="cyan")
    table.add_column("value", justify="right")
    for k, v in metrics.items():
        if isinstance(v, float):
            if "ratio" in k or "sharpe" in k:
                vstr = f"{v:.3f}"
            elif "return" in k or "drawdown" in k or "volatility" in k or "alpha" in k or "rate" in k or "tracking" in k:
                vstr = f"{v:.2%}"
            else:
                vstr = f"{v:.4f}"
        else:
            vstr = str(v)
        table.add_row(k, vstr)
    console.print(table)
    console.print(f"[green]Outputs saved to:[/] {out_dir}")
    console.print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

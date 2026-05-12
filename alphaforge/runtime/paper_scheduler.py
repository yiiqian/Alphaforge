"""APScheduler 守护进程 — 每个交易日 15:30 自动跑一次 paper-trading。

设计：
- BlockingScheduler 跑在前台（systemd / docker / `nohup` 都可以兜底）。
- Cron: 周一到周五 15:30 触发；交易日过滤在 job 内部做（节假日跳过）。
- 启动时可 backfill：如果最近一次 NAV 早于今天，且今天 ≥ 15:30 是交易日，先补跑今天。
- 捕获所有 job 异常并打日志，不让单次失败把守护进程拖垮。
- Ctrl-C 优雅退出。
"""

from __future__ import annotations

import datetime as dt
import signal
from typing import Callable

import pandas as pd

from alphaforge.infra.logger import logger
from alphaforge.notify.base import Notifier
from alphaforge.runtime.paper import (
    PaperRunner,
    format_for_notification,
    format_run_result,
)


def _today_local() -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.now().date())


def make_job(runner: PaperRunner, *,
             account_name: str = "default",
             notifier: Notifier | None = None,
             on_finish: Callable[[str], None] | None = None) -> Callable[[], None]:
    """生成一个 0-arg job，APScheduler 直接 schedule 这个回调。

    notifier: 不为 None 时，每次跑完用 notifier.send(...) 推送一条精简卡片。
    on_finish: 兼容旧用法 — 仅接收 plain-text run 报告。
    """

    def _job() -> None:
        today = _today_local()
        if not runner.calendar.is_trade_day(today):
            logger.info(f"[paper-scheduler] {today.date()} not a trade day, skipping.")
            return
        try:
            res = runner.run(today)
            text = format_run_result(res, account_name=account_name)
            logger.info("\n" + text)

            if notifier is not None:
                title, body, level = format_for_notification(res, account_name=account_name)
                try:
                    ok = notifier.send(title=title, text=body, level=level)
                    if not ok:
                        logger.warning("[paper-scheduler] notifier.send returned False")
                except Exception:
                    logger.exception("[paper-scheduler] notifier.send raised; ignored.")

            if on_finish is not None:
                try:
                    on_finish(text)
                except Exception:
                    logger.exception("on_finish callback raised; ignored.")
        except Exception as e:
            logger.exception(f"[paper-scheduler] run failed for {today.date()}")
            if notifier is not None:
                try:
                    notifier.send(
                        title=f"❌ Paper run failed @ {today.date()}",
                        text=f"`{type(e).__name__}: {e}`\n\n请到日志文件查看完整堆栈。",
                        level="error",
                    )
                except Exception:
                    logger.exception("notifier.send (error path) raised; ignored.")

    return _job


def run_daemon(
    runner: PaperRunner,
    *,
    account_name: str = "default",
    cron: str = "30 15 * * 1-5",     # 15:30, Mon-Fri
    backfill: bool = True,
    notifier: Notifier | None = None,
    on_finish: Callable[[str], None] | None = None,
) -> None:
    """启动调度器并阻塞。Ctrl-C 优雅退出。"""

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as e:
        raise ImportError(
            "apscheduler not installed. `uv add apscheduler` or `pip install apscheduler`."
        ) from e

    job = make_job(runner, account_name=account_name, notifier=notifier, on_finish=on_finish)

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        job,
        trigger=CronTrigger.from_crontab(cron, timezone="Asia/Shanghai"),
        id=f"paper_run_{account_name}",
        replace_existing=True,
        misfire_grace_time=60 * 30,  # 30 分钟内的迟到也补跑
        coalesce=True,
    )

    if backfill:
        # 启动时检查：如果今天是交易日且当前已过 15:30，且今天还没跑过 → 立刻补跑一次
        now = dt.datetime.now()
        today = _today_local()
        if runner.calendar.is_trade_day(today) and now.time() >= dt.time(15, 30):
            nav_df = runner.state.nav_curve()
            already_ran = (
                not nav_df.empty
                and pd.to_datetime(nav_df["date"]).max() == today
            )
            if not already_ran:
                logger.info(
                    f"[paper-scheduler] backfill: running today's slot now (after 15:30, no NAV row yet)."
                )
                job()

    logger.info(
        f"[paper-scheduler] daemon started (account={account_name}, cron='{cron}'). "
        "Press Ctrl+C to stop."
    )

    def _stop(signum, _frame):  # noqa: ANN001
        logger.info(f"[paper-scheduler] received signal {signum}, shutting down.")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass

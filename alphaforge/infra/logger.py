"""日志：优先 loguru；缺失时降级到 stdlib logging（保持相同 API：logger.info/warning/exception/debug）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False

try:
    from loguru import logger as _logger
    _USE_LOGURU = True
except ImportError:  # pragma: no cover
    _USE_LOGURU = False
    _logger = logging.getLogger("alphaforge")


def configure(level: str = "INFO", log_dir: str | Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    if _USE_LOGURU:
        _logger.remove()
        _logger.add(
            sys.stderr,
            level=level,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level: <7}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"
            ),
            colorize=True,
        )
        if log_dir is not None:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            _logger.add(
                log_dir / "alphaforge_{time:YYYY-MM-DD}.log",
                level="DEBUG", rotation="00:00", retention="14 days", encoding="utf-8",
            )
    else:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s | %(levelname)-7s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%H:%M:%S",
        )
        if log_dir is not None:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_dir / "alphaforge.log", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s:%(funcName)s:%(lineno)d - %(message)s"))
            logging.getLogger("alphaforge").addHandler(fh)

    _CONFIGURED = True


configure()
logger = _logger

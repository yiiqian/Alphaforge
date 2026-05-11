"""配置加载 — YAML + 环境变量。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> dict[str, Any]:
    """加载 YAML 文件并展开 ${ENV_VAR} 占位。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    text = path.read_text(encoding="utf-8")
    text = os.path.expandvars(text)
    return yaml.safe_load(text) or {}


def load_dotenv(path: str | Path = ".env") -> None:
    """简易 .env 加载（不引入额外依赖）。"""
    path = Path(path)
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'").strip('"')
        os.environ.setdefault(k, v)


def project_root() -> Path:
    """项目根目录 — pyproject.toml 所在的目录。"""
    p = Path(__file__).resolve()
    for parent in [p, *p.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()

"""Load deployment settings before any business module fixes its paths."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def env_path() -> Path:
    override = os.environ.get("ENV_FILE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parent / ".env"


def load_environment() -> Path:
    """Use process variables first; an explicit env file never falls back elsewhere."""
    path = env_path()
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
    return path


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()

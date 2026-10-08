"""本机服务监听配置：管理页面和 API 可直接访问。"""
from __future__ import annotations
from app_common import _env

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

def check_local_host() -> None:
    host = (_env("HOST") or "127.0.0.1").lower()
    if host not in LOOPBACK_HOSTS:
        raise SystemExit("本机版仅支持 HOST=127.0.0.1、localhost 或 ::1")

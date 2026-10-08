"""请求限速与访客 IP 识别。"""

from __future__ import annotations

import ipaddress
import re
import time

from fastapi import Request

from app_common import logger

LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}

def _safe_log_field(value, limit: int = 120) -> str:
    """净化写入日志的外部输入：去控制字符（防日志注入/嫁祸拉黑）并截断。"""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "?", str(value))
    return cleaned[:limit]


def _client_ip(request: Request) -> str:
    """获取真实访客 IP：仅在直连对端是本机反代时信任 X-Real-IP，忽略 X-Forwarded-For。"""
    direct = request.client.host if request.client else "unknown"
    if direct in LOCAL_IPS:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            try:
                return str(ipaddress.ip_address(real_ip))
            except ValueError:
                logger.warning("忽略非法的 X-Real-IP 头：%s", _safe_log_field(real_ip))
    return direct


# ── 限速（进程内内存滑动窗口）────────────────────────────────────────────────
RATE_GENERAL = {"limit": 120, "window": 60}   # 每 IP 每分钟总请求上限
RATE_SENSITIVE = {"limit": 5, "window": 60}   # 敏感端点每 IP 每分钟上限
SENSITIVE_PATHS = (
    "/api/run",
    "/api/sync",
    "/api/contacts/fetch",
    "/api/credentials/extract",
    "/api/ledger/harvest-creator",
    "/api/reset-running",
    "/api/upload-state",
    "/api/credentials/upload",
    "/api/config",
)
_hits: dict = {}
_MAX_RATE_KEYS = 20000  # _hits 键数上限，超出时淘汰最久未活动的键（防内存无界增长）


def _check_rate(ip: str, path: str) -> bool:
    now = time.time()
    sensitive = any(path.startswith(p) for p in SENSITIVE_PATHS)
    rule = RATE_SENSITIVE if sensitive else RATE_GENERAL
    key = f"{ip}:{path}" if sensitive else ip
    q = _hits.get(key)
    if q is None:
        if len(_hits) >= _MAX_RATE_KEYS:
            stale = sorted(_hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
            for k, _ in stale[: _MAX_RATE_KEYS // 10]:
                _hits.pop(k, None)
        q = _hits.setdefault(key, [])
    while q and q[0] <= now - rule["window"]:
        q.pop(0)
    if len(q) >= rule["limit"]:
        return False
    q.append(now)
    return True

"""多账号注册表与按账号的数据路径（M4 多账号改造）。

隔离策略：「每账号一个数据目录 + 独立子进程」。
- 注册表 ``data/accounts.json`` 记录账号 id / 显示名 / 启用 / 顺序 / 通知邮箱；
- 每个账号的数据在 ``data/accounts/<id>/``（config/state/ledger/runtime/logs），
  worker 子进程通过环境变量 ``DATA_DIR`` 指向该目录，``core.config`` 等模块即天然按账号隔离；
- 本模块所有路径都按账号 id **现算**（不在 import 时固化），以便同一个 Web/编排进程
  管理多个账号——这与 core.config 里 import 时固化的模块级常量不同，切勿在本模块缓存
  某账号的具体文件路径。

账号根与注册表路径支持环境变量覆盖（测试 / 桌面打包用）：
``SPARKKEEPER_ACCOUNTS_ROOT``、``SPARKKEEPER_ACCOUNTS_REGISTRY``。
"""

from __future__ import annotations

import datetime
import copy
import json
import os
import re
import shutil
import threading
from pathlib import Path

from .config import BASE_DIR, DATA_DIR, atomic_write_text

# 账号数量硬上限（本地面板与云端注册表双侧都按此校验）。
MAX_ACCOUNTS = 10

# 账号 id 白名单：小写字母/数字/下划线/连字符，3~20 位。
# 同时是目录名，严格白名单可杜绝 ``..``、绝对路径、分隔符等目录穿越输入。
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_-]{3,20}$")

_env_root = os.environ.get("SPARKKEEPER_ACCOUNTS_ROOT", "").strip()
ACCOUNTS_ROOT = Path(_env_root).resolve() if _env_root else (DATA_DIR / "accounts")

_env_registry = os.environ.get("SPARKKEEPER_ACCOUNTS_REGISTRY", "").strip()
REGISTRY_PATH = Path(_env_registry).resolve() if _env_registry else (DATA_DIR / "accounts.json")

# 账号数据目录内的标准文件名（与 core.config / runtime / ledger 的约定保持一致）。
CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
LEDGER_NAME = "ledger.json"
RUNTIME_NAME = "runtime.json"
LOG_DIR_NAME = "logs"

# RLock（可重入）：add/update/remove 持锁后会调用同样加锁的 save_registry，
# 用普通 Lock 会在同一线程二次 acquire 时自死锁。
_lock = threading.RLock()
_overview_cache = {}


class AccountError(ValueError):
    """账号参数非法 / 数量超限 / 账号不存在等可预期错误。"""


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def valid_account_id(account_id: str) -> bool:
    return isinstance(account_id, str) and bool(_ACCOUNT_ID_RE.match(account_id))


def validate_account_id(account_id: str) -> str:
    """校验账号 id，非法直接抛 AccountError；合法则原样返回。"""
    if not valid_account_id(account_id):
        raise AccountError(
            f"账号 id 非法：{account_id!r}（只允许小写字母、数字、下划线、连字符，3~20 位）"
        )
    return account_id


def account_dir(account_id: str) -> Path:
    """返回账号数据目录（不保证存在）。白名单校验后拼接，杜绝目录穿越。"""
    validate_account_id(account_id)
    return ACCOUNTS_ROOT / account_id


def account_file(account_id: str, name: str) -> Path:
    """返回账号目录内某个标准文件的路径（name 不允许带分隔符）。"""
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise AccountError(f"非法文件名：{name!r}")
    return account_dir(account_id) / name


def _default_registry() -> dict:
    return {"version": 1, "max_accounts": MAX_ACCOUNTS, "accounts": []}


def _normalize_emails(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    seen = set()
    for item in value:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load_registry() -> dict:
    """读取注册表；文件缺失/损坏时返回空表（不抛异常，便于首启）。"""
    reg = _default_registry()
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            reg.update(data)
    except Exception:
        pass
    # 账号条目做一次基本清洗，保证下游字段类型稳定。
    cleaned = []
    for a in reg["accounts"]:
        if not isinstance(a, dict) or not valid_account_id(str(a.get("id", ""))):
            continue
        cleaned.append(
            {
                "id": a["id"],
                "display_name": str(a.get("display_name") or a["id"]),
                "enabled": bool(a.get("enabled", True)),
                "order": int(a.get("order", 0)) if str(a.get("order", 0)).lstrip("-").isdigit() else 0,
                "notify_emails": _normalize_emails(a.get("notify_emails")),
                "created_at": a.get("created_at") or now_iso(),
                "note": str(a.get("note") or ""),
            }
        )
    reg["accounts"] = sorted(cleaned, key=lambda a: (a["order"], a["id"]))
    reg["max_accounts"] = MAX_ACCOUNTS
    return reg


def save_registry(reg: dict) -> None:
    with _lock:
        reg = dict(reg)
        reg["version"] = 1
        reg["max_accounts"] = MAX_ACCOUNTS
        atomic_write_text(REGISTRY_PATH, json.dumps(reg, ensure_ascii=False, indent=2))


def list_accounts(only_enabled: bool = False) -> list[dict]:
    accounts = load_registry()["accounts"]
    return [dict(a) for a in accounts if (not only_enabled or a["enabled"])]


def get_account(account_id: str) -> dict | None:
    validate_account_id(account_id)
    for a in load_registry()["accounts"]:
        if a["id"] == account_id:
            return dict(a)
    return None


def count_accounts() -> int:
    return len(load_registry()["accounts"])


def add_account(
    account_id: str,
    display_name: str | None = None,
    *,
    notify_emails=None,
    enabled: bool = True,
    order: int | None = None,
    note: str = "",
    exist_ok: bool = False,
) -> dict:
    """注册一个账号并创建其数据目录。

    - 新账号数量达到 MAX_ACCOUNTS 时抛 AccountError；
    - 已存在时默认抛错，``exist_ok=True`` 则原样返回（不重复计数、不覆盖配置）。
    """
    validate_account_id(account_id)
    with _lock:
        reg = load_registry()
        for a in reg["accounts"]:
            if a["id"] == account_id:
                if exist_ok:
                    return dict(a)
                raise AccountError(f"账号已存在：{account_id}")
        if len(reg["accounts"]) >= MAX_ACCOUNTS:
            raise AccountError(f"账号数量已达上限（{MAX_ACCOUNTS}），不能再添加")
        if order is None:
            order = (max((a["order"] for a in reg["accounts"]), default=-1)) + 1
        entry = {
            "id": account_id,
            "display_name": str(display_name or account_id),
            "enabled": bool(enabled),
            "order": int(order),
            "notify_emails": _normalize_emails(notify_emails),
            "created_at": now_iso(),
            "note": str(note or ""),
        }
        reg["accounts"].append(entry)
        account_dir(account_id).mkdir(parents=True, exist_ok=True)
        save_registry(reg)
        return dict(entry)


def update_account(
    account_id: str,
    *,
    display_name: str | None = None,
    enabled: bool | None = None,
    order: int | None = None,
    notify_emails=None,
    note: str | None = None,
) -> dict:
    validate_account_id(account_id)
    with _lock:
        reg = load_registry()
        target = None
        for a in reg["accounts"]:
            if a["id"] == account_id:
                target = a
                break
        if target is None:
            raise AccountError(f"账号不存在：{account_id}")
        if display_name is not None:
            target["display_name"] = str(display_name)
        if enabled is not None:
            target["enabled"] = bool(enabled)
        if order is not None:
            target["order"] = int(order)
        if notify_emails is not None:
            target["notify_emails"] = _normalize_emails(notify_emails)
        if note is not None:
            target["note"] = str(note)
        save_registry(reg)
        return dict(target)


def remove_registry_account(account_id: str, *, delete_dir: bool = False) -> None:
    """从注册表移除账号。默认**保留**数据目录（凭证删除是高危操作）；
    仅当显式 ``delete_dir=True`` 才连目录一起删除。"""
    validate_account_id(account_id)
    with _lock:
        reg = load_registry()
        before = len(reg["accounts"])
        reg["accounts"] = [a for a in reg["accounts"] if a["id"] != account_id]
        if len(reg["accounts"]) == before:
            raise AccountError(f"账号不存在：{account_id}")
        save_registry(reg)
    if delete_dir:
        d = account_dir(account_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _state_cookies_ok(path: Path) -> bool:
    data = _read_json(path)
    return bool(data and isinstance(data.get("cookies"), list) and data["cookies"])


def account_overview(account_id: str, meta: dict | None = None) -> dict:
    """聚合某账号的运行/凭证/台账概要，供 Web 面板与编排器使用（只读、容错）。"""
    meta = meta if meta is not None else get_account(account_id)
    if meta is None:
        raise AccountError(f"账号不存在：{account_id}")
    d = account_dir(account_id)
    signatures = []
    for name in (STATE_NAME, RUNTIME_NAME, LEDGER_NAME):
        try:
            st = (d / name).stat()
            signatures.append((st.st_mtime_ns, st.st_size))
        except OSError:
            signatures.append(None)
    fingerprint = (str(d), json.dumps(meta, sort_keys=True, ensure_ascii=False), tuple(signatures))
    with _lock:
        cached = _overview_cache.get(account_id)
        if cached and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])
    state_path = d / STATE_NAME
    runtime = _read_json(d / RUNTIME_NAME) or {}
    ledger_entries = []
    try:
        lj = json.loads((d / LEDGER_NAME).read_text(encoding="utf-8"))
        if isinstance(lj, list):
            ledger_entries = lj
    except Exception:
        ledger_entries = []

    last_run = runtime.get("last_run") or {}
    paused_until = float(runtime.get("auto_paused_until") or 0)
    result = {
        **meta,
        "dir": str(d),
        "has_state": _state_cookies_ok(state_path),
        "running": bool(runtime.get("running")),
        "session_status": runtime.get("session_status", "unknown"),
        "consecutive_failures": int(runtime.get("consecutive_failures") or 0),
        "auto_paused_until": paused_until,
        "manual_required": bool(runtime.get("manual_required")),
        "manual_reason": runtime.get("manual_reason"),
        "last_run": {
            "at": last_run.get("at"),
            "dry_run": bool(last_run.get("dry_run", False)),
            "ok": len(last_run.get("ok") or []),
            "failed": len(last_run.get("failed") or []),
            "skipped": len(last_run.get("skipped") or []),
            "unknown": len(last_run.get("unknown") or []),
            "logged_out": bool(last_run.get("logged_out")),
            "rate_limited": bool(last_run.get("rate_limited")),
        } if last_run else None,
        "ledger_total": len(ledger_entries),
        "ledger_selected": sum(1 for e in ledger_entries if isinstance(e, dict) and e.get("selected")),
    }
    with _lock:
        _overview_cache[account_id] = (fingerprint, copy.deepcopy(result))
        while len(_overview_cache) > MAX_ACCOUNTS:
            _overview_cache.pop(next(iter(_overview_cache)))
    return result

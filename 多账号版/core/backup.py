"""每账号数据备份与 7 份轮转（M6 备份与运维）。

- 备份位置严格限定在 ``<data>/backups/``（已 gitignore），**绝不放 static/**，
  避免历史上备份文件被公网直接下载的问题。
- 每个槽位是一个目录 ``daily-1..daily-7``，内容：
  ``manifest.json``、``accounts.json``（注册表副本）、
  ``<账号id>/{state,config,ledger,runtime}.json``（存在才复制）。
- 轮转策略：每次写入“下一个槽位”，第 8 次起覆盖最旧槽位（比按星期命名更能
  容忍中途跳过，永远保留最近 7 份、自动覆盖）。
- 纯文件秒级复制，不持长锁、不启动浏览器；与发送任务的互斥 / 延后重试由
  ``multi_service.scheduled_backup`` 负责，本模块只做文件与状态。
- 状态文件 ``backups/backup.state.json`` 记录当前槽位、各槽位摘要、最近结果与
  最近一次因发送占用而跳过的信息（供面板可见告警）。

云端目标 Python 3.9：只用标准库，文件写入走 core.config 的原子写。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from core import accounts as A
from core.config import atomic_write_text

logger = logging.getLogger("douyin-cloud-streak")

BACKUP_ROOT = A.ACCOUNTS_ROOT.parent / "backups"
STATE_PATH = BACKUP_ROOT / "backup.state.json"
REGISTRY_NAME = "accounts.json"
SLOT_PREFIX = "daily-"
SLOT_COUNT = 7

# 每个账号需要备份的私有数据（存在才复制，缺失计入 manifest 而非报错）。
ACCOUNT_FILES = [A.STATE_NAME, A.CONFIG_NAME, A.LEDGER_NAME, A.RUNTIME_NAME]

_TRUE = {"1", "true", "yes", "on"}


def backup_enabled() -> bool:
    """备份总开关，默认开启（独立于发送开关 SPARKKEEPER_AUTO_RUN）。"""
    return os.environ.get("SPARKKEEPER_BACKUP_ENABLED", "1").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_state() -> dict:
    return {
        "version": 1,
        "current_slot": 0,
        "slots": {},          # key: "1".."7"
        "last_result": None,  # 最近一次备份结果（成功/失败）
        "last_skip": None,    # 最近一次因发送占用而延后 / 放弃的信息
    }


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update(data)
            return base
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("读取备份状态失败，按空状态重建：%s", STATE_PATH)
    return _default_state()


def _save_state(state: dict) -> None:
    atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))


def _cleanup_stale_tmp() -> None:
    """清理上次崩溃可能残留的临时目录（.tmp-*），忽略错误。"""
    try:
        for child in BACKUP_ROOT.glob(".tmp-*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    except Exception:
        pass


def _copy_file(src: Path, dst: Path) -> int:
    """复制单个文件，返回字节数；源不存在返回 -1。"""
    if not src.exists():
        return -1
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return src.stat().st_size


def create_backup(*, reason: str = "manual") -> dict:
    """执行一次备份并轮转槽位。

    返回结果 dict（同时写入 state.last_result）。账号级缺文件 / 单文件复制失败
    只计入 warnings，不让整次备份失败；只有槽位无法落盘才算失败。
    """
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_tmp()
    state = _load_state()

    next_slot = (int(state.get("current_slot") or 0) % SLOT_COUNT) + 1
    slot_dir = BACKUP_ROOT / f"{SLOT_PREFIX}{next_slot}"
    tmp_dir = BACKUP_ROOT / f".tmp-{os.getpid()}-{int(time.time())}"
    tmp_dir.mkdir(parents=True, exist_ok=False)

    started = _now()
    account_entries: dict = {}
    total_files = 0
    total_bytes = 0
    warnings: list = []

    try:
        # 1) 注册表副本（含显示名 / 启用态 / 顺序 / 收件人等）。
        registry_copied = False
        if A.REGISTRY_PATH.exists():
            size = _copy_file(A.REGISTRY_PATH, tmp_dir / REGISTRY_NAME)
            if size >= 0:
                registry_copied = True
                total_files += 1
                total_bytes += size
        else:
            warnings.append("注册表 accounts.json 不存在")

        # 2) 各账号私有四件套。
        for meta in A.list_accounts():
            aid = meta["id"]
            files_meta: dict = {}
            missing: list = []
            try:
                A.validate_account_id(aid)  # 防御：id 必须安全，禁止路径穿越
            except Exception as e:
                warnings.append(f"账号 id 非法已跳过：{aid}（{e}）")
                continue
            for name in ACCOUNT_FILES:
                src = A.account_file(aid, name)
                size = _copy_file(src, tmp_dir / aid / name)
                if size >= 0:
                    files_meta[name] = size
                    total_files += 1
                    total_bytes += size
                else:
                    missing.append(name)
            account_entries[aid] = {
                "name": meta.get("display_name") or aid,
                "files": files_meta,
                "missing": missing,
            }

        # 3) 清单。
        manifest = {
            "created_at": started,
            "reason": reason,
            "slot": next_slot,
            "registry": registry_copied,
            "account_count": len(account_entries),
            "accounts": account_entries,
            "file_count": total_files,
            "bytes": total_bytes,
            "warnings": warnings,
            "version": 1,
        }
        atomic_write_text(tmp_dir / "manifest.json",
                          json.dumps(manifest, ensure_ascii=False, indent=2))

        # 4) 整体替换槽位（先删旧槽，再同卷 rename，短窗口可接受）。
        if slot_dir.exists():
            shutil.rmtree(slot_dir, ignore_errors=False)
        os.replace(tmp_dir, slot_dir)

        result = {
            "ok": True, "at": started, "slot": next_slot, "reason": reason,
            "dir": slot_dir.name, "registry": registry_copied,
            "accounts": len(account_entries), "files": total_files,
            "bytes": total_bytes, "warnings": warnings,
        }
        state["current_slot"] = next_slot
        state["slots"][str(next_slot)] = {
            "slot": next_slot, "dir": slot_dir.name, "created_at": started,
            "reason": reason, "accounts": len(account_entries),
            "files": total_files, "bytes": total_bytes,
        }
        state["last_result"] = result
        state["last_skip"] = None
        _save_state(state)
        logger.info("备份完成：%s（账号 %s，文件 %s，%.1f KB）",
                    slot_dir.name, len(account_entries), total_files,
                    total_bytes / 1024)
        return result
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        result = {"ok": False, "at": started, "slot": next_slot,
                  "reason": reason, "error": err}
        state["last_result"] = result
        try:
            _save_state(state)
        except Exception:
            logger.exception("备份失败后写状态也失败")
        logger.exception("备份失败：%s", err)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return result


def mark_skip(*, reason: str, attempt: int, next_retry_at: str | None = None) -> None:
    """记录一次因发送占用导致的延后 / 放弃（面板可见告警），不改变槽位。"""
    state = _load_state()
    state["last_skip"] = {
        "at": _now(), "reason": reason, "attempt": attempt,
        "next_retry_at": next_retry_at,
    }
    try:
        _save_state(state)
    except Exception:
        logger.exception("写备份跳过状态失败")


def list_backups() -> dict:
    """返回备份概览（供 /api/multi/backups 与面板）。"""
    state = _load_state()
    slots = []
    for i in range(1, SLOT_COUNT + 1):
        meta = state.get("slots", {}).get(str(i))
        if meta:
            slots.append(meta)
        else:
            slots.append({"slot": i, "dir": f"{SLOT_PREFIX}{i}",
                          "created_at": None, "reason": None,
                          "accounts": 0, "files": 0, "bytes": 0})
    return {
        "enabled": backup_enabled(),
        "slot_count": SLOT_COUNT,
        "backup_time": os.environ.get("SPARKKEEPER_BACKUP_TIME", "23:00"),
        "current_slot": state.get("current_slot", 0),
        "slots": slots,
        "last_result": state.get("last_result"),
        "last_skip": state.get("last_skip"),
    }

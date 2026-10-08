"""In-process reservations shared by sending, collection and account mutations.

Reserve synchronously before starting a background thread. A reservation is a
token, rather than a thread-owned lock, so the worker may release it safely.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path


class JobBusy(RuntimeError):
    """A conflicting job is already reserved or running."""


class JobManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}

    def reserve(self, *, account_id: str | None, kind: str,
                triggered: str = "manual", global_scope: bool = False) -> dict:
        if not kind or (not global_scope and not account_id):
            raise ValueError("账号任务需要 account_id；全局任务需要 global_scope=True")
        with self._lock:
            for active in self._jobs.values():
                if (global_scope or active["global_scope"]
                        or active["account_id"] == account_id
                        or (kind == "send" and active["kind"] == "send")):
                    raise JobBusy("账号或任务通道正忙，请等待当前任务结束")
            job = {
                "job_id": uuid.uuid4().hex,
                "account_id": account_id,
                "kind": kind,
                "triggered": triggered,
                "global_scope": bool(global_scope),
                "started_at": time.time(),
                "phase": "reserved",
            }
            self._jobs[job["job_id"]] = job
            return dict(job)

    def claim(self, reservation: dict, *, account_id: str | None, kind: str) -> dict:
        with self._lock:
            job = self.require(reservation, account_id=account_id, kind=kind)
            if job["phase"] != "reserved":
                raise JobBusy("任务预留已经开始执行")
            self._jobs[job["job_id"]]["phase"] = "running"
            return dict(self._jobs[job["job_id"]])

    def require(self, reservation: dict, *, account_id: str | None, kind: str) -> dict:
        with self._lock:
            job = self._jobs.get(reservation.get("job_id"))
            if not job or job["account_id"] != account_id or job["kind"] != kind:
                raise ValueError("任务预留已失效或不属于本次操作")
            return dict(job)

    def release(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def is_active(self, job_id: str | None = None, *, account_id: str | None = None,
                  kind: str | None = None) -> bool:
        with self._lock:
            return any((job_id is None or j["job_id"] == job_id)
                       and (account_id is None or j["global_scope"] or j["account_id"] == account_id)
                       and (kind is None or j["kind"] == kind)
                       for j in self._jobs.values())

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]


_manager = JobManager()


def reserve(*, account_id: str | None, kind: str, triggered: str = "manual",
            global_scope: bool = False) -> dict:
    """Reserve controller resources and reject surviving workers from an old controller."""
    reservation = _manager.reserve(account_id=account_id, kind=kind,
                                   triggered=triggered, global_scope=global_scope)
    try:
        # Lazy imports keep the pure manager usable without project paths or I/O.
        from core import accounts as A
        from core.config import DATA_DIR
        from core.storage import file_lock
        # A login browser occupies the whole controller, including after its
        # parent has gone away. The worker keeps this OS guard until it closes.
        extraction_guard = DATA_DIR / ".credential-extract" / ".guard"
        if Path(str(extraction_guard) + ".lock").exists():
            with file_lock(extraction_guard, timeout=0.1):
                pass
        account_ids = [a["id"] for a in A.list_accounts()] if global_scope else [account_id]
        for aid in account_ids:
            directory = A.account_dir(aid)
            if directory.is_dir():
                with file_lock(directory / ".worker.guard", timeout=0.1):
                    pass
    except TimeoutError as exc:
        _manager.release(reservation["job_id"])
        raise JobBusy("仍有浏览器执行器运行，请等待执行器结束") from exc
    except Exception:
        _manager.release(reservation["job_id"])
        raise
    return reservation


require = _manager.require
claim = _manager.claim
release = _manager.release
is_active = _manager.is_active
snapshot = _manager.snapshot

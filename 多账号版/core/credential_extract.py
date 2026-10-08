"""Account-scoped, human-confirmed local browser login extraction.

The web process owns the frozen account and the global job reservation. A fresh
child process owns Chromium and only writes a private candidate. No credentials
or screenshots cross the public status API; confirmation is the sole commit.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

MAX_STATE_BYTES = 5 * 1024 * 1024
TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
ACTIVE_STATUSES = {"starting", "waiting", "ready", "saving", "stopping"}
ERRORS = {
    "startup_failure": "浏览器启动失败，请检查本机 Chromium 安装。",
    "browser_closed": "登录浏览器已关闭，本次提取已结束。",
    "invalid_state": "未获得有效的抖音登录状态，请重新登录。",
    "worker_failed": "登录提取未完成，请重试。",
    "timeout": "本次提取超过 5 分钟，旧凭据已保留。",
    "save_failed": "保存失败，旧凭据已保留。可以重试保存或取消。",
    "cleanup_failed": "浏览器尚未完全关闭，请稍候。任务仍保持互斥。",
    "file_cleanup_failed": "临时登录凭据尚未清理完成，任务仍保持互斥。",
}


class CredentialExtractBusy(RuntimeError):
    pass


class CredentialExtractNotFound(RuntimeError):
    pass


class CredentialExtractInvalid(ValueError):
    pass


def has_login_cookie(cookies, *, now: float | None = None) -> bool:
    """Recognize a nonempty, unexpired session cookie on the Douyin domain.

    This is a login-presence check, not verification of the person's identity.
    """
    now = time.time() if now is None else now
    if not isinstance(cookies, list):
        return False
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get("name") not in {"sessionid", "sessionid_ss"}:
            continue
        value, domain, expires = cookie.get("value"), cookie.get("domain"), cookie.get("expires", -1)
        if not isinstance(value, str) or not value.strip() or not isinstance(domain, str):
            continue
        domain = domain.lower().lstrip(".")
        if domain != "douyin.com" and not domain.endswith(".douyin.com"):
            continue
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or (expires != -1 and expires <= now)):
            continue
        return True
    return False


def validate_storage_state(raw: bytes, *, require_login: bool = False, now=None) -> dict:
    """Validate bounded Playwright storage state without putting values in errors."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_STATE_BYTES:
        raise CredentialExtractInvalid("登录态文件为空或超过 5 MB。")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CredentialExtractInvalid("登录态文件不是有效的 JSON。") from None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise CredentialExtractInvalid("登录态文件缺少 cookies 数组。")
    if len(data["cookies"]) > 2000:
        raise CredentialExtractInvalid("登录态文件结构不合法。")
    for cookie in data["cookies"]:
        if not isinstance(cookie, dict):
            raise CredentialExtractInvalid("登录态 Cookie 结构不合法。")
        if not all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")):
            raise CredentialExtractInvalid("登录态 Cookie 结构不合法。")
        if not cookie["name"] or not cookie["domain"] or not cookie["path"].startswith("/"):
            raise CredentialExtractInvalid("登录态 Cookie 结构不合法。")
        expires = cookie.get("expires", -1)
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or expires < -1):
            raise CredentialExtractInvalid("登录态 Cookie 结构不合法。")
        if any(key in cookie and not isinstance(cookie[key], bool) for key in ("secure", "httpOnly")):
            raise CredentialExtractInvalid("登录态 Cookie 结构不合法。")
        if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
            raise CredentialExtractInvalid("登录态 Cookie 结构不合法。")
    origins = data.get("origins", [])
    if not isinstance(origins, list) or len(origins) > 100:
        raise CredentialExtractInvalid("登录态 origins 结构不合法。")
    for origin in origins:
        if (not isinstance(origin, dict) or not isinstance(origin.get("origin"), str)
                or not origin["origin"].startswith(("https://", "http://"))
                or not isinstance(origin.get("localStorage"), list)):
            raise CredentialExtractInvalid("登录态 origins 结构不合法。")
        if not all(isinstance(item, dict) and isinstance(item.get("name"), str)
                   and isinstance(item.get("value"), str) for item in origin["localStorage"]):
            raise CredentialExtractInvalid("登录态 localStorage 结构不合法。")
    if require_login and not has_login_cookie(data["cookies"], now=now):
        raise CredentialExtractInvalid(ERRORS["invalid_state"])
    return data


def build_worker_cmd(task_dir: Path, account_dir: Path, task_id: str, deadline: float,
                     guard_file: Path) -> list[str]:
    entry = [sys.executable, "--credential-worker"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "core.credential_worker"]
    return [*entry, "--task-dir", str(task_dir), "--account-dir", str(account_dir),
            "--task-id", task_id, "--deadline", str(deadline), "--guard-file", str(guard_file),
            "--parent-pid", str(os.getpid())]


class CredentialExtractManager:
    def __init__(self, timeout_seconds: float = 300, *, root: Path | None = None,
                 poll_interval: float = .2, popen=None):
        self.timeout_seconds = min(300., max(.1, float(timeout_seconds)))
        self.root = Path(root).resolve() if root is not None else None
        self.poll_interval = poll_interval
        self._popen = popen or subprocess.Popen
        self._lock = threading.RLock()
        self._task = None
        self._closed = False

    def _root(self) -> Path:
        if self.root is not None:
            return self.root
        from core.config import DATA_DIR
        return (DATA_DIR / ".credential-extract").resolve()

    def _snapshot(self, task=None) -> dict:
        task = task or self._task
        if task is None:
            return {"job_id": None, "account_id": None, "display_name": None, "status": "idle",
                    "running": False, "ready": False, "count": 0, "started_at": None,
                    "deadline": None, "remaining_seconds": 0, "error": None}
        return {"job_id": task["job_id"], "account_id": task["account_id"],
                "display_name": task["display_name"], "status": task["status"],
                "running": task["status"] in ACTIVE_STATUSES, "ready": task["status"] == "ready",
                "count": task["count"], "started_at": task["started_at"], "deadline": task["deadline"],
                "remaining_seconds": max(0, math.ceil(task["end_monotonic"] - time.monotonic()))
                if task["status"] in ACTIVE_STATUSES else 0, "error": task["error"]}

    def _require(self, aid, job_id=None):
        task = self._task
        if (task is None or (aid is not None and task["account_id"] != aid)
                or (job_id is not None and task["job_id"] != job_id)):
            raise CredentialExtractNotFound("提取任务不存在或已经被新任务替换。")
        return task

    def _prune_closed_tasks(self, root):
        """Caller holds the global OS guard: no login worker can own these."""
        keep = self._task["dir"] if self._task and self._task["status"] in ACTIVE_STATUSES else None
        for directory in root.iterdir():
            if directory == keep or not TASK_ID_RE.fullmatch(directory.name) or not directory.is_dir():
                continue
            if directory.resolve().parent != root.resolve() or directory.is_symlink():
                continue
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                # A concurrent external cleanup may already have removed it.
                pass
            except OSError:
                raise CredentialExtractBusy(ERRORS["file_cleanup_failed"]) from None

    def reopen(self) -> bool:
        """Permit a later lifespan in the same desktop process after cleanup."""
        from core.storage import file_lock
        with self._lock:
            if self._task and self._task["status"] in ACTIVE_STATUSES:
                return False
            root = self._root()
            if root.is_dir():
                try:
                    with file_lock(root / ".guard", timeout=.1):
                        self._prune_closed_tasks(root)
                except (TimeoutError, CredentialExtractBusy):
                    # A surviving worker retains its own hard deadline. Keep
                    # the service readable and let start() reject overlap.
                    pass
            self._closed = False
            return True

    def start(self, aid: str) -> dict:
        from core import accounts as A, jobs
        from core.orchestrator import BASE_DIR, build_worker_env
        from core.storage import file_lock
        A.validate_account_id(aid)
        meta = A.get_account(aid)
        if meta is None:
            raise CredentialExtractInvalid("账号不存在。")
        with self._lock:
            if self._closed:
                raise CredentialExtractBusy("服务正在关闭。")
            if self._task and self._task["status"] in ACTIVE_STATUSES:
                raise CredentialExtractBusy("已有凭据提取任务，请先完成或取消。")
            root = self._root()
            root.mkdir(parents=True, exist_ok=True)
            guard = root / ".guard"
            try:
                with file_lock(guard, timeout=.1):
                    self._prune_closed_tasks(root)
                reservation = jobs.reserve(account_id=aid, kind="credentials", global_scope=True)
            except (jobs.JobBusy, TimeoutError):
                raise CredentialExtractBusy("已有账号任务或登录浏览器运行，请稍后重试。") from None
            task_id = uuid.uuid4().hex
            task_dir = root / task_id
            task = {"job_id": task_id, "account_id": aid, "display_name": meta.get("display_name") or aid,
                    "status": "starting", "count": 0, "error": None, "started_at": time.time(),
                    "deadline": time.time() + self.timeout_seconds,
                    "end_monotonic": time.monotonic() + self.timeout_seconds,
                    "dir": task_dir, "proc": None, "browser_closed": False,
                    "reservation": reservation, "released": False}
            self._task = task
            try:
                task_dir.mkdir(mode=0o700)
                jobs.claim(reservation, account_id=aid, kind="credentials")
                env = build_worker_env()
                env["DATA_DIR"] = str(A.account_dir(aid))
                env["PYTHONUTF8"] = "1"
                flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
                task["proc"] = self._popen(
                    build_worker_cmd(task_dir, A.account_dir(aid), task_id, task["deadline"], guard),
                    cwd=str(BASE_DIR), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags, start_new_session=os.name != "nt")
                # Do not announce a task until its child owns both OS guards.
                # If this controller dies before that point, the child checks
                # its parent PID and exits without opening a login browser.
                handshake_end = min(task["end_monotonic"], time.monotonic() + 5)
                while time.monotonic() < handshake_end:
                    try:
                        acknowledgement = json.loads((task_dir / "guard-ready.json").read_text(encoding="utf-8"))
                        if isinstance(acknowledgement, dict) and acknowledgement.get("job_id") == task_id:
                            break
                    except (OSError, ValueError):
                        pass
                    if task["proc"].poll() is not None:
                        raise CredentialExtractInvalid(ERRORS["startup_failure"])
                    time.sleep(.02)
                else:
                    raise CredentialExtractInvalid(ERRORS["startup_failure"])
                thread = threading.Thread(target=self._monitor, args=(task,), daemon=True,
                                          name="credential-extract-monitor")
                task["thread"] = thread
                thread.start()
            except Exception:
                if self._stop_process(task):
                    self._finish(task, "failed", "startup_failure")
                else:
                    task.update(status="stopping", error=ERRORS["cleanup_failed"])
                raise CredentialExtractInvalid(ERRORS["startup_failure"]) from None
            return self._snapshot(task)

    def status(self, aid=None, job_id=None) -> dict:
        with self._lock:
            if self._task is None and aid is None and job_id is None:
                return self._snapshot()
            task = self._require(aid, job_id)
            self._poll(task)
            return self._snapshot(task)

    def _read_worker_state(self, task):
        try:
            path = task["dir"] / "status.json"
            if path.stat().st_size > 4096:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("job_id") == task["job_id"] else None
        except (OSError, ValueError):
            return None

    def _poll(self, task):
        if task is not self._task or task["status"] not in ACTIVE_STATUSES:
            return
        if task["status"] in {"saving", "stopping"}:
            return
        if time.monotonic() >= task["end_monotonic"]:
            self._end(task, "timeout", "timeout")
            return
        state = self._read_worker_state(task)
        if state:
            status = state.get("status")
            if status in {"waiting", "ready"}:
                task["status"] = status
                count = state.get("count", 0)
                task["count"] = min(2000, max(0, count)) if isinstance(count, int) and not isinstance(count, bool) else 0
            elif status in {"failed", "timeout"}:
                code = state.get("error_code")
                self._end(task, status, code if code in ERRORS else "worker_failed")
                return
        if task["proc"] is not None and task["proc"].poll() is not None and not task["browser_closed"]:
            self._end(task, "failed", "worker_failed")

    def _monitor(self, task):
        while True:
            with self._lock:
                if task is not self._task or task["status"] not in ACTIVE_STATUSES:
                    return
                if task["status"] == "stopping":
                    if self._stop_process(task):
                        self._finish(task, task.get("ending_status", "failed"), task.get("ending_error", "worker_failed"))
                else:
                    self._poll(task)
            time.sleep(self.poll_interval)

    def _stop_process(self, task) -> bool:
        from core.orchestrator import _terminate_tree
        from core.storage import atomic_write_text
        proc = task["proc"]
        if proc is None or task["browser_closed"]:
            return True
        try:
            atomic_write_text(task["dir"] / "stop.json", json.dumps({"job_id": task["job_id"]}))
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                _terminate_tree(proc)
                proc.wait(timeout=5)
            except Exception:
                return False
        task["browser_closed"] = True
        return True

    def _finish(self, task, status, error_code=None):
        from core import jobs
        # This tree was generated from a UUID below our own root; check it again
        # before recursive cleanup. Never delete account or user-supplied paths.
        directory = task["dir"].resolve()
        root = self._root().resolve()
        if directory.parent == root and TASK_ID_RE.fullmatch(directory.name):
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                # Startup can fail before mkdir succeeds; an already removed
                # task directory also needs no further cleanup or reservation.
                pass
            except OSError:
                task.update(status="stopping", ending_status=status, ending_error=error_code,
                            error=ERRORS["file_cleanup_failed"])
                return False
        task.update(status=status, error=ERRORS.get(error_code), count=task["count"])
        if not task["released"]:
            jobs.release(task["reservation"]["job_id"])
            task["released"] = True
        return True

    def _end(self, task, status, error_code=None):
        task.update(status="stopping", ending_status=status, ending_error=error_code)
        if self._stop_process(task):
            self._finish(task, status, error_code)
        else:
            task["error"] = ERRORS["cleanup_failed"]

    def confirm(self, aid: str, job_id: str) -> dict:
        from core import accounts as A
        from core.config import atomic_write_bytes
        with self._lock:
            task = self._require(aid, job_id)
            self._poll(task)
            if task["status"] != "ready":
                raise CredentialExtractInvalid("当前任务尚未准备好，或已经结束。")
            task.update(status="saving", error=None)
            if not self._stop_process(task):
                task.update(status="stopping", ending_status="failed", ending_error="cleanup_failed",
                            error=ERRORS["cleanup_failed"])
                raise CredentialExtractInvalid(ERRORS["cleanup_failed"])
            if time.monotonic() >= task["end_monotonic"]:
                self._finish(task, "timeout", "timeout")
                raise CredentialExtractInvalid(ERRORS["timeout"])
            try:
                candidate = task["dir"] / "candidate.json"
                if candidate.stat().st_size > MAX_STATE_BYTES:
                    raise CredentialExtractInvalid(ERRORS["invalid_state"])
                raw = candidate.read_bytes()
                data = validate_storage_state(raw, require_login=True)
                if A.get_account(task["account_id"]) is None:
                    raise CredentialExtractInvalid("目标账号已经不存在。")
            except (OSError, CredentialExtractInvalid):
                self._finish(task, "failed", "invalid_state")
                raise CredentialExtractInvalid(ERRORS["invalid_state"]) from None
            try:
                atomic_write_bytes(A.account_file(task["account_id"], "state.json"), raw)
            except Exception:
                task.update(status="ready", error=ERRORS["save_failed"])
                raise CredentialExtractInvalid(ERRORS["save_failed"]) from None
            task["count"] = len(data["cookies"])
            self._finish(task, "success")
            return self._snapshot(task)

    def cancel(self, aid: str, job_id: str) -> dict:
        with self._lock:
            task = self._require(aid, job_id)
            if task["status"] == "stopping":
                if self._stop_process(task):
                    self._finish(task, task.get("ending_status", "cancelled"), task.get("ending_error"))
            elif task["status"] in ACTIVE_STATUSES:
                self._end(task, "cancelled")
            return self._snapshot(task)

    def cleanup(self):
        with self._lock:
            self._closed = True
            if self._task and self._task["status"] == "stopping":
                task = self._task
                if self._stop_process(task):
                    self._finish(task, task.get("ending_status", "cancelled"), task.get("ending_error"))
            elif self._task and self._task["status"] in ACTIVE_STATUSES:
                self._end(self._task, "cancelled")


manager = CredentialExtractManager()

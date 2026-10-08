"""Fresh visible login browser, bounded even if its controller disappears.

Only the private task directory receives the candidate. The account's current
state.json is never opened or changed by this worker.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from contextlib import ExitStack
from pathlib import Path


def _write_status(task_dir, task_id, status, *, count=0, error_code=None):
    from core.storage import atomic_write_text
    atomic_write_text(task_dir / "status.json", json.dumps(
        {"job_id": task_id, "status": status, "count": count, "error_code": error_code}))


def _stop_requested(task_dir, task_id):
    try:
        path = task_dir / "stop.json"
        if path.stat().st_size > 1024:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("job_id") == task_id
    except (OSError, ValueError):
        return False


def run_login(task_dir: Path, task_id: str, deadline: float, *, browser_factory=None,
              clock=time.time, sleep=time.sleep):
    """Run login and await controller confirmation/cancellation, never commit.

    Injectable browser/clock exist for offline tests; production always uses a
    fresh visible browser and no saved state.
    """
    from core.credential_extract import has_login_cookie, validate_storage_state
    from core.storage import atomic_write_text
    if browser_factory is None:
        from core.browser import open_browser
        browser_factory = open_browser
    ready = False
    retain_candidate = False
    try:
        _write_status(task_dir, task_id, "starting")
        with browser_factory(headless=False, use_state=False) as (_, browser, context, page):
            if _stop_requested(task_dir, task_id):
                return 0
            _write_status(task_dir, task_id, "waiting")
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                          timeout=max(1, min(60000, int((deadline - clock()) * 1000))))
            except Exception:
                # Navigation can time out while the user-facing page is usable.
                # Cookie polling remains bounded; exceptions never reach logs.
                pass
            while clock() < deadline:
                if _stop_requested(task_dir, task_id):
                    retain_candidate = ready
                    return 0
                if not browser.is_connected() or page.is_closed():
                    _write_status(task_dir, task_id, "failed", error_code="browser_closed")
                    return 1
                if not ready and has_login_cookie(context.cookies(), now=clock()):
                    state = context.storage_state()
                    raw = json.dumps(state, ensure_ascii=False).encode("utf-8")
                    data = validate_storage_state(raw, require_login=True, now=clock())
                    atomic_write_text(task_dir / "candidate.json", raw.decode("utf-8"))
                    ready = True
                    _write_status(task_dir, task_id, "ready", count=len(data["cookies"]))
                sleep(.2 if ready else .5)
            _write_status(task_dir, task_id, "timeout", error_code="timeout")
            return 2
    except Exception:
        _write_status(task_dir, task_id, "failed", error_code="worker_failed" if ready else "startup_failure")
        return 1
    finally:
        # A controller stop may be a confirmation: retain the candidate for its
        # atomic commit. An orphan that times out must discard its own candidate.
        if not retain_candidate:
            try:
                (task_dir / "candidate.json").unlink(missing_ok=True)
            except OSError:
                pass


def _hard_stop_own_tree():
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        finally:
            os._exit(2)
    else:
        # The controller creates a new session for this worker; never kill the
        # caller's process group if this entry was launched differently.
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGKILL)
        os._exit(2)


def _parent_alive(parent_pid):
    if parent_pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, parent_pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(parent_pid, 0)
        return True
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local account login browser worker")
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--account-dir", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--deadline", required=True, type=float)
    parser.add_argument("--guard-file", required=True, type=Path)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args(argv)
    # Fix import-time state paths before importing any business modules. No env
    # file is read and no root-state fallback is permitted in a login worker.
    os.environ["DATA_DIR"] = str(args.account_dir.resolve())
    os.environ.pop("STATE_FILE_PATH", None)
    os.environ["SPARKKEEPER_NO_ROOT_STATE_FALLBACK"] = "1"
    from core.credential_extract import TASK_ID_RE
    from core.storage import file_lock
    task_dir = args.task_dir.resolve()
    guard = args.guard_file.resolve()
    if (not TASK_ID_RE.fullmatch(args.task_id) or task_dir.name != args.task_id
            or task_dir.parent != guard.parent or not task_dir.is_dir()
            or not args.account_dir.is_dir()):
        return 2
    if not _parent_alive(args.parent_pid):
        return 2
    seconds = max(0, min(300, args.deadline - time.time()))
    if not seconds:
        _write_status(task_dir, args.task_id, "timeout", error_code="timeout")
        return 2
    done = threading.Event()
    def watchdog():
        # Independent of Playwright, so stuck launch/navigation/close cannot
        # leave an orphan indefinitely after the parent process has exited.
        if not done.wait(seconds + 5):
            try:
                _write_status(task_dir, args.task_id, "timeout", error_code="timeout")
                (task_dir / "candidate.json").unlink(missing_ok=True)
            finally:
                _hard_stop_own_tree()
    threading.Thread(target=watchdog, daemon=True, name="credential-browser-deadline").start()
    try:
        with ExitStack() as stack:
            stack.enter_context(file_lock(guard, timeout=.1))
            stack.enter_context(file_lock(args.account_dir.resolve() / ".worker.guard", timeout=.1))
            if not _parent_alive(args.parent_pid):
                return 2
            from core.storage import atomic_write_text
            atomic_write_text(task_dir / "guard-ready.json", json.dumps({"job_id": args.task_id}))
            return run_login(task_dir, args.task_id, time.time() + seconds)
    except Exception:
        _write_status(task_dir, args.task_id, "failed", error_code="worker_failed")
        return 1
    finally:
        done.set()


if __name__ == "__main__":
    raise SystemExit(main())

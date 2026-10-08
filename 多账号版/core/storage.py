"""Small, process-safe primitives for JSON read/modify/write transactions."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def file_lock(path: Path, timeout: float = 30):
    """Lock a stable sidecar, including nested use in the same thread.

    Never lock the JSON inode itself: atomic replacement changes that inode.
    The OS releases this lock when a worker dies; sidecar files stay in place.
    """
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _registry_guard:
        thread_lock = _thread_locks.setdefault(key, threading.RLock())
    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError(f"Timed out acquiring storage lock: {path.name}")
    depths = getattr(_local, "depths", None)
    if depths is None:
        depths = _local.depths = {}
    handle = None
    acquired = False
    try:
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(Path(str(path) + ".lock"), "a+b")
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring storage lock: {path.name}")
                time.sleep(0.02)
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
    finally:
        try:
            if handle is not None:
                try:
                    if acquired:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            thread_lock.release()


def atomic_write_text(path: Path, text: str) -> None:
    """Replace only after fsync; failure preserves the previous destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp.")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (2 ** attempt))
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass

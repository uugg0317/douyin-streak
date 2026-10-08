"""多账号串行编排器（M4 多账号改造）。

每晚 00:00（或手动触发）由 Web 主进程在后台线程调用 :func:`run_all`：
按注册表顺序，**逐账号**用子进程运行 core.worker（见 M4.2），一个账号跑完、
浏览器随子进程关闭后再跑下一个，绝不并发。编排器自身不导入 core.automation、
不启动浏览器，只负责调度子进程、读结果、按号熔断跳过与异常隔离。

- 冷却与人工处理状态按账号隔离；手动触发也遵守安全阻断，演练只绕过失败冷却；
- 单号无凭证 / 超时 / 非零退出 / 异常都只记为该号失败，**不中断后续账号**；
- 汇总结果 :func:`run_all` 返回，由调用方决定是否发漏发邮件（M4.4），本模块不耦合邮件。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import accounts as A, jobs
from core.runtime import breaker_policy
from core.storage import file_lock, atomic_write_text

logger = logging.getLogger("douyin-cloud-streak")

BASE_DIR = Path(__file__).resolve().parent.parent
RESULT_PREFIX = "WORKER_RESULT "
# 单号硬超时（秒）：70 人 × 间隔 2~3 秒 + 冷启动约 4~5 分钟，30 分钟留足余量。
DEFAULT_ACCOUNT_TIMEOUT = 1800

# 这些状态都属于「没有按预期把消息发出去」，需要进入漏发报告。
MISS_STATUSES = {
    "failed", "partial", "logged_out", "rate_limited", "timeout",
    "executor_error", "no_state", "breaker_skipped", "empty",
    "unknown", "manual_required",
    "busy",
}

_lock = threading.Lock()


class OrchestratorBusy(RuntimeError):
    """已有一轮多账号编排正在运行。"""


# ── 子进程装配 ─────────────────────────────────────────────────────────────

def build_worker_env() -> dict:
    """子进程环境：继承主进程（PATH、DISPLAY、SMTP 等），但清掉 DATA_DIR，
    由 worker 自己按 --data-dir 设置，避免继承值抢先固化。"""
    env = dict(os.environ)
    env.pop("DATA_DIR", None)
    # The single-account override takes precedence over DATA_DIR in core.config.
    # A multi-account worker must only open its own account's state.json.
    env.pop("STATE_FILE_PATH", None)
    env["SPARKKEEPER_NO_ROOT_STATE_FALLBACK"] = "1"
    return env


def build_worker_cmd(account_dir: Path, dry_run: bool, only_names, budget,
                    mode: str = "send") -> list[str]:
    entry = [sys.executable, "--worker"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "core.worker"]
    argv = [*entry, "--data-dir", str(account_dir),
            "--mode", mode]
    if dry_run:
        argv.append("--dry-run")
    if only_names:
        argv += ["--only", ",".join(str(n) for n in only_names)]
    if budget is not None:
        argv += ["--budget", str(budget)]
    return argv


def _terminate_tree(proc: subprocess.Popen) -> None:
    """连子进程树一起终止（worker 会派生 Chromium，只 kill 父进程会留下浏览器）。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=20,
            )
            return
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def run_subprocess(argv: list[str], *, cwd: Path, env: dict, timeout: float,
                   stdin_text: str | None = None):
    """运行子进程并强制超时杀树，返回 (rc, stdout, stderr, timed_out, duration_sec)。"""
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    start = time.time()
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        text=True, encoding="utf-8", errors="replace",
        creationflags=creationflags, start_new_session=os.name != "nt",
    )
    timed_out = False
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.warning("子进程超过 %.0f 秒，终止进程树：%s", timeout, " ".join(argv[:4]))
        _terminate_tree(proc)
        try:
            out, err = proc.communicate(timeout=25)
        except Exception:
            out, err = "", ""
    return proc.returncode, out or "", err or "", timed_out, round(time.time() - start, 1)


def _default_runner(account_dir: Path, dry_run: bool, only_names, timeout: float, budget):
    argv = build_worker_cmd(account_dir, dry_run, only_names, budget, mode="send")
    return run_subprocess(argv, cwd=BASE_DIR, env=build_worker_env(), timeout=timeout)


def run_worker(account_id: str, *, mode: str = "send", dry_run: bool = False,
               only_names=None, timeout: float = DEFAULT_ACCOUNT_TIMEOUT, budget=0,
               reservation: dict | None = None) -> dict:
    """对单个账号同步运行一次 worker 子进程（用于手动采集等单号任务，不经串行汇总）。

    返回 rc / 解析后的 envelope / 原始输出 / 是否超时；调用方自行在后台线程中调用。
    """
    kind = "contacts" if mode == "fetch-contacts" else mode
    reservation = reservation or jobs.reserve(account_id=account_id, kind=kind)
    reservation = jobs.claim(reservation, account_id=account_id, kind=kind)
    try:
        d = A.account_dir(account_id)
        if not d.is_dir():
            raise FileNotFoundError(f"账号数据目录不存在：{d}")
        if mode == "send" and _blocked_item(A.get_account(account_id) or {"id": account_id}, dry_run):
            return {"rc": 3, "result": {"status": "manual_required"},
                    "stdout": "", "stderr": "", "timed_out": False, "duration_sec": 0,
                    "job_id": reservation["job_id"]}
        argv = build_worker_cmd(d, dry_run, only_names, budget, mode=mode)
        rc, out, err, timed_out, dur = run_subprocess(
            argv, cwd=BASE_DIR, env=build_worker_env(), timeout=timeout)
        return {"rc": rc, "result": parse_worker_result(out), "stdout": out,
                "stderr": err, "timed_out": timed_out, "duration_sec": dur,
                "job_id": reservation["job_id"]}
    finally:
        jobs.release(reservation["job_id"])


def parse_worker_result(stdout: str) -> dict | None:
    result = None
    for line in (stdout or "").splitlines():
        if line.startswith(RESULT_PREFIX):
            try:
                value = json.loads(line[len(RESULT_PREFIX):])
                if isinstance(value, dict):
                    result = value
            except Exception:
                pass
    return result


def _details(result: dict | None, key: str) -> list[dict]:
    if not result:
        return []
    inner = result.get("result") or {}
    items = inner.get(key) or []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({"name": it.get("name"), "reason": it.get("reason")})
        else:
            out.append({"name": str(it), "reason": None})
    return out


def classify(rc: int, result: dict | None, timed_out: bool) -> tuple[str, str | None]:
    """把 worker 退出码 / envelope 归并成账号级状态。"""
    if timed_out:
        return "timeout", "发送超时，已终止该账号子进程"
    if result:
        inner = result.get("result") or {}
        if result.get("status") == "no_state":
            return "no_state", result.get("error")
        if result.get("status") == "busy":
            return "busy", result.get("error") or "账号执行器正忙"
        if result.get("status") == "error":
            return "executor_error", result.get("error")
        if rc != 0:
            return "executor_error", f"worker 退出码 {rc}"
        if result.get("logged_out"):
            return "logged_out", "登录态失效（掉线）"
        if (result.get("rate_limited") or result.get("security_verification") or result.get("safety_verification")
                or inner.get("rate_limited") or inner.get("security_verification") or inner.get("safety_verification")):
            return "rate_limited", "触发限流 / 安全验证"
        if result.get("status") == "unknown" or result.get("unknown_count") or inner.get("unknown"):
            return "unknown", "存在结果未知的发送，需人工核对后再恢复"
        if result.get("status") == "manual_required" or result.get("manual_required") or inner.get("manual_required"):
            return "manual_required", result.get("error") or "账号需要人工处理"
        total = result.get("total") or 0
        if total == 0 and not result.get("ok_count") and not result.get("failed_count"):
            return "empty", "该账号没有勾选任何好友"
        if result.get("failed_count"):
            return ("partial" if result.get("ok_count") else "failed"), None
        if result.get("skipped_count"):
            return "partial", None
        return "ok", None
    if rc != 0:
        return "executor_error", f"worker 退出码 {rc} 且无结果输出"
    return "executor_error", "worker 未输出结果"


# ── 编排入口 ───────────────────────────────────────────────────────────────

def _read_runtime(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid runtime")
        return value
    except (OSError, ValueError):
        return {"manual_required": True, "manual_reason": "runtime_unreadable"}


def _write_runtime(path: Path, state: dict) -> None:
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def _account_policy(account_id: str) -> tuple[int, int]:
    try:
        cfg = json.loads(A.account_file(account_id, A.CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return breaker_policy(cfg if isinstance(cfg, dict) else {})


def review_account(account_id: str) -> dict:
    """Record explicit human review; delivery ledger entries are left intact."""
    if A.get_account(account_id) is None:
        raise A.AccountError(f"账号不存在：{account_id}")
    path = A.account_file(account_id, A.RUNTIME_NAME)
    with file_lock(path):
        state = _read_runtime(path)
        state.update(manual_required=False, manual_reason=None, manual_reviewed=True,
                     session_status="unknown", consecutive_failures=0, auto_paused_until=0,
                     reviewed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        _write_runtime(path, state)
    return {"id": account_id, "manual_required": False, "reviewed_at": state["reviewed_at"]}


def _empty_item(account: dict, status: str = "pending", error=None) -> dict:
    return {
        "id": account["id"], "name": account.get("display_name") or account["id"],
        "status": status, "has_state": False,
        "ok_count": 0, "failed_count": 0, "skipped_count": 0, "unknown_count": 0,
        "logged_out": False, "rate_limited": False, "manual_required": False,
        "duration_sec": 0, "error": error,
        "failed_detail": [], "skipped_detail": [], "unknown_detail": [],
    }


def _blocked_item(account: dict, dry_run: bool) -> dict | None:
    """Persist cooldown decisions per account; force never bypasses safety."""
    account_id = account["id"]
    path = A.account_file(account_id, A.RUNTIME_NAME)
    threshold, cooldown = _account_policy(account_id)
    with file_lock(path):
        state = _read_runtime(path)
        last = state.get("last_run") or {}
        previous_unsafe = bool(last.get("unknown") or last.get("rate_limited")
                               or last.get("security_verification") or last.get("safety_verification")
                               or last.get("manual_required"))
        if state.get("manual_required") or (previous_unsafe and not state.get("manual_reviewed")):
            if not state.get("manual_required"):
                state.update(manual_required=True, manual_reason="security_or_unknown", manual_reviewed=False)
                _write_runtime(path, state)
            item = _empty_item(account, "manual_required", "账号需要人工处理安全验证或核对未知发送结果")
            item["manual_required"] = True
            return item
        if dry_run:
            return None
        now = time.time()
        paused = float(state.get("auto_paused_until") or 0)
        if paused and now >= paused:
            state.update(auto_paused_until=0, consecutive_failures=0)
            _write_runtime(path, state)
        elif not paused and int(state.get("consecutive_failures") or 0) >= threshold:
            paused = now + cooldown
            state["auto_paused_until"] = paused
            _write_runtime(path, state)
        if paused > now:
            return _empty_item(account, "breaker_skipped", "账号连续失败，正在冷却中")
    return None


def _record_account_outcome(item: dict, dry_run: bool) -> None:
    if dry_run or item["status"] in {"breaker_skipped", "busy"}:
        return
    path = A.account_file(item["id"], A.RUNTIME_NAME)
    threshold, cooldown = _account_policy(item["id"])
    with file_lock(path):
        state = _read_runtime(path)
        # A normal worker already called runtime.record_run. Only update counters
        # here when that write did not happen (crash, timeout or injected runner).
        worker_recorded = item.pop("_worker_recorded", False)
        if not worker_recorded:
            hard_fail = item["status"] in {"failed", "logged_out", "no_state", "timeout", "executor_error"}
            if hard_fail:
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
            elif item["status"] == "ok" and item["ok_count"] and not state.get("manual_required"):
                state["consecutive_failures"] = 0
        if item["status"] in {"rate_limited", "unknown", "timeout", "executor_error", "manual_required"}:
            state.update(manual_required=True, manual_reason=item["status"],
                         manual_reviewed=False, session_status="manual_required")
            item["manual_required"] = True
        if int(state.get("consecutive_failures") or 0) >= threshold:
            state["auto_paused_until"] = max(float(state.get("auto_paused_until") or 0), time.time() + cooldown)
        _write_runtime(path, state)


def _run_one(account: dict, *, dry_run: bool, only_names, timeout: float, budget,
             runner) -> dict:
    account_id = account["id"]
    account_dir = A.account_dir(account_id)
    overview = A.account_overview(account_id)
    item = _empty_item(account)
    item["has_state"] = bool(overview.get("has_state"))

    # 凭证预检：没有登录态就不起子进程（worker 也会判，这里省一次冷启动）。
    if not overview["has_state"]:
        item["status"] = "no_state"
        item["error"] = "账号目录缺少有效登录态 state.json"
        return item

    start = time.time()
    runtime_path = A.account_file(account_id, A.RUNTIME_NAME)
    before_mtime = runtime_path.stat().st_mtime_ns if runtime_path.exists() else None
    rc, out, err, timed_out, duration = runner(account_dir, dry_run, only_names, timeout, budget)
    result = parse_worker_result(out)
    status, error = classify(rc, result, timed_out)
    item.update(
        status=status,
        duration_sec=duration or round(time.time() - start, 1),
        error=error,
        logged_out=bool(result and result.get("logged_out")),
        rate_limited=bool(result and result.get("rate_limited")),
    )
    if result:
        inner = result.get("result") or {}
        item.update(
            ok_count=int(result.get("ok_count") or 0),
            failed_count=int(result.get("failed_count") or 0),
            skipped_count=int(result.get("skipped_count") or 0),
            unknown_count=int(result.get("unknown_count") or len(inner.get("unknown") or [])),
            failed_detail=_details(result, "failed"),
            skipped_detail=_details(result, "skipped"),
            unknown_detail=_details(result, "unknown"),
        )
        if runtime_path.exists() and runtime_path.stat().st_mtime_ns != before_mtime:
            with file_lock(runtime_path):
                saved = _read_runtime(runtime_path).get("last_run") or {}
            item["_worker_recorded"] = bool(result.get("at") and saved.get("at") == result["at"])
    if status in {"executor_error", "timeout"} and err:
        # 保留末尾一点 stderr 便于排查（不回灌整个浏览器日志）。
        item["error_detail_tail"] = err.strip()[-600:]
    return item


def run_all(*, only_account: str | None = None, dry_run: bool = False, only_names=None,
            force: bool = False, timeout: float = DEFAULT_ACCOUNT_TIMEOUT, budget="0",
            runner=None, progress_cb=None, reservation: dict | None = None) -> dict:
    """串行运行所有启用账号（或单个账号），返回汇总 dict。

    - only_account：只跑指定账号（手动单号触发）；
    - force：兼容旧调用参数，不再绕过安全暂停或失败冷却；
    - dry_run：演练，不真实发送且可绕过失败冷却，仍遵守人工处理阻断；
    - runner：可注入的子进程执行器（测试用），签名 (dir, dry_run, only_names, timeout, budget)。
    """
    owns_reservation = reservation is None
    if owns_reservation:
        try:
            reservation = jobs.reserve(account_id=only_account, kind="send", global_scope=only_account is None)
            reservation = jobs.claim(reservation, account_id=only_account, kind="send")
        except jobs.JobBusy as exc:
            raise OrchestratorBusy(str(exc)) from exc
    else:
        jobs.require(reservation, account_id=only_account, kind="send")
    if not _lock.acquire(blocking=False):
        if owns_reservation:
            jobs.release(reservation["job_id"])
        raise OrchestratorBusy("已有一轮多账号发送正在运行")
    runner = runner or _default_runner
    start = time.time()
    summary = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": bool(dry_run),
        "forced": False,
        "job_id": reservation["job_id"],
        "accounts": [],
        "totals": {"ok": 0, "failed": 0, "skipped": 0, "unknown": 0,
                   "manual_required_accounts": 0,
                   "logged_out_accounts": 0, "rate_limited_accounts": 0,
                   "error_accounts": 0, "miss_accounts": 0},
        "duration_sec": 0,
        "has_miss": False,
    }
    try:
        accounts = A.list_accounts(only_enabled=True)
        if only_account:
            A.validate_account_id(only_account)
            accounts = [a for a in accounts if a["id"] == only_account]
            if not accounts:
                # 允许对存在但被停用的账号手动触发：直接从注册表取。
                meta = A.get_account(only_account)
                if meta is None:
                    raise A.AccountError(f"账号不存在：{only_account}")
                accounts = [meta]

        total_n = len(accounts)
        for idx, account in enumerate(accounts, start=1):
            account_id = account["id"]
            item = _blocked_item(account, dry_run)

            if item is None:
                try:
                    item = _run_one(account, dry_run=dry_run, only_names=only_names,
                                   timeout=timeout, budget=budget, runner=runner)
                except Exception as e:
                    logger.error("[%s] 编排异常：%s", account_id, e)
                    item = _empty_item(account, "executor_error", f"编排异常: {e}")

            _record_account_outcome(item, dry_run)
            item.pop("_worker_recorded", None)

            summary["accounts"].append(item)
            t = summary["totals"]
            t["ok"] += item["ok_count"]
            t["failed"] += item["failed_count"]
            t["skipped"] += item["skipped_count"]
            t["unknown"] += item["unknown_count"]
            t["manual_required_accounts"] += bool(item.get("manual_required"))
            t["logged_out_accounts"] += 1 if item["status"] == "logged_out" else 0
            t["rate_limited_accounts"] += 1 if item["status"] == "rate_limited" else 0
            if item["status"] in {"executor_error", "timeout", "no_state"}:
                t["error_accounts"] += 1
            if item["status"] in MISS_STATUSES:
                t["miss_accounts"] += 1
            logger.info("编排进度 %s/%s：%s → %s（成功 %s / 失败 %s / 跳过 %s）",
                        idx, total_n, account_id, item["status"],
                        item["ok_count"], item["failed_count"], item["skipped_count"])
            if progress_cb:
                try:
                    progress_cb(idx, total_n, item)
                except Exception:
                    pass

        summary["has_miss"] = any(a["status"] in MISS_STATUSES for a in summary["accounts"])
        summary["duration_sec"] = round(time.time() - start, 1)
        return summary
    finally:
        _lock.release()
        if owns_reservation:
            jobs.release(reservation["job_id"])

"""多账号发送服务层（M4 多账号改造）。

把 orchestrator（串行调度子进程）、notifier（漏发邮件）和编排状态收口在一处，
供 FastAPI 后台线程（手动 /api/run）与 APScheduler 定时任务共同调用：

- :func:`scheduled_run`：定时回调，等价于「全部启用账号、串行、遵守按号熔断」；
- :func:`manual_run`：手动触发，可指定单号，遵守冷却与人工处理状态；
- 编排状态写在数据根的 ``orchestrator_state.json``（与 accounts.json 同级），
  供前端轮询；多账号没有单一 runtime.json，因此状态独立成文件；
- 真实发送结束后发漏发邮件；dry-run 演练不发邮件，避免演练噪音；
- API 与定时任务共用 jobs 的同步预留，忙时返回 busy 而不排队。

本模块不导入 core.automation（其路径在 import 时固化为单账号 data/），
所有真实浏览器工作都在 worker 子进程内按账号 DATA_DIR 完成。
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from core import accounts as A
from core import backup as backup_mod
from core import notifier
from core import orchestrator, jobs
from core import scheduler as scheduler_mod

logger = logging.getLogger("douyin-cloud-streak")

STATE_PATH = A.ACCOUNTS_ROOT.parent / "orchestrator_state.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_state(state: dict) -> None:
    try:
        A.atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error("写编排状态失败：%s", e)


def get_state() -> dict:
    """读取编排状态；文件缺失/损坏时返回安全默认值。"""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("invalid state")
    except Exception:
        state = {"running": False, "last_summary": None}
    active = [j for j in jobs.snapshot() if j["kind"] == "send"]
    if active:
        state.update(running=True, job_id=active[0]["job_id"])
    return state


def _slim_summary(summary: dict) -> dict:
    """落盘 / 给前端的精简汇总（保留每号计数与状态，去掉冗长明细尾部）。"""
    accounts = []
    for a in summary.get("accounts") or []:
        accounts.append({
            "id": a.get("id"), "name": a.get("name"), "status": a.get("status"),
            "ok_count": a.get("ok_count", 0), "failed_count": a.get("failed_count", 0),
            "skipped_count": a.get("skipped_count", 0),
            "unknown_count": a.get("unknown_count", 0),
            "manual_required": a.get("manual_required", False),
            "logged_out": a.get("logged_out", False), "rate_limited": a.get("rate_limited", False),
            "duration_sec": a.get("duration_sec", 0), "error": a.get("error"),
        })
    return {
        "at": summary.get("at"), "dry_run": summary.get("dry_run"),
        "forced": summary.get("forced"), "duration_sec": summary.get("duration_sec"),
        "has_miss": summary.get("has_miss"), "totals": summary.get("totals"),
        "accounts": accounts,
        "job_id": summary.get("job_id"),
    }


def _collect_notify_recipients(summary: dict) -> list:
    """汇总本轮涉及账号在注册表中的 notify_emails（保序、去重、忽略空值）。

    全局 SMTP_TO 始终收到汇总邮件；账号自己登记的邮箱在此并入，实现按号投递。
    注册表读取失败不影响发送主流程，退化为仅用全局 SMTP_TO。
    """
    out: list = []
    seen: set = set()
    try:
        meta_by_id = {m["id"]: m for m in A.list_accounts()}
        for item in summary.get("accounts") or []:
            meta = meta_by_id.get(item.get("id"))
            if not meta:
                continue
            for raw in meta.get("notify_emails") or []:
                addr = (raw or "").strip()
                key = addr.lower()
                if addr and key not in seen:
                    seen.add(key)
                    out.append(addr)
    except Exception:
        logger.exception("汇总账号级收件人失败，退化为仅使用全局 SMTP_TO")
    return out


def reserve_run(account_id: str | None = None, dry_run: bool = False,
                triggered: str = "manual") -> dict:
    """Reserve before a route announces success or starts its worker thread."""
    if account_id is not None:
        A.validate_account_id(account_id)
        if A.get_account(account_id) is None:
            raise A.AccountError(f"账号不存在：{account_id}")
    elif not A.list_accounts(only_enabled=True):
        raise A.AccountError("没有已启用账号")
    return jobs.reserve(account_id=account_id, kind="send", triggered=triggered,
                        global_scope=account_id is None)


def run_once(*, account_id: str | None = None, dry_run: bool = False,
             triggered: str = "manual", runner=None,
             notifier_func=None, reservation: dict | None = None) -> dict:
    """跑一轮。返回 {started:bool, busy:bool, summary?}。

    - 抢不到任务预留：started=False, busy=True（调用方应回 409，不排队）；
    - runner / notifier_func 仅测试注入。
    """
    try:
        reservation = reservation or reserve_run(account_id, dry_run, triggered)
        reservation = jobs.claim(reservation, account_id=account_id, kind="send")
    except jobs.JobBusy:
        logger.warning("已有一轮多账号编排进行中，本次触发被拒绝")
        return {"started": False, "busy": True}

    force = False
    state = {
        "running": True, "triggered": triggered, "started_at": _now(),
        "finished_at": None, "dry_run": bool(dry_run), "account_id": account_id,
        "last_summary": get_state().get("last_summary"),
        "job_id": reservation["job_id"],
    }
    _write_state(state)
    try:
        kwargs = dict(dry_run=dry_run, force=force, reservation=reservation)
        if runner is not None:
            kwargs["runner"] = runner
        if account_id:
            kwargs["only_account"] = account_id
        summary = orchestrator.run_all(**kwargs)

        # 邮件：仅真实发送触发（演练不发）。未配置 SMTP 时 notify 自行 disabled。
        notify_result = None
        if not dry_run:
            try:
                if notifier_func is not None:
                    # 测试注入：保持单参签名，不做账号级收件人合并。
                    notify_result = notifier_func(summary)
                else:
                    notify_result = notifier.notify(
                        summary,
                        extra_recipients=_collect_notify_recipients(summary))
            except Exception as e:  # 通知绝不能影响主流程
                logger.error("漏发通知异常（已忽略）：%s", e)
                notify_result = {"action": "failed", "error": str(e)}

        state.update(running=False, finished_at=_now(), last_summary=_slim_summary(summary),
                     notify=notify_result)
        _write_state(state)
        logger.info("多账号编排结束（%s，单号=%s，dry=%s）：%s",
                    triggered, account_id, dry_run, summary.get("totals"))
        return {"started": True, "busy": False, "job_id": reservation["job_id"],
                "summary": summary, "notify": notify_result}
    except orchestrator.OrchestratorBusy:
        state.update(running=False, finished_at=_now())
        _write_state(state)
        return {"started": False, "busy": True}
    except Exception as e:
        logger.error("多账号编排异常：%s", e)
        state.update(running=False, finished_at=_now(), error=str(e))
        _write_state(state)
        raise
    finally:
        jobs.release(reservation["job_id"])


def scheduled_run() -> dict:
    """定时任务回调：全部启用账号串行，遵守按号熔断，真实发送。"""
    return run_once(triggered="scheduled")


def manual_run(account_id: str | None = None, dry_run: bool = False,
               reservation: dict | None = None) -> dict:
    """手动触发遵守账号冷却与人工处理阻断；可指定单号。"""
    return run_once(account_id=account_id, dry_run=dry_run, triggered="manual",
                    reservation=reservation)


def reset_running() -> bool:
    """人工兜底：把卡在 running 的编排状态复位（不杀子进程，仅清状态标志）。"""
    st = get_state()
    if jobs.is_active(kind="send"):
        return False
    if not st.get("running"):
        return False
    st["running"] = False
    st["finished_at"] = _now()
    st["reset"] = True
    _write_state(st)
    return True


# ── 夜间备份（M6）─────────────────────────────────────────────────────────
BACKUP_MAX_RETRIES = 3       # 23:00 撞上发送时，最多延后重试 3 次（每次 15 分钟）
BACKUP_RETRY_MINUTES = 15


def manual_backup() -> dict:
    """手动立即备份；发送在跑时返回 busy（路由层回 409），不排队。"""
    if get_state().get("running"):
        return {"ok": False, "busy": True, "reason": "sending_in_progress"}
    try:
        reservation = jobs.reserve(account_id=None, kind="backup", global_scope=True)
    except jobs.JobBusy:
        return {"ok": False, "busy": True, "reason": "account_task_in_progress"}
    try:
        return backup_mod.create_backup(reason="manual")
    finally:
        jobs.release(reservation["job_id"])


def scheduled_backup(attempt: int = 1) -> dict:
    """定时备份回调：发送在跑则延后 15 分钟重试（最多 3 次），不直接跳过。

    评审 A9/P2-1：发送在跑不能静默跳过备份，必须延后重试并留下可见告警；
    超过最大重试仍撞发送则放弃本次并记录 giving_up，提示白天手动补备份。
    """
    reservation = None
    if not get_state().get("running"):
        try:
            reservation = jobs.reserve(account_id=None, kind="backup", triggered="scheduled", global_scope=True)
        except jobs.JobBusy:
            pass
    if reservation is None:
        if attempt <= BACKUP_MAX_RETRIES:
            run_at = datetime.now().astimezone() + timedelta(minutes=BACKUP_RETRY_MINUTES)
            at_str = run_at.isoformat(timespec="seconds")
            logger.warning("账号任务进行中，夜间备份延后 %s 分钟（第 %s 次重试，%s）",
                           BACKUP_RETRY_MINUTES, attempt, at_str)
            backup_mod.mark_skip(reason="sending_in_progress", attempt=attempt,
                                 next_retry_at=at_str)
            scheduler_mod.schedule_backup_retry(
                functools.partial(scheduled_backup, attempt=attempt + 1), run_at)
            return {"deferred": True, "attempt": attempt, "next_retry_at": at_str}
        logger.error("夜间备份连续 %s 次与账号任务冲突，放弃本次备份，请白天手动补备份",
                     BACKUP_MAX_RETRIES)
        backup_mod.mark_skip(reason="giving_up", attempt=attempt)
        return {"deferred": False, "given_up": True, "attempt": attempt}
    try:
        return backup_mod.create_backup(reason="scheduled" if attempt == 1 else "retry")
    finally:
        jobs.release(reservation["job_id"])

"""每天定时触发发送任务。"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import load_config
from .runtime import load_runtime, update_runtime, breaker_policy

logger = logging.getLogger("douyin-cloud-streak")
TZ = "Asia/Shanghai"

# 连续失败达到此轮数即熔断暂停（默认 3 轮；定时任务每天一轮，即连挂 3 天才停）
FAILURE_BREAKER_THRESHOLD, FAILURE_BREAKER_COOLDOWN = breaker_policy()
# 熔断冷却时长（秒），默认 6 小时；冷却结束自动恢复并清零计数

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None
_harvest_func: Callable | None = None
_prewarm_func: Callable | None = None
_backup_func: Callable | None = None
# 多账号模式：定时到点直接调编排器（按号熔断/启用态在编排器内处理），
# 不走单账号 runtime 熔断、不读单账号 config、不做主进程浏览器预热。
_multi_account: bool = False
_MULTI_TRUE = {"1", "true", "yes", "on"}


def _fire_run() -> None:
    """真正触发一次发送，并吞掉「上一轮还没跑完」这类正常冲突。

    app._start_run 在已有任务时会抛 HTTPException(409)。这是并发保护在正常
    工作（例如上一轮发送还没结束），不该被 APScheduler 记成任务异常刷栈。
    """
    if not _run_func:
        return
    try:
        result = _run_func()
        if isinstance(result, dict) and result.get("busy"):
            logger.info("定时任务与活跃任务冲突，本轮未启动")
    except Exception as e:
        logger.warning("本次定时发送未启动：%s", e)


def _daily_job() -> None:
    cfg = load_config()
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("自动运行已关闭（auto_run_enabled=false），本次定时任务跳过")
        return

    # ── 熔断：连续失败达阈值则暂停自动发送，冷却期结束自动恢复 ──
    # 多账号手动入口同样执行按号冷却和人工处理预检。
    # 不改写 config.json 的 auto_run_enabled（那会污染用户配置且无法区分是谁关的），
    # 暂停状态只存 runtime.json 的 auto_paused_until 时间戳。
    st = load_runtime()
    if st.get("manual_required"):
        logger.warning("账号需要人工核对，暂停定时发送")
        return
    threshold, cooldown = breaker_policy(cfg)
    now = time.time()
    paused_until = float(st.get("auto_paused_until") or 0)
    if paused_until and now < paused_until:
        logger.warning(
            "自动发送处于熔断暂停中，剩余 %.0f 分钟（连续失败 %s 轮）",
            (paused_until - now) / 60, st.get("consecutive_failures"),
        )
        return
    if paused_until and now >= paused_until:
        # 冷却结束：清零计数与暂停标记，自动恢复，今天正常尝试一次
        update_runtime(auto_paused_until=0, consecutive_failures=0)
        st["consecutive_failures"] = 0  # 同步本地快照，避免下面立即又触发熔断
        logger.info("熔断冷却期结束，已自动恢复自动发送")
    if int(st.get("consecutive_failures") or 0) >= threshold:
        update_runtime(auto_paused_until=now + cooldown)
        logger.error(
            "连续失败 %s 轮已达阈值，暂停自动发送 %s 分钟",
            st.get("consecutive_failures"), cooldown // 60,
        )
        return

    # 注意：不能写 `cfg.get("jitter_minutes") or 30`。
    # 配置里合法的 0（＝不抖动、0 点准时发）是 falsy，会被 or 吞掉变回 30。
    # 线上日志即为证据：配置 jitter_minutes=0，却打印「随机延迟 1205 秒后开始发送」，
    # 实际发送从 00:00 拖到 00:20。
    try:
        jitter = max(0, int(cfg.get("jitter_minutes") or 0))
    except (TypeError, ValueError):
        jitter = 0
    if jitter and _scheduler is not None:
        # 抖动窗口用「再排一个一次性任务」实现，而不是在当前调度线程里 sleep：
        # APScheduler 的线程池是有限资源，sleep 满 jitter 分钟会一直占着一个
        # 工作线程，把同一时刻的其他任务（预启动、周级采集、补发）一起顶住。
        delay = random.uniform(0, jitter * 60)
        logger.info("随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", delay, jitter)
        _scheduler.add_job(
            _fire_run,
            DateTrigger(run_date=datetime.now() + timedelta(seconds=delay), timezone=TZ),
            id="daily_send_jitter",
            replace_existing=True,
            misfire_grace_time=600,
        )
        return
    _fire_run()


def _multi_daily_job() -> None:
    """多账号模式的每日任务：到点即触发编排器，不做单账号熔断 / 抖动。

    - 全局开关 SPARKKEEPER_AUTO_RUN（默认开）；
    - 每个账号是否启用、是否处于按号熔断冷却，由 orchestrator.run_all 判定；
    - 决策上多账号不错峰（统一 00:00 后在后端内串行），因此无 jitter。
    """
    if os.environ.get("SPARKKEEPER_AUTO_RUN", "1").strip().lower() not in _MULTI_TRUE:
        logger.info("多账号自动运行已关闭（SPARKKEEPER_AUTO_RUN=0），本次定时跳过")
        return
    _fire_run()


def _fire_backup() -> None:
    if not _backup_func:
        logger.warning("备份回调未配置，跳过夜间备份")
        return
    try:
        _backup_func()
    except Exception as e:
        logger.warning("本次夜间备份未完成：%s", e)


def _multi_backup_job() -> None:
    """多账号夜间备份：独立于发送开关，仅受 SPARKKEEPER_BACKUP_ENABLED 控制。

    与发送互斥由 multi_service.scheduled_backup 判定：发送在跑则延后 15 分钟重试
    （有限次数）并告警，而非直接跳过造成静默缺备份。
    """
    if os.environ.get("SPARKKEEPER_BACKUP_ENABLED", "1").strip().lower() not in _MULTI_TRUE:
        logger.info("夜间备份已关闭（SPARKKEEPER_BACKUP_ENABLED=0），跳过")
        return
    _fire_backup()


def _prewarm_job() -> None:
    """浏览器预启动任务：在每日发送前 60 秒调用，提前启动浏览器并加载页面。"""
    cfg = load_config()
    if not bool(cfg.get("auto_run_enabled", True)):
        logger.info("自动运行已关闭，跳过浏览器预启动")
        return
    if _prewarm_func:
        try:
            _prewarm_func()
        except Exception as e:
            logger.warning("浏览器预启动任务异常: %s", e)


def configure(run_func: Callable, harvest_func: Callable | None = None,
              prewarm_func: Callable | None = None,
              multi_account: bool = False,
              backup_func: Callable | None = None) -> None:
    """注册每日发送任务与（可选）周级 creator 采集任务、浏览器预启动任务。

    multi_account=True 时切到多账号模式：daily_send 到点直接调 run_func（编排器
    内部按号启用态/熔断处理），时间固定由 SPARKKEEPER_SCHEDULE_TIME（默认 00:00）
    决定，不注册主进程浏览器预热与单账号周采集（它们的路径按单账号固化）；
    backup_func 为多账号夜间备份回调（默认 23:00，见 apply_schedule）。
    """
    global _scheduler, _run_func, _harvest_func, _prewarm_func, _multi_account, _backup_func
    _run_func = run_func
    _harvest_func = harvest_func
    _prewarm_func = prewarm_func
    _backup_func = backup_func
    _multi_account = bool(multi_account)
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def _parse_hhmm(value) -> tuple[int, int]:
    """解析 HH:MM 配置；非法值告警并回退 21:00，避免整个调度器因配置脏数据起不来。"""
    try:
        hh_s, mm_s = str(value).split(":")
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"越界: {value!r}")
        return hh, mm
    except Exception as e:
        logger.warning("schedule_time 配置非法（%s），已回退为 21:00", e)
        return 21, 0


def apply_schedule() -> None:
    if _scheduler is None:
        return

    if _multi_account:
        # 多账号：固定 00:00（可用 SPARKKEEPER_SCHEDULE_TIME 覆盖），不读单账号 config；
        # 不做抖动（不错峰、后端内串行）；不做主进程预热与单账号周采集。
        raw = os.environ.get("SPARKKEEPER_SCHEDULE_TIME", "00:00")
        try:
            hh_s, mm_s = str(raw).split(":")
            hh, mm = int(hh_s), int(mm_s)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except Exception:
            logger.warning("SPARKKEEPER_SCHEDULE_TIME 非法（%r），回退 00:00", raw)
            hh, mm = 0, 0
        _scheduler.add_job(
            _multi_daily_job,
            CronTrigger(hour=hh, minute=mm, timezone=TZ),
            id="daily_send", replace_existing=True,
            coalesce=True, misfire_grace_time=3600,
        )
        logger.info("多账号定时任务已更新：每天 %02d:%02d (%s) 串行发送全部账号", hh, mm, TZ)
        # 夜间数据备份：默认 23:00（SPARKKEEPER_BACKUP_TIME 可覆盖），独立开关
        # SPARKKEEPER_BACKUP_ENABLED（默认开），与发送开关 AUTO_RUN 解耦。
        if os.environ.get("SPARKKEEPER_BACKUP_ENABLED", "1").strip().lower() in _MULTI_TRUE:
            try:
                bh_s, bm_s = str(os.environ.get("SPARKKEEPER_BACKUP_TIME", "23:00")).split(":")
                bh, bm = int(bh_s), int(bm_s)
                if not (0 <= bh <= 23 and 0 <= bm <= 59):
                    raise ValueError
            except Exception:
                logger.warning("SPARKKEEPER_BACKUP_TIME 非法，回退 23:00")
                bh, bm = 23, 0
            _scheduler.add_job(
                _multi_backup_job,
                CronTrigger(hour=bh, minute=bm, timezone=TZ),
                id="daily_backup", replace_existing=True,
                coalesce=True, misfire_grace_time=3600,
            )
            logger.info("多账号夜间备份已更新：每天 %02d:%02d (%s)", bh, bm, TZ)
        else:
            for jid in ("daily_backup", "backup_retry"):
                if _scheduler.get_job(jid):
                    _scheduler.remove_job(jid)
        for job_id in ("daily_prewarm", "daily_send_jitter", "weekly_harvest", "retry_send"):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
        return

    cfg = load_config()
    hh, mm = _parse_hhmm(cfg.get("schedule_time", "21:00"))
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=hh, minute=mm, timezone=TZ),
        id="daily_send",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("定时任务已更新：每天 %02d:%02d (%s)", hh, mm, TZ)

    # 浏览器预启动任务：发送时间前 60 秒触发，提前启动浏览器、打开聊天页
    # 并等待联系人列表加载完成。
    #
    # 提前量依据实测量定（移植自 A 的性能优化）：2026-09-18 那次运行，从
    # 「开始预启动」到「联系人列表已加载，页面完全就绪」仅用 8.6 秒
    # （23:55:00 → 23:55:08）。旧值为 5 分钟，是实际耗时的约 35 倍，白白让
    # 一个 Chromium 常驻约 5 分钟（约 300-500 MB 内存）。60 秒留出约 7 倍余量。
    if _prewarm_func:
        # 通用计算：发送时刻往前推 60 秒（自动处理跨小时/跨天）
        prewarm_total = hh * 60 + mm - 1
        prewarm_hh = prewarm_total // 60 % 24
        prewarm_mm = prewarm_total % 60
        prewarm_ss = 0
        _scheduler.add_job(
            _prewarm_job,
            CronTrigger(hour=prewarm_hh, minute=prewarm_mm, second=prewarm_ss, timezone=TZ),
            id="daily_prewarm",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=180,
        )
        logger.info("浏览器预启动任务已更新：每天 %02d:%02d:%02d (%s)（发送前60秒）",
                    prewarm_hh, prewarm_mm, prewarm_ss, TZ)
    elif _scheduler.get_job("daily_prewarm"):
        _scheduler.remove_job("daily_prewarm")
        logger.info("浏览器预启动任务已移除")

    # 周级 creator 抖音号采集（默认周一 03:00；off/空 = 关闭）
    day = str(cfg.get("schedule_harvest_day") or "off").strip().lower()
    if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} and _harvest_func:
        _scheduler.add_job(
            _harvest_func,
            CronTrigger(day_of_week=day, hour=3, minute=0, timezone=TZ),
            id="weekly_harvest",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("周级采集已更新：每周 %s 03:00 (%s)", day, TZ)
    elif _scheduler.get_job("weekly_harvest"):
        _scheduler.remove_job("weekly_harvest")
        logger.info("周级采集已关闭")


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("daily_send")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def next_harvest_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("weekly_harvest")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def next_backup_time() -> str | None:
    """下次备份时间：若存在因发送占用而安排的一次性重试，优先取它（更早）。"""
    if _scheduler is None:
        return None
    cands = []
    for jid in ("backup_retry", "daily_backup"):
        job = _scheduler.get_job(jid)
        if job and job.next_run_time:
            cands.append(job.next_run_time)
    return min(cands).isoformat() if cands else None


def schedule_backup_retry(func: Callable, run_at: datetime) -> None:
    """发送占用导致备份延时时，安排一次性备份重试（func 由调用方绑定好 attempt）。"""
    if _scheduler is None:
        return
    _scheduler.add_job(
        func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id="backup_retry", replace_existing=True,
        coalesce=True, misfire_grace_time=3600,
    )
    logger.info("已安排备份延后重试：%s", run_at)


def schedule_retry(run_func: Callable, delay_minutes: int = 45) -> None:
    if _scheduler is None:
        return
    if _scheduler.get_job("retry_send"):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        run_func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id="retry_send",
        replace_existing=True,
    )
    logger.info("已安排 %s 分钟后自动补发本次失败的好友", delay_minutes)


def cancel_retry() -> None:
    if _scheduler and _scheduler.get_job("retry_send"):
        _scheduler.remove_job("retry_send")
        logger.info("已取消待执行的补发任务")


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None

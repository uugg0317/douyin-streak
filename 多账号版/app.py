"""Douyin Cloud Streak：单账号抖音续火花 Web 服务入口。"""

from __future__ import annotations

import ipaddress
import json
import functools
import logging
import os
import random
import re
import secrets
import sys
import threading
import time
import hashlib
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# 确保在 Windows 控制台下输出 Unicode/Emoji 正常
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from bootstrap import _env, load_environment

# DATA_DIR、账号根目录与模式开关必须在 core 导入之前确定。
ENV_PATH = load_environment()

import uvicorn
from fastapi import Body, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, model_validator

from core import automation, ledger, scheduler
from core import accounts as accounts_mod
from core import multi_service
from core import backup as backup_mod
from core import orchestrator, jobs, credential_extract
from core.log_tail import read_tail
from core.storage import file_lock
from core.browser import open_browser
from core.config import (
    DATA_DIR,
    DEFAULT_CONFIG,
    ROOT_STATE_PATH,
    STATE_PATH,
    atomic_write_bytes,
    atomic_write_text,
    get_valid_state_path,
    load_config,
    save_config,
)
from core.harvester import creator_map
from core.runtime import (
    load_harvest_last,
    load_runtime,
    recent_logs,
    record_contacts,
    record_harvest,
    record_run,
    set_running,
    setup_logging,
    update_runtime,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# 注：STATE_PATH 已由 core.config 定义并导入（DATA_DIR / "state.json"），
# 这里不要再重复赋值，避免两处定义漂移。
PID_PATH = DATA_DIR / "server.pid"  # 单实例锁文件：防旧实例 scheduler 残留再发消息
# 允许用环境变量覆盖锁文件路径。用途：测试/本地演练时避免抢占生产实例的锁
# （TestClient 会触发 lifespan，进而调用 _acquire_instance_lock 并写入 PID 文件）。
if os.environ.get("INSTANCE_LOCK_PATH", "").strip():
    PID_PATH = Path(os.environ["INSTANCE_LOCK_PATH"].strip())

logger = setup_logging()

# 多账号模式（M4）：置 1 启用按账号目录隔离的串行发送与 /api/multi/* 接口；
# 默认关闭，保持单账号行为不变，便于灰度与回滚。
# 此目录是多账号发行版；没有显式设置时启用多账号，0 仍可运行兼容单账号入口。
MULTI_ACCOUNT = os.environ.get("SPARKKEEPER_MULTI_ACCOUNT", "1").strip().lower() in {"1", "true", "yes", "on"}
# 多账号好友采集串行锁：一次只允许一个账号起浏览器采集（worker 子进程）。


class _OwnedLock:
    """带世代令牌的运行锁，支持请求线程到后台线程的一次交接。

    detach/adopt 显式移交当前任务的释放权，旧令牌和其它请求线程无权释放。
    活跃任务不得由复位接口强制解锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gen = 0
        self._meta = threading.Lock()

    def acquire(self, blocking: bool = True) -> bool:
        if not self._lock.acquire(blocking=blocking):
            return False
        with self._meta:
            self._gen += 1
            _LOCK_OWNER.token = (self, self._gen)
        return True

    def _holds_current(self) -> bool:
        tok = getattr(_LOCK_OWNER, "token", None)
        return bool(tok) and tok[0] is self and tok[1] == self._gen

    def detach(self):
        """请求线程把当前世代的释放权交给唯一后台线程。"""
        with self._meta:
            if not self._holds_current():
                raise RuntimeError("当前线程没有运行锁的交接权")
            token = _LOCK_OWNER.token
            _LOCK_OWNER.token = None
            return token

    def adopt(self, token) -> None:
        with self._meta:
            if not self._lock.locked() or token != (self, self._gen):
                raise RuntimeError("运行锁交接令牌已失效")
            _LOCK_OWNER.token = token

    def release(self) -> bool:
        """仅当前世代的持有者可释放；越权或重复释放均为无副作用空操作。"""
        if not self._holds_current():
            return False
        with self._meta:
            _LOCK_OWNER.token = None
            try:
                self._lock.release()
                return True
            except RuntimeError:
                return False

    def force_release(self) -> bool:
        """活跃任务只能自行归还锁；外部复位不能释放运行权。"""
        return False

    def locked(self) -> bool:
        return self._lock.locked()


_LOCK_OWNER = threading.local()
run_lock = _OwnedLock()
contacts_fetching = False
harvesting = False
_run_started_at = 0.0  # 当前持锁任务的开始时间戳，供 /api/reset-running 判断是否真卡死
# harvest_last 现从 runtime.json 持久化读取（服务重启后采集摘要不丢）

# 「强制重置」的最小间隔：任务开始后这段时间内不允许重置。
# 理由：reset 会强行释放他人持有的 run_lock，紧接着再来一次「立即续火花」
# 就会与仍在发送的旧任务并发，导致同一好友被重复发送（即历史上的定时超发）。
RESET_MIN_AGE_SECONDS = int(os.environ.get("RESET_MIN_AGE_SECONDS", "600"))


def _mark_run_started() -> None:
    global _run_started_at
    _run_started_at = time.time()


# ── 本机免登录后台 ───────────────────────────────────────────────────────
from local_access import _check_local_bind, _is_loopback


# ── 并发控制 ──────────────────────────────────────────────────────────────


def _acquire_lock(blocking: bool = True) -> bool:
    """获取全局运行锁：发送 / 同步联系人 / creator 采集三者共用同一把闸门。

    历史实现先用 harvesting 布尔量判重、再单独抢 run_lock，两个动作之间存在
    竞态窗口（两个并发请求可同时通过判重各自启动线程）。现在统一以 run_lock
    为唯一互斥量（Lock.acquire 本身是原子的），harvesting 仅用于界面展示。
    """
    return run_lock.acquire(blocking=blocking)


def _release_lock() -> None:
    """归还运行锁。幂等：越权释放 / 重复释放均为无副作用空操作。

    旧实现直接 `run_lock.release()` 并只吞 RuntimeError，无法识别「自己释放的
    其实是别人的锁」。现在由 _OwnedLock 校验持有者与世代：
      - worker 收尾时若锁已被 /api/reset-running 强制释放、且新任务已接手，
        本调用不会误放新任务的锁。
    """
    run_lock.release()


def _force_release_lock() -> bool:
    """人工兜底：无视归属强制解锁（供 /api/reset-running 使用）。

    推进会失效旧持有者的释放权，因此旧 worker 随后收尾时不会误放新任务的锁。
    """
    return run_lock.force_release()


def _start_daemon_or_rollback(target) -> None:
    """启动后台 daemon 线程；启动失败时归还运行锁并回滚 running 状态。

    失败场景：抢到 run_lock 之后 `Thread.start()` 抛 RuntimeError
    （线程资源耗尽 / 解释器正在关闭）。旧实现无兜底，锁已被占用却无人释放，
    此后 /api/run、/api/sync、/api/ledger/harvest-creator 全部永久 409，
    而 /api/reset-running 还被 RESET_MIN_AGE_SECONDS 时间闸挡住。

    注意：必须先 release 再 set_running(False)，因为 set_running 内部会写
    runtime.json（可能抛异常），放在前面会再次泄漏锁。
    """
    ownership = run_lock.detach()

    def run_with_ownership() -> None:
        run_lock.adopt(ownership)
        try:
            target()
        finally:
            _release_lock()

    try:
        threading.Thread(target=run_with_ownership, daemon=True).start()
    except Exception as e:
        run_lock.adopt(ownership)
        _release_lock()
        for _reset in (lambda: set_running(False),
                       lambda: update_runtime(harvesting=False, contacts_fetching=False)):
            try:
                _reset()
            except Exception:
                pass
        logger.error("后台任务线程启动失败，已归还运行锁：%s", e)
        raise HTTPException(status_code=500, detail="无法启动后台任务线程，请稍后重试")


# ── 后台任务 ──────────────────────────────────────────────────────────────


def _reject_if_extracting() -> None:
    """反向护栏：凭证提取进行中时不再启动发送/同步/采集。

    旧实现只有单向检查（/api/credentials/extract 检查 run_lock），而
    _start_run / _start_fetch_contacts / _start_harvest_creator 从不检查提取状态，
    于是「提取运行中再点立即续火花」仍能拿到 run_lock 并启动第二个 Chromium，
    与提取用的浏览器同时存在 → 1~2G 内存的云服务器易 OOM 并打断发送。

    调用时机：必须在**成功抢到 run_lock 之后**调用（抢锁成功才说明没有并发
    任务在进行），失败时归还锁并返回 409。
    """
    extracting = _extract_state.get("running") or credential_extract.manager.status().get("running")
    if not extracting:
        guard = DATA_DIR / ".credential-extract" / ".guard"
        if Path(str(guard) + ".lock").exists():
            try:
                with file_lock(guard, timeout=0.1):
                    pass
            except (TimeoutError, OSError):
                extracting = True
    if not extracting:
        return
    _release_lock()
    raise HTTPException(status_code=409, detail="凭证提取进行中，请等待提取完成后再试")


def _start_run(dry: bool, only_names: list[str] | None = None) -> None:
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    _reject_if_extracting()
    if not dry:
        current = load_runtime()
        if current.get("manual_required") or float(current.get("auto_paused_until") or 0) > time.time():
            _release_lock()
            raise HTTPException(status_code=409, detail="账号处于冷却或等待人工核对，暂不能发送")
    _mark_run_started()

    def worker() -> None:
        try:
            set_running(True)
            try:
                result = automation.run_send(dry_run=dry, only_names=only_names)
                record_run(result)
                logger.info("本次发送完成：成功 %s 人，失败 %s 人，dry=%s",
                            len(result.get("ok", [])), len(result.get("failed", [])), dry)
                if not dry and result.get("failed") and not result.get("logged_out"):
                    _schedule_retry(result)
                elif not dry:
                    scheduler.cancel_retry()
            finally:
                set_running(False)
        finally:
            _release_lock()

    _start_daemon_or_rollback(worker)


def _schedule_retry(result: dict) -> None:
    """安排 45 分钟后补发失败好友。"""
    if result.get("unknown") or result.get("rate_limited") or result.get("security_verification") or result.get("safety_verification"):
        return
    failed_names = [
        f["name"] for f in result.get("failed", [])
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"] != "_system"
    ]
    if not failed_names:
        return
    rt = load_runtime()
    today = datetime.now().date().isoformat()
    if rt.get("retry_date") != today:
        update_runtime(retry_date=today)
        scheduler.schedule_retry(lambda: _start_run(False, failed_names))


def _start_fetch_contacts() -> None:
    global contacts_fetching
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")
    _reject_if_extracting()
    _mark_run_started()

    def worker() -> None:
        global contacts_fetching
        try:
            contacts_fetching = True
            try:
                data = automation.fetch_chat_contacts()
                record_contacts(data)
                if data.get("names"):
                    stats = ledger.merge_consumer_contacts(data["names"])
                    logger.info("台账已同步：新增 %s 人，更新 %s 人，共 %s 人",
                                 stats["added"], stats["updated"], stats["total"])
            finally:
                contacts_fetching = False
        finally:
            _release_lock()

    _start_daemon_or_rollback(worker)


def _start_harvest_creator() -> None:
    """后台线程执行 creator 抖音号采集 + 台账合并（只读，不发送消息）。"""
    global harvesting
    # 与发送/同步共用同一把 run_lock：抢锁原子，不存在「先判重再抢锁」的竞态
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行，请稍后再试")
    _reject_if_extracting()
    harvesting = True
    _mark_run_started()

    def worker() -> None:
        global harvesting
        try:
            res = creator_map.collect_short_id_map()
            merge_stats = None
            if res.get("mapping"):
                merge_stats = ledger.merge_creator_map(res["mapping"])
                res["merge"] = merge_stats
                logger.info("creator 采集合并完成：%s 条映射，join %s 人，新增 %s 人，共 %s 人",
                             res["count"], merge_stats["joined"], merge_stats["added"],
                             merge_stats["total"])
            harvest_last = {
                "at": res.get("at"), "count": res.get("count"),
                "hit": res.get("hit"), "error": res.get("error"), "merge": merge_stats,
            }
            record_harvest(harvest_last)
        finally:
            harvesting = False
            _release_lock()

    _start_daemon_or_rollback(worker)


def _scheduled_harvest() -> None:
    try:
        _start_harvest_creator()
    except HTTPException as e:
        logger.warning("周级采集跳过：%s", e.detail)


# ── FastAPI ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _check_local_bind()
    # The operating system owns this lock for the entire controller lifetime;
    # a PID file alone cannot prevent concurrent starts or identify frozen exes.
    guard = file_lock(PID_PATH, timeout=0.1)
    try:
        guard.__enter__()
    except TimeoutError as exc:
        raise RuntimeError("该数据目录已有服务运行，请先停止旧实例") from exc
    try:
        atomic_write_text(PID_PATH, str(os.getpid()))
        credential_extract.manager.reopen()
        if MULTI_ACCOUNT:
            scheduler.configure(multi_service.scheduled_run, harvest_func=None,
                                prewarm_func=None, multi_account=True,
                                backup_func=multi_service.scheduled_backup)
        else:
            scheduler.configure(lambda: _start_run(False), harvest_func=_scheduled_harvest,
                                prewarm_func=automation.prewarm_browser)
        yield
    finally:
        try:
            try:
                credential_extract.manager.cleanup()
            finally:
                scheduler.shutdown()
        finally:
            try:
                PID_PATH.unlink(missing_ok=True)
            finally:
                guard.__exit__(None, None, None)


app = FastAPI(
    title="Douyin Cloud Streak",
    lifespan=lifespan,
    # 加固：生产环境关闭公开 API 文档与 OpenAPI 描述，减少攻击侦察面
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── IP 白名单免登录 ───────────────────────────────────────────────────────
# 安全红线：本机永不锁定 / nginx 直连判定用的本地环回集合。
# （原定义在下方「IP 连续失败锁定」段落，因白名单需要提前引用而移到这里，
#   语义不变；下方保留同名别名以免影响其他引用。）
LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}

# 白名单 IP 从环境变量读取（逗号分隔），避免源码即凭据。
# 未配置时白名单为空 —— 免登录功能自动失效，退回正常认证。
@app.get("/favicon.ico")
async def _favicon_ico():
    """兼容浏览器默认请求 /favicon.ico：返回多尺寸 ICO 图标。"""
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


# vendor 目录下是第三方只读库（Vue / Element Plus / axios / three.js）。
# 它们不含任何业务逻辑，也永远不会在同一个版本号下改变内容，因此可以长期缓存。
# 历史上这里是"一律 no-store"，导致每次打开后台都要重新下载 2.34 MB 的 JS/CSS，
# 在外网访问时表现为明显卡顿。区分对待后：
#   HTML / 应用资源 -> no-cache（每次校验，内容变了立刻拿到新版）
#   vendor 库       -> 长期强缓存（浏览器直接用本地副本，不再产生请求）
_VENDOR_PREFIX = "/static/vendor/"
_VENDOR_MAX_AGE = 30 * 24 * 3600  # 30 天


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """静态资源缓存策略。

    安全意图（不可放松）：**绝不使用缓存的旧版 HTML**——旧版可能缺少发送前的
    二次确认框，一旦被浏览器复用就可能误触真实发送。

    该意图由 `no-cache` 单独保证：它要求浏览器每次都必须向服务器校验，
    而本项目已经正确发出 ETag，内容未变时服务器返回 304（0 字节），
    既不会用到旧版 HTML，也不产生重复下载。

    因此这里**不再需要 `no-store`**。`no-store` 的语义是"完全禁止缓存"，
    代价是连没变过的 vendor 库也要每次重下，属纯浪费。

    例外：/static/vendor/ 下的第三方库允许长期强缓存（见上方常量说明）。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith(_VENDOR_PREFIX):
        # 只读第三方库：内容寻址、不会变，允许浏览器与代理长期缓存
        response.headers["Cache-Control"] = f"public, max-age={_VENDOR_MAX_AGE}, immutable"
        # 注意：Starlette 的 MutableHeaders **没有 pop()**，只能用 del（键不存在
        # 时 del 是安全的，不会抛 KeyError）。早期写法用 pop() 会让本分支直接
        # 500 —— 已由本地实测捕获。
        if "pragma" in response.headers:
            del response.headers["pragma"]
    elif path.startswith("/static/") or path == "/":
        # 应用资源与 HTML：必须每次校验（等价旧行为的安全保证，但不再禁止缓存）
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ── IP 连续失败锁定 / 黑名单 ────────────────────────────────────────────────
LOGIN_FAIL_LIMIT = 10  # 同一 IP 连续认证失败达到此次数即拉黑
def _safe_log_field(value, limit: int = 120) -> str:
    """净化写入日志的外部输入。

    安全红线：URL 里的 %0a/%0d 会被解析成真正的换行，直接把换行写进日志
    就能伪造出一整行 `AUTH_FAIL ip=<任意IP>`，从而借 fail2ban 拉黑任意地址
    （日志注入 → 嫁祸拉黑）。因此所有外部输入进日志前都要去控制字符并截断。
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "?", str(value))
    return cleaned[:limit]


def _client_ip(request: Request) -> str:
    """获取真实访客 IP。
    线上经 Nginx 反向代理：Nginx 已设置 X-Real-IP=$remote_addr（真实 TCP 对端，
    不可被客户端伪造）。仅当直连对端是本机反代时才信任 X-Real-IP；
    完全忽略 X-Forwarded-For（客户端可伪造其最左侧值，用于暴力试令牌或嫁祸拉黑）。
    返回值必须是合法 IP，否则回退为直连对端——避免伪造头污染黑名单与日志。
    """
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
    if path.startswith("/api/avatar"):
        # 头像代理一次面板会并发几十~几百张，给独立宽松配额，不挤占通用 API 池；
        # 否则同机打开旧单账号页刷数百张头像会把面板自身请求也打成 429。
        rule = {"limit": 600, "window": 60}
        key = f"{ip}:avatar"
    else:
        sensitive = any(path.startswith(p) for p in SENSITIVE_PATHS) or bool(
            re.fullmatch(r"/api/multi/accounts/[a-z0-9_-]+/credentials/extract", path)
        )
        rule = RATE_SENSITIVE if sensitive else RATE_GENERAL
        key = f"{ip}:{path}" if sensitive else ip
    q = _hits.get(key)
    if q is None:
        if len(_hits) >= _MAX_RATE_KEYS:
            # 公网长期运行时访客 IP 会不断累积，这里按最近一次请求时间淘汰一批
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


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """全局限速：先于业务逻辑执行，认证失败请求同样计入。"""
    ip = _client_ip(request)
    path = request.url.path
    # 面板自身静态资源（HTML/JS/CSS/图标）与入口页不计入 API 配额：它们不含敏感
    # 动作，且一旦被打成 429，Vue/element-plus 无法加载挂载，会表现为黑屏、按钮失效。
    if path == "/" or path == "/favicon.ico" or path.startswith("/static/"):
        return await call_next(request)
    if not _check_rate(ip, path):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试"})
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """补齐安全响应头（HSTS / nosniff / XFO / Referrer / CSP）。"""
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self'",
    )
    return response


@app.middleware("http")
async def _local_access_and_origin_guard(request: Request, call_next):
    """Enforce the local-only mode at request time, including alternate ASGI launchers."""
    direct = request.client.host if request.client else ""
    if not _is_loopback(direct) or not _is_loopback(_client_ip(request)) or not _is_loopback(request.url.hostname or ""):
        return JSONResponse(status_code=403, content={"detail": "本地免登录版仅允许本机访问"})
    if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin", "")
        if request.headers.get("sec-fetch-site", "") == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "不允许跨站操作"})
        if origin:
            supplied = urlparse(origin)
            if (supplied.scheme, supplied.netloc) != (request.url.scheme, request.url.netloc):
                return JSONResponse(status_code=403, content={"detail": "不允许跨站操作"})
    return await call_next(request)


# ── 请求体模型 ────────────────────────────────────────────────────────────


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict

    @model_validator(mode="after")
    def _check_known_keys(self) -> "ConfigBody":
        unknown = [k for k in self.config if k not in DEFAULT_CONFIG]
        if unknown:
            raise ValueError("未知配置项：" + ", ".join(map(str, unknown)))
        return self


def _reject_unknown_config_keys(raw_cfg: dict) -> None:
    """拒绝未知配置项。

    /api/config 同时接受 {"config": {...}} 与裸配置体两种形状（前端用的是后者），
    因此不能直接用 ConfigBody 校验，这里复用同一套判据。
    """
    unknown = [k for k in raw_cfg if k not in DEFAULT_CONFIG]
    if unknown:
        raise HTTPException(
            status_code=400, detail="未知配置项：" + ", ".join(map(str, unknown))
        )


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry: bool | None = None
    dry_run: bool | None = None

    @model_validator(mode="after")
    def _require_dry_flag(self) -> "RunBody":
        if self.dry is None and self.dry_run is None:
            raise ValueError("必须显式指定 dry（true=干跑 / false=真实发送）")
        return self


class LedgerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[dict]


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    selected_names: list[str] = []


# ── API 路由 ──────────────────────────────────────────────────────────────


@app.get("/")
def index(request: Request) -> Response:
    """Open the local multi-account console immediately."""
    return RedirectResponse(url="/static/multi.html", status_code=302)


def _if_none_match_hit(header_value: str | None, etag: str) -> bool:
    """判断 If-None-Match 是否命中当前 ETag（支持逗号分隔列表与 W/ 弱校验前缀）。"""
    if not header_value:
        return False
    if header_value.strip() == "*":
        return True
    for candidate in header_value.split(","):
        token = candidate.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == etag:
            return True
    return False


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": "sparkkeeper"}


@app.get("/api/browser/status")
def api_browser_status() -> dict:
    """查询浏览器常驻状态（是否存活、已运行多久）。"""
    return automation.get_browser_status()


@app.post("/api/browser/restart", status_code=202)
def api_browser_restart() -> dict:
    """提交「关闭浏览器」请求并立即返回，实际关闭在工作线程空闲时完成。

    返回 202（已受理）而非 200：本接口**不再同步等待**关闭完成。原因：
    automation 的单 worker 执行器一轮发送可占用 60 秒以上，旧实现在此处
    `submit().result()` 同步等待，发送期间（或浏览器卡死时）本接口会长期挂起
    —— 恰在最需要点它的时刻失效，并连带占满 AnyIO 线程池，拖累
    /api/status、/api/ledger 等同期请求。

    客户端应据返回的 closing 字段与后续 GET /api/browser/status 判断结果。
    """
    ok = automation.restart_browser()
    return {"ok": ok, "closing": ok, "async": True,
            "hint": "关闭请求已受理，稍后可用 /api/browser/status 确认"}


@app.get("/api/status")
def api_status() -> dict:
    rt = load_runtime()
    valid_state = get_valid_state_path()
    return {
        "state_file_exists": valid_state is not None,
        "session_status": rt.get("session_status", "unknown"),
        "running": rt.get("running", False),
        "last_run": rt.get("last_run"),
        "next_run": scheduler.next_run_time(),
        "next_harvest": scheduler.next_harvest_time(),
        "history_count": len(rt.get("history", [])),
        "consecutive_failures": int(rt.get("consecutive_failures", 0)),
        "auto_paused_until": rt.get("auto_paused_until", 0),
        "send_progress": rt.get("send_progress"),
        "auth_required": False,
        "version": "0.2.0",
    }


@app.get("/api/config")
def api_config() -> dict:
    return load_config()


@app.get("/api/contacts")
def api_contacts() -> dict:
    rt = load_runtime()
    return {
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": contacts_fetching,
    }


@app.post("/api/contacts/fetch")
def api_contacts_fetch() -> dict:
    _start_fetch_contacts()
    return {"ok": True, "started": True}


@app.get("/api/ledger")
def api_ledger() -> dict:
    rt = load_runtime()
    entries = ledger.load_ledger()
    contacts = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname") or ""
        spark = int(e.get("streak_days") or e.get("spark_days") or 0)
        contacts.append({
            "display_name": name,
            "nickname": name,
            "avatar": e.get("avatar") or "",
            "streak_days": spark,
            "spark_days": spark,
            "selected": bool(e.get("selected", False)),
            # last_status 优先取台账里记录的真实结果：历史实现只看 last_sent_at
            # 是否存在，而失败也会写 last_sent_at，导致失败在界面上显示成「成功」。
            "last_status": e.get("last_status") or ("success" if e.get("last_sent_at") else "pending"),
            "last_sent_at": e.get("last_sent_at")
        })
    b_daily = rt.get("b_channel_daily") or {}
    return {
        "entries": entries,
        "contacts": contacts,
        "selected_count": sum(1 for e in entries if e.get("selected")),
        "pending_send": [
            {"display_name": e["display_name"], "send_channel": e["send_channel"]}
            for e in automation.compute_pending()
        ],
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": contacts_fetching,
        "harvesting": harvesting,
        "harvest_last": load_harvest_last(),
        "b_channel_daily": {
            "date": b_daily.get("date"),
            "count": b_daily.get("count", 0),
        },
    }


@app.post("/api/sync")
def api_sync() -> dict:
    """与前端同步接口对齐"""
    _start_fetch_contacts()
    return {"ok": True, "started": True}


@app.post("/api/ledger/selection")
def api_ledger_selection(body: SelectionBody) -> dict:
    """一键保存选中的好友列表

    安全红线：拒绝空列表。selected_names 为空 = 全部取消勾选，一旦被误触
    （前端异常、网络重试、或空 body 的探测请求），run_send 会因为
    targets 为空而静默跳过发送且不产生任何告警，导致火花在无人察觉时断掉。
    如需全部取消，请显式逐条传入选中的空集合语义——这里选择直接拒绝。
    """
    if not body.selected_names:
        raise HTTPException(
            status_code=400,
            detail="selected_names 不能为空；该操作会清空全部勾选，已被拒绝",
        )
    selected_set = set(body.selected_names or [])
    entries = ledger.load_ledger()
    changes = []
    for e in entries:
        name = e.get("display_name") or e.get("nickname")
        if name:
            changes.append({
                "display_name": name,
                "selected": name in selected_set
            })
    stats = ledger.set_selected(changes)
    return {"ok": True, **stats}


# 尚未实现的通道（core/sender.py 目前只有 creator_channel = None）。
# 开启该开关后，run_send 走到无会话好友会抛 AttributeError 导致必定失败，
# 因此在实现完成前禁止通过接口打开。
_UNIMPLEMENTED_SWITCHES = {
    "allow_first_message": "通道 B（首条消息）尚未实现：core/sender.py 为空壳，开启后无会话好友必定发送失败",
}


def _reject_unimplemented_switches(raw_cfg: dict) -> None:
    """拒绝对未实现功能开关的开启操作（只拦 true，不拦显式 false）。"""
    if not isinstance(raw_cfg, dict):
        return
    for key, reason in _UNIMPLEMENTED_SWITCHES.items():
        if raw_cfg.get(key) is True:
            raise HTTPException(status_code=400, detail=f"拒绝开启 {key}：{reason}")


class DeleteFriendBody(BaseModel):
    display_name: str


@app.post("/api/ledger/delete")
def api_ledger_delete(body: DeleteFriendBody) -> dict:
    """删除单个好友台账条目"""
    name = (body.display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="display_name 不能为空")
    ok = ledger.remove_entry(name)
    return {"ok": ok, "deleted": ok, "display_name": name}


class BatchDeleteBody(BaseModel):
    display_names: list[str]


@app.post("/api/ledger/batch-delete")
def api_ledger_batch_delete(body: BatchDeleteBody) -> dict:
    """批量删除勾选的好友台账条目"""
    names = [n.strip() for n in (body.display_names or []) if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="display_names 不能为空")
    deleted = 0
    for name in names:
        if ledger.remove_entry(name):
            deleted += 1
    return {"ok": True, "deleted": deleted, "requested": len(names)}


# 抖音头像域名白名单（防盗链代理）
_ALLOWED_AVATAR_HOSTS = {
    "p3.douyinpic.com", "p9.douyinpic.com", "p11.douyinpic.com",
    "p26.douyinpic.com", "p3-pc.douyinpic.com", "p9-pc.douyinpic.com",
    "p26-pc.douyinpic.com", "p11-pc.douyinpic.com",
    "p3.huoshanimg.com", "p9.huoshanimg.com", "p11.huoshanimg.com",
    "p26.huoshanimg.com",
}


MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 单张头像大小上限，防止被拉大文件打爆内存


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随 302/301。

    安全红线：若允许跟随跳转，白名单内的 CDN 域名只要返回一次跳转
    （或被接管、或存在任意跳转参数），本接口就会去请求任意 URL —— 包括
    云厂商元数据地址 169.254.169.254 与内网服务，形成 SSRF。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_AVATAR_OPENER = urllib.request.build_opener(_NoRedirectHandler)


@app.get("/api/avatar")
def api_avatar(url: str):
    """Proxy allow-listed avatar images for an authenticated browser session."""
    parsed = urlparse(url)
    # scheme 必须显式限定：file:// 会被 urllib 交给本地文件处理器，
    # 而 file://<白名单域名>/etc/passwd 的 hostname 恰好能通过白名单校验。
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="不允许的头像协议")
    if parsed.hostname not in _ALLOWED_AVATAR_HOSTS:
        raise HTTPException(status_code=400, detail="不允许的头像域名")
    try:
        req = urllib.request.Request(url, headers={
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        with _AVATAR_OPENER.open(req, timeout=10) as resp:
            # 多读 1 字节即可判断是否超限，避免把超大响应整体读入内存
            data = resp.read(MAX_AVATAR_BYTES + 1)
            content_type = resp.headers.get("Content-Type", "image/webp")
        if len(data) > MAX_AVATAR_BYTES:
            raise HTTPException(status_code=413, detail="头像文件过大")
        if not str(content_type).lower().startswith("image/"):
            content_type = "image/webp"
        return Response(content=data, media_type=content_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"头像获取失败: {e}")


@app.post("/api/ledger/harvest-creator")
def api_harvest_creator() -> dict:
    _start_harvest_creator()
    return {"ok": True, "started": True}


@app.get("/api/ledger/stats")
def api_ledger_stats() -> dict:
    return ledger.stats()


@app.put("/api/ledger")
def api_ledger_save(body: LedgerBody) -> dict:
    changes: list[dict] = []
    for e in body.entries or []:
        name = str(e.get("display_name", "")).strip()
        if name and isinstance(e.get("selected"), bool):
            changes.append({
                "display_name": name,
                "selected": e["selected"],
                "selected_order": e.get("selected_order"),
            })
    stats = ledger.set_selected(changes)
    return {"ok": True, **stats}


@app.put("/api/config")
@app.post("/api/config")
def api_config_save(body: dict = Body(...)) -> dict:
    raw_cfg = body.get("config") if isinstance(body, dict) and "config" in body and isinstance(body["config"], dict) else body
    if not isinstance(raw_cfg, dict):
        raise HTTPException(status_code=400, detail="配置体必须是 JSON 对象")
    _reject_unknown_config_keys(raw_cfg)
    _reject_unimplemented_switches(raw_cfg)
    try:
        cfg = save_config(raw_cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


@app.post("/api/run")
def api_run(body: RunBody) -> dict:
    targets = ledger.get_selected()
    if not targets:
        cfg = load_config()
        if not cfg.get("friends"):
            raise HTTPException(status_code=400, detail="未勾选任何好友！请先在「好友与消息」中勾选好友后再执行。")
    _start_run(bool(body.dry or body.dry_run))
    return {"ok": True, "started": True}


@app.post("/api/reset-running")
def api_reset_running() -> dict:
    """仅清理没有活跃任务的残留显示状态。"""
    global harvesting, contacts_fetching
    if run_lock.locked() or _extract_state.get("running"):
        raise HTTPException(status_code=409, detail="任务仍在运行，不能复位；请等待结束或重启服务")
    harvesting = False
    contacts_fetching = False
    set_running(False)
    return {"ok": True, "message": "残留运行状态已复位"}


@app.post("/api/upload-state")
@app.post("/api/credentials/upload")
async def api_upload_state(
    file: UploadFile = File(...),
) -> dict:
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    # 结构白名单校验：仅接受 Playwright storage_state 形状
    if not all(
        isinstance(c, dict) and c.get("name") and c.get("domain") and isinstance(c.get("path", ""), str)
        for c in data["cookies"]
    ):
        raise HTTPException(status_code=400, detail="cookies 字段结构不合法")
    if "origins" in data and not isinstance(data["origins"], list):
        raise HTTPException(status_code=400, detail="origins 字段必须是数组")
    # 安全模式（STATE_FILE_PATH=/dev/shm/state.json）：只写内存盘，磁盘不留明文
    external_state = os.environ.get("STATE_FILE_PATH", "").strip()
    if external_state:
        target = Path(external_state)
        try:
            atomic_write_bytes(target, raw)
        except Exception:
            raise HTTPException(status_code=500, detail="无法写入 STATE_FILE_PATH，请检查目录权限")
    else:
        atomic_write_bytes(STATE_PATH, raw)
    logger.info("已更新登录态 state.json（%s 字节）", len(raw))
    return {"ok": True, "size": len(raw)}


@app.get("/api/logs")
def api_logs(n: int = 300) -> dict:
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


# ── 多账号管理与编排（M4）───────────────────────────────────────────────────


def _require_multi() -> None:
    if not MULTI_ACCOUNT:
        raise HTTPException(status_code=404, detail="多账号模式未启用（需设置 SPARKKEEPER_MULTI_ACCOUNT=1）")


def _multi_mutation(kind: str, *, global_scope: bool = False):
    """Reserve all mutating account operations against sending and backup."""
    def decorate(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            aid = None if global_scope else (kwargs.get("aid") or (args[0] if args else None))
            if aid is not None:
                try:
                    accounts_mod.validate_account_id(aid)
                except accounts_mod.AccountError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            try:
                reservation = jobs.reserve(account_id=aid, kind=kind, global_scope=global_scope)
            except jobs.JobBusy:
                raise HTTPException(status_code=409, detail="账号或相关任务正在运行，请稍后修改")
            try:
                return func(*args, **kwargs)
            finally:
                jobs.release(reservation["job_id"])
        return wrapped
    return decorate


def _gen_account_id() -> str:
    for _ in range(20):
        cand = "acc" + secrets.token_hex(3)
        if accounts_mod.get_account(cand) is None:
            return cand
    raise HTTPException(status_code=500, detail="无法分配账号 id，请重试")


def _validate_storage_state(raw: bytes) -> dict:
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    if not all(
        isinstance(c, dict) and c.get("name") and c.get("domain") and isinstance(c.get("path", ""), str)
        for c in data["cookies"]
    ):
        raise HTTPException(status_code=400, detail="cookies 字段结构不合法")
    if "origins" in data and not isinstance(data["origins"], list):
        raise HTTPException(status_code=400, detail="origins 字段必须是数组")
    return data


@app.get("/api/multi/state")
def api_multi_state() -> dict:
    return {
        "multi": MULTI_ACCOUNT,
        "max_accounts": accounts_mod.MAX_ACCOUNTS,
        "state": multi_service.get_state(),
        "jobs": jobs.snapshot(),
        "next_run": scheduler.next_run_time(),
        "next_backup": scheduler.next_backup_time(),
        "auto_run_enabled": os.environ.get("SPARKKEEPER_AUTO_RUN", "1").strip().lower() in {"1", "true", "yes", "on"},
        "backup_enabled": os.environ.get("SPARKKEEPER_BACKUP_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"},
        "schedule_time": os.environ.get("SPARKKEEPER_SCHEDULE_TIME", "00:00"),
        "accounts": [accounts_mod.account_overview(a["id"], meta=a) for a in accounts_mod.list_accounts()],
    }


@app.get("/api/multi/accounts")
def api_multi_accounts() -> dict:
    return {
        "accounts": [accounts_mod.account_overview(a["id"], meta=a) for a in accounts_mod.list_accounts()],
        "max_accounts": accounts_mod.MAX_ACCOUNTS,
    }


@app.post("/api/multi/accounts", status_code=201)
@_multi_mutation("registry", global_scope=True)
def api_multi_account_create(body: dict = Body(...)) -> dict:
    _require_multi()
    aid = (body.get("id") or "").strip() or _gen_account_id()
    try:
        meta = accounts_mod.add_account(
            aid, display_name=body.get("display_name"),
            enabled=bool(body.get("enabled", True)), note=body.get("note", ""))
    except accounts_mod.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "account": accounts_mod.account_overview(meta["id"])}


@app.patch("/api/multi/accounts/{aid}")
@_multi_mutation("account")
def api_multi_account_update(aid: str, body: dict = Body(...)) -> dict:
    _require_multi()
    try:
        accounts_mod.validate_account_id(aid)
        meta = accounts_mod.update_account(
            aid,
            display_name=body.get("display_name") if "display_name" in body else None,
            enabled=body.get("enabled") if "enabled" in body else None,
            note=body.get("note") if "note" in body else None)
    except accounts_mod.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "account": accounts_mod.account_overview(meta["id"])}


@app.post("/api/multi/accounts/{aid}/remove")
@_multi_mutation("remove")
def api_multi_account_remove(aid: str, body: dict = Body(default={})) -> dict:
    _require_multi()
    delete_dir = bool((body or {}).get("delete_dir"))
    try:
        accounts_mod.validate_account_id(aid)
        accounts_mod.remove_registry_account(aid, delete_dir=delete_dir)
    except accounts_mod.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "removed": aid, "delete_dir": delete_dir}


@app.post("/api/multi/accounts/{aid}/state")
async def api_multi_account_upload_state(aid: str, file: UploadFile = File(...)) -> dict:
    _require_multi()
    try:
        accounts_mod.validate_account_id(aid)
        if accounts_mod.get_account(aid) is None:
            raise accounts_mod.AccountError(f"账号不存在：{aid}")
    except accounts_mod.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    raw = await file.read()
    _validate_storage_state(raw)
    try:
        reservation = jobs.reserve(account_id=aid, kind="state")
    except jobs.JobBusy:
        raise HTTPException(status_code=409, detail="该账号有任务运行，请稍后更新凭据")
    try:
        target = accounts_mod.account_file(aid, "state.json")
        atomic_write_bytes(target, raw)
    finally:
        jobs.release(reservation["job_id"])
    logger.info("已更新账号 %s 的登录态 state.json（%s 字节）", aid, len(raw))
    return {"ok": True, "id": aid, "size": len(raw)}


def _credential_result(action, *args) -> dict:
    """Only return task metadata; login-state bytes stay in the worker data directory."""
    try:
        result = action(*args)
    except (credential_extract.CredentialExtractBusy, jobs.JobBusy) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except credential_extract.CredentialExtractNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (credential_extract.CredentialExtractInvalid, accounts_mod.AccountError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("登录提取操作失败（%s）", type(exc).__name__)
        raise HTTPException(status_code=500, detail="登录提取操作失败，请稍后重试") from exc
    fields = ("job_id", "account_id", "display_name", "status", "running", "ready",
              "count", "started_at", "deadline", "remaining_seconds", "error")
    return {"ok": True, **{key: result[key] for key in fields if key in result}}


@app.post("/api/multi/accounts/{aid}/credentials/extract", status_code=202)
def api_multi_credentials_extract(aid: str) -> dict:
    _require_multi()
    # Bridge the legacy lock into the new reservation atomically. Old single-account
    # routes cannot begin between the busy check and the account-scoped reservation.
    if not _acquire_lock(blocking=False):
        raise HTTPException(status_code=409, detail="发送或采集正在运行，请稍后再登录")
    try:
        if _extract_state.get("running"):
            raise HTTPException(status_code=409, detail="已有登录提取任务正在运行")
        return _credential_result(credential_extract.manager.start, aid)
    finally:
        _release_lock()


@app.get("/api/multi/credentials/extract-status")
def api_multi_credentials_status() -> dict:
    _require_multi()
    return _credential_result(credential_extract.manager.status)


@app.post("/api/multi/accounts/{aid}/credentials/extract/{job_id}/confirm")
def api_multi_credentials_confirm(aid: str, job_id: str) -> dict:
    _require_multi()
    return _credential_result(credential_extract.manager.confirm, aid, job_id)


@app.post("/api/multi/accounts/{aid}/credentials/extract/{job_id}/cancel")
def api_multi_credentials_cancel(aid: str, job_id: str) -> dict:
    _require_multi()
    return _credential_result(credential_extract.manager.cancel, aid, job_id)


@app.post("/api/multi/run", status_code=202)
def api_multi_run(body: dict = Body(default={})) -> dict:
    _require_multi()
    body = body or {}
    aid = (body.get("account_id") or "").strip() or None
    dry = bool(body.get("dry_run") or body.get("dry"))
    if aid:
        try:
            accounts_mod.validate_account_id(aid)
        except accounts_mod.AccountError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        reservation = multi_service.reserve_run(aid, dry, "manual")
    except jobs.JobBusy:
        raise HTTPException(status_code=409, detail="发送或相关账号任务正在运行")
    except accounts_mod.AccountError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    def _job() -> None:
        try:
            multi_service.run_once(account_id=aid, dry_run=dry, triggered="manual", reservation=reservation)
        except Exception as e:
            logger.exception("多账号手动任务异常：%s", e)
        finally:
            jobs.release(reservation["job_id"])

    try:
        threading.Thread(target=_job, daemon=True).start()
    except Exception:
        jobs.release(reservation["job_id"])
        raise HTTPException(status_code=500, detail="无法启动任务线程，请稍后重试")
    return {"ok": True, "started": True, "account_id": aid, "dry_run": dry, "job_id": reservation["job_id"]}


@app.post("/api/multi/reset")
def api_multi_reset() -> dict:
    _require_multi()
    if jobs.is_active(kind="send"):
        raise HTTPException(status_code=409, detail="发送任务仍在运行，不能复位")
    changed = multi_service.reset_running()
    return {"ok": True, "reset": changed}


@app.get("/api/multi/backups")
def api_multi_backups() -> dict:
    _require_multi()
    info = backup_mod.list_backups()
    info["next_backup"] = scheduler.next_backup_time()
    return info


@app.post("/api/multi/backups/run", status_code=201)
def api_multi_backup_run() -> dict:
    _require_multi()
    if multi_service.get_state().get("running"):
        raise HTTPException(status_code=409, detail="发送任务正在运行，备份会自动延后，请稍后再试")
    result = multi_service.manual_backup()
    if result.get("busy"):
        raise HTTPException(status_code=409, detail="发送任务正在运行，备份会自动延后")
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error") or "备份失败")
    return result


# ── 多账号：按账号的数据读写（M4.5b）─────────────────────────────────────────


def _account_dir_or_404(aid: str) -> Path:
    try:
        accounts_mod.validate_account_id(aid)
    except accounts_mod.AccountError:
        raise HTTPException(status_code=400, detail=f"非法账号 id：{aid}")
    if accounts_mod.get_account(aid) is None:
        raise HTTPException(status_code=404, detail=f"账号不存在：{aid}")
    return accounts_mod.account_dir(aid)


@app.get("/api/multi/accounts/{aid}/config")
def api_multi_get_config(aid: str) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    return {"config": load_config(accounts_mod.account_file(aid, "config.json"))}


@app.put("/api/multi/accounts/{aid}/config")
@_multi_mutation("config")
def api_multi_save_config(aid: str, body: dict = Body(...)) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    raw_cfg = body.get("config") if isinstance(body, dict) and isinstance(body.get("config"), dict) else body
    if not isinstance(raw_cfg, dict):
        raise HTTPException(status_code=400, detail="配置体必须是 JSON 对象")
    _reject_unknown_config_keys(raw_cfg)
    _reject_unimplemented_switches(raw_cfg)
    try:
        cfg = save_config(raw_cfg, path=accounts_mod.account_file(aid, "config.json"), lock_gap=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "config": cfg}


@app.get("/api/multi/accounts/{aid}/ledger")
def api_multi_get_ledger(aid: str) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    lp = accounts_mod.account_file(aid, "ledger.json")
    entries = ledger.load_ledger(lp)
    return {"entries": entries, "selected_count": sum(1 for e in entries if e.get("selected"))}


@app.post("/api/multi/accounts/{aid}/ledger/selection")
def api_multi_set_selection(aid: str, body: SelectionBody) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    if not body.selected_names:
        raise HTTPException(status_code=400, detail="selected_names 不能为空；该操作会清空全部勾选，已被拒绝")
    lp = accounts_mod.account_file(aid, "ledger.json")
    stats = ledger.set_selected_names(body.selected_names, path=lp)
    return {"ok": True, **stats}


@app.get("/api/multi/accounts/{aid}/runtime")
def api_multi_get_runtime(aid: str) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    rp = accounts_mod.account_file(aid, "runtime.json")
    try:
        rt = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
    except Exception:
        rt = {}
    return {"runtime": rt}


@app.post("/api/multi/accounts/{aid}/review")
def api_multi_review_account(aid: str, body: dict = Body(...)) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    if body.get("confirmed") is not True:
        raise HTTPException(status_code=400, detail="请先人工核对账号状态和待确认消息，再确认恢复")
    try:
        reservation = jobs.reserve(account_id=aid, kind="review")
    except jobs.JobBusy:
        raise HTTPException(status_code=409, detail="该账号有任务运行，暂不能确认恢复")
    try:
        state = orchestrator.review_account(aid)
    finally:
        jobs.release(reservation["job_id"])
    return {"ok": True, "manual_required": state.get("manual_required", False),
            "message": "已恢复账号任务；当天成功和待确认联系人仍会跳过"}


@app.get("/api/multi/accounts/{aid}/logs")
def api_multi_get_logs(aid: str, n: int = 300) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    lf = accounts_mod.account_dir(aid) / "logs" / "app.log"
    return {"logs": read_tail(lf, n)}


class MultiCopyBody(BaseModel):
    source_id: str
    copy_ledger: bool = False


@app.post("/api/multi/accounts/{aid}/copy-from")
def api_multi_copy_from(aid: str, body: MultiCopyBody) -> dict:
    _require_multi()
    dst = _account_dir_or_404(aid)
    src = _account_dir_or_404(body.source_id)
    if body.source_id == aid:
        raise HTTPException(status_code=400, detail="源账号与目标账号相同")
    try:
        reservation = jobs.reserve(account_id=aid, kind="copy")
    except jobs.JobBusy:
        raise HTTPException(status_code=409, detail="目标账号有任务运行，请稍后复制")
    copied = []
    try:
        src_cfg = src / "config.json"
        if src_cfg.exists():
            save_config(load_config(src_cfg), path=dst / "config.json", lock_gap=False)
            copied.append("config")
        if body.copy_ledger:
            src_l = src / "ledger.json"
            if src_l.exists():
                rows = ledger.load_ledger(src_l)
                for row in rows:
                    for key in ("delivery_records", "last_ok", "last_attempt_at", "last_sent_at", "last_msg"):
                        row.pop(key, None)
                    row["last_status"] = "pending"
                ledger.save_ledger(rows, path=dst / "ledger.json")
                copied.append("ledger")
    finally:
        jobs.release(reservation["job_id"])
    if not copied:
        raise HTTPException(status_code=400, detail="源账号没有可复制的 config.json/ledger.json")
    logger.info("账号 %s 从 %s 复制了 %s", aid, body.source_id, ",".join(copied))
    return {"ok": True, "copied": copied}


@app.post("/api/multi/accounts/{aid}/contacts/fetch", status_code=202)
def api_multi_contacts_fetch(aid: str) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    try:
        reservation = jobs.reserve(account_id=aid, kind="contacts")
    except jobs.JobBusy:
        raise HTTPException(status_code=409, detail="该账号或全账号任务正在运行，请稍后再采集")

    def _fetch_job() -> None:
        try:
            orchestrator.run_worker(aid, mode="fetch-contacts", timeout=1800, reservation=reservation)
        except Exception as e:
            logger.exception("账号 %s 好友采集失败：%s", aid, e)
        finally:
            jobs.release(reservation["job_id"])

    try:
        threading.Thread(target=_fetch_job, daemon=True).start()
    except Exception:
        jobs.release(reservation["job_id"])
        raise HTTPException(status_code=500, detail="无法启动采集线程，请稍后重试")
    return {"ok": True, "started": True, "id": aid, "job_id": reservation["job_id"]}


@app.get("/api/multi/accounts/{aid}/contacts/status")
def api_multi_contacts_status(aid: str) -> dict:
    _require_multi()
    _account_dir_or_404(aid)
    rp = accounts_mod.account_file(aid, "runtime.json")
    rt = {}
    try:
        if rp.exists():
            rt = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        rt = {}
    active = [j for j in jobs.snapshot() if j.get("account_id") == aid and j.get("kind") == "contacts"]
    return {"fetching": bool(active), "job_id": active[0]["job_id"] if active else None,
            "contacts_at": rt.get("contacts_at"),
            "contacts_error": rt.get("contacts_error")}






# ── 本地提取通行证（浏览器扫码登录） ──────────────────────
# 提取占用的浏览器与发送链路互斥：本锁只保护「检查 + 置位 _extract_state」这段临界区，
# 使「判重」与「置 running=True」变成原子操作，消除 TOCTOU。
_extract_lock = threading.Lock()
_extract_state = {"running": False, "status": "idle", "count": 0, "error": None, "screenshot": None}


def _extract_worker():
    """后台线程入口：把提取任务投递到 automation 的单 worker 执行器执行。

    为什么经受控入口而不是直接 new 一个线程跑 Playwright：
      `_extract_body` 会自己 `open_browser()`。若不经该执行器，它会与发送链路的
      浏览器**同时存在** —— 该文件 /api/credentials/extract 的注释已说明
      「两个 Chromium 同时拉起极易 OOM，导致正在进行的发送失败（火花因此断掉）」。
    经 `automation.run_in_pw_thread` 后，提取与发送/同步天然串行。
    """
    try:
        automation.run_in_pw_thread(_extract_body)
    except Exception as e:
        logger.error("凭证提取任务投递失败: %s", e)
        _extract_state["status"] = "failed"
        _extract_state["error"] = str(e)
        _extract_state["running"] = False


def _extract_body():
    """在 pw 工作线程内执行：启动浏览器，等待用户扫码登录，提取 state.json"""
    global _extract_state
    try:
        import shutil
        _extract_state["status"] = "waiting"
        _extract_state["error"] = None
        _extract_state["screenshot"] = None

        # 截图放在 data/ 目录（受保护，不通过公开 static 目录暴露），由认证 API 返回
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = DATA_DIR / "extract_qr.png"

        # 统一走 core.browser.open_browser：UA / 时区 / 启动参数与发送链路完全一致，
        # 且退出时必定同时关闭 browser 与 playwright 进程
        # （旧实现只 close 了 browser、漏了 p.stop()，每次提取都会残留一个驱动进程）。
        # use_state=False：扫码必须用干净会话，带上过期 Cookie 会直接跳过扫码页。
        with open_browser(headless=False, use_state=False) as (p, browser, context, page):
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

            # 等待页面加载后截图
            time.sleep(3)
            try:
                page.screenshot(path=str(screenshot_path), full_page=False)
                _extract_state["screenshot"] = "/api/credentials/extract-screenshot"
            except Exception as e:
                logger.warning("截图失败: %s", e)

            deadline = time.time() + 300  # 最长等待5分钟
            logged_in = False
            last_screenshot = time.time()
            while time.time() < deadline:
                cookies = context.cookies()
                if any(c["name"].startswith("sessionid") for c in cookies):
                    logged_in = True
                    break
                # 每10秒更新一次截图
                if time.time() - last_screenshot > 10:
                    try:
                        page.screenshot(path=str(screenshot_path), full_page=False)
                        _extract_state["screenshot"] = "/api/credentials/extract-screenshot"
                        last_screenshot = time.time()
                    except Exception:
                        pass
                time.sleep(1.5)

            if logged_in:
                time.sleep(2)
                context.storage_state(path=str(STATE_PATH))
                try:
                    shutil.copy2(STATE_PATH, ROOT_STATE_PATH)
                except Exception:
                    pass
                cookies = context.cookies()
                _extract_state["count"] = len(cookies)
                _extract_state["status"] = "success"
                logger.info("本地提取通行证成功：%s 个 Cookie", len(cookies))
            else:
                _extract_state["status"] = "failed"
                _extract_state["error"] = "5分钟内未检测到登录，请重试"
    except Exception as e:
        _extract_state["status"] = "failed"
        _extract_state["error"] = str(e)
        logger.error("本地提取通行证失败: %s", e)
    finally:
        _extract_state["running"] = False


@app.post("/api/credentials/extract")
def api_credentials_extract() -> dict:
    """启动本地浏览器提取通行证（扫码登录）"""
    if MULTI_ACCOUNT:
        raise HTTPException(status_code=409, detail="请在多账号控制台的凭据页为指定账号登录")
    global _extract_state
    # 「判重 + 置位」必须在同一临界区内完成（旧实现两步分离，存在 TOCTOU：
    # 两个并发请求可双双通过判重，各自拉起一个 Chromium）。
    if not _extract_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有提取任务在运行")
    try:
        if _extract_state["running"]:
            raise HTTPException(status_code=409, detail="已有提取任务在运行")
        # 发送/同步/采集进行中时不再叠加第二个浏览器实例：
        # 云服务器通常只有 1~2G 内存，两个 Chromium 同时拉起极易 OOM，
        # 导致正在进行的发送失败（火花因此断掉）。
        if run_lock.locked():
            raise HTTPException(status_code=409, detail="发送/同步任务进行中，请稍后再提取通行证")
        _extract_state = {"running": True, "status": "starting", "count": 0,
                          "error": None, "screenshot": None}
    finally:
        _extract_lock.release()

    try:
        threading.Thread(target=_extract_worker, daemon=True).start()
    except Exception as e:
        # 提取不持 run_lock，失败时只需回滚提取状态
        _extract_state["running"] = False
        _extract_state["status"] = "failed"
        _extract_state["error"] = f"无法启动提取线程: {e}"
        logger.error("凭证提取线程启动失败：%s", e)
        raise HTTPException(status_code=500, detail="无法启动提取线程，请稍后重试")
    return {"ok": True}


@app.get("/api/credentials/extract-status")
def api_credentials_extract_status() -> dict:
    """查询提取通行证状态"""
    if MULTI_ACCOUNT:
        raise HTTPException(status_code=409, detail="请使用按账号隔离的登录提取状态接口")
    return {
        "running": _extract_state["running"],
        "status": _extract_state["status"],
        "count": _extract_state["count"],
        "error": _extract_state["error"],
        "screenshot": _extract_state.get("screenshot"),
    }


@app.get("/api/credentials/extract-screenshot")
def api_credentials_extract_screenshot():
    """返回凭证提取时的实时截图（需认证，不放在公开 static 目录）"""
    if MULTI_ACCOUNT:
        raise HTTPException(status_code=409, detail="多账号登录提取在本机浏览器中核对，不提供全局截图")
    screenshot_path = DATA_DIR / "extract_qr.png"
    if not screenshot_path.exists():
        raise HTTPException(status_code=404, detail="暂无截图")
    return FileResponse(str(screenshot_path), media_type="image/png")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")  # 本地免登录版默认仅监听本机
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")

"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测"操作频繁 / 安全验证"等提示，命中即停本轮；
- 发送后确认新出站消息回显；无法确认时记录未知并禁止当天自动重发。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .browser import open_browser, _COMMON_ARGS, _CHROME_UA, _apply_stealth
from . import ledger
from playwright.sync_api import sync_playwright
from .config import DATA_DIR, get_valid_state_path, load_config
from .guard import detect_rate_limit
from .msg_builder import build_message
from .runtime import load_runtime, update_runtime
from .sender import creator_channel

logger = logging.getLogger("douyin-cloud-streak")

STATE_PATH = DATA_DIR / "state.json"
SCREENSHOT_PATH = DATA_DIR / "last_error.png"
CHAT_URL = "https://www.douyin.com/chat"
LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """安全读取整型配置：非法值回退到 default，0 是合法值。

    不能用 `cfg.get(key, default) or default` 这种写法：配置里合法的 0
    （例如 max_friends_per_run=0「不限制」、jitter_minutes=0「不抖动」）
    是 falsy，会被 or 悄悄换成 default，让「0」这个档位永远无法生效。
    """
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        logger.warning("配置项 %s=%r 不是整数，已按默认值 %s 处理", key, cfg.get(key), default)
        return default

# ── 专用 Playwright 工作线程 ──────────────────────────────────────────────
# 所有 Playwright Sync API 操作（预启动 / 发送 / 采集联系人）都在这一个
# 单线程执行器里串行运行，解决两个问题：
#   1. APScheduler / uvicorn 线程里跑着 asyncio 事件循环，直接调用
#      Playwright Sync API 会报 "Sync API inside the asyncio loop"；
#   2. Playwright 对象绑定创建它的线程，预启动与发送必须在同一线程，
#      否则无法复用预启动的浏览器。
def _pw_thread_initializer() -> None:
    # 显式给工作线程绑定一个「新建但未运行」的事件循环：
    # get_event_loop() 会返回它，is_running() 为 False，Playwright Sync API
    # 的 asyncio 冲突检查即可通过；Playwright 随后在自己的 greenlet 里运行
    # 专属循环，与此外壳循环互不干扰。
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass


_pw_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="pw-worker",
    initializer=_pw_thread_initializer,
)


def _run_in_pw_thread(func, *args, timeout: float | None = None, **kwargs):
    """把 Playwright 操作提交到专用工作线程同步执行并返回结果。

    timeout=None（默认）：一直等待 —— 发送/采集这类长任务用这个。
    timeout=N：最多等 N 秒；超时抛 concurrent.futures.TimeoutError。
      仅供**不应阻塞 HTTP 线程**的控制类接口使用（如关闭浏览器）。注意单 worker
      执行器被长任务占满时，超时只让调用方尽早返回，排队中的任务仍会执行。
    """
    if threading.current_thread().name.startswith("pw-worker"):
        raise RuntimeError(
            "检测到在 pw 工作线程内再次提交到单 worker 执行器 —— 这会造成自我等待死锁。"
            "请直接调用底层函数（例如 _close_prewarmed），不要经本入口。"
        )
    return _pw_executor.submit(func, *args, **kwargs).result(timeout=timeout)


# 浏览器预启动实例（全局，由 scheduler 在发送前触发）
_prewarmed_browser = None  # {"p", "browser", "context", "page", "ready", "created_at"}
_prewarm_lock = threading.Lock()  # 防止并发预启动


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _screenshot(page) -> None:
    try:
        page.screenshot(path=str(SCREENSHOT_PATH), timeout=5000)
        logger.info("已保存页面截图: %s", SCREENSHOT_PATH)
    except Exception:
        pass


# ── 登录检测 ──────────────────────────────────────────────────────────────


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


# ── 浏览器预启动 ──────────────────────────────────────────────────────────

def prewarm_browser() -> bool:
    """公共入口（可在任意线程调用）：把预启动提交到专用 Playwright 工作线程。

    fire-and-forget：不阻塞调用线程（scheduler / uvicorn），任务在单线程
    执行器里排队；随后的 run_send 同样提交到该执行器，因此会天然等待
    预启动完成后再执行，并直接复用同一个浏览器实例。
    """
    future = _pw_executor.submit(_prewarm_browser_impl)
    # 不阻塞等待，但捕获异常避免静默
    future.add_done_callback(lambda f: f.exception())
    return True


def _prewarm_browser_impl() -> bool:
    """在专用 Playwright 工作线程中预启动浏览器并加载聊天页面。"""
    global _prewarmed_browser

    # 单线程执行器内本就串行，这里用非阻塞锁再兜底防止重复预启动
    if not _prewarm_lock.acquire(blocking=False):
        logger.info("预启动正在进行中，跳过重复调用")
        return False

    try:
        if _prewarmed_browser and _prewarmed_browser.get("ready"):
            age = time.time() - _prewarmed_browser.get("created_at", 0)
            if BROWSER_MAX_IDLE_SECONDS == 0 or age < BROWSER_MAX_IDLE_SECONDS:
                logger.info("浏览器已预启动（%.0f秒前），跳过重复预启动", age)
                return True
            logger.info("预启动浏览器已过期（%.0f秒），重新启动", age)
            _close_prewarmed()

        p = browser = context = page = None
        try:
            logger.info("开始预启动浏览器...")
            valid_state = get_valid_state_path()
            state_file = str(valid_state) if valid_state and valid_state.exists() else None

            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True, args=_COMMON_ARGS)
            defaults = {
                "viewport": {"width": 1366, "height": 768},
                "user_agent": _CHROME_UA,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "ignore_https_errors": True,
            }
            if state_file:
                defaults["storage_state"] = state_file
            context = browser.new_context(**defaults)
            page = context.new_page()
            _apply_stealth(page)

            # 打开聊天页面
            logger.info("预启动：打开抖音聊天页面...")
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            try:
                page.wait_for_selector("input", timeout=15000)
            except Exception:
                time.sleep(2)

            # 验证登录态
            logged, why = check_login(page)
            if not logged:
                logger.warning("预启动浏览器登录态无效：%s", why)
                try:
                    browser.close()
                    p.stop()
                except Exception:
                    pass
                return False

            # 充分等待联系人列表出现（带 reload 重试），确保发送时页面完全就绪
            list_ready = False
            for attempt in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=30000)
                    list_ready = True
                    break
                except Exception:
                    logger.info("预启动：联系人列表未出现，刷新重试（第%s次）", attempt + 1)
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_selector("input", timeout=10000)
                    except Exception:
                        time.sleep(2)
            if list_ready:
                logger.info("预启动：联系人列表已加载，页面完全就绪")
                time.sleep(0.5)
            else:
                logger.warning("预启动：联系人列表等待超时，发送时将继续等待")

            _prewarmed_browser = {
                "p": p, "browser": browser, "context": context, "page": page,
                "ready": True, "created_at": time.time()
            }
            logger.info("浏览器预启动成功，已加载聊天页面并验证登录态")
            return True

        except Exception as e:
            logger.error("浏览器预启动失败：%s", e)
            try:
                if browser:
                    browser.close()
                if p:
                    p.stop()
            except Exception:
                pass
            return False
    finally:
        try:
            _prewarm_lock.release()
        except RuntimeError:
            pass


def run_in_pw_thread(func, *args, **kwargs):
    """受控公共入口：把任意 Playwright 操作投递到专用工作线程串行执行。

    存在的理由：`_run_in_pw_thread` 是模块私有实现，外部模块不应直接依赖。
    凭证提取（扫码登录）需要自己开一个浏览器，若不走本执行器就会与发送链路
    的浏览器**同时存在**，在 1~2G 内存的云服务器上极易 OOM 并打断正在进行的
    发送。统一经此入口后，提取与发送天然串行，不会出现两个 Chromium。

    注意：送入的函数会在 pw 工作线程内执行，**不得**在其中再调用
    `run_send` / `fetch_chat_contacts` / `restart_browser` 等会再次提交到同一
    执行器的公共入口，否则单 worker 自我等待死锁。
    """
    return _run_in_pw_thread(func, *args, **kwargs)


def _close_prewarmed() -> None:
    """关闭预启动的浏览器实例（必须在专用 Playwright 工作线程内调用）。"""
    global _prewarmed_browser
    if _prewarmed_browser:
        try:
            _prewarmed_browser["browser"].close()
        except Exception:
            pass
        try:
            _prewarmed_browser["p"].stop()
        except Exception:
            pass
        _prewarmed_browser = None
        logger.info("预启动浏览器已关闭")


# 本次运行结束后，预启动实例保留多久（秒）。期间再次运行可直接复用。
# 设为 0 表示维持旧行为（每次运行结束立即关闭浏览器）。
# 移植自 A 的性能优化：旧行为下每次 run_send 结束即关闭，手动「立即续火花」
# 每点一次都要重新付约 20 秒冷启动。
KEEP_PREWARMED_SECONDS = max(0, int(os.environ.get("KEEP_PREWARMED_SECONDS", "600")))

# 浏览器常驻模式：发送完成后不关闭浏览器，下次发送直接复用，彻底消除冷启动。
# 开启后预启动浏览器无过期时间（除非崩溃或登录态失效），适合服务器长期运行。
# 环境变量 KEEP_BROWSER_ALWAYS=true 开启，默认关闭。
KEEP_BROWSER_ALWAYS = os.environ.get("KEEP_BROWSER_ALWAYS", "false").lower() in ("1", "true", "yes")
# 预启动浏览器最大空闲秒数，超过则在下次预启动时重启。0 = 永不过期（常驻模式默认）。
BROWSER_MAX_IDLE_SECONDS = max(0, int(os.environ.get(
    "BROWSER_MAX_IDLE_SECONDS", "0" if KEEP_BROWSER_ALWAYS else "900"
)))


def _keep_prewarmed_alive(seconds: int = KEEP_PREWARMED_SECONDS) -> bool:
    """保留预启动实例供下次复用（必须在专用 Playwright 工作线程内调用）。

    把 created_at 重置为当前时刻，实例进入「刚预热」状态：
    - `_get_prewarmed_page()` 只校验 ready + 登录态，因此仍会复用它；
    - 下一次 `_prewarm_browser_impl()` 会因为 age 很小而跳过重复启动。

    安全性：复用前 `_get_prewarmed_page()` 会重新 check_login；
    且每次发送前 `_locate_contact()` 都会校验右侧会话确实切换成功，
    不会因为页面停留在上一个会话而错发。
    """
    global _prewarmed_browser
    if seconds <= 0 or not _prewarmed_browser or not _prewarmed_browser.get("ready"):
        return False
    _prewarmed_browser["created_at"] = time.time()
    _prewarmed_browser["reused_at"] = time.time()
    if KEEP_BROWSER_ALWAYS:
        logger.info("常驻模式：浏览器永久保持，下次发送直接复用（免冷启动）")
    else:
        logger.info("预启动浏览器保留 %s 秒供下次复用（避免重复冷启动）", seconds)
    return True


def _get_prewarmed_page():
    """获取预启动的浏览器页面（在工作线程内调用）。登录态有效则返回，否则 None。"""
    global _prewarmed_browser
    if not _prewarmed_browser or not _prewarmed_browser.get("ready"):
        return None

    try:
        page = _prewarmed_browser["page"]
        logged, why = check_login(page)
        if logged:
            age = time.time() - _prewarmed_browser.get("created_at", 0)
            logger.info("使用预启动浏览器（已预热 %.0f 秒）", age)
            return _prewarmed_browser
        logger.warning("预启动浏览器登录态失效：%s", why)
    except Exception as e:
        logger.warning("预启动浏览器异常：%s", e)

    _close_prewarmed()
    return None


# ── 联系人定位 ────────────────────────────────────────────────────────────


def _find_contact(page, name: str):
    """按「精确文本」查找联系人标题；找不到返回 None。

    【绝不子串匹配】旧实现在精确匹配失败后退化为
    `filter(has_text=name)`，那是子串语义：目标「张三」会命中会话「张三丰」，
    于是点开错误的人，消息发给上一个人（校验判据当时也是子串，一路放行）。

    退化路径改为「遍历会话标题做规范化后的严格相等比较」，两个理由：
      1) 台账里的昵称可能含不可见字符（实测确实遇到过『某好友\\xa0.☹』
         带不间断空格），只写 `==` 会失配 -> 漏发；
      2) 精确匹配失败时仍需一个兜底，否则会掉进很慢的搜索路径。

    返回值由 Locator 变为 Locator/None，调用方必须判空（见 _locate_contact）。
    """
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    target = _norm_text(name)
    if not target:
        return None
    candidates = page.locator(".conversationConversationItemtitle")
    # 一次取回全部标题（一次往返），避免逐个 inner_text() 产生 N 次往返
    for i, text in enumerate(candidates.all_text_contents() or []):
        if _norm_text(text) == target:
            return candidates.nth(i)
    return None


def _verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功。

    【只用精确匹配】旧实现先试 exact=True，不中再试 exact=False，而后者是
    子串语义：「张三」会命中标题「张三丰」。于是当某次点击没生效、页面仍停在
    上一个好友「张三丰」时，本函数照样返回 True，_click_and_verify 判定
    「已切换」，消息就发给了上一个人。
    """
    try:
        loc = page.get_by_text(name, exact=True)
        for i in range(loc.count()):
            try:
                box = loc.nth(i).bounding_box()
            except Exception:
                continue
            if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                return True
    except Exception:
        pass
    return False


def _norm_text(s) -> str:
    """规范化文本用于比较：不间断空格转普通空格并去首尾空白。"""
    return (s or "").replace("\xa0", " ").strip()


def _verify_item_active(page, name: str) -> bool:
    """即时切换判据：被点击的会话项 wrapper 会立即带上
    conversationConversationItemcurConversation（当前会话）class。

    该 class 由前端在点击瞬间添加，不依赖右侧消息区/标题渲染，
    比 _verify_in_conversation 的顶部标题文本更快、更稳，可消除
    连续切换时标题渲染延迟导致的空等（实测个别好友曾因此空等十余秒）。
    """
    try:
        active_title = page.locator(
            ".conversationConversationItemcurConversation .conversationConversationItemtitle"
        ).first
        if active_title.count() == 0:
            return False
        cur = _norm_text(active_title.inner_text(timeout=1000))
        target = _norm_text(name)
        # 【严格相等】必须两边都过 _norm_text 之后再比，缺一不可：
        #   1) 旧写法 `cur == target or cur in target or target in cur` 是子串语义，
        #      「张三」与「张三丰」会互相判为同一人。连续发送时只要某次点击没生效、
        #      页面仍停在上一个好友，本函数就返回 True，消息发给上一个人 —— 这是
        #      「发错人」的实际入口（已用假页面复现）。
        #   2) 但若图省事写成 `cur == name`（不过 _norm_text）又会反过来漏发：
        #      台账里真实存在带不间断空格的昵称（如『某好友\xa0.☹』），原始字节不等。
        return bool(cur) and bool(target) and cur == target
    except Exception:
        return False


def _wait_any(locator, timeout: float, interval: float = 0.15) -> bool:
    """轮询等待 locator 出现，最长 timeout 秒。

    替代固定 time.sleep(N)：条件早满足就早返回，最坏情况与固定等待等长。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if locator.count():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(locator.count())
    except Exception:
        return False


def _wait_in_conversation(page, name: str, timeout: float) -> bool:
    """轮询等待会话真正切到目标好友，最长 timeout 秒（原为固定 sleep）。

    原实现在点击后硬等 3~4 秒才开始校验；实测切换通常几百毫秒就完成，
    这里改成条件满足即返回，最坏情况仍不超过原来的固定等待。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _verify_item_active(page, name) or _verify_in_conversation(page, name):
            return True
        time.sleep(0.15)
    return False


def _search_and_open(page, name: str) -> bool:
    """搜索兜底：好友不在聊天列表时，用搜索框找到并打开会话。

    本路径原先有 4+4 秒（或 3+3 秒）固定 sleep，一旦有好友走到这里，
    整轮耗时会被直接顶破 1 分钟。现全部改为「轮询到条件满足即返回」，
    最长等待与旧版一致，但正常情况可省下数秒。
    """
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        btn = page.get_by_text("发消息", exact=False).first
        _wait_any(btn, 4)                      # 原：sleep(4)
        if btn.count():
            btn.click(force=True)
            _wait_in_conversation(page, name, 4)   # 原：sleep(4)
            return True
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            # 兜底不再用 exact=False：那是子串语义，会把「张三」点成「张三丰」。
            # 改为复用 _find_contact 的「规范化后严格相等」查找（它同样返回
            # Locator/None）。查不到就放弃本轮定位，交由上层判失败并跳过，
            # 宁可漏发也不发错人。本路径原本就极少触发。
            candidate = _find_contact(page, name)
        if candidate is None or candidate.count() == 0:
            return False
        candidate.click(force=True)
        _wait_in_conversation(page, name, 3)   # 原：sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            _wait_in_conversation(page, name, 3)   # 原：sleep(3)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


_JS_SCROLL_TOP = (
    "() => { const item=document.querySelector('.conversationConversationItemwrapper');"
    " if(!item) return false; let n=item;"
    " while(n){ const s=getComputedStyle(n);"
    " if((s.overflowY==='auto'||s.overflowY==='scroll')&&n.scrollHeight>n.clientHeight+20)"
    " { n.scrollTop=0; return true; } n=n.parentElement; } return false; }"
)

_JS_SCROLL_DOWN = (
    "() => { const item=document.querySelector('.conversationConversationItemwrapper'); if(!item) return null;"
    " let n=item; while(n){ const s=getComputedStyle(n);"
    " if((s.overflowY==='auto'||s.overflowY==='scroll') && n.scrollHeight>n.clientHeight+20) break;"
    " n=n.parentElement; } if(!n) return null;"
    " const before=n.scrollTop; n.scrollTop=before + n.clientHeight*0.9;"
    " return {before, after:n.scrollTop, height:n.scrollHeight, client:n.clientHeight}; }"
)


def _scroll_list_to_top(page) -> None:
    """把会话虚拟列表滚回顶部（慢路径起点）。"""
    try:
        page.evaluate(_JS_SCROLL_TOP)
    except Exception:
        pass


def _scroll_list_one_page(page):
    """会话列表向下滚动约一屏，返回滚动位置信息（用于判断是否到底）。"""
    try:
        return page.evaluate(_JS_SCROLL_DOWN)
    except Exception:
        return None


def _click_and_verify(page, target, name: str) -> bool:
    """点击会话项并确认切换成功（选中 class 优先，顶部标题兜底）。"""
    try:
        target.click(force=True, timeout=10000)
    except Exception:
        return False
    # 轮询间隔 0.3 → 0.15 秒（好友间每人省约 0.15 秒），但**总上限只增不减**：
    # 28 × 0.15 = 4.2 秒，与旧实现 14 × 0.3 = 4.2 秒完全相同。
    # 不能改成 14 × 0.15 = 2.1 秒 —— 那会把「渲染慢但本来能成功」的切换误判成
    # 失败，回退到 _locate_contact 的慢路径（回顶 + 逐屏遍历），反而更慢。
    # 这里保持「先 sleep 再检查」的顺序，等 DOM 稳定一拍再读，
    # 避免刚点完就判据（_verify_item_active 用的是包含匹配，会误判成已切换）。
    for _ in range(28):
        time.sleep(0.15)
        if _verify_item_active(page, name) or _verify_in_conversation(page, name):
            return True
    return False


def _order_targets_by_list(page, targets: list) -> list:
    """按好友在会话列表中的上下位置排序发送顺序。

    虚拟列表只渲染可视区，若发送顺序在列表顶部/底部之间跳跃，就要反复
    回顶+滚底。按列表位置从上到下发送，使定位滚动基本单调向下；
    当前不可见（未渲染、需滚动/搜索）的好友统一排到最后。
    """
    try:
        titles = [_norm_text(t) for t in
                  page.locator(".conversationConversationItemtitle").all_text_contents()]

        def position(entry):
            nm = _norm_text(entry.get("display_name", ""))
            for i, t in enumerate(titles):
                # 严格相等，且两边都已过 _norm_text（titles 在上方构造时已归一化）。
                # 旧写法带 `nm in t or t in nm`：虽然只影响发送顺序、不影响发给谁，
                # 但「张三」会被排到「张三丰」的位置。统一判据口径，避免这份
                # 「位置匹配」日后被当成定位依据或与其它判据行为不一致。
                if nm and nm == t:
                    return i
            return 9999

        ordered = sorted(targets, key=position)
        return ordered
    except Exception:
        return targets


def _locate_contact(page, name: str) -> bool:
    """定位并打开好友会话。

    抖音左侧会话列表是虚拟滚动（只渲染可视区），且打开/发送后列表停留位置、
    顺序会变化。旧逻辑只会单调向下滚，一旦列表停在底部而目标在顶部就永远找不到，
    最终落到很慢的搜索兜底（十余秒）。这里改为：
      1) 快路径：当前可视区直接点；
      2) 慢路径：先滚回顶部，再一屏屏向下系统遍历整个列表；
      3) 仍找不到才用搜索框兜底。
    """
    # 快路径：当前可视区（_find_contact 现在可能返回 None，必须先判空）
    try:
        target = _find_contact(page, name)
        if target is not None and target.count() and _click_and_verify(page, target.first, name):
            return True
    except Exception as e:
        logger.info("快路径定位 %s 异常: %s", name, str(e)[:100])

    # 慢路径：回顶后逐屏遍历（好友仅十余人，几屏内必覆盖）
    _scroll_list_to_top(page)
    time.sleep(0.35)
    for _ in range(12):
        try:
            target = _find_contact(page, name)
            if target is not None and target.count() and _click_and_verify(page, target.first, name):
                return True
            info = _scroll_list_one_page(page)
            time.sleep(0.45)  # 等虚拟列表渲染新的一屏
            if info and info.get("after", 0) + info.get("client", 0) >= info.get("height", 0) - 5:
                # 已滚到底，最后在底部再确认一次
                target = _find_contact(page, name)
                if target is not None and target.count() and _click_and_verify(page, target.first, name):
                    return True
                break
        except Exception as e:
            logger.info("遍历定位 %s 异常: %s", name, str(e)[:100])
            time.sleep(0.4)

    # 搜索兜底（极少触发）
    if _search_and_open(page, name):
        time.sleep(random.uniform(0.5, 1))
        return _verify_item_active(page, name) or _verify_in_conversation(page, name)
    return False


# ── 消息输入与发送 ────────────────────────────────────────────────────────


def _wait_text_in_box(input_box, msg_text: str, timeout: float = 1.0) -> bool:
    """轮询等待文字真正进入输入框。

    替代原先「固定 sleep(0.4) 再检查一次」：渲染快时几毫秒就返回，
    渲染慢时最多等 timeout 秒。注意 timeout 比原来的 0.4 秒更宽裕，
    因此既能提速、又比旧实现更不容易误判「文字未进入输入框」。
    """
    deadline = time.time() + timeout
    while True:
        try:
            if msg_text in (input_box.inner_text() or ""):
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.03)


def _type_and_send(page, input_box, msg_text: str) -> bool | None:
    """False means Enter was never attempted; None means it may have fired."""
    send_attempted = False
    try:
        input_box.click()
        time.sleep(0.06)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.06)
        # 消息很短（如「[续火花吧]」5 字符），40ms 逐字延迟既保留拟人节奏，
        # 又比原来 100ms 快一半多
        page.keyboard.type(msg_text, delay=40)
        if not _wait_text_in_box(input_box, msg_text, timeout=1.0):
            try:
                cur = (input_box.inner_text() or "")[:30]
            except Exception:
                cur = "<读取失败>"
            logger.warning("文字未进入输入框，当前内容: %r", cur)
            return False
        send_attempted = True
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return None if send_attempted else False


_OUTBOUND_SNAPSHOT_JS = r"""
(text) => {
    // Require direction evidence on the message wrapper. A cleared editor or
    // an old matching incoming message is never a delivery confirmation.
    const normalize = s => String(s || '').replace(/\s+/g, ' ').trim();
    const wanted = normalize(text);
    const wrappers = new Set();
    for (const node of document.querySelectorAll(
        '[data-message-id], [data-msg-id], [class*="message"], [class*="Message"], [class*="bubble"], [class*="Bubble"]'
    )) {
        if (node.closest('[contenteditable="true"]') || normalize(node.textContent) !== wanted) continue;
        let current = node;
        let wrapper = null;
        let blocked = false;
        for (let depth = 0; current && depth < 5; depth++, current = current.parentElement) {
            const classes = String(current.className || '');
            const direction = current.getAttribute('data-direction') || current.getAttribute('data-sender') || '';
            const own = /^(outgoing|sent|self|mine)$/.test(direction) ||
                current.getAttribute('data-is-self') === 'true' ||
                classes.split(/\s+/).some(c => /^(?:(?:message|msg|bubble)[\w-]*(?:self|outgoing|mine|right)|(?:self|outgoing|mine|right)[\w-]*(?:message|msg|bubble))[\w-]*$/i.test(c));
            const state = current.getAttribute('data-status') || '';
            if (/(failed|error|pending|sending)/i.test(state + ' ' + classes) ||
                (own && current.querySelector('[data-status="failed"], [data-status="sending"], [class*="sendFailed"], [class*="Sending"]'))) {
                blocked = true;
                break;
            }
            if (own) wrapper = current;
        }
        if (wrapper && !blocked) wrappers.add(wrapper);
    }
    const ids = [];
    for (const wrapper of wrappers) {
        const message = wrapper.matches('[data-message-id], [data-msg-id]') ? wrapper :
            wrapper.querySelector('[data-message-id], [data-msg-id]');
        const id = message && (message.getAttribute('data-message-id') || message.getAttribute('data-msg-id'));
        if (id) ids.push(id);
    }
    return {count: wrappers.size, ids};
}
"""


def _outbound_snapshot(page, msg_text: str) -> dict | None:
    try:
        snapshot = page.evaluate(_OUTBOUND_SNAPSHOT_JS, msg_text)
        if isinstance(snapshot, dict) and isinstance(snapshot.get("count"), int):
            return snapshot
    except Exception:
        pass
    return None


def _wait_new_outbound(page, msg_text: str, before: dict | None, wait: float = 8) -> bool:
    if before is None:
        return False
    previous_ids = set(before.get("ids") or [])
    deadline = time.monotonic() + wait
    while True:
        snapshot = _outbound_snapshot(page, msg_text)
        if snapshot is not None:
            # Native message IDs can confirm a new item even in a virtual list;
            # without IDs require the visible count to increase.
            if (previous_ids or before.get("count", 0) == 0) and set(snapshot.get("ids") or []) - previous_ids:
                return True
            if not snapshot.get("ids") and not previous_ids and snapshot.get("count", 0) > before.get("count", 0):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)


def _send_message(page, msg_text: str, dry_run: bool) -> tuple[bool | None, str]:
    """True=confirmed echo, False=not attempted, None=uncertain delivery."""
    if detect_rate_limit(page):
        return False, "发送前检测到验证提示"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False, "找不到聊天输入框"

    if dry_run:
        return True, "dry-run"

    before = _outbound_snapshot(page, msg_text)
    attempted = _type_and_send(page, input_box, msg_text)
    if attempted is False:
        return False, "文字未能输入到输入框"
    if _wait_new_outbound(page, msg_text, before, wait=8):
        return True, "ok"
    return None, "已尝试发送但未确认新出站消息回显，结果未知，需人工核对"


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool | None, str]:
    """完整流程：定位好友 → 校验切换 → 发送消息。"""
    if not _locate_contact(page, name):
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"

    # ★ 发送前最终防线：在「输入文字并按 Enter」之前再确认一次当前会话是目标好友。
    #
    # 定位阶段已校验过一次，这里为什么还要再来一次：定位校验与真正打字之间还隔着
    # detect_rate_limit、输入框解析等若干次页面交互，页面状态存在被改变的可能；
    # 这是发错人之前最后一道可以便宜拦住的关口。
    #
    # 判据直接复用 _click_and_verify 用的那一对函数（二者都已收紧为严格相等），
    # 【不另写一套 strict 版本】—— 文档原稿建议新写 _verify_current_conversation_strict，
    # 但那与收紧后的 _verify_item_active 逐行等价，会造成两份必须同步维护的逻辑。
    # 这里同时保留 `or` 双判据（curConversation class + 顶部标题）而不是只用其中一个：
    # 单判据一旦因抖音改版失效，会对每个好友都返回 False -> 全员漏发。
    #
    # 该防线是 fail-closed 的：宁可漏发也不发错人。「两个判据同时失效导致全员漏发」
    # 这种新失败模式由 run_send 结尾的「0 成功」ERROR 告警兜住。
    if not (_verify_item_active(page, name) or _verify_in_conversation(page, name)):
        logger.warning("发送前最终校验失败：目标「%s」不是当前会话，已取消发送", name)
        return False, f"发送前校验失败：当前会话不是「{name}」，为避免发错人已取消发送"

    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"
    return _send_message(page, msg_text, dry_run)


# ── 联系人同步 ────────────────────────────────────────────────────────────

_EXTRACT_JS = r"""
    () => {
        const out = [];
        const seen = new Set();
        
        // 尝试多种常见的抖音会话标题选择器
        const selectors = [
            '.conversationConversationItemtitle',
            '[class*="conversationConversationItemtitle"]',
            '[class*="Itemtitle"]',
            '[class*="Item-title"]',
            '[class*="item-title"]',
            '[class*="conversation-item-title"]'
        ];
        
        let titleElements = [];
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            if (els && els.length > 0) {
                titleElements = Array.from(els);
                break;
            }
        }
        
        // 兜底扫描左侧面板中所有会话容器
        if (titleElements.length === 0) {
            const items = document.querySelectorAll('[class*="conversation"], [class*="Conversation"], [class*="chat-item"], li');
            items.forEach(it => {
                const rect = it.getBoundingClientRect();
                if (rect.width > 50 && rect.left < 360) {
                    const titleEl = it.querySelector('[class*="title"], [class*="name"], span');
                    if (titleEl) titleElements.push(titleEl);
                }
            });
        }

        // 从元素文本中提取火花天数：必须包含火焰图标或火花标识
        function extractStreak(rootEl) {
            if (!rootEl) return '';
            // 方法1：找包含火花相关 class 的元素
            const streakEls = rootEl.querySelectorAll('[class*="Streak"], [class*="streak"], [class*="commonStreak"], [class*="fire"], [class*="Fire"], [class*="spark"], [class*="Spark"]');
            for (const el of streakEls) {
                const text = (el.textContent || '').trim();
                // 必须包含数字，且数字在合理范围（1-999天）
                const m = text.match(/\d+/);
                if (m) {
                    const num = parseInt(m[0]);
                    if (num >= 1 && num <= 999) return m[0];
                }
            }
            // 方法2：找包含火焰emoji且有数字的元素
            const allEls = rootEl.querySelectorAll('*');
            for (const el of allEls) {
                const text = (el.textContent || '').trim();
                if (text && text.length < 20 && (text.includes('🔥') || text.includes('🔥') || /[\u{1F525}]/u.test(text))) {
                    const m = text.match(/\d+/);
                    if (m) {
                        const num = parseInt(m[0]);
                        if (num >= 1 && num <= 999) return m[0];
                    }
                }
            }
            // 方法3：在昵称同级/父级找小尺寸数字元素（火花天数通常是小字体）
            let p = rootEl.parentElement;
            for (let i = 0; i < 4 && p; i++) {
                const spans = p.querySelectorAll('span, div, em, i, b');
                for (const el of spans) {
                    const text = (el.textContent || '').trim();
                    // 纯数字且长度<=3，且元素尺寸较小
                    if (/^\d{1,3}$/.test(text)) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.width < 60 && rect.height > 0 && rect.height < 30) {
                            return text;
                        }
                    }
                }
                p = p.parentElement;
            }
            return '';
        }

        titleElements.forEach(t => {
            const name = (t.textContent || '').trim();
            if (!name || seen.has(name) || name.length > 40) return;
            if (name === '消息' || name === '私信' || name === '朋友私信' || name === '通知') return;
            seen.add(name);
            
            // 扫描父级附近节点中的火花标签和头像
            let streak = '';
            let avatar = '';
            let p = t.parentElement;
            for (let i = 0; i < 6 && p; i++) {
                // 火花标签（改进版）
                if (!streak) {
                    streak = extractStreak(p);
                }
                // 头像 img
                if (!avatar) {
                    const imgs = p.querySelectorAll('img');
                    for (const img of imgs) {
                        const src = img.src || img.getAttribute('src') || '';
                        const rect = img.getBoundingClientRect();
                        // 头像通常是小尺寸正方形，且在左侧
                        if (src && rect.width > 20 && rect.width < 80 && rect.left < 200) {
                            avatar = src;
                            break;
                        }
                    }
                }
                if (streak && avatar) break;
                p = p.parentElement;
            }
            
            // 再兜底：在整个会话项里找第一个 img
            if (!avatar) {
                let item = t.closest('[class*="conversation"], [class*="Conversation"], [class*="chat-item"], li');
                if (item) {
                    const img = item.querySelector('img');
                    if (img && (img.src || img.getAttribute('src'))) {
                        avatar = img.src || img.getAttribute('src');
                    }
                }
            }
            
            out.push({ name: name, streak: streak, avatar: avatar });
        });
        return out;
    }
"""


def _open_chat_page(page) -> bool:
    """打开抖音私信页并等待加载，返回是否成功。"""
    for attempt in range(3):
        try:
            page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
            return True
        except Exception as e:
            logger.info("打开页面失败（第 %s 次）: %s", attempt + 1, str(e)[:80])
            time.sleep(5)
    return False


def _scroll_and_extract(page, collected: list[dict], max_rounds: int = 20) -> None:
    """滚动聊天列表并提取联系人，直到没有新数据。

    去重使用「(name, streak) 集合」而非 `x not in collected`：
    后者是 O(n²)——collected 是字典列表，每个候选都要与全部历史项做字典
    相等比较。滚动 20 轮、上百联系人时是上千次字典比较，纯属浪费 CPU。
    另外把稳定计数改为局部变量，不再挂在函数对象属性上
    （旧写法 `_scroll_and_extract._stable` 是跨调用共享的可变状态，
    并发调用会互相干扰，也不可重入）。
    """
    seen = {(c.get("name"), c.get("streak")) for c in collected}
    stable = 0
    for _ in range(max_rounds):
        data = page.evaluate(_EXTRACT_JS) or []
        new_items = [x for x in data if (x.get("name"), x.get("streak")) not in seen]
        if new_items:
            collected.extend(new_items)
            seen.update((x.get("name"), x.get("streak")) for x in new_items)
            stable = 0
        else:
            stable += 1
            if stable >= 2:
                break
        try:
            page.mouse.move(200, 350)
            page.mouse.wheel(0, 800)
        except Exception:
            pass
        page.wait_for_timeout(1200)


def fetch_chat_contacts() -> dict:
    """公共入口（可在任意线程调用）：在专用 Playwright 工作线程中采集联系人。"""
    return _run_in_pw_thread(_fetch_chat_contacts_impl)


_CONTACT_TITLE_SELECTOR = ".conversationConversationItemtitle"


def _wait_for_contacts(page, timeout_ms: int = 20000) -> bool:
    """等待联系人列表渲染完成，就绪立即返回 True。

    移植自 A 的性能优化：替代固定 `page.wait_for_timeout(10000)`——那是无条件
    等待，无论页面 3 秒还是 10 秒就绪都要坐满 10 秒。这里复用本模块已有的
    `_wait_any`（条件早满足就早返回，最坏情况与固定等待等长），
    单次采集通常可省下 5-8 秒。
    """
    return _wait_any(page.locator(_CONTACT_TITLE_SELECTOR), timeout_ms / 1000.0)


def _fetch_chat_contacts_impl() -> dict:
    """从抖音私信页左侧聊天列表读取联系人（含火花天数），供网页端勾选。"""
    result = {"at": _now(), "names": [], "error": None}
    if not get_valid_state_path():
        result["error"] = "尚未上传登录态 state.json"
        return result

    try:
        with open_browser() as (p, browser, context, page):
            if not _open_chat_page(page):
                result["error"] = "无法打开抖音私信页面"
                return result

            # 事件驱动等待列表渲染（旧写法无条件等 10 秒）
            if not _wait_for_contacts(page):
                logger.info("联系人列表 20 秒内未出现，仍继续尝试检查登录态")

            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result

            collected: list[dict] = []
            for attempt in range(3):
                try:
                    page.wait_for_selector(_CONTACT_TITLE_SELECTOR, timeout=45000)
                except Exception:
                    logger.info("第 %s 次等待联系人列表超时", attempt + 1)

                _scroll_and_extract(page, collected)

                if collected:
                    break
                try:
                    page.reload(wait_until="domcontentloaded", timeout=90000)
                    _wait_for_contacts(page, timeout_ms=15000)  # 旧写法固定等 12 秒
                except Exception:
                    pass

            result["names"] = collected
            logger.info("已读取聊天列表联系人 %s 个", len(result["names"]))
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    return result


# ── 通道选择 ───────────────────────────────────────────────────────────────


def _b_channel_daily() -> tuple[str, int]:
    """通道 B 今日已发条数：优先 runtime 计数，跨天自动归零。"""
    today = ledger.business_date()
    rec = load_runtime().get("b_channel_daily") or {}
    if rec.get("date") != today:
        return today, 0
    return today, int(rec.get("count", 0) or 0)


def compute_pending(cfg: dict | None = None) -> list[dict]:
    """预测本次运行会真实发送的名单（与 run_send 通道判定一致）。"""
    cfg = cfg or load_config()
    entries = ledger.get_selected()
    daily_limit = max(1, _cfg_int(cfg, "first_message_daily_limit", 1))
    _, creator_sent_today = _b_channel_daily()
    allow_first = bool(cfg.get("allow_first_message"))
    pending: list[dict] = []
    for e in entries:
        if ledger.send_block_reason(e):
            continue
        if e.get("has_conversation"):
            pending.append({**e, "send_channel": "consumer"})
        elif allow_first and creator_sent_today < daily_limit:
            pending.append({**e, "send_channel": "creator"})
    return pending


# ── 主发送流程 ─────────────────────────────────────────────────────────────


def _send_consumer(page, entry: dict, msg: str, dry_run: bool, result: dict) -> None:
    """通道 A：consumer 重防护发送。"""
    name = entry["display_name"]
    attempt_id = None
    if not dry_run:
        claimed, why, attempt_id = ledger.claim_send(entry)
        if not claimed:
            result["skipped"].append({"name": name, "reason": why})
            return
    try:
        ok, why = send_to_contact(page, name, msg, dry_run)
    except Exception as exc:
        ok, why = None, f"发送流程中断，结果未知，需人工核对：{exc}"
    if ok:
        result["ok"].append(name)
        if not dry_run:
            ledger.confirm_join(name)
        logger.info("已发送给 %s：%s", name, msg if not dry_run else "(干跑)")
    elif ok is None:
        result.setdefault("unknown", []).append({"name": name, "reason": why})
        logger.warning("发送给 %s 结果未知，已禁止当天自动重发：%s", name, why)
        if detect_rate_limit(page):
            result["rate_limited"] = True
    else:
        if entry.get("channel") == "creator":
            result["skipped"].append({
                "name": name,
                "reason": "consumer 定位失败（该好友为 creator-only），已降级跳过",
            })
            ledger.mark_no_consumer_conversation(name)
        else:
            result["failed"].append({"name": name, "reason": why})
            logger.warning("发送给 %s 失败：%s", name, why)
            if detect_rate_limit(page):
                result["rate_limited"] = True
                logger.warning("疑似触发限流，停止本轮")
    if not dry_run:
        ledger.update_send_result(name, ok, _now(), msg=msg, entry=entry,
                                  attempt_id=attempt_id, reason=why)


def _send_creator(entry: dict, msg: str, dry_run: bool, result: dict, p) -> None:
    """通道 B：creator 首条消息。"""
    name = entry["display_name"]
    if creator_channel is None:
        # core/sender.py 仍是空壳：这里若直接调用会抛 AttributeError，
        # 被上层 except 捕获后整轮发送就此中断（后面所有好友都不再发）。
        # 只把该好友记为 skipped，其余好友继续。开关侧另有 /api/config 拦截。
        result["skipped"].append(
            {"name": name, "reason": "通道 B（creator 首条消息）尚未实现"}
        )
        return
    cfg = load_config()
    allow_first = bool(cfg.get("allow_first_message"))
    daily_limit = max(1, _cfg_int(cfg, "first_message_daily_limit", 1))
    today, count = _b_channel_daily()

    if not allow_first:
        result["skipped"].append({"name": name, "reason": "无会话且未开启「允许首条消息」"})
        return
    if count >= daily_limit:
        result["skipped"].append({
            "name": name,
            "reason": f"今日已发送首条消息 {count}/{daily_limit}",
        })
        return

    attempt_id = None
    if not dry_run:
        claimed, why, attempt_id = ledger.claim_send(entry)
        if not claimed:
            result["skipped"].append({"name": name, "reason": why})
            return
    try:
        ok, why = creator_channel.send_first_message(entry, msg, dry_run, p)
    except Exception as exc:
        ok, why = None, f"通道 B 流程中断，结果未知：{exc}"
    if ok:
        result["ok"].append(name)
        logger.info("通道 B 已发送给 %s：%s", name, msg if not dry_run else "(干跑)")
        if not dry_run:
            update_runtime(b_channel_daily={"date": today, "count": count + 1})
    elif ok is None:
        result.setdefault("unknown", []).append({"name": name, "reason": f"通道B: {why}"})
        if not dry_run:
            update_runtime(b_channel_daily={"date": today, "count": count + 1})
    else:
        result["failed"].append({"name": name, "reason": f"通道B: {why}"})
        logger.warning("通道 B 发送给 %s 失败：%s", name, why)
        if "限流" in why or "停止" in why:
            result["rate_limited"] = True
            logger.warning("通道 B 触发限流，停止本轮")
    if not dry_run:
        ledger.update_send_result(name, ok, _now(), via_creator=True, msg=msg,
                                  entry=entry, attempt_id=attempt_id, reason=why)


def run_send(dry_run: bool = False, only_names: list[str] | None = None) -> dict:
    """公共入口（可在任意线程调用）：在专用 Playwright 工作线程中执行发送。

    与预启动任务共用同一个单线程执行器，因此会天然等待预启动完成，
    并复用其浏览器实例，不会出现两个任务同时操作浏览器。
    """
    result = _run_in_pw_thread(_run_send_impl, dry_run, only_names)

    # 「本轮 0 成功」告警。
    #
    # 会话定位与校验现在全部是 fail-closed（宁可漏发也不发错人），因此
    # 「抖音改版导致判据整体失效」这类问题的新表现不再是「发错人」，而是
    # 「本轮一个人都没发出去」。这种情况必须用 ERROR 显式报出来：
    # 否则日志里只有每人一行 warning，得等到第二天发现火花断了才知道，白断一晚。
    if isinstance(result, dict) and not dry_run:
        try:
            failed = result.get("failed") or []
            if failed and not result.get("ok"):
                logger.error(
                    "本轮发送 0 成功：失败 %s 人、跳过 %s 人 —— 请检查登录态是否过期，"
                    "或抖音页面结构变化导致会话定位/校验判据失效",
                    len(failed), len(result.get("skipped") or []),
                )
        except Exception:
            pass
    return result


# ── 发送时间预算（移植自 A 的性能优化）───────────────────────────────────
# 目标：让一轮发送在指定秒数内完成，且不牺牲安全间隔。
#
# 做法不是砍固定等待（那些已压到接近下限），而是按「剩余时间 / 剩余人数」
# 动态分配好友之间的间隔：人多时自动收紧、人少时放宽到上限，恒定不超过
# SEND_BUDGET_SECONDS。预算耗尽就停止本轮，剩余好友留给后台补发任务，
# 避免把本轮无限拖长（原实现固定 random(gap_min, gap_max)，10 人约 30 秒
# 间隔、20 人约 60 秒，人数一多必然超时）。
SEND_BUDGET_SECONDS = max(0, int(os.environ.get("SEND_BUDGET_SECONDS", "60")))
# 任意两个好友之间的间隔上下限（秒）。实际取值以配置的 send_gap_min/max 为准，
# 并进一步受时间预算约束：SEND_INTERVAL_MAX **只是配置缺失时的兜底上限**，
# 不再反过来夹住配置（旧实现的 min(SEND_INTERVAL_MAX, gap_max) 正是那个 bug）。
SEND_INTERVAL_MIN = max(0.3, float(os.environ.get("SEND_INTERVAL_MIN", "1.0")))
SEND_INTERVAL_MAX = max(SEND_INTERVAL_MIN, float(os.environ.get("SEND_INTERVAL_MAX", "4.0")))
# 每个好友的固定开销估算（秒）：_click_and_verify 轮询 + _type_and_send + 输入框清空校验。
# 依据 2026-09-18 实测（00:20:07→00:20:59，11 人 56 秒，间隔均值约 3s）反推约 1.5 秒。
# 仅用于「剩余时间是否还塞得下一个人」的预检，不参与实际等待时长计算。
_PER_FRIEND_FIXED_COST = max(0.0, float(os.environ.get("PER_FRIEND_FIXED_COST", "1.5")))


def _send_interval(deadline: float | None, remaining: int, gap_max: float) -> float:
    """按剩余时间预算动态决定下一个好友之前的等待秒数。

    - deadline=None：返回 0，由调用方走原来的随机间隔逻辑；
    - 剩余时间充足：间隔上限取配置的 gap_max；
    - 时间偏紧：收紧到 avg = 剩余时间 / 剩余人数，并按 ±25% 抖动，
      既保证按时完成，又不产生固定节奏特征。
    """
    if deadline is None:
        return 0.0
    left = deadline - time.time()
    if left <= 0:
        return 0.0
    avg = left / max(1, remaining)

    # hi = 本次间隔的上限，**以配置的 gap_max 为准**。
    #
    # 旧写法 `min(SEND_INTERVAL_MAX, max(gap_max, SEND_INTERVAL_MIN))` 与本函数
    # 文档「取较大者」正好相反：硬编码的 4.0 会把配置里更大的 gap_max 夹掉，
    # 配置成 6~12 时实际最多只等 4 秒，「间隔上限」形同虚设（与 deadline=None
    # 那条路径行为不一致，同样的配置在两处得到不同节奏）。
    #
    # 但也不能简单反过来写 max(SEND_INTERVAL_MAX, gap_max)：gap_max 现在是 2，
    # 那样会把间隔顶到 4 秒，10 人从约 30 秒拖到 35 秒以上，与「1 分钟内发完」冲突。
    # 正确语义只有一条 —— 配置说了算。gap_max 由 _run_send_impl 从
    # data/config.json 读出，且已保证 >= gap_min >= 1；SEND_INTERVAL_MAX
    # 仅在它缺失（0/None）时才兜底。
    cap = float(gap_max) if gap_max else SEND_INTERVAL_MAX
    hi = max(SEND_INTERVAL_MIN, cap)
    center = max(SEND_INTERVAL_MIN, min(hi, avg))
    # 抖动后必须再次夹紧：否则 ±25% 会突破 hi 上限
    jittered = center * random.uniform(0.75, 1.25)
    return max(SEND_INTERVAL_MIN, min(hi, jittered))


def _run_send_impl(dry_run: bool = False, only_names: list[str] | None = None) -> dict:
    """主入口：从好友台账读取勾选目标，逐个发送（在专用工作线程内运行）。"""
    cfg = load_config()
    messages = cfg.get("messages") or ["🔥"]
    # max_friends_per_run=0 在配置里表示「不限制」，必须保留 0 的语义，
    # 不能被 `or 20` 换成 20 —— 否则「不限制」这一档永远失效。
    max_n = max(0, _cfg_int(cfg, "max_friends_per_run", 20))
    gap_min = max(1, _cfg_int(cfg, "send_gap_min", 6))
    gap_max = max(gap_min, _cfg_int(cfg, "send_gap_max", 12))

    result = {
        "at": _now(), "dry_run": bool(dry_run),
        "ok": [], "failed": [], "unknown": [], "skipped": [],
        "logged_out": False, "rate_limited": False,
    }

    if not get_valid_state_path():
        result["failed"].append({"name": "_system", "reason": "尚未上传登录态 state.json"})
        return result

    targets = ledger.get_selected()
    if not targets and cfg.get("friends"):
        stats = ledger.import_config_friends(cfg["friends"])
        logger.info("已从 config.friends 迁移进台账：新增 %s 人，勾选 %s 人",
                     stats["added"], stats["selected"])
        targets = ledger.get_selected()
    if only_names is not None:
        targets = [t for t in targets if t.get("display_name") in only_names]
    if not dry_run:
        pending = []
        for entry in targets:
            reason = ledger.send_block_reason(entry)
            if reason:
                result["skipped"].append({"name": entry["display_name"], "reason": reason})
            else:
                pending.append(entry)
        targets = pending
    targets = targets[:max_n] if max_n > 0 else targets

    if not targets:
        logger.info("未配置任何好友，跳过发送")
        return result

    using_prewarmed = False
    browser_ctx = None
    p = browser = context = page = None
    try:
        # 优先使用预启动的浏览器（预启动已打开页面并等待联系人列表，节省数分钟）
        prewarmed = _get_prewarmed_page()
        if prewarmed:
            p, browser, context, page = (
                prewarmed["p"], prewarmed["browser"], prewarmed["context"], prewarmed["page"]
            )
            using_prewarmed = True
            try:
                logged, why = check_login(page)
            except Exception as e:
                # page 已崩（TargetClosedError 等）：get_browser_status 只看缓存标志、
                # 探不出这种情况。关闭预启动实例，回落到下面的冷启动分支重建，
                # 而不是让异常冒泡成「运行异常」直接结束本轮。
                logger.warning("预启动 page 探活异常（%s），关闭后改走冷启动", e)
                _close_prewarmed()
                using_prewarmed = False
                prewarmed = None
                p = browser = context = page = None
            else:
                if not logged:
                    result["logged_out"] = True
                    result["failed"].append({"name": "_system", "reason": why})
                    _screenshot(page)
                    return result
                logger.info("使用预启动浏览器，待发送好友 %s 人，dry_run=%s", len(targets), dry_run)

        if not using_prewarmed:
            # 冷启动兜底（无预启动实例，或预启动 page 探活异常时）
            logger.info("未检测到可用预启动浏览器，执行冷启动...")
            browser_ctx = open_browser()
            p, browser, context, page = browser_ctx.__enter__()
            if not _open_chat_page(page):
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result

            # 智能等待：DOM 加载 + 输入框出现
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            try:
                page.wait_for_selector("input", timeout=8000)
            except Exception:
                time.sleep(2)
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page)
                return result

            # 阻塞等待左侧联系人列表渲染完成（最多 3 轮，每轮 45 秒）
            list_ready = False
            for _ in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
                    list_ready = True
                    break
                except Exception:
                    logger.info("联系人列表尚未出现，刷新后重试等待")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=90000)
                        try:
                            page.wait_for_selector("input", timeout=10000)
                        except Exception:
                            time.sleep(3)
                    except Exception:
                        pass
            if list_ready:
                logger.info("联系人列表已加载完成")
                time.sleep(0.5)
            else:
                logger.warning("等待联系人列表超时，仍尝试继续发送")
            logger.info("冷启动完成，待发送好友 %s 人，dry_run=%s", len(targets), dry_run)

        # 按好友在会话列表中的上下位置排序，使定位滚动基本单调向下，
        # 避免在虚拟列表顶部/底部之间来回滚动（底部好友统一留到最后）
        targets = _order_targets_by_list(page, targets)
        logger.info("发送顺序（按列表位置）：%s",
                     [t.get("display_name") for t in targets])

        # ★ 发送循环：预启动 / 冷启动两条路径共用（修复原先预启动成功反而不发送的 bug）
        # 建立时间预算：整轮在 SEND_BUDGET_SECONDS 内完成（0 表示不限制，走原随机间隔）
        deadline = (time.time() + SEND_BUDGET_SECONDS) if SEND_BUDGET_SECONDS > 0 else None
        n_targets = len(targets)
        if deadline is not None:
            logger.info("本轮发送预算 %s 秒，目标 %s 人（人均约 %.1f 秒）",
                        SEND_BUDGET_SECONDS, n_targets, SEND_BUDGET_SECONDS / max(1, n_targets))

        # 实时进度（前端运行中轮询显示）；收尾在 finally 统一清空
        update_runtime(send_progress={
            "total": n_targets, "done": 0, "current": None, "ok": 0, "failed": 0, "unknown": 0,
        })

        for idx, entry in enumerate(targets):
            cur_name = entry.get("display_name", "")
            update_runtime(send_progress={
                "total": n_targets, "done": idx, "current": cur_name,
                "ok": len(result["ok"]), "failed": len(result["failed"]),
                "unknown": len(result["unknown"]),
            })
            # 预算预检：若剩余时间不足以容纳「最小间隔 + 下一个人的固定开销 + 余量」，
            # 就不再开新好友，剩余交给补发任务。
            # 余量用于吸收固定开销估算误差——否则最后一个人的开销会落在预算之外
            # （实测：余量 1.15 时 45 秒档位会到 45.8 秒；1.35 可全覆盖各档位）。
            if deadline is not None and idx > 0:
                left = deadline - time.time()
                if left < SEND_INTERVAL_MIN + _PER_FRIEND_FIXED_COST * 1.35:
                    result["skipped"].append({
                        "name": "_system",
                        "reason": (f"本轮 {SEND_BUDGET_SECONDS} 秒预算不足以容纳后续 "
                                   f"{n_targets - idx} 人，留给补发"),
                    })
                    logger.warning("预算不足（剩余 %.1fs），跳过后续 %s 人，交给补发任务",
                                   left, n_targets - idx)
                    break

            msg = build_message(messages, last_sent_msg=str(entry.get("last_msg", "")))
            if entry.get("has_conversation"):
                _send_consumer(page, entry, msg, dry_run, result)
            else:
                _send_creator(entry, msg, dry_run, result, p)

            if result["rate_limited"]:
                break

            remaining = n_targets - idx - 1
            if remaining <= 0:
                break

            # 预算耗尽：停止本轮
            if deadline is not None and time.time() >= deadline:
                result["skipped"].append({
                    "name": "_system",
                    "reason": f"本轮 {SEND_BUDGET_SECONDS} 秒预算已用尽，剩余 {remaining} 人留给补发",
                })
                logger.warning("发送预算已用尽，剩余 %s 人本轮跳过（将由补发任务处理）", remaining)
                break

            if deadline is None:
                # 未设预算：保持原有随机间隔行为
                time.sleep(random.uniform(max(1.0, gap_min), max(gap_min, gap_max)))
            else:
                time.sleep(_send_interval(deadline, remaining, gap_max))

        if deadline is not None:
            logger.info("本轮发送结束，用时 %.1f 秒（预算 %s 秒）",
                        time.time() - (deadline - SEND_BUDGET_SECONDS), SEND_BUDGET_SECONDS)
        result["total"] = n_targets
        result["done"] = len(result["ok"]) + len(result["failed"]) + len(result["unknown"])
    except Exception as e:
        logger.error("运行异常: %s", e)
        result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    finally:
        # 无论正常结束、预算 break、限流还是异常，都清空进度，避免前端卡在“进行中”
        try:
            update_runtime(send_progress=None)
        except Exception:
            pass
        # 收尾策略（移植自 A 的性能优化）：
        # - 冷启动路径（with open_browser 托管）：由 with 关闭，无法保留。
        # - 预启动路径：成功且未限流时保留实例，供短时间内再次运行复用；
        #   失败/限流则立即关闭，避免带着异常页面继续用。
        if using_prewarmed:
            keep = (
                KEEP_PREWARMED_SECONDS > 0
                and not result.get("rate_limited")
                and not result.get("logged_out")
            )
            if not (keep and _keep_prewarmed_alive()):
                _close_prewarmed()
        elif browser_ctx is not None:
            if (KEEP_BROWSER_ALWAYS
                    and not result.get("rate_limited")
                    and not result.get("logged_out")
                    and browser is not None):
                # 常驻模式：把冷启动的浏览器转为预启动状态保存，下次直接复用
                global _prewarmed_browser
                _prewarmed_browser = {
                    "p": p, "browser": browser, "context": context, "page": page,
                    "ready": True, "created_at": time.time(),
                }
                logger.info("常驻模式：浏览器已保留，下次发送直接复用（免冷启动）")
            else:
                try:
                    browser_ctx.__exit__(None, None, None)
                except Exception:
                    pass
    return result


# ── 浏览器常驻模式管理 ──────────────────────────────────────────────────────

def get_browser_status() -> dict:
    """查询当前浏览器状态（可在任意线程调用，只读元数据不操作 page）。"""
    if _prewarmed_browser and _prewarmed_browser.get("ready"):
        age = time.time() - _prewarmed_browser.get("created_at", 0)
        return {
            "alive": True,
            "age_seconds": int(age),
            "keep_always": KEEP_BROWSER_ALWAYS,
            "max_idle_seconds": BROWSER_MAX_IDLE_SECONDS,
        }
    return {"alive": False, "keep_always": KEEP_BROWSER_ALWAYS}


def restart_browser() -> bool:
    """非阻塞关闭当前浏览器；下次发送时自动冷启动。

    语义：**只是关闭，不是重启**（内部不含 chromium.launch）。返回 True 表示
    关闭请求已受理，不表示浏览器已关闭 —— 实际关闭在 pw 工作线程内完成。

    为什么投递到后台而不在此同步等待：`_pw_executor` 是单 worker，一轮发送可
    占用它 60 秒以上；若此处在 HTTP 线程内 `submit().result()` 同步等待，发送
    进行中（或浏览器卡死、run_send 长时间不返回）时 `/api/browser/restart` 会
    长期挂起 —— **恰在最需要点它的时刻失效**，并连带占满 AnyIO 线程池。

    注意：本函数可在任意线程调用，但**不得**在 pw 工作线程内调用（自查会抛
    RuntimeError）。pw 线程内请直接调用 `_close_prewarmed()`。
    """
    if threading.current_thread().name.startswith("pw-worker"):
        _close_prewarmed()
        return True

    def _do_close():
        _close_prewarmed()
        return True

    try:
        future = _pw_executor.submit(_do_close)
    except Exception as e:
        logger.warning("提交关闭浏览器任务失败：%s", e)
        return False
    # 捕获异常但绝不阻塞等待（fire-and-forget）
    future.add_done_callback(lambda f: f.exception())
    logger.info("已提交关闭浏览器请求，将在工作线程空闲时执行")
    return True


# 别名兼容
sync_contacts = fetch_chat_contacts

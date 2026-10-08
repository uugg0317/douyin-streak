#!/usr/bin/env python3
"""扫码提取抖音 Cookie —— 最小可独立运行的复现脚本。

【重要事实：本流程没有 ticket 换取环节】
    抖音没有用于「扫码换 Cookie」的公开 HTTP 接口参与本流程。整个登录由
    **真实 Chromium 自己完成**：浏览器加载 douyin.com 后自行渲染二维码、
    自行轮询扫码状态、自行在登录成功时写入 sessionid Cookie。本代码只做两件事：

        1. 截图（把浏览器里的二维码给你看）
        2. 轮询 context.cookies()，发现 sessionid 就落盘

    因此不存在「回调取 ticket」「请求换取 Cookie」这两步。经全项目检索确认：
    ticket / oauth / sso / check_qrconnect / get_qrcode / httpx / aiohttp /
    requests.post 在基线项目中均为 0 命中。详见 flow.md。

【与基线的对应关系】
本脚本展示项目的 Playwright 登录态获取流程。
    第 1517-1585 行的 _extract_body()。逐节点行号对照见 flow.md。
    差异仅两处，均为「脱离 Web 服务后必须做的替换」，不改变核心逻辑：
      · open_browser(...) 上下文管理器 -> 内联的 sync_playwright() 启动/关闭
        （open_browser 定义在 core/browser.py:58-115）
      · _extract_state 字典 -> 本地状态变量 + 回调（Web 端用它做进度轮询）

【安全】
    本脚本不打印任何 Cookie 值。日志里只出现 Cookie 的「数量」与「名字」，
    sessionid 的值一律显示为 <REDACTED:len=N>。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

# ── 与 core/browser.py:29-32 完全一致的 UA（保持与基线发送链路同一指纹）──────
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── 与 core/browser.py:34-39 一致；注意基线在非 root 时不加 --no-sandbox ──────
_COMMON_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]

DOUYIN_URL = "https://www.douyin.com/"          # 基线 app.py:1536
LOGIN_TIMEOUT_SECONDS = 300                     # 基线 app.py:1548「最长等待5分钟」
SCREENSHOT_INTERVAL_SECONDS = 10                # 基线 app.py:1557「每10秒更新一次截图」
POLL_INTERVAL_SECONDS = 1.5                     # 基线 app.py:1564


# ══════════════════════════════════════════════════════════════════════════
# 脱敏工具（基线没有这块，因为它的 Cookie 从不进日志）
# ══════════════════════════════════════════════════════════════════════════

SENSITIVE_PREFIXES = ("sessionid", "sid_guard", "uid_tt", "passport", "sso", "odin_tt")


def _mask(name: str, value: str) -> str:
    """任何可能作为凭据的 Cookie 值都只暴露长度。"""
    if name.startswith(SENSITIVE_PREFIXES):
        return f"<REDACTED:len={len(value)}>"
    return f"<len={len(value)}>"


def describe_cookies(cookies: list[dict[str, Any]]) -> str:
    """把 Cookie 列表压成一行摘要，绝不输出值。"""
    names = [c.get("name", "?") for c in cookies]
    key = [n for n in names if n.startswith("sessionid")]
    return (
        f"cookies={len(cookies)} "
        f"sessionid_present={bool(key)} "
        f"sessionid_names={key or 'none'} "
        f"distinct_names={len(set(names))}"
    )


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
# 核心：与 app.py:1517-1585 的 _extract_body 一一对应
# ══════════════════════════════════════════════════════════════════════════
def extract_cookie(
    out_path: Path,
    screenshot_path: Path,
    headless: bool = False,
    timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
    on_state: Callable[[str, str | None], None] | None = None,
) -> dict[str, Any]:
    """打开浏览器等扫码，成功后把 Playwright storage_state 写到 out_path。

    返回一个**脱敏**的结果字典，可直接打印或写日志。

    Raises:
        RuntimeError: 超时未检测到登录，或 Chromium 启动失败。
        ImportError: 未安装 playwright。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise ImportError(
            "缺少 playwright。修复：pip install -r requirements.txt "
            "然后再装浏览器：python -m playwright install chromium"
        ) from exc

    def emit(status: str, error: str | None = None) -> None:
        """等价于基线的 _extract_state 更新（app.py:1522-1524/1574-1579）。"""
        if on_state:
            on_state(status, error)
        _log(f"[state] status={status}" + (f" error={error}" if error else ""))

    screenshot_path.parent.mkdir(parents=True, exist_ok=True)   # app.py:1527
    out_path.parent.mkdir(parents=True, exist_ok=True)

    emit("waiting")                                            # app.py:1522

    p = None
    browser = None
    try:
        p = sync_playwright().start()                           # core/browser.py:86
        try:
            browser = p.chromium.launch(headless=headless, args=_COMMON_ARGS)
        except Exception as exc:
            raise RuntimeError(
                f"Chromium 启动失败：{type(exc).__name__}: {exc}\n"
                "  修复建议：python -m playwright install chromium\n"
                "  若已安装仍失败，检查是否缺少系统依赖："
                "python -m playwright install --with-deps chromium"
            ) from exc

        # 等价于 core/browser.py:90-101（viewport / UA / locale / timezone）
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=_CHROME_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
        )
        page = context.new_page()

        # 反自动化指纹。基线在 core/browser.py:103 调 _apply_stealth(page)，
        # 而 _apply_stealth（core/browser.py:42-55）在缺少新版 playwright_stealth
        # 时会静默跳过（except Exception: pass）。此处保持同样的可选语义，
        # 但把跳过原因打出来 —— 基线那处静默 Except 曾让「stealth 是否生效」无从排查。
        try:
            from playwright_stealth import Stealth

            Stealth().apply_stealth_sync(page)
            _log("[stealth] 已注入 playwright_stealth")
        except ImportError:
            _log("[stealth] 未安装 playwright_stealth，跳过注入（与基线降级行为一致）")
        except Exception as exc:  # noqa: BLE001
            _log(f"[stealth] 注入失败，跳过：{type(exc).__name__}: {exc}")

        # ── app.py:1536-1538 ────────────────────────────────────────────
        try:
            page.goto(DOUYIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 - 基线此处也是 pass
            _log(f"[warn] 导航异常但继续（与基线一致）：{type(exc).__name__}: {exc}")

        # ── app.py:1541-1546：等 3 秒后首次截图 ──────────────────────────
        time.sleep(3)
        try:
            page.screenshot(path=str(screenshot_path), full_page=False)
            _log(f"[qr] 已生成截图：{screenshot_path}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[warn] 截图失败：{type(exc).__name__}: {exc}")

        # ── app.py:1548-1564：轮询等待 sessionid 出现 ────────────────────
        deadline = time.time() + timeout_seconds
        logged_in = False
        last_screenshot = time.time()
        polls = 0
        while time.time() < deadline:
            cookies = context.cookies()
            polls += 1
            if any(c["name"].startswith("sessionid") for c in cookies):
                logged_in = True
                break
            if time.time() - last_screenshot > SCREENSHOT_INTERVAL_SECONDS:
                try:
                    page.screenshot(path=str(screenshot_path), full_page=False)
                    _log(f"[qr] 截图已刷新（第 {polls} 次轮询，{describe_cookies(cookies)}）")
                    last_screenshot = time.time()
                except Exception:  # noqa: BLE001 - 基线此处也是 pass
                    pass
            time.sleep(POLL_INTERVAL_SECONDS)

        if not logged_in:
            # 基线 app.py:1578-1579 的原文
            raise RuntimeError("5分钟内未检测到登录，请重试")

        # ── app.py:1566-1576：落盘 ──────────────────────────────────────
        time.sleep(2)
        context.storage_state(path=str(out_path))
        cookies = context.cookies()
        _log(f"[ok] 提取成功：{describe_cookies(cookies)}")
        emit("success")

        return {
            "ok": True,
            "out_path": str(out_path),
            "cookies_total": len(cookies),
            "cookie_names": sorted({c["name"] for c in cookies}),
            "sessionid_names": sorted(
                {c["name"] for c in cookies if c["name"].startswith("sessionid")}
            ),
            "poll_rounds": polls,
            "elapsed_seconds": round(timeout_seconds - (deadline - time.time()), 1),
        }
    finally:
        # 等价于 core/browser.py:106-115：browser 与 playwright 都要关。
        # 基线注释特别指出旧实现漏了 p.stop()，每次提取都残留一个驱动进程。
        if browser:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if p:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="扫码提取抖音 Cookie（复现 app.py:1517-1585 的 _extract_body）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python cookie_flow.py                 # 显示浏览器窗口，扫码登录\n"
            "  python cookie_flow.py --headless       # 无窗口，只看截图\n"
            "  python cookie_flow.py --timeout 120    # 缩短到 2 分钟\n"
        ),
    )
    ap.add_argument("--out", type=Path, default=here / "state.json",
                    help="storage_state 输出路径（默认 ./state.json）")
    ap.add_argument("--screenshot", type=Path, default=here / "extract_qr.png",
                    help="二维码截图路径（默认 ./extract_qr.png）")
    ap.add_argument("--headless", action="store_true",
                    help="无头模式（只靠截图看二维码）")
    ap.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_SECONDS,
                    help=f"等待扫码的秒数（默认 {LOGIN_TIMEOUT_SECONDS}）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    _log("=" * 66)
    _log("抖音扫码提取 Cookie（无 ticket 换取环节；登录由 Chromium 自行完成）")
    _log(f"目标站点      : {DOUYIN_URL}")
    _log(f"浏览器模式    : {'headless' if args.headless else '有窗口（可直接扫码）'}")
    _log(f"输出 state    : {args.out}")
    _log(f"二维码截图    : {args.screenshot}")
    _log(f"等待上限      : {args.timeout}s")
    _log("=" * 66)

    if not args.headless:
        _log("请在弹出的 Chromium 窗口里用抖音 App 扫码；本脚本每 10 秒刷新一次截图。")
    else:
        _log(f"无头模式：请打开 {args.screenshot} 查看二维码，每 10 秒刷新。")

    try:
        result = extract_cookie(
            out_path=args.out,
            screenshot_path=args.screenshot,
            headless=args.headless,
            timeout_seconds=args.timeout,
        )
    except ImportError as exc:
        _log(f"[FAIL] {exc}")
        return 3
    except RuntimeError as exc:
        _log(f"[FAIL] {exc}")
        _log("修复建议：")
        _log("  1) 确认二维码是否正常显示（看截图文件）")
        _log("  2) 若截图是空白/滑块页，说明触发风控，换网络或稍后再试")
        _log("  3) 扫码后需在手机端点击「确认登录」")
        _log(f"  4) 时间不够可加长：--timeout 600")
        return 2

    _log("-" * 66)
    _log("结果（已脱敏，不含任何 Cookie 值）：")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    _log("-" * 66)

    # 结构校验：不打印值，只报结构
    try:
        data = json.loads(args.out.read_text(encoding="utf-8"))
        ok = (
            isinstance(data, dict)
            and isinstance(data.get("cookies"), list)
            and bool(data["cookies"])
        )
        _log(f"[verify] {args.out.name} 结构合法={ok} "
             f"cookies={len(data.get('cookies', []))} "
             f"origins={len(data.get('origins', []))}")
        if not ok:
            _log("[verify] 结构不符合 Playwright storage_state 形状，请检查输出文件")
            return 1
    except Exception as exc:  # noqa: BLE001
        _log(f"[verify] 读取输出文件失败：{type(exc).__name__}: {exc}")
        return 1

    _log("完成。该文件可直接作为 DOUYIN_STORAGE_STATE 使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

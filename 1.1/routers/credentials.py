"""凭证与日志路由：登录态上传、扫码提取、日志查看。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import app_common
from fastapi import APIRouter, File, UploadFile
from fastapi import HTTPException
from fastapi.responses import FileResponse

from app_common import logger
from core.browser import open_browser
from core.config import (
    DATA_DIR,
    STATE_PATH,
    atomic_write_bytes,
    atomic_write_text,
)
from core.runtime import recent_logs

router = APIRouter()


def _state_write_path() -> Path:
    external = os.environ.get("STATE_FILE_PATH", "").strip()
    return Path(external) if external else STATE_PATH


@router.post("/api/upload-state")
@router.post("/api/credentials/upload")
async def api_upload_state(
    file: UploadFile = File(...),
) -> dict:
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    # 结构白名单校验：仅接受 Playwright storage_state 形状
    if not all(
        isinstance(c, dict) and c.get("name") and c.get("domain") and isinstance(c.get("path", ""), str)
        for c in data["cookies"]
    ):
        raise HTTPException(status_code=400, detail="cookies 字段结构不合法")
    if "origins" in data and not isinstance(data["origins"], list):
        raise HTTPException(status_code=400, detail="origins 字段必须是数组")
    # 写入前原子获取运行锁：读取/校验上传文件期间不阻塞任务，但绝不在发送过程中
    # 替换登录态。安全模式可把目标指向 /dev/shm/state.json。
    if not app_common.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="后台任务运行中，请结束后再替换登录态")
    try:
        target = _state_write_path()
        try:
            atomic_write_bytes(target, raw)
        except Exception:
            raise HTTPException(status_code=500, detail="无法写入登录态文件，请检查目录权限")
    finally:
        app_common.run_lock.release()
    logger.info("已更新登录态 state.json（%s 字节）", len(raw))
    return {"ok": True, "size": len(raw)}


@router.get("/api/logs")
def api_logs(n: int = 300) -> dict:
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


# ── 本地提取通行证（浏览器扫码登录） ──────────────────────
_extract_state = {"running": False, "status": "idle", "count": 0, "error": None, "screenshot": None}


def _extract_worker():
    """后台线程：启动浏览器，等待用户扫码登录，提取 state.json"""
    global _extract_state
    try:
        _extract_state["status"] = "waiting"
        _extract_state["error"] = None
        _extract_state["screenshot"] = None

        # 截图放在 data/（不通过公开 static 暴露），由认证 API 返回
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = DATA_DIR / "extract_qr.png"

        # 统一走 core.browser.open_browser：UA/时区/启动参数与发送链路一致，
        # 退出时同时关闭 browser 与 playwright 进程。use_state=False 用干净会话。
        with open_browser(headless=False, use_state=False) as (p, browser, context, page):
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

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
                storage = context.storage_state()
                atomic_write_text(
                    _state_write_path(),
                    json.dumps(storage, ensure_ascii=False, indent=2),
                )
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
        try:
            app_common.run_lock.release()
        except RuntimeError:
            pass


@router.post("/api/credentials/extract")
def api_credentials_extract() -> dict:
    """启动本地浏览器提取通行证（扫码登录）"""
    global _extract_state
    if _extract_state["running"]:
        raise HTTPException(status_code=409, detail="已有提取任务在运行")
    # 发送/同步/采集进行中不叠加第二个浏览器实例（防 OOM 拖垮正在进行的发送）
    if not app_common.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="发送/同步任务进行中，请稍后再提取通行证")
    app_common._mark_run_started()
    _extract_state = {"running": True, "status": "starting", "count": 0, "error": None, "screenshot": None}
    try:
        threading.Thread(target=_extract_worker, daemon=True).start()
    except Exception:
        app_common.run_lock.release()
        _extract_state["running"] = False
        raise
    return {"ok": True}


@router.get("/api/credentials/extract-status")
def api_credentials_extract_status() -> dict:
    """查询提取通行证状态"""
    return {
        "running": _extract_state["running"],
        "status": _extract_state["status"],
        "count": _extract_state["count"],
        "error": _extract_state["error"],
        "screenshot": _extract_state.get("screenshot"),
    }


@router.get("/api/credentials/extract-screenshot")
def api_credentials_extract_screenshot():
    """返回凭证提取时的实时截图（需认证，不放在公开 static 目录）"""
    screenshot_path = DATA_DIR / "extract_qr.png"
    if not screenshot_path.exists():
        raise HTTPException(status_code=404, detail="暂无截图")
    return FileResponse(str(screenshot_path), media_type="image/png")

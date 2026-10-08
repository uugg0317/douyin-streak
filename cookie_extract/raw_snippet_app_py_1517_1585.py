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

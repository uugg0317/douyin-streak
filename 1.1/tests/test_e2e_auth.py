"""隔离本机 HTTP 服务，验证页面/API 直接访问及跨站修改拦截。"""
from __future__ import annotations
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class DirectAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix="douyin-streak-direct-")
        sandbox = Path(cls.sandbox.name)
        (sandbox / "data").mkdir()
        (sandbox / "data/config.json").write_text(json.dumps({"auto_run_enabled": False, "schedule_harvest_day": "off"}), encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        env = os.environ.copy()
        for key in ("AUTH_TOKEN", "URL_ACCESS_TOKEN", "URL_PASSWORD_SALT", "URL_PASSWORD_HASH", "LOGIN_GATE", "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"):
            env.pop(key, None)
        env.update({"DATA_DIR": str(sandbox / "data"), "ENV_FILE_PATH": str(sandbox / "missing.env"), "INSTANCE_LOCK_PATH": str(sandbox / "server.pid"), "HOST": "127.0.0.1", "PORT": str(cls.port), "SESSION_WARN_EMAIL_ENABLED": "off", "PYTHONDONTWRITEBYTECODE": "1"})
        # 实际 app/lifespan + HTTP 中间件；隔离定时业务和看门狗，避免外部操作。
        boot = "import os, app, uvicorn; app.scheduler.configure = lambda *a, **k: None; app._start_watchdog = lambda: None; uvicorn.run(app.app, host='127.0.0.1', port=int(os.environ['PORT']), access_log=False)"
        cls.log = open(sandbox / "server.log", "w", encoding="utf-8")
        cls.process = subprocess.Popen([sys.executable, "-B", "-c", boot], cwd=ROOT, env=env, stdout=cls.log, stderr=cls.log)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if cls.process.poll() is not None:
                    raise RuntimeError("临时服务启动失败：" + (sandbox / "server.log").read_text(encoding="utf-8"))
                try:
                    with cls.request("GET", "/api/health") as response:
                        if json.load(response).get("ok"):
                            return
                except (OSError, urllib.error.URLError):
                    time.sleep(.1)
            raise RuntimeError("临时服务健康检查超时")
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def _cleanup(cls):
        process = getattr(cls, "process", None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if getattr(cls, "log", None):
            cls.log.close()
        if getattr(cls, "sandbox", None):
            cls.sandbox.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @classmethod
    def request(cls, method, path, body=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else None
        request_headers = dict(headers or {})
        if raw is not None:
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=raw, method=method, headers=request_headers)
        return urllib.request.urlopen(req, timeout=3)

    def test_page_and_read_apis_open_without_headers_or_cookies(self):
        with self.request("GET", "/") as response:
            html = response.read().decode()
            self.assertNotIn('class="login-gate"', html)
            self.assertNotIn('v-show="authed"', html)
            self.assertIn('扫码提取登录态', html)
            self.assertNotIn('set-cookie', response.headers)
        for path in ("/api/status", "/api/config", "/api/ledger", "/api/logs", "/api/email-config", "/api/browser/status", "/api/credentials/extract-status"):
            with self.subTest(path=path), self.request("GET", path) as response:
                self.assertEqual(response.status, 200)
                self.assertIsInstance(json.load(response), dict)
                self.assertNotIn('set-cookie', response.headers)

    def test_form_save_accepts_same_origin_without_login(self):
        with self.request("POST", "/api/config", {"auto_run_enabled": False}, {"Origin": f"http://127.0.0.1:{self.port}"}) as response:
            self.assertTrue(json.load(response)["ok"])

    def test_cross_site_requests_are_rejected_before_business_logic(self):
        for headers in ({"Origin": "https://external.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("POST", "/api/run", {"dry_run": False}, headers)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

    def test_management_login_endpoints_removed(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("POST", "/api/auth/login", {})
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_nonlocal_host_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("GET", "/api/config", headers={"Host": "external.example"})
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

if __name__ == "__main__":
    unittest.main()

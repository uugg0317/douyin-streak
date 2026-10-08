"""Local, in-memory UI fixture. Never imports the app or reads .env/account data.

Run: python tests/ui_fixture_server.py --port 8765
Dummy management token: ui-fixture-token

Only files under static/ can be served; all API responses and mutations are fake.
GET /__test/state exposes only the fake state and fake API call history.
POST /__test/control accepts reset, delays, failures, job_seconds and accounts.
Delay/failure keys are "METHOD /path" (or just "/path"). A failure may be an
HTTP status integer or {"status": 503, "count": 1, "detail": "mock failure"}.
Account overrides are keyed by ID with overview, ledger, config, logs, runtime.
Credential extraction is also fake. Control credentials/status, credentials_seconds
or credentials_ready_seconds to test ready, failure, cancellation and deadlines.
"""

from __future__ import annotations

import argparse
import copy
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote, urlsplit


STATIC_ROOT = (Path(__file__).resolve().parents[1] / "static").resolve()
TOKEN = "ui-fixture-token"
COOKIE = "spark_ui_fixture"
ALLOWED_STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2", ".ico", ".json"}


def initial_accounts() -> dict:
    accounts = {}
    for aid, name, total, selected in [("alpha", "模拟主账号", 6, 4), ("beta", "模拟朋友账号", 4, 2)]:
        ledger = [{"user_id": f"fake-{aid}-{i + 1}", "display_name": f"{name}好友{i + 1}", "nickname": f"{name}好友{i + 1}",
                   "streak_days": 9 + i * 7, "selected": i < selected,
                   "last_status": "unknown" if aid == "beta" and i == 0 else "success" if i < selected else "pending",
                   "last_sent_at": "2026-10-02T00:00:00+08:00" if i < selected else None,
                   "last_msg": "[续火花吧]" if i < selected else ""} for i in range(total)]
        accounts[aid] = {
            "overview": {"id": aid, "display_name": name, "enabled": True, "note": "虚构 UI 验证账号",
                         "has_state": aid == "alpha", "running": False, "session_status": "ok",
                         "manual_required": aid == "beta", "manual_reason": "模拟待确认结果" if aid == "beta" else None,
                         "consecutive_failures": 0, "auto_paused_until": 0,
                         "last_run": {"at": "2026-10-02T00:00:00+08:00", "dry_run": True,
                                      "ok": selected - (aid == "beta"), "failed": 0, "unknown": int(aid == "beta"),
                                      "skipped": total - selected, "logged_out": False, "rate_limited": False}},
            "ledger": ledger,
            "config": {"schedule_time": "00:00", "jitter_minutes": 0, "send_gap_min": 2 if aid == "alpha" else 3,
                       "send_gap_max": 3 if aid == "alpha" else 4, "max_friends_per_run": 20 if aid == "alpha" else 70,
                       "friends": [], "messages": ["[续火花吧]"], "creator_user_detail_path": "aweme/v1/creator/im/user_detail/",
                       "creator_max_scrolls": 80, "auto_run_enabled": False, "allow_first_message": False,
                       "first_message_daily_limit": 1, "schedule_harvest_day": ""},
            "logs": f"[模拟] {name}：此页面只操作虚构数据。\n[模拟] 台账加载完成，定时任务已关闭。",
            "runtime": {"running": False, "contacts_at": "2026-10-02T09:00:00+08:00", "contacts_error": None},
            "credentials_revision": 0,
        }
    return accounts


class FixtureStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.accounts = initial_accounts()
            self.calls = []
            self.sessions = set()
            self.delays = {}
            self.failures = {}
            self.jobs = []
            self.job_seconds = 4.0
            self.backups = {"slot_count": 7, "slots": [{"slot": i + 1, "created_at": "2026-10-01T23:00:00+08:00" if i == 0 else None,
                                                        "files": 12 if i == 0 else 0, "size": 2048 if i == 0 else 0} for i in range(7)], "next_backup": None}
            self.job_counter = 0
            self.credentials = None
            self.credentials_seconds = 300.0
            self.credentials_ready_seconds = 2.0

    def credentials_active(self):
        return bool(self.credentials and self.credentials["status"] in {"starting", "waiting", "ready", "saving", "cancelling", "stopping"})

    def public_credentials(self):
        self.refresh_credentials()
        if not self.credentials:
            return {"job_id": None, "account_id": None, "display_name": "", "status": "idle",
                    "running": False, "count": 0, "error": None, "started_at": None,
                    "deadline": None, "remaining_seconds": 0, "ready": False}
        public = {key: copy.deepcopy(self.credentials.get(key)) for key in
                  ("job_id", "account_id", "display_name", "status", "running", "count", "error", "started_at", "deadline")}
        public["remaining_seconds"] = max(0, int((self.credentials["deadline"] - time.time()) + 0.999))
        public["ready"] = self.credentials["status"] == "ready"
        return public

    def finish_credentials(self, status, error=None):
        if not self.credentials:
            return
        self.credentials.update(status=status, running=False, error=error)
        self.jobs = [job for job in self.jobs if job["job_id"] != self.credentials["job_id"]]

    def refresh_credentials(self):
        if not self.credentials_active():
            return
        now = time.monotonic()
        if now >= self.credentials["_expires"]:
            self.finish_credentials("timeout", "模拟提取任务已到五分钟期限，原凭据保持不变")
        elif self.credentials["status"] in {"starting", "waiting"} and now >= self.credentials["_ready_at"]:
            self.credentials.update(status="ready", count=12)

    def refresh_jobs(self):
        self.refresh_credentials()
        now = time.monotonic()
        for job in list(self.jobs):
            if job["deadline"] > now:
                continue
            account = self.accounts.get(job.get("account_id"))
            if account and job["kind"] == "contacts":
                account["runtime"]["contacts_at"] = "2026-10-02T10:00:00+08:00"
            self.jobs.remove(job)
        for aid, account in self.accounts.items():
            account["overview"]["running"] = any(j["kind"] == "send" and (not j["account_id"] or j["account_id"] == aid) for j in self.jobs)
            account["runtime"]["running"] = account["overview"]["running"]

    def overview(self, aid):
        account = self.accounts[aid]
        overview = copy.deepcopy(account["overview"])
        overview.update(ledger_total=len(account["ledger"]), ledger_selected=sum(bool(x.get("selected")) for x in account["ledger"]))
        return overview

    def public_jobs(self):
        return [{k: v for k, v in j.items() if k != "deadline"} for j in self.jobs]

    def state(self):
        self.refresh_jobs()
        return {"multi": True, "max_accounts": 10, "accounts": [self.overview(aid) for aid in self.accounts],
                "jobs": self.public_jobs(), "next_run": None, "next_backup": None,
                "auto_run_enabled": False, "backup_enabled": False, "schedule_time": "00:00",
                "state": {"running": any(j["kind"] == "send" for j in self.jobs),
                          "current_account": next((j["account_id"] for j in self.jobs if j["kind"] == "send"), None),
                          "last_summary": {"at": "2026-10-02T00:00:00+08:00", "dry_run": True,
                                           "totals": {"ok": 5, "failed": 0, "unknown": 1, "skipped": 4}, "accounts": []}}}


STORE = FixtureStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkLocalFixture/1.0"

    def log_message(self, *_):
        pass

    def response(self, data, status=200, cookie=None, content_type="application/json; charset=utf-8"):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def error(self, status, detail):
        self.response({"detail": detail}, status)

    def is_local(self):
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 <= length <= 6 * 1024 * 1024:
            return None
        raw = self.rfile.read(length)
        if self.headers.get("Content-Type", "").startswith("multipart/"):
            # Deliberately discard uploaded bytes: never store cookie file content.
            return {"mock_upload": True}
        try:
            value = json.loads(raw or b"{}")
            return value if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            return None

    def authenticated(self):
        return True

    def static_file(self, path):
        if path in {"/", "/favicon.ico"}:
            path = "/static/multi.html" if path == "/" else "/static/favicon.svg"
        if not path.startswith("/static/"):
            return self.error(404, "Fixture serves only static files and fake API endpoints")
        relative = path[len("/static/"):]
        if "\\" in relative or any(part in {".", ".."} or part.startswith(".") for part in relative.split("/")):
            return self.error(404, "Invalid static path")
        target = (STATIC_ROOT / relative).resolve()
        try:
            target.relative_to(STATIC_ROOT)
        except ValueError:
            return self.error(404, "Invalid static path")
        if not target.is_file() or target.suffix.lower() not in ALLOWED_STATIC_SUFFIXES:
            return self.error(404, "Static file not found")
        self.response(target.read_bytes(), content_type=mimetypes.guess_type(str(target))[0] or "application/octet-stream")

    def handle_request(self):
        if not self.is_local():
            return self.error(403, "Localhost only")
        path = unquote(urlsplit(self.path).path)
        if self.command in {"GET", "HEAD"} and not path.startswith(("/api/", "/__test/")):
            return self.static_file(path)
        data = self.body() if self.command not in {"GET", "HEAD"} else {}
        if data is None:
            return self.error(400, "Invalid fixture body")
        if path == "/__test/state" and self.command == "GET":
            with STORE.lock:
                return self.response({"fixture": True, "state": STORE.state(), "accounts": copy.deepcopy(STORE.accounts),
                                      "calls": copy.deepcopy(STORE.calls), "delays": STORE.delays, "failures": STORE.failures,
                                      "credentials": STORE.public_credentials()})
        if path == "/__test/control" and self.command == "POST":
            with STORE.lock:
                if data.get("reset"):
                    STORE.reset()
                for key in ("delays", "failures"):
                    if isinstance(data.get(key), dict):
                        setattr(STORE, key, copy.deepcopy(data[key]))
                if "job_seconds" in data:
                    STORE.job_seconds = max(0, min(120, float(data["job_seconds"])))
                if "credentials_seconds" in data:
                    STORE.credentials_seconds = max(0, min(300, float(data["credentials_seconds"])))
                if "credentials_ready_seconds" in data:
                    STORE.credentials_ready_seconds = max(0, min(300, float(data["credentials_ready_seconds"])))
                if STORE.credentials and isinstance(data.get("credentials"), dict):
                    fields = data["credentials"]
                    for key in ("status", "count", "error"):
                        if key in fields:
                            STORE.credentials[key] = copy.deepcopy(fields[key])
                    if "deadline" in fields:
                        STORE.credentials["deadline"] = float(fields["deadline"])
                        STORE.credentials["_expires"] = time.monotonic() + max(0, float(fields["deadline"]) - time.time())
                    if STORE.credentials["status"] == "ready" and "count" not in fields:
                        STORE.credentials["count"] = 12
                    STORE.credentials["running"] = STORE.credentials_active()
                    if not STORE.credentials_active():
                        STORE.finish_credentials(STORE.credentials["status"], STORE.credentials.get("error"))
                for aid, fields in data.get("accounts", {}).items():
                    if aid not in STORE.accounts or not isinstance(fields, dict):
                        continue
                    for key in ("overview", "ledger", "config", "logs", "runtime"):
                        if key in fields:
                            if key in {"overview", "config", "runtime"} and isinstance(fields[key], dict):
                                STORE.accounts[aid][key].update(fields[key])
                            elif key == "ledger" and isinstance(fields[key], list) or key == "logs" and isinstance(fields[key], str):
                                STORE.accounts[aid][key] = copy.deepcopy(fields[key])
                return self.response({"ok": True, "fixture": True})
        if path == "/api/health":
            return self.response({"ok": True, "fixture": True})
        key = f"{self.command} {path}"
        with STORE.lock:
            # Login and uploads are intentionally redacted even in this fake log.
            log_data = {"redacted": True} if path.endswith(("/login", "/state")) else copy.deepcopy(data)
            STORE.calls.append({"method": self.command, "path": path, "body": log_data})
            delay = STORE.delays.get(key, STORE.delays.get(path, 0))
            failure_key = key if key in STORE.failures else path
            failure = STORE.failures.get(failure_key)
            if isinstance(failure, dict) and "count" in failure:
                if failure["count"] <= 0:
                    failure = None
                else:
                    failure = copy.deepcopy(failure)
                    STORE.failures[failure_key]["count"] -= 1
        if delay:
            time.sleep(max(0, min(10, float(delay))))
        if failure:
            return self.error(int(failure.get("status", 503) if isinstance(failure, dict) else failure),
                              failure.get("detail", "模拟请求失败") if isinstance(failure, dict) else "模拟请求失败")
        with STORE.lock:
            return self.fake_api(path, data)

    def fake_api(self, path, data):
        method = self.command
        if path == "/api/auth/login" and method == "POST":
            if data.get("token") != TOKEN:
                return self.error(401, "管理令牌不正确（模拟）")
            STORE.sessions.add("dummy-session")
            return self.response({"ok": True}, cookie=f"{COOKIE}=dummy-session; Path=/; HttpOnly; SameSite=Lax")
        if path == "/api/auth/logout" and method == "POST":
            STORE.sessions.clear()
            return self.response({"ok": True}, cookie=f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        if not self.authenticated():
            return self.error(401, "请登录模拟控制台")
        STORE.refresh_jobs()
        if path == "/api/multi/state" and method == "GET":
            return self.response(STORE.state())
        if path == "/api/multi/credentials/extract-status" and method == "GET":
            return self.response(STORE.public_credentials())
        extraction_action = re.fullmatch(r"/api/multi/accounts/[A-Za-z0-9_-]+/credentials/extract/[A-Za-z0-9_-]+/(confirm|cancel)", path)
        if method not in {"GET", "HEAD"} and STORE.credentials_active() and not extraction_action:
            return self.error(409, "模拟登录提取占用全局任务，请先确认或取消")
        if path == "/api/multi/accounts":
            if method == "GET":
                return self.response({"accounts": STORE.state()["accounts"], "max_accounts": 10})
            if method == "POST":
                aid = data.get("id") or f"fixture{len(STORE.accounts) + 1}"
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", aid) or aid in STORE.accounts:
                    return self.error(400, "模拟账号 ID 无效或已存在")
                template = copy.deepcopy(initial_accounts()["alpha"])
                template["overview"].update(id=aid, display_name=data.get("display_name") or "新建模拟账号", has_state=False, last_run=None)
                template["ledger"] = []
                STORE.accounts[aid] = template
                return self.response({"ok": True, "account": STORE.overview(aid)}, 201)
        if path == "/api/multi/backups" and method == "GET":
            return self.response(STORE.backups)
        if path == "/api/multi/backups/run" and method == "POST":
            STORE.backups["slots"][1].update(created_at="2026-10-02T10:00:00+08:00", files=12, size=2048)
            return self.response({"ok": True, "slot": 2, "files": 12}, 201)
        if path == "/api/multi/reset" and method == "POST":
            if any(j["kind"] == "send" for j in STORE.jobs):
                return self.error(409, "模拟发送任务仍在运行")
            return self.response({"ok": True, "reset": False})
        if path == "/api/multi/run" and method == "POST":
            aid = data.get("account_id") or None
            if aid and aid not in STORE.accounts:
                return self.error(404, "模拟账号不存在")
            return self.start_job(aid, "send", data)
        match = re.fullmatch(r"/api/multi/accounts/([A-Za-z0-9_-]+)/?(.*)", path)
        if not match:
            return self.error(404, "Unknown fake endpoint")
        aid, resource = match.groups()
        account = STORE.accounts.get(aid)
        if account is None:
            return self.error(404, "模拟账号不存在")
        if resource == "credentials/extract" and method == "POST":
            if STORE.credentials_active():
                return self.error(409, "已有模拟登录提取任务，请先确认或取消")
            if STORE.jobs:
                return self.error(409, "已有模拟任务运行，请等待后再提取登录")
            STORE.job_counter += 1
            job_id = f"fixture-extract-{STORE.job_counter}"
            STORE.credentials = {"job_id": job_id, "account_id": aid,
                                 "display_name": account["overview"]["display_name"],
                                 "status": "waiting", "running": True, "count": 0, "error": None,
                                 "started_at": time.time(),
                                 "deadline": time.time() + STORE.credentials_seconds,
                                 "_expires": time.monotonic() + STORE.credentials_seconds,
                                 "_ready_at": time.monotonic() + STORE.credentials_ready_seconds}
            STORE.jobs.append({"job_id": job_id, "account_id": aid, "kind": "credential_extract",
                               "global_scope": True, "deadline": STORE.credentials["_expires"]})
            return self.response({"ok": True, "started": True, **STORE.public_credentials()}, 202)
        action = re.fullmatch(r"credentials/extract/([A-Za-z0-9_-]+)/(confirm|cancel)", resource)
        if action and method == "POST":
            job_id, command = action.groups()
            extraction = STORE.credentials
            if not extraction or extraction["job_id"] != job_id or extraction["account_id"] != aid:
                return self.error(404, "模拟任务与账号不匹配，请重新核对")
            if not STORE.credentials_active():
                return self.error(409, "模拟提取任务已结束或到期")
            if command == "confirm":
                if extraction["status"] != "ready":
                    return self.error(400, "模拟登录尚未完成")
                account["overview"]["has_state"] = True
                account["credentials_revision"] += 1
                STORE.finish_credentials("success")
            else:
                STORE.finish_credentials("cancelled")
            return self.response({"ok": True, **STORE.public_credentials()})
        if not resource and method in {"PATCH", "POST"}:
            for field in ("display_name", "enabled", "note"):
                if field in data:
                    account["overview"][field] = data[field]
            return self.response({"ok": True, "account": STORE.overview(aid)})
        if resource == "remove" and method == "POST":
            del STORE.accounts[aid]
            return self.response({"ok": True, "removed": aid, "delete_dir": False})
        if resource == "ledger" and method == "GET":
            return self.response({"entries": account["ledger"], "selected_count": sum(bool(r.get("selected")) for r in account["ledger"])})
        if resource == "ledger/selection" and method == "POST":
            names = data.get("selected_names", [])
            if not names:
                return self.error(400, "至少保留一个模拟好友")
            for row in account["ledger"]:
                row["selected"] = row["display_name"] in names
            return self.response({"ok": True, "selected_count": sum(r["selected"] for r in account["ledger"])})
        if resource == "config":
            if method in {"PUT", "POST"}:
                account["config"].update(data.get("config", data))
            if method in {"GET", "PUT", "POST"}:
                return self.response({"ok": True, "config": account["config"]})
        if resource == "logs" and method == "GET":
            return self.response({"logs": account["logs"]})
        if resource == "runtime" and method == "GET":
            return self.response({"runtime": account["runtime"]})
        if resource == "contacts/status" and method == "GET":
            job = next((j for j in STORE.jobs if j["kind"] == "contacts" and j["account_id"] == aid), None)
            return self.response({"fetching": bool(job), "job_id": job["job_id"] if job else None,
                                  "contacts_at": account["runtime"]["contacts_at"], "contacts_error": account["runtime"]["contacts_error"]})
        if resource == "contacts/fetch" and method == "POST":
            return self.start_job(aid, "contacts", data)
        if resource == "review" and method == "POST":
            if data.get("confirmed") is not True:
                return self.error(400, "必须确认人工核对（模拟）")
            account["overview"].update(manual_required=False, manual_reason=None, consecutive_failures=0, auto_paused_until=0)
            return self.response({"ok": True, "manual_required": False, "message": "模拟任务已恢复"})
        if resource == "state" and method == "POST":
            account["overview"]["has_state"] = True
            return self.response({"ok": True, "id": aid, "size": 0})
        if resource == "copy-from" and method == "POST":
            source = STORE.accounts.get(data.get("source_id"))
            if not source or data.get("source_id") == aid:
                return self.error(400, "无效模拟源账号")
            account["config"] = copy.deepcopy(source["config"])
            copied = ["config"]
            if data.get("copy_ledger"):
                account["ledger"] = copy.deepcopy(source["ledger"])
                for row in account["ledger"]:
                    row["last_status"] = "pending"
                copied.append("ledger")
            return self.response({"ok": True, "copied": copied})
        return self.error(404, "Unknown fake endpoint")

    def start_job(self, aid, kind, data):
        if any(j.get("global_scope") or j["account_id"] is None or aid is None or j["account_id"] == aid for j in STORE.jobs):
            return self.error(409, "模拟账号已有任务")
        STORE.job_counter += 1
        job_id = f"fixture-job-{STORE.job_counter}"
        STORE.jobs.append({"job_id": job_id, "account_id": aid, "kind": kind, "global_scope": aid is None,
                           "dry_run": bool(data.get("dry_run", False)), "deadline": time.monotonic() + STORE.job_seconds})
        return self.response({"ok": True, "started": True, "id": aid, "account_id": aid, "job_id": job_id,
                              "dry_run": bool(data.get("dry_run", False)), "fixture": True}, 202)

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Fake UI fixture ready on http://127.0.0.1:{server.server_port}/ (dummy token: {TOKEN})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""Offline regression tests: temporary files, fake worker and fake notifier only."""

import contextlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import accounts as A, jobs, multi_service as service, orchestrator as O, runtime as R, scheduler as S, worker as W


class ReservationTests(unittest.TestCase):
    def test_same_account_all_operations_share_exclusion(self):
        manager = jobs.JobManager()
        first = manager.reserve(account_id="alpha", kind="contacts")
        for kind in ("send", "state", "contacts", "review"):
            with self.assertRaises(jobs.JobBusy):
                manager.reserve(account_id="alpha", kind=kind)
        other = manager.reserve(account_id="bravo", kind="state")
        self.assertTrue(manager.release(first["job_id"]))
        self.assertTrue(manager.release(other["job_id"]))

    def test_global_reservation_is_atomic_under_race(self):
        manager = jobs.JobManager()
        barrier = threading.Barrier(3)
        outcomes = []
        def reserve(global_scope):
            barrier.wait()
            try:
                token = manager.reserve(account_id=None if global_scope else "alpha",
                                        kind="send" if global_scope else "contacts",
                                        global_scope=global_scope)
                outcomes.append(token)
            except jobs.JobBusy:
                outcomes.append("busy")
        threads = [threading.Thread(target=reserve, args=(scope,)) for scope in (False, True)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes.count("busy"), 1)
        self.assertEqual(len(manager.snapshot()), 1)

    def test_send_lane_prevents_late_orchestrator_rejection(self):
        manager = jobs.JobManager()
        manager.reserve(account_id="alpha", kind="send")
        with self.assertRaises(jobs.JobBusy):
            manager.reserve(account_id="bravo", kind="send")
        manager.reserve(account_id="bravo", kind="contacts")

    def test_release_from_another_thread_and_stale_release_are_safe(self):
        manager = jobs.JobManager()
        first = manager.reserve(account_id="alpha", kind="send")
        thread = threading.Thread(target=manager.release, args=(first["job_id"],))
        thread.start()
        thread.join(2)
        second = manager.reserve(account_id="alpha", kind="state")
        self.assertFalse(manager.release(first["job_id"]))
        self.assertTrue(manager.is_active(second["job_id"]))

    def test_claim_cannot_execute_the_same_token_twice(self):
        manager = jobs.JobManager()
        token = manager.reserve(account_id="alpha", kind="send")
        manager.claim(token, account_id="alpha", kind="send")
        with self.assertRaises(jobs.JobBusy):
            manager.claim(token, account_id="alpha", kind="send")
        self.assertTrue(manager.is_active(token["job_id"]))


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("core.config.DATA_DIR", self.tmp))
        self.stack.enter_context(patch.object(A, "ACCOUNTS_ROOT", self.tmp / "accounts"))
        self.stack.enter_context(patch.object(A, "REGISTRY_PATH", self.tmp / "accounts.json"))
        self.stack.enter_context(patch.object(service, "STATE_PATH", self.tmp / "orchestrator_state.json"))
        self.stack.enter_context(patch.object(R, "DATA_DIR", self.tmp / "single"))
        self.stack.enter_context(patch.object(R, "RUNTIME_PATH", self.tmp / "single" / "runtime.json"))
        self.stack.enter_context(patch.object(R, "_rt_cache", {"mtime": None, "data": None}))
        self.stack.enter_context(patch.object(R, "load_config", return_value={
            "failure_breaker_threshold": 2, "failure_breaker_cooldown": 120,
        }))
        for active in jobs.snapshot():
            jobs.release(active["job_id"])
        self.addCleanup(lambda: [jobs.release(j["job_id"]) for j in jobs.snapshot()])
        for account_id in ("alpha", "bravo"):
            A.add_account(account_id)
            # Synthetic cookie is only parsed locally; no browser or account is used.
            A.account_file(account_id, A.STATE_NAME).write_text(
                json.dumps({"cookies": [{"name": "offline", "value": "fake"}]}), encoding="utf-8")
            A.account_file(account_id, A.CONFIG_NAME).write_text(
                json.dumps({"failure_breaker_threshold": 2, "failure_breaker_cooldown": 120}), encoding="utf-8")

    def envelope(self, **changes):
        value = {"status": "ok", "total": 1, "ok_count": 1,
                 "failed_count": 0, "skipped_count": 0, "result": {"ok": ["friend"]}}
        value.update(changes)
        return value

    def runner(self, **changes):
        envelope = self.envelope(**changes)
        return lambda *args: (0, O.RESULT_PREFIX + json.dumps(envelope), "", False, 0.01)

    def state(self, account_id="alpha"):
        path = A.account_file(account_id, A.RUNTIME_NAME)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save_state(self, state, account_id="alpha"):
        A.account_file(account_id, A.RUNTIME_NAME).write_text(json.dumps(state), encoding="utf-8")

    def run_one(self, **changes):
        return O.run_all(only_account="alpha", runner=self.runner(**changes))

    def test_sync_reservation_prevents_collection_before_background_start(self):
        token = service.reserve_run("alpha")
        with self.assertRaises(jobs.JobBusy):
            jobs.reserve(account_id="alpha", kind="contacts")
        with patch.object(O, "_default_runner", self.runner()):
            result = service.manual_run("alpha", reservation=token, dry_run=True)
        self.assertTrue(result["started"])

    def test_service_reserved_execution_releases_and_reports_job_id(self):
        token = service.reserve_run("alpha")
        with self.assertRaises(jobs.JobBusy):
            jobs.reserve(account_id="alpha", kind="state")
        result = service.run_once(account_id="alpha", reservation=token, runner=self.runner(),
                                  notifier_func=lambda summary: {"action": "offline"})
        self.assertEqual(result["job_id"], token["job_id"])
        self.assertEqual(result["summary"]["job_id"], token["job_id"])
        self.assertFalse(jobs.is_active(token["job_id"]))

    def test_busy_service_does_not_report_started(self):
        token = jobs.reserve(account_id="alpha", kind="contacts")
        result = service.run_once(account_id="alpha", runner=self.runner(), dry_run=True)
        self.assertEqual(result, {"started": False, "busy": True})
        self.assertTrue(jobs.is_active(token["job_id"]))

    def test_reset_keeps_live_reservation_and_only_clears_stale_state(self):
        token = service.reserve_run("alpha")
        self.assertFalse(service.reset_running())
        self.assertTrue(service.get_state()["running"])
        jobs.release(token["job_id"])
        service._write_state({"running": True})
        self.assertTrue(service.reset_running())
        self.assertFalse(service.get_state()["running"])

    def test_consecutive_failures_create_account_cooldown(self):
        for _ in range(2):
            summary = self.run_one(ok_count=0, failed_count=1,
                                   result={"failed": [{"name": "friend", "reason": "offline_failure"}]})
            self.assertEqual(summary["accounts"][0]["status"], "failed")
        state = self.state()
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertGreater(state["auto_paused_until"], time.time())
        fake = Mock(side_effect=AssertionError("cooldown must not launch worker"))
        summary = O.run_all(only_account="alpha", force=True, runner=fake)
        self.assertEqual(summary["accounts"][0]["status"], "breaker_skipped")
        fake.assert_not_called()
        self.assertEqual(self.state("bravo"), {})

    def test_expired_cooldown_clears_counter_and_runs_once(self):
        self.save_state({"consecutive_failures": 2, "auto_paused_until": time.time() - 1})
        self.run_one()
        self.assertEqual(self.state()["consecutive_failures"], 0)
        self.assertEqual(self.state()["auto_paused_until"], 0)

    def test_unknown_is_visible_and_requires_review_even_for_manual_force(self):
        self.save_state({"consecutive_failures": 1})
        summary = self.run_one(ok_count=0, result={"unknown": [{"name": "friend", "reason": "unconfirmed"}]})
        self.assertEqual(summary["totals"]["unknown"], 1)
        self.assertEqual(summary["accounts"][0]["unknown_count"], 1)
        self.assertEqual(summary["accounts"][0]["status"], "unknown")
        self.assertEqual(self.state()["consecutive_failures"], 1)
        fake = Mock(side_effect=AssertionError("manual cannot bypass unknown"))
        summary = O.run_all(only_account="alpha", force=True, dry_run=True, runner=fake)
        self.assertEqual(summary["accounts"][0]["status"], "manual_required")
        fake.assert_not_called()

    def test_security_verification_is_blocked_until_explicit_review(self):
        for field in ("security_verification", "safety_verification"):
            self.save_state({})
            summary = self.run_one(result={field: True})
            self.assertEqual(summary["accounts"][0]["status"], "rate_limited")
            self.assertTrue(self.state()["manual_required"])

    def test_review_preserves_unknown_ledger_and_acknowledges_previous_run(self):
        ledger = A.account_file("alpha", A.LEDGER_NAME)
        ledger.write_text('[{"last_status":"unknown"}]', encoding="utf-8")
        before = ledger.read_bytes()
        self.save_state({"manual_required": True, "consecutive_failures": 2,
                         "auto_paused_until": time.time() + 120,
                         "last_run": {"unknown": [{"name": "friend"}]}})
        reviewed = O.review_account("alpha")
        self.assertFalse(reviewed["manual_required"])
        self.assertTrue(self.state()["manual_reviewed"])
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(self.run_one()["accounts"][0]["status"], "ok")

    def test_worker_runtime_record_is_not_counted_twice(self):
        def runner(*args):
            self.save_state({"consecutive_failures": 1, "last_run": {"at": "offline-run"}})
            envelope = self.envelope(at="offline-run", ok_count=0, failed_count=1, result={"failed": ["friend"]})
            return 0, O.RESULT_PREFIX + json.dumps(envelope), "", False, 0.01
        O.run_all(only_account="alpha", runner=runner)
        self.assertEqual(self.state()["consecutive_failures"], 1)

    def test_worker_error_marks_account_for_review_and_releases_job(self):
        summary = O.run_all(only_account="alpha", runner=Mock(side_effect=RuntimeError("offline_crash")))
        self.assertEqual(summary["accounts"][0]["status"], "executor_error")
        self.assertTrue(self.state()["manual_required"])
        self.assertFalse(jobs.is_active(kind="send"))

    def test_reserved_contact_worker_releases_without_real_subprocess(self):
        token = jobs.reserve(account_id="alpha", kind="contacts")
        with patch.object(O, "run_subprocess", return_value=(0, "", "", False, 0.01)) as runner:
            result = O.run_worker("alpha", mode="fetch-contacts", reservation=token)
        self.assertEqual(result["job_id"], token["job_id"])
        runner.assert_called_once()
        self.assertFalse(jobs.is_active(token["job_id"]))

    def test_runtime_unknown_keeps_previous_failures_and_manual_flag(self):
        R.update_runtime(consecutive_failures=1)
        R.record_run({"at": "offline", "unknown": [{"name": "friend"}], "ok": []})
        state = R.load_runtime()
        self.assertTrue(state["manual_required"])
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertEqual(state["history"][0]["unknown_count"], 1)
        R.record_run({"at": "offline2", "ok": ["friend"]})
        self.assertTrue(R.load_runtime()["manual_required"])
        self.assertEqual(R.load_runtime()["session_status"], "manual_required")

    def test_runtime_failure_cooldown_and_dry_run_isolation(self):
        for _ in range(2):
            R.record_run({"failed": ["friend"], "ok": []})
        state = R.load_runtime()
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertGreater(state["auto_paused_until"], time.time())
        R.record_run({"dry_run": True, "ok": ["friend"]})
        self.assertEqual(R.load_runtime()["consecutive_failures"], 2)

    def test_scheduler_manual_required_never_calls_sender(self):
        callback = Mock()
        with patch.object(S, "load_config", return_value={"auto_run_enabled": True}), \
                patch.object(S, "load_runtime", return_value={"manual_required": True}), \
                patch.object(S, "_run_func", callback):
            S._daily_job()
        callback.assert_not_called()

    def test_frozen_worker_uses_executable_worker_entry(self):
        with patch.object(O.sys, "frozen", True, create=True):
            argv = O.build_worker_cmd(self.tmp / "alpha", True, None, 0)
        self.assertEqual(argv[:2], [O.sys.executable, "--worker"])
        self.assertNotIn("-m", argv)
        argv = O.build_worker_cmd(self.tmp / "alpha", False, None, 0)
        self.assertEqual(argv[1:3], ["-m", "core.worker"])

    def test_nonzero_worker_result_cannot_report_success(self):
        status, _ = O.classify(1, self.envelope(), False)
        self.assertEqual(status, "executor_error")

    def spawn_guard_holder(self):
        guard = A.account_dir("alpha") / ".worker.guard"
        script = ("import sys\nfrom pathlib import Path\nfrom core.storage import file_lock\n"
                  "with file_lock(Path(sys.argv[1])):\n"
                  " print('ready', flush=True)\n sys.stdin.buffer.read(1)\n")
        proc = subprocess.Popen([sys.executable, "-c", script, str(guard)], cwd=O.BASE_DIR,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def stop():
            if proc.poll() is None:
                proc.stdin.write(b"x")
                proc.stdin.flush()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
        self.addCleanup(stop)
        ready = threading.Event()
        lines = []
        def read():
            lines.append(proc.stdout.readline())
            ready.set()
        threading.Thread(target=read, daemon=True).start()
        self.assertTrue(ready.wait(5), "offline lock helper did not become ready")
        self.assertEqual(lines, [b"ready\r\n"] if sys.platform == "win32" else [b"ready\n"])
        return proc

    def test_surviving_worker_blocks_account_and_global_reservations_then_death_releases(self):
        proc = self.spawn_guard_holder()
        for kind in ("send", "contacts", "state", "review"):
            with self.assertRaises(jobs.JobBusy):
                jobs.reserve(account_id="alpha", kind=kind)
            self.assertFalse(jobs.snapshot())
        with self.assertRaises(jobs.JobBusy):
            jobs.reserve(account_id=None, kind="backup", global_scope=True)
        proc.terminate()
        proc.wait(timeout=5)
        token = jobs.reserve(account_id="alpha", kind="state")
        self.assertTrue(jobs.release(token["job_id"]))

    def test_worker_busy_guard_never_imports_browser_business(self):
        self.spawn_guard_holder()
        with patch.object(W, "_execute", side_effect=AssertionError("must not execute")) as execute, \
                patch.object(W, "_emit") as emit:
            result = W.main(["--data-dir", str(A.account_dir("alpha"))])
        self.assertEqual(result, 4)
        self.assertEqual(emit.call_args.args[0]["status"], "busy")
        execute.assert_not_called()

    def test_worker_does_not_misclassify_body_timeout_as_busy(self):
        with patch.object(W, "_execute", side_effect=TimeoutError("storage transaction")), \
                patch.object(W, "_emit") as emit:
            with self.assertRaises(TimeoutError):
                W.main(["--data-dir", str(A.account_dir("alpha"))])
        emit.assert_not_called()
        token = jobs.reserve(account_id="alpha", kind="state")
        jobs.release(token["job_id"])

    def test_worker_env_removes_single_account_credential_override(self):
        with patch.dict(O.os.environ, {"STATE_FILE_PATH": "offline-other-account.json", "DATA_DIR": "offline"}):
            env = O.build_worker_env()
        self.assertNotIn("STATE_FILE_PATH", env)
        self.assertNotIn("DATA_DIR", env)
        self.assertEqual(env["SPARKKEEPER_NO_ROOT_STATE_FALLBACK"], "1")

    def test_backup_excludes_collection_and_mutation_for_entire_copy(self):
        def copy(*, reason):
            for kind in ("contacts", "state", "send"):
                with self.assertRaises(jobs.JobBusy):
                    jobs.reserve(account_id="alpha", kind=kind)
            return {"ok": True}
        with patch.object(service.backup_mod, "create_backup", side_effect=copy):
            self.assertTrue(service.manual_backup()["ok"])
        self.assertFalse(jobs.snapshot())

    def test_backup_refuses_active_collector_and_scheduled_backup_defers(self):
        jobs.reserve(account_id="alpha", kind="contacts")
        with patch.object(service.backup_mod, "create_backup") as copy, \
                patch.object(service.backup_mod, "mark_skip"), \
                patch.object(service.scheduler_mod, "schedule_backup_retry") as retry:
            self.assertTrue(service.manual_backup()["busy"])
            self.assertTrue(service.scheduled_backup()["deferred"])
        copy.assert_not_called()
        retry.assert_called_once()

    def test_unknown_worker_with_manual_flag_keeps_unknown_classification(self):
        value = self.envelope(manual_required=True, unknown_count=1,
                              result={"unknown": [{"name": "friend", "reason": "unconfirmed"}]})
        self.assertEqual(O.classify(0, value, False)[0], "unknown")


if __name__ == "__main__":
    unittest.main()

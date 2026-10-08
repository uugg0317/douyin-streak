"""Local temporary-data extraction tests: no real browser, network or account."""
import contextlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import accounts as A, config, jobs, orchestrator as O
from core import credential_extract as C, credential_worker as W
from core.storage import atomic_write_text, file_lock


def fake_state(**cookie_changes):
    cookie = {"name": "sessionid", "value": "offline-test-value", "domain": ".douyin.com",
              "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}
    cookie.update(cookie_changes)
    return {"cookies": [cookie], "origins": []}


class FakeProcess:
    pid = 987654
    def __init__(self, task_dir, *, hanging=False):
        self.task_dir = task_dir
        self.returncode = None
        self.hanging = hanging
        self.waits = 0
        self.on_wait = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits += 1
        if self.on_wait:
            self.on_wait()
        if self.hanging and self.returncode is None:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class StateValidationTests(unittest.TestCase):
    def test_session_cookie_requires_exact_name_nonempty_value_domain_and_expiry(self):
        now = 1000
        self.assertTrue(C.has_login_cookie(fake_state()["cookies"], now=now))
        self.assertTrue(C.has_login_cookie(fake_state(name="sessionid_ss", domain="www.douyin.com", expires=1001)["cookies"], now=now))
        for changes in ({"name": "sessionid_impostor"}, {"value": ""}, {"value": " "},
                        {"domain": "douyin.com.evil.invalid"}, {"domain": "evildouyin.com"},
                        {"expires": 1000}, {"expires": -2}, {"expires": True}, {"expires": float("nan")}):
            self.assertFalse(C.has_login_cookie(fake_state(**changes)["cookies"], now=now), changes)

    def test_schema_rejects_malformed_shapes_without_echoing_values(self):
        bad = [{}, {"cookies": []}, {"cookies": "secret"},
               fake_state(path="invalid"), fake_state(value=12), fake_state(httpOnly="true"),
               fake_state(sameSite="broken"), fake_state(sameSite=[]), fake_state(expires=float("inf")),
               {**fake_state(), "origins": [{}]}, {**fake_state(), "origins": "secret"}]
        for data in bad:
            with self.assertRaises(C.CredentialExtractInvalid) as caught:
                C.validate_storage_state(json.dumps(data).encode())
            self.assertNotIn("offline-test-value", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))

    def test_size_json_encoding_and_login_presence_limits(self):
        for raw in (b"", b"not-json", b"\xff", b"x" * (C.MAX_STATE_BYTES + 1)):
            with self.assertRaises(C.CredentialExtractInvalid):
                C.validate_storage_state(raw)
        raw = json.dumps(fake_state(name="unrelated")).encode()
        C.validate_storage_state(raw)
        with self.assertRaises(C.CredentialExtractInvalid):
            C.validate_storage_state(raw, require_login=True)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="extract-offline-")))
        self.stack.enter_context(patch.object(A, "ACCOUNTS_ROOT", self.tmp / "accounts"))
        self.stack.enter_context(patch.object(A, "REGISTRY_PATH", self.tmp / "accounts.json"))
        for active in jobs.snapshot():
            jobs.release(active["job_id"])
        for aid in ("alpha", "bravo"):
            A.add_account(aid, display_name="模拟 " + aid)
            A.account_file(aid, "state.json").write_bytes(("old-" + aid).encode())
        self.processes = []
        self.launches = []
        def popen(argv, **kwargs):
            self.launches.append((argv, kwargs))
            directory = Path(argv[argv.index("--task-dir") + 1])
            task_id = argv[argv.index("--task-id") + 1]
            atomic_write_text(directory / "guard-ready.json", json.dumps({"job_id": task_id}))
            proc = FakeProcess(directory)
            self.processes.append(proc)
            return proc
        self.manager = C.CredentialExtractManager(root=self.tmp / "private", popen=popen)
        self.real_thread_start = threading.Thread.start
        self.stack.enter_context(patch("threading.Thread.start"))
        self.addCleanup(self.manager.cleanup)
        self.addCleanup(lambda: [jobs.release(j["job_id"]) for j in jobs.snapshot()])

    def ready(self, state=None):
        task = self.manager._task
        atomic_write_text(task["dir"] / "candidate.json", json.dumps(state or fake_state()))
        atomic_write_text(task["dir"] / "status.json", json.dumps(
            {"job_id": task["job_id"], "status": "ready", "count": 1}))
        return self.manager.status()

    def assert_old_credentials(self):
        for aid in ("alpha", "bravo"):
            self.assertEqual(A.account_file(aid, "state.json").read_bytes(), ("old-" + aid).encode())

    def test_start_frozen_account_safe_status_and_global_exclusion(self):
        result = self.manager.start("alpha")
        self.assertEqual(result["account_id"], "alpha")
        self.assertEqual(result["display_name"], "模拟 alpha")
        self.assertEqual(len(result["job_id"]), 32)
        self.assertNotIn(str(self.tmp), json.dumps(result))
        self.assertNotIn("offline-test-value", json.dumps(self.ready()))
        self.assert_old_credentials()
        for kind in ("contacts", "state", "config", "send", "backup", "credentials"):
            with self.assertRaises(jobs.JobBusy):
                jobs.reserve(account_id="bravo", kind=kind)
        with self.assertRaises(C.CredentialExtractBusy):
            self.manager.start("bravo")
        argv, kwargs = self.launches[0]
        self.assertEqual(argv[1:3], ["-m", "core.credential_worker"])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("STATE_FILE_PATH", kwargs["env"])

    def test_existing_other_account_job_blocks_extraction_before_launch(self):
        token = jobs.reserve(account_id="bravo", kind="contacts")
        with self.assertRaises(C.CredentialExtractBusy):
            self.manager.start("alpha")
        self.assertEqual(self.launches, [])
        self.assertTrue(jobs.is_active(token["job_id"]))

    def test_confirm_writes_only_frozen_account_after_browser_closes(self):
        result = self.manager.start("alpha")
        self.ready()
        proc = self.processes[0]
        def closing_check():
            self.assertTrue(jobs.snapshot())
            self.assert_old_credentials()
        proc.on_wait = closing_check
        original_writer = config.atomic_write_bytes
        def writing_check(path, raw):
            self.assertIsNotNone(proc.returncode)
            self.assertTrue(jobs.snapshot())
            self.assertEqual(path, A.account_file("alpha", "state.json"))
            original_writer(path, raw)
        with patch.object(config, "atomic_write_bytes", side_effect=writing_check):
            saved = self.manager.confirm("alpha", result["job_id"])
        self.assertEqual(saved["status"], "success")
        self.assertEqual(json.loads(A.account_file("alpha", "state.json").read_text()), fake_state())
        self.assertEqual(A.account_file("bravo", "state.json").read_bytes(), b"old-bravo")
        self.assertFalse((self.tmp / "state.json").exists())
        self.assertFalse(self.manager._task["dir"].exists())
        self.assertEqual(jobs.snapshot(), [])

    def test_wrong_account_and_stale_id_never_cancel_or_commit(self):
        started = self.manager.start("alpha")
        self.ready()
        for method in (self.manager.confirm, self.manager.cancel):
            with self.assertRaises(C.CredentialExtractNotFound):
                method("bravo", started["job_id"])
            with self.assertRaises(C.CredentialExtractNotFound):
                method("alpha", "0" * 32)
        self.assertEqual(self.manager.status()["status"], "ready")
        self.assert_old_credentials()

    def test_confirm_and_cancel_race_has_one_serialized_outcome(self):
        started = self.manager.start("alpha")
        self.ready()
        gate = threading.Barrier(3)
        outcomes = []
        def act(action):
            gate.wait()
            try:
                outcomes.append(action("alpha", started["job_id"])["status"])
            except C.CredentialExtractInvalid:
                outcomes.append("invalid")
        threads = [threading.Thread(target=act, args=(action,))
                   for action in (self.manager.confirm, self.manager.cancel)]
        for thread in threads:
            self.real_thread_start(thread)
        gate.wait()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        final = self.manager.status()["status"]
        self.assertIn(final, {"success", "cancelled"})
        if final == "success":
            self.assertEqual(outcomes, ["success", "success"])
            self.assertEqual(json.loads(A.account_file("alpha", "state.json").read_text()), fake_state())
        else:
            self.assertCountEqual(outcomes, ["cancelled", "invalid"])
            self.assert_old_credentials()
        self.assertEqual(jobs.snapshot(), [])

    def test_cancel_discards_candidate_and_preserves_old_credentials(self):
        started = self.manager.start("alpha")
        self.ready()
        cancelled = self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assert_old_credentials()
        self.assertEqual(jobs.snapshot(), [])
        self.assertFalse(self.manager._task["dir"].exists())
        newer = self.manager.start("bravo")
        self.assertNotEqual(started["job_id"], newer["job_id"])
        with self.assertRaises(C.CredentialExtractNotFound):
            self.manager.cancel("alpha", started["job_id"])
        self.assertTrue(self.manager.status()["running"])

    def test_timeout_in_ready_closes_before_releasing_reservation(self):
        self.manager.start("alpha")
        self.ready()
        self.processes[0].on_wait = lambda: self.assertTrue(jobs.snapshot())
        self.manager._task["end_monotonic"] = 0
        result = self.manager.status()
        self.assertEqual(result["status"], "timeout")
        self.assert_old_credentials()
        self.assertEqual(jobs.snapshot(), [])

    def test_worker_failure_uses_only_allowlisted_error_and_discards_candidate(self):
        self.manager.start("alpha")
        self.ready()
        task = self.manager._task
        atomic_write_text(task["dir"] / "status.json", json.dumps({"job_id": task["job_id"],
            "status": "failed", "error_code": "offline-test-value", "error": "secret", "path": str(self.tmp)}))
        result = self.manager.status()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("offline-test-value", json.dumps(result))
        self.assertNotIn(str(self.tmp), json.dumps(result))
        self.assert_old_credentials()

    def test_old_worker_status_cannot_mark_current_task_ready(self):
        self.manager.start("alpha")
        task = self.manager._task
        atomic_write_text(task["dir"] / "status.json", json.dumps({"job_id": "0" * 32, "status": "ready", "count": 99}))
        self.assertEqual(self.manager.status()["status"], "starting")
        self.assertEqual(self.manager.status()["count"], 0)

    def test_invalid_candidate_fails_closed_before_target_write(self):
        started = self.manager.start("alpha")
        self.ready(fake_state(domain="example.invalid"))
        with self.assertRaises(C.CredentialExtractInvalid):
            self.manager.confirm("alpha", started["job_id"])
        self.assertEqual(self.manager.status()["status"], "failed")
        self.assert_old_credentials()
        self.assertEqual(jobs.snapshot(), [])

    def test_atomic_save_failure_keeps_ready_candidate_and_can_retry(self):
        started = self.manager.start("alpha")
        self.ready()
        with patch.object(config, "atomic_write_bytes", side_effect=OSError("fake-sensitive-error")):
            with self.assertRaises(C.CredentialExtractInvalid) as caught:
                self.manager.confirm("alpha", started["job_id"])
        self.assertNotIn("fake-sensitive", str(caught.exception))
        self.assert_old_credentials()
        self.assertEqual(self.manager.status()["status"], "ready")
        self.assertTrue(self.manager._task["dir"].exists())
        self.assertTrue(jobs.snapshot())
        self.assertTrue(self.manager._task["browser_closed"])
        saved = self.manager.confirm("alpha", started["job_id"])
        self.assertEqual(saved["status"], "success")
        self.assertEqual(self.processes[0].waits, 1)

    def test_hanging_child_requires_tree_termination_before_reservation_release(self):
        started = self.manager.start("alpha")
        proc = self.processes[0]
        proc.hanging = True
        def terminate(child):
            self.assertIs(child, proc)
            self.assertTrue(jobs.snapshot())
            child.returncode = -9
        with patch.object(O, "_terminate_tree", side_effect=terminate) as killed:
            result = self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(result["status"], "cancelled")
        killed.assert_called_once_with(proc)
        self.assertEqual(jobs.snapshot(), [])

    def test_failed_tree_cleanup_retains_reservation_until_confirmed_dead(self):
        started = self.manager.start("alpha")
        proc = self.processes[0]
        proc.hanging = True
        with patch.object(O, "_terminate_tree", side_effect=OSError("fake-error")):
            result = self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(result["status"], "stopping")
        self.assertTrue(jobs.snapshot())
        self.assert_old_credentials()
        proc.returncode = -9
        self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(jobs.snapshot(), [])

    def test_shutdown_cancels_and_prevents_further_launches(self):
        self.manager.start("alpha")
        self.ready()
        self.manager.cleanup()
        self.assertEqual(self.manager.status()["status"], "cancelled")
        self.assert_old_credentials()
        self.assertEqual(jobs.snapshot(), [])
        with self.assertRaises(C.CredentialExtractBusy):
            self.manager.start("bravo")

    def test_reopen_allows_same_process_service_restart_after_cleanup(self):
        self.manager.start("alpha")
        self.manager.cleanup()
        self.assertTrue(self.manager.reopen())
        self.assertEqual(self.manager.start("bravo")["account_id"], "bravo")

    def test_cleanup_file_failure_keeps_global_job_until_retry_finishes(self):
        started = self.manager.start("alpha")
        self.ready()
        with patch.object(C.shutil, "rmtree", side_effect=OSError("fake-denied")):
            result = self.manager.cancel("alpha", started["job_id"])
            self.assertEqual(result["status"], "stopping")
            self.assertTrue(jobs.snapshot())
            self.manager.cleanup()
            self.assertFalse(self.manager.reopen())
            self.assertTrue(self.manager._closed)
            self.assertTrue(jobs.snapshot())
        self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(jobs.snapshot(), [])
        self.assertFalse(self.manager._task["dir"].exists())
        self.assertTrue(self.manager.reopen())

    def test_cleanup_failure_after_commit_keeps_success_as_final_outcome(self):
        started = self.manager.start("alpha")
        self.ready()
        with patch.object(C.shutil, "rmtree", side_effect=OSError("fake-denied")):
            saved = self.manager.confirm("alpha", started["job_id"])
        self.assertEqual(saved["status"], "stopping")
        self.assertEqual(json.loads(A.account_file("alpha", "state.json").read_text()), fake_state())
        self.assertTrue(jobs.snapshot())
        final = self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(final["status"], "success")
        self.assertEqual(jobs.snapshot(), [])

    def test_closed_orphan_folders_pruned_but_other_directories_preserved(self):
        root = self.manager._root()
        orphan = root / ("b" * 32)
        orphan.mkdir(parents=True)
        (orphan / "candidate.json").write_text("offline-sensitive-data")
        other = root / "user-folder"
        other.mkdir()
        self.manager.start("alpha")
        self.assertFalse(orphan.exists())
        self.assertTrue(other.exists())

    def test_unexpected_save_exception_does_not_leave_permanent_saving_state(self):
        started = self.manager.start("alpha")
        self.ready()
        with patch.object(config, "atomic_write_bytes", side_effect=RuntimeError("fake")):
            with self.assertRaises(C.CredentialExtractInvalid):
                self.manager.confirm("alpha", started["job_id"])
        self.assertEqual(self.manager.status()["status"], "ready")
        self.assert_old_credentials()

    def test_missing_child_guard_acknowledgement_fails_without_announcing_started(self):
        def no_ack(argv, **kwargs):
            directory = Path(argv[argv.index("--task-dir") + 1])
            proc = FakeProcess(directory)
            proc.returncode = 1
            return proc
        self.manager._popen = no_ack
        with self.assertRaises(C.CredentialExtractInvalid):
            self.manager.start("alpha")
        self.assertEqual(self.manager.status()["status"], "failed")
        self.assertEqual(jobs.snapshot(), [])

    def test_launch_failure_releases_reservation_without_raw_error(self):
        self.manager._popen = Mock(side_effect=OSError("fake-sensitive-path"))
        with self.assertRaises(C.CredentialExtractInvalid) as caught:
            self.manager.start("alpha")
        self.assertNotIn("fake-sensitive", str(caught.exception))
        self.assertEqual(self.manager.status()["status"], "failed")
        self.assertEqual(jobs.snapshot(), [])
        self.assert_old_credentials()

    def test_task_mkdir_failure_releases_reservation_when_directory_was_never_created(self):
        original_mkdir = Path.mkdir
        def denied_task_mkdir(directory, *args, **kwargs):
            if directory.parent == self.manager._root() and C.TASK_ID_RE.fullmatch(directory.name):
                raise PermissionError("offline-denied-create")
            return original_mkdir(directory, *args, **kwargs)
        with patch.object(Path, "mkdir", denied_task_mkdir):
            with self.assertRaises(C.CredentialExtractInvalid):
                self.manager.start("alpha")
        self.assertEqual(self.manager.status()["status"], "failed")
        self.assertEqual(jobs.snapshot(), [])
        self.assertFalse(self.manager._task["dir"].exists())
        self.assert_old_credentials()
        self.assertEqual(self.manager.start("bravo")["account_id"], "bravo")

    def test_already_removed_task_directory_does_not_leave_stopping_reservation(self):
        started = self.manager.start("alpha")
        self.ready()
        with patch.object(config, "atomic_write_bytes", side_effect=OSError("offline-denied-save")):
            with self.assertRaises(C.CredentialExtractInvalid):
                self.manager.confirm("alpha", started["job_id"])
        self.assertTrue(self.manager._task["browser_closed"])
        C.shutil.rmtree(self.manager._task["dir"])
        cancelled = self.manager.cancel("alpha", started["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(jobs.snapshot(), [])
        self.assert_old_credentials()

    def test_thread_failure_closes_started_process_and_releases(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("fake")):
            with self.assertRaises(C.CredentialExtractInvalid):
                self.manager.start("alpha")
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertEqual(jobs.snapshot(), [])

    def test_unknown_account_and_path_traversal_do_not_launch(self):
        for aid in ("absent", "../alpha"):
            with self.assertRaises((C.CredentialExtractInvalid, A.AccountError)):
                self.manager.start(aid)
        self.assertEqual(self.launches, [])

    def test_frozen_worker_command_is_separate_dispatch(self):
        with patch.object(C.sys, "frozen", True, create=True):
            argv = C.build_worker_cmd(Path("task"), Path("account"), "0" * 32, 1000, Path("guard"))
        self.assertEqual(argv[1], "--credential-worker")
        self.assertNotIn("--worker", argv)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp_context = tempfile.TemporaryDirectory(prefix="extract-browser-fake-")
        self.addCleanup(self.tmp_context.cleanup)
        self.tmp = Path(self.tmp_context.name)
        self.job_id = "a" * 32
        self.now = 1000.
        self.browser = Mock()
        self.browser.is_connected.return_value = True
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.context = Mock()
        self.context.cookies.return_value = []
        self.context.storage_state.return_value = fake_state()
        self.closed = False
        self.kwargs = None

    @contextlib.contextmanager
    def browser_factory(self, **kwargs):
        self.kwargs = kwargs
        try:
            yield None, self.browser, self.context, self.page
        finally:
            self.closed = True

    def run_worker(self, sleep, deadline=1010):
        return W.run_login(self.tmp, self.job_id, deadline, browser_factory=self.browser_factory,
                           clock=lambda: self.now, sleep=sleep)

    def read_status(self):
        return json.loads((self.tmp / "status.json").read_text())

    def test_clean_visible_browser_waits_for_manual_login_then_confirm_stop(self):
        def sleep(seconds):
            status = self.read_status()["status"]
            self.assertFalse(self.closed)
            if status == "waiting":
                self.context.cookies.return_value = fake_state()["cookies"]
            else:
                self.assertEqual(status, "ready")
                self.assertTrue((self.tmp / "candidate.json").exists())
                self.assertNotIn("offline-test-value", (self.tmp / "status.json").read_text())
                atomic_write_text(self.tmp / "stop.json", json.dumps({"job_id": self.job_id}))
            self.now += seconds
        self.assertEqual(self.run_worker(sleep), 0)
        self.assertEqual(self.kwargs, {"headless": False, "use_state": False})
        self.assertTrue(self.closed)
        self.assertTrue((self.tmp / "candidate.json").exists())
        self.context.storage_state.assert_called_once_with()

    def test_ready_without_confirmation_times_out_and_discards_candidate(self):
        self.context.cookies.return_value = fake_state()["cookies"]
        self.assertEqual(self.run_worker(lambda seconds: setattr(self, "now", self.now + seconds), deadline=1001), 2)
        self.assertEqual(self.read_status()["status"], "timeout")
        self.assertFalse((self.tmp / "candidate.json").exists())
        self.assertTrue(self.closed)

    def test_manual_browser_close_discards_uncommitted_candidate(self):
        self.context.cookies.return_value = fake_state()["cookies"]
        def sleep(seconds):
            self.page.is_closed.return_value = True
            self.now += seconds
        self.assertEqual(self.run_worker(sleep), 1)
        self.assertEqual(self.read_status()["error_code"], "browser_closed")
        self.assertFalse((self.tmp / "candidate.json").exists())
        self.assertTrue(self.closed)

    def test_stale_stop_request_does_not_cancel_current_login(self):
        atomic_write_text(self.tmp / "stop.json", json.dumps({"job_id": "b" * 32}))
        self.assertEqual(self.run_worker(lambda seconds: setattr(self, "now", self.now + seconds), deadline=1001), 2)
        self.assertEqual(self.read_status()["status"], "timeout")

    def test_browser_exception_does_not_expose_sensitive_message(self):
        self.context.cookies.side_effect = RuntimeError("fake-sensitive-cookie-value")
        self.assertEqual(self.run_worker(lambda seconds: None), 1)
        self.assertNotIn("fake-sensitive", json.dumps(self.read_status()))
        self.assertTrue(self.closed)

    def main_args(self, parent_pid=1234):
        task_dir = self.tmp / self.job_id
        task_dir.mkdir(exist_ok=True)
        account_dir = self.tmp / "account"
        account_dir.mkdir(exist_ok=True)
        return ["--task-dir", str(task_dir), "--task-id", self.job_id, "--account-dir", str(account_dir),
                "--guard-file", str(self.tmp / ".guard"), "--deadline", str(time.time() + 300),
                "--parent-pid", str(parent_pid)]

    def test_worker_dead_parent_never_opens_browser_or_acknowledges(self):
        import os
        argv = self.main_args()
        with patch.dict(os.environ), patch.object(W, "_parent_alive", return_value=False), patch.object(W, "run_login") as login:
            self.assertEqual(W.main(argv), 2)
        login.assert_not_called()
        self.assertFalse((self.tmp / self.job_id / "guard-ready.json").exists())

    def test_worker_guard_ack_precedes_browser_and_independent_deadline_is_armed(self):
        import os
        argv = self.main_args()
        watchdogs = []
        class FakeThread:
            def __init__(self, target, **kwargs):
                watchdogs.append(target)
            def start(self):
                pass
        task_dir = self.tmp / self.job_id
        def login(directory, job_id, deadline):
            self.assertEqual(directory, task_dir)
            self.assertEqual(json.loads((task_dir / "guard-ready.json").read_text())["job_id"], self.job_id)
            self.assertGreater(deadline, time.time())
            self.assertLessEqual(deadline - time.time(), 300)
            return 0
        done = Mock()
        done.wait.return_value = False
        with patch.dict(os.environ), patch.object(W, "_parent_alive", return_value=True), \
                patch.object(W.threading, "Thread", FakeThread), patch.object(W.threading, "Event", return_value=done), \
                patch.object(W, "run_login", side_effect=login), patch.object(W, "_hard_stop_own_tree") as hard_stop:
            self.assertEqual(W.main(argv), 0)
            (task_dir / "candidate.json").write_text("offline-private-state")
            watchdogs[0]()
        done.set.assert_called_once()
        self.assertGreater(done.wait.call_args.args[0], 300)
        self.assertLessEqual(done.wait.call_args.args[0], 305)
        hard_stop.assert_called_once()
        self.assertFalse((task_dir / "candidate.json").exists())


if __name__ == "__main__":
    unittest.main()

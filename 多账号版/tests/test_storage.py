"""Storage tests use temporary JSON files; no real account data is opened."""
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import ledger, storage


def _select_worker(path, start):
    start.wait(10)
    for index in range(35):
        ledger.set_selected([{"display_name": "Alice", "selected": True,
                              "selected_order": index}], path=Path(path))


def _result_worker(path, start):
    start.wait(10)
    for _ in range(35):
        ledger.update_send_result("Alice", True, "2026-10-02T12:00:00+08:00",
                                  msg="test", path=Path(path))


def _claim_worker(path, start, output):
    start.wait(10)
    output.put(ledger.claim_send({"display_name": "Alice", "user_id": "u-1"},
                                 "2026-10-02T12:00:00+08:00", path=Path(path))[0])


def _hold_lock_worker(path, ready):
    with storage.file_lock(Path(path)):
        ready.set()
        multiprocessing.Event().wait(30)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.json"
        self.patch = mock.patch.object(ledger, "LEDGER_PATH", self.path)
        self.patch.start()
        ledger._save([{**ledger._default_entry("Alice"), "user_id": "u-1", "selected": True}])

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_concurrent_selection_and_send_result_preserve_both_fields(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        workers = [ctx.Process(target=func, args=(str(self.path), start))
                   for func in (_select_worker, _result_worker)]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(20)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        current = ledger.load_ledger()[0]
        self.assertTrue(current["selected"])
        self.assertEqual(current["selected_order"], 34)
        self.assertEqual(current["last_status"], "success")
        self.assertEqual(current["last_msg"], "test")

    def test_only_one_process_can_claim_same_friend_day(self):
        ctx = multiprocessing.get_context("spawn")
        start, output = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=_claim_worker, args=(str(self.path), start, output))
                   for _ in range(3)]
        for worker in workers:
            worker.start()
        start.set()
        claims = [output.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sum(claims), 1)
        self.assertEqual(ledger.load_ledger()[0]["last_status"], "unknown")

    def test_missing_main_recovers_valid_backup(self):
        original = self.path.read_text(encoding="utf-8")
        Path(str(self.path) + ".bak").write_text(original, encoding="utf-8")
        self.path.unlink()
        self.assertEqual(ledger.load_ledger()[0]["display_name"], "Alice")
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_corrupt_main_recovers_backup(self):
        original = self.path.read_text(encoding="utf-8")
        Path(str(self.path) + ".bak").write_text(original, encoding="utf-8")
        self.path.write_text("{bad", encoding="utf-8")
        self.assertEqual(ledger.load_ledger()[0]["user_id"], "u-1")
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_backup_does_not_move_main_before_replacement(self):
        original = self.path.read_text(encoding="utf-8")
        real_write = storage.atomic_write_text

        def fail_main(path, text):
            self.assertTrue(self.path.exists())
            if Path(path) == self.path:
                raise OSError("simulated replacement failure")
            return real_write(path, text)

        with mock.patch.object(ledger, "atomic_write_text", side_effect=fail_main):
            with self.assertRaises(OSError):
                ledger.set_selected([{"display_name": "Alice", "selected": False}])
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertEqual(Path(str(self.path) + ".bak").read_text(encoding="utf-8"), original)

    def test_atomic_replace_failure_preserves_destination(self):
        with mock.patch.object(storage.os, "replace", side_effect=OSError("busy")), \
                mock.patch.object(storage.time, "sleep"):
            with self.assertRaises(OSError):
                storage.atomic_write_text(self.path, "[]")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))[0]["display_name"], "Alice")
        self.assertFalse(list(self.path.parent.glob("*.tmp.*")))

    def test_backup_write_failure_aborts_primary_update(self):
        original = self.path.read_bytes()
        with mock.patch.object(ledger, "atomic_write_text", side_effect=OSError("backup busy")):
            with self.assertRaises(OSError):
                ledger.set_selected([{"display_name": "Alice", "selected": False}])
        self.assertEqual(self.path.read_bytes(), original)

    def test_nested_path_lock_is_reentrant(self):
        with storage.file_lock(self.path, timeout=.1):
            ledger.set_selected([{"display_name": "Alice", "selected": True}])
        self.assertTrue(ledger.load_ledger()[0]["selected"])

    def test_corrupt_main_and_backup_are_preserved_without_empty_overwrite(self):
        backup = Path(str(self.path) + ".bak")
        self.path.write_text("{bad-main", encoding="utf-8")
        backup.write_text("{bad-backup", encoding="utf-8")
        with self.assertRaises(ledger.LedgerCorruptionError):
            ledger.set_selected([{"display_name": "Alice", "selected": True}])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{bad-main")
        self.assertEqual(backup.read_text(encoding="utf-8"), "{bad-backup")

    def test_complete_name_selection_updates_all_rows_in_one_transaction(self):
        ledger._save([ledger._default_entry("Alice"),
                      {**ledger._default_entry("Bob"), "selected": True}])
        result = ledger.set_selected_names(["Alice", "New"], path=self.path)
        entries = {e["display_name"]: e for e in ledger.load_ledger()}
        self.assertTrue(entries["Alice"]["selected"])
        self.assertFalse(entries["Bob"]["selected"])
        self.assertTrue(entries["New"]["selected"])
        self.assertEqual(entries["New"]["selected_order"], 1)
        self.assertEqual(result["added"], 1)

    def test_process_death_releases_storage_lock(self):
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        worker = ctx.Process(target=_hold_lock_worker, args=(str(self.path), ready))
        worker.start()
        try:
            self.assertTrue(ready.wait(10))
        finally:
            worker.terminate()
            worker.join(10)
        with storage.file_lock(self.path, timeout=.5):
            self.assertEqual(ledger.load_ledger()[0]["display_name"], "Alice")


if __name__ == "__main__":
    unittest.main()

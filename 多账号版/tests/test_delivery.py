"""Offline delivery tests. Browser, rate-limit, config and runtime are mocked."""
import tempfile
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from core import automation, ledger

AT = "2026-10-02T12:00:00+08:00"


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.json"
        self.patch = mock.patch.object(ledger, "LEDGER_PATH", self.path)
        self.patch.start()
        self.entry = {**ledger._default_entry("Alice"), "user_id": "u-1",
                      "has_conversation": True, "selected": True}
        ledger._save([self.entry])

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _claim(self):
        claimed, reason, attempt = ledger.claim_send(self.entry, AT)
        self.assertTrue(claimed, reason)
        return attempt

    def test_confirmed_success_is_not_sent_twice_on_same_day(self):
        attempt = self._claim()
        ledger.update_send_result("Alice", True, AT, msg="test", entry=self.entry, attempt_id=attempt)
        self.assertFalse(ledger.claim_send(self.entry, AT)[0])
        self.assertEqual(ledger.load_ledger()[0]["last_sent_at"], AT)

    def test_unknown_reservation_survives_reload_and_blocks_retry(self):
        self._claim()
        current = ledger.load_ledger()[0]
        self.assertIsNone(current["last_ok"])
        self.assertIsNone(current["last_sent_at"])
        self.assertFalse(ledger.claim_send(self.entry, AT)[0])

    def test_definite_failure_can_be_retried(self):
        attempt = self._claim()
        ledger.update_send_result("Alice", False, AT, entry=self.entry, attempt_id=attempt)
        self.assertTrue(ledger.claim_send(self.entry, AT)[0])

    def test_different_account_paths_do_not_share_daily_claim(self):
        self._claim()
        other = Path(self.temp.name) / "other-account" / "ledger.json"
        ledger._save([self.entry], other)
        self.assertTrue(ledger.claim_send(self.entry, AT, path=other)[0])

    def test_business_date_uses_shanghai_at_utc_boundary(self):
        self.assertEqual(ledger.business_date("2026-10-01T17:00:00+00:00"), "2026-10-02")
        self._claim()
        self.assertTrue(ledger.claim_send(self.entry, "2026-10-02T16:01:00+00:00")[0])

    def test_stable_id_deduplicates_after_display_name_change(self):
        self._claim()
        renamed = {**self.entry, "display_name": "Renamed"}
        self.assertFalse(ledger.claim_send(renamed, AT)[0])

    def test_legacy_name_and_success_timestamp_are_honored(self):
        legacy = {**ledger._default_entry("Legacy"), "last_sent_at": AT, "selected": True}
        ledger._save([legacy])
        self.assertFalse(ledger.claim_send(legacy, AT)[0])

    def test_cancelled_selection_is_respected_before_browser_send(self):
        ledger.set_selected_names([], path=self.path)
        claimed, reason, _ = ledger.claim_send(self.entry, AT)
        self.assertFalse(claimed)
        self.assertIn("取消勾选", reason)

    def test_same_name_different_ids_require_manual_review(self):
        ledger._save([self.entry, {**self.entry, "user_id": "u-2"}])
        claimed, reason, _ = ledger.claim_send(self.entry, AT)
        self.assertFalse(claimed)
        self.assertIn("同名", reason)

    def test_late_attempt_cannot_overwrite_newer_reservation(self):
        old = self._claim()
        ledger.update_send_result("Alice", False, AT, entry=self.entry, attempt_id=old)
        newer = self._claim()
        ledger.update_send_result("Alice", True, AT, entry=self.entry, attempt_id=old)
        self.assertEqual(ledger.load_ledger()[0]["delivery_records"]["2026-10-02"]["attempt_id"], newer)
        self.assertEqual(ledger.load_ledger()[0]["last_status"], "unknown")

    def test_editor_clearing_alone_is_unknown_and_never_retries(self):
        page = mock.Mock()
        with mock.patch.object(automation, "detect_rate_limit", return_value=False), \
                mock.patch.object(automation, "_outbound_snapshot", return_value={"count": 0, "ids": []}), \
                mock.patch.object(automation, "_type_and_send", return_value=True) as send, \
                mock.patch.object(automation, "_wait_new_outbound", return_value=False):
            outcome, reason = automation._send_message(page, "test", False)
        self.assertIsNone(outcome)
        self.assertIn("未知", reason)
        send.assert_called_once()

    def test_new_outbound_echo_is_positive_confirmation(self):
        page = mock.Mock()
        with mock.patch.object(automation, "_outbound_snapshot", return_value={"count": 2, "ids": ["old", "new"]}):
            self.assertTrue(automation._wait_new_outbound(page, "test", {"count": 1, "ids": ["old"]}, wait=0))
        with mock.patch.object(automation, "_outbound_snapshot", return_value={"count": 1, "ids": ["old"]}):
            self.assertFalse(automation._wait_new_outbound(page, "test", {"count": 1, "ids": ["old"]}, wait=0))

    def test_existing_message_gaining_id_is_not_a_new_echo(self):
        with mock.patch.object(automation, "_outbound_snapshot", return_value={"count": 1, "ids": ["existing"]}):
            self.assertFalse(automation._wait_new_outbound(mock.Mock(), "test", {"count": 1, "ids": []}, wait=0))

    def test_outbound_dom_filter_excludes_incoming_editor_and_pending(self):
        node_exe = shutil.which("node")
        if not node_exe:
            self.skipTest("Node is unavailable for offline DOM model")
        # Execute the production extractor against a tiny node model. No browser
        # engine or remote page is used; the cases test directional evidence.
        harness = r"""
const extract = EXTRACTOR;
function node(text, cls, attrs = {}, editor = false) {
    return {textContent: text, className: cls, parentElement: null,
        closest: () => editor ? {} : null,
        getAttribute: name => attrs[name] || null,
        matches: () => !!(attrs['data-message-id'] || attrs['data-msg-id']),
        querySelector: () => null};
}
const cases = [
    [node('test', 'messageItemIncoming')],
    [node('test', 'messageItemSelf', {}, true)],
    [node('test', 'messageItemSelf', {'data-status': 'sending'})],
    [node('test', 'messageItemSelf', {'data-status': 'failed'})],
    [node('test', 'ChatRightPanel')],
    [node('test', 'messageItemSelf', {'data-message-id': 'out-1'})],
    [node('test', 'messageItem', {'data-direction': 'outgoing', 'data-message-id': 'out-2'})]
];
const pendingParent = node('test', 'messageItemSelf', {'data-status': 'sending'});
const outgoingChild = node('test', 'messageContentSelf');
outgoingChild.parentElement = pendingParent;
cases.push([outgoingChild, pendingParent]);
const result = cases.map(nodes => {
    global.document = {querySelectorAll: () => nodes};
    return extract('test');
});
process.stdout.write(JSON.stringify(result));
""".replace("EXTRACTOR", automation._OUTBOUND_SNAPSHOT_JS)
        script = Path(self.temp.name) / "dom-model.js"
        script.write_text(harness, encoding="utf-8")
        completed = subprocess.run([node_exe, str(script)], capture_output=True, text=True, timeout=10, check=True)
        results = json.loads(completed.stdout)
        self.assertEqual([r["count"] for r in results], [0, 0, 0, 0, 0, 1, 1, 0])
        self.assertEqual(results[-2]["ids"], ["out-2"])

    def test_send_crossing_midnight_finishes_original_claim(self):
        claim_at = "2026-10-02T23:59:59+08:00"
        finish_at = "2026-10-03T00:00:01+08:00"
        claimed, reason, attempt = ledger.claim_send(self.entry, claim_at)
        self.assertTrue(claimed, reason)
        ledger.update_send_result("Alice", True, finish_at, entry=self.entry, attempt_id=attempt)
        current = ledger.load_ledger()[0]
        self.assertEqual(current["delivery_records"]["2026-10-02"]["status"], "success")
        self.assertEqual(current["last_sent_at"], finish_at)
        self.assertFalse(ledger.claim_send(self.entry, finish_at)[0])

    def test_exception_during_enter_is_unknown(self):
        page, box = mock.Mock(), mock.Mock()
        page.keyboard.press.side_effect = [None, None, OSError("lost acknowledgement")]
        with mock.patch.object(automation, "_wait_text_in_box", return_value=True), \
                mock.patch.object(automation.time, "sleep"):
            self.assertIsNone(automation._type_and_send(page, box, "test"))

    def test_consumer_unknown_is_reported_and_next_trigger_skips(self):
        result = {"ok": [], "failed": [], "unknown": [], "skipped": [], "rate_limited": False}
        with mock.patch.object(automation, "send_to_contact", return_value=(None, "unknown")) as sender, \
                mock.patch.object(automation, "_now", return_value=AT), \
                mock.patch.object(ledger, "_now", return_value=AT):
            automation._send_consumer(mock.Mock(), self.entry, "test", False, result)
            automation._send_consumer(mock.Mock(), self.entry, "test", False, result)
        sender.assert_called_once()
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["failed"])

    def test_dry_run_does_not_modify_ledger_or_daily_claim(self):
        before = self.path.read_bytes()
        result = {"ok": [], "failed": [], "unknown": [], "skipped": [], "rate_limited": False}
        with mock.patch.object(automation, "send_to_contact", return_value=(True, "dry-run")):
            automation._send_consumer(mock.Mock(), self.entry, "test", True, result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(ledger.send_block_reason(self.entry, AT))


if __name__ == "__main__":
    unittest.main()

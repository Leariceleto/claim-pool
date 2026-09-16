import importlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests.test_excel_import import install_fastapi_stub


class PaymentReminderTests(unittest.TestCase):
    def setUp(self):
        install_fastapi_stub()
        self.app = importlib.import_module("app")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = patch.object(self.app, "DB_PATH", Path(self.temp.name) / "test.db")
        db.start()
        self.addCleanup(db.stop)
        recipients = patch.object(self.app, "UNCLAIMED_REMINDER_RECIPIENTS", {"董芳": "ou_d", "何玲": "ou_h"})
        recipients.start()
        self.addCleanup(recipients.stop)
        self.app.init_db()

    def payment(self, amount=10000, claimed=0, status="pending", due="2026-09-17 17:00:00"):
        with self.app.get_conn() as conn:
            pid = conn.execute(
                "INSERT INTO payments (imported_at, payer_name, amount_cents, status, reminder_due_at) VALUES (?, ?, ?, ?, ?)",
                ("2026-09-16 12:00:00", "测试付款方", amount, status, due),
            ).lastrowid
            if claimed:
                conn.execute(
                    "INSERT INTO claims (payment_id, department, actor_id, actor_name, status, created_at, amount_cents) VALUES (?, '测试部门', 'u', '认领人', 'accepted', '2026-09-16 13:00:00', ?)",
                    (pid, claimed),
                )
        return pid

    def test_partial_claim_reports_both_amounts_and_only_sends_once(self):
        self.payment(claimed=3000, status="partial_claiming")
        with patch.object(self.app, "feishu_send_text", return_value=True) as send:
            self.app.process_payment_reminders("2026-09-17 16:59:59")
            send.assert_not_called()
            self.app.process_payment_reminders("2026-09-17 17:00:00")
            self.app.process_payment_reminders("2026-09-18 17:00:00")
        self.assertEqual(send.call_count, 2)
        self.assertEqual({call.args[0] for call in send.call_args_list}, {"ou_d", "ou_h"})
        message = send.call_args.args[1]
        self.assertIn("已认领金额：¥ 30.00", message)
        self.assertIn("剩余未认领金额：¥ 70.00", message)

    def test_full_closed_draft_and_legacy_payments_are_not_sent(self):
        self.payment(claimed=10000, status="claimed")
        self.payment(status="closed")
        self.payment(status="draft")
        self.payment(due=None)
        with patch.object(self.app, "feishu_send_text") as send:
            self.app.process_payment_reminders("2026-09-17 17:00:00")
        send.assert_not_called()

    def test_schedule_uses_next_calendar_day_at_17_beijing_time(self):
        with patch.object(self.app, "datetime") as clock:
            clock.now.return_value = datetime(2026, 12, 31, 23, 59, tzinfo=self.app.REMINDER_TIMEZONE)
            self.assertEqual(self.app.next_day_reminder_at(), "2027-01-01 17:00:00")
            clock.now.assert_called_once_with(self.app.REMINDER_TIMEZONE)

    def test_retry_only_failed_recipient_with_original_snapshot(self):
        self.payment()
        with patch.object(self.app, "feishu_send_text", side_effect=[True, False, True]) as send:
            self.app.process_payment_reminders("2026-09-17 17:00:00")
            self.app.process_payment_reminders("2026-09-17 17:01:00")
            self.app.process_payment_reminders("2026-09-17 17:02:00")
        self.assertEqual([call.args[0] for call in send.call_args_list], ["ou_d", "ou_h", "ou_h"])
        self.assertEqual(send.call_args_list[1].args[1], send.call_args_list[2].args[1])
        self.assertIn("已认领金额：¥ 0.00", send.call_args.args[1])

    def test_missing_recipient_leaves_schedule_pending(self):
        self.payment()
        with patch.object(self.app, "UNCLAIMED_REMINDER_RECIPIENTS", {"董芳": "ou_d", "何玲": ""}), patch.object(self.app, "feishu_send_text") as send:
            self.app.process_payment_reminders("2026-09-17 17:00:00")
        send.assert_not_called()
        with self.app.get_conn() as conn:
            self.assertIsNotNone(conn.execute("SELECT reminder_due_at FROM payments").fetchone()[0])

    def test_downtime_catches_up_and_refund_offsets_claim_total(self):
        pid = self.payment(claimed=7000, status="partial_claiming")
        with self.app.get_conn() as conn:
            conn.execute("INSERT INTO claims (payment_id, department, actor_id, actor_name, status, created_at, amount_cents) VALUES (?, '部门', 'u', '同事', 'accepted', '2026-09-17 12:00:00', -2000)", (pid,))
        with patch.object(self.app, "feishu_send_text", return_value=True) as send:
            self.app.process_payment_reminders("2026-09-18 10:00:00")
        self.assertEqual(send.call_count, 2)
        self.assertIn("已认领金额：¥ 50.00", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

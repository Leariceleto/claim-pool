import importlib
import json
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
        for name, value in {
            "DB_PATH": Path(self.temp.name) / "test.db",
            "UNCLAIMED_REMINDER_RECIPIENTS": {"董芳": "ou_d", "何玲": "ou_h"},
        }.items():
            mock = patch.object(self.app, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.app.init_db()

    def payment(self, claimed=10000, imported_at="2026-09-16 12:00:00"):
        with self.app.get_conn() as conn:
            pid = conn.execute(
                "INSERT INTO payments (imported_at, payer_name, amount_cents, status) VALUES (?, '测试付款方', 10000, 'claimed')",
                (imported_at,),
            ).lastrowid
            cid = conn.execute(
                "INSERT INTO claims (payment_id, department, actor_id, actor_name, status, created_at, amount_cents) VALUES (?, '测试部门', 'u', '认领人', 'accepted', '2026-09-16 18:00:00', ?)",
                (pid, claimed),
            ).lastrowid
        return pid, cid

    def notify(self, pid, cid, amount=10000, at="2026-09-16 18:00:00"):
        with patch.object(self.app, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat(at).replace(tzinfo=self.app.REMINDER_TIMEZONE)
            with self.app.get_conn() as conn:
                self.app.notify_late_payment_claimed(conn, pid, [cid], {"name": "认领人"}, amount)

    def jobs(self):
        with self.app.get_conn() as conn:
            return conn.execute("SELECT * FROM notification_outbox ORDER BY id").fetchall()

    def test_partial_claim_reports_both_amounts_and_only_sends_once(self):
        pid, cid = self.payment(claimed=3000)
        self.notify(pid, cid, amount=3000)
        self.notify(pid, cid, amount=3000)
        with patch.object(self.app, "deliver_notification", return_value="") as send:
            self.app.process_notification_outbox(1000)
            self.app.process_notification_outbox(2000)
        self.assertEqual(send.call_count, 2)
        self.assertEqual({call.args[0]["recipient_id"] for call in send.call_args_list}, {"ou_d", "ou_h"})
        message = send.call_args.args[0]["message"]
        self.assertIn("本次认领金额：¥ 30.00", message)
        self.assertIn("剩余未认领金额：¥ 70.00", message)

    def test_only_claims_strictly_after_upload_day_17_notify(self):
        for at, expected in [("2026-09-16 16:59:59", 0), ("2026-09-16 17:00:00", 0),
                             ("2026-09-16 17:00:01", 2), ("2026-09-17 09:00:00", 2)]:
            with self.subTest(at=at):
                pid, cid = self.payment()
                before = len(self.jobs())
                self.notify(pid, cid, at=at)
                self.assertEqual(len(self.jobs()) - before, expected)
        pid, cid = self.payment(imported_at="2026-09-16 18:00:00")
        self.notify(pid, cid, at="2026-09-16 18:01:00")
        self.assertEqual(len(self.jobs()), 6)

    def test_idle_worker_never_generates_unclaimed_reminders(self):
        self.payment(claimed=0)
        with patch.object(self.app.REMINDER_STOP, "is_set", side_effect=[False, True]), \
                patch.object(self.app.REMINDER_STOP, "wait"), patch.object(self.app, "deliver_notification") as send:
            self.app.reminder_loop()
        send.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_retry_only_failed_recipient_with_original_message(self):
        pid, cid = self.payment()
        self.notify(pid, cid)
        with patch.object(self.app, "deliver_notification", side_effect=["", "test_failure", ""]) as send:
            self.app.process_notification_outbox(1000)
            self.app.process_notification_outbox(1060)
            self.app.process_notification_outbox(1120)
        self.assertEqual([call.args[0]["recipient_id"] for call in send.call_args_list], ["ou_d", "ou_h", "ou_h"])
        self.assertEqual(send.call_args_list[1].args[0]["message"], send.call_args_list[2].args[0]["message"])

    def test_missing_recipient_does_not_notify_other_admins(self):
        pid, cid = self.payment()
        with patch.object(self.app, "UNCLAIMED_REMINDER_RECIPIENTS", {"董芳": "ou_d", "何玲": ""}):
            self.notify(pid, cid)
        self.assertEqual(self.jobs(), [])

    def test_legacy_snapshot_is_not_required_or_used_for_recipients(self):
        pid, cid = self.payment()
        with self.app.get_conn() as conn:
            conn.execute("INSERT INTO payment_reminders (payment_id, message, recipients_json) VALUES (?, 'Old reminder', ?)",
                         (pid, json.dumps(["old_admin"])))
        self.notify(pid, cid)
        self.assertEqual({job["recipient_id"] for job in self.jobs()}, {"ou_d", "ou_h"})

    def test_dashboard_filters_only_apply_to_admin_roles(self):
        from starlette.requests import Request

        request = Request({"type": "http", "method": "GET", "path": "/me", "query_string": b"", "headers": [], "scheme": "http", "server": ("localhost", 8002)})
        for role in ["claimant", "general_manager", "admin", "superadmin"]:
            actor = dict(id="test-user", name="测试", role=role, department="年会", team="", authed="1")
            with patch.object(self.app, "actor_from_request", return_value=actor), patch.object(self.app, "personal_dashboard_data", return_value=[]) as dashboard:
                self.app.personal_center(request, dashboard_department="年会", filter_payer="南京")
            expected = "年会" if role in {"admin", "superadmin"} else ""
            self.assertEqual(dashboard.call_args.kwargs["department"], expected)
            self.assertEqual(dashboard.call_args.kwargs["filters"].get("filter_payer", ""), "南京" if expected else "")


if __name__ == "__main__":
    unittest.main()

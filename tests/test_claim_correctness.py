import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from unittest.mock import patch

import app


class ClaimCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {
            "DB_PATH": Path(self.temp.name) / "test.db",
            "UPLOAD_DIR": Path(self.temp.name) / "uploads",
            "FEISHU_SUPERADMIN_OPEN_IDS": set(),
            "UNCLAIMED_REMINDER_RECIPIENTS": {"董芳": "d", "何玲": "h"},
            "FEISHU_NOTIFY_CHAT_ID": "chat-test",
        }.items():
            mock = patch.object(app, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        network = patch.object(app, "_feishu_request", side_effect=AssertionError("Real network forbidden in tests"))
        network.start()
        self.addCleanup(network.stop)
        app.init_db()
        self.department = next(iter(app.CATALOG))
        self.team = next(iter(app.CATALOG[self.department]))
        self.project = app.CATALOG[self.department][self.team][0]
        self.actor = dict(id="u", name="User", role="admin", authed="1")
        with closing(app.get_conn()) as conn, conn:
            conn.execute("INSERT INTO app_users (open_id, name, managed_role, is_admin, created_at) VALUES ('u', 'User', 'admin', 1, '2026-09-20')")
            conn.execute("INSERT INTO user_profiles (open_id, name, department, team, updated_at) VALUES ('u', 'User', ?, ?, '2026-09-20')", (self.department, self.team))
        self.cookie = {app.SESSION_COOKIE: app.make_session({"id": "u", "name": "User"})}

    def request(self, method, path, data=None, authenticated=True, extra_headers=()):
        # 直接走真实 ASGI 中间件和路由，不引入 HTTP 客户端依赖，也不启动通知线程。
        async def run():
            target = urlsplit(path)
            body = urlencode(data or {}, doseq=True).encode()
            headers = [(b"content-type", b"application/x-www-form-urlencoded")]
            headers.extend(extra_headers)
            if authenticated:
                headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in self.cookie.items()).encode()))
            scope = dict(type="http", asgi={"version": "3.0"}, http_version="1.1", method=method,
                         scheme="https", path=target.path, raw_path=target.path.encode(), root_path="",
                         query_string=target.query.encode(), headers=headers,
                         client=("127.0.0.1", 1000), server=("testserver", 443))
            sent_request = False
            finished = asyncio.Event()
            messages = []

            async def receive():
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await finished.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    finished.set()

            await asyncio.wait_for(app.app(scope, receive, send), timeout=10)
            start = next(m for m in messages if m["type"] == "http.response.start")
            return SimpleNamespace(status_code=start["status"],
                                   headers={k.decode(): v.decode() for k, v in start["headers"]},
                                   text=b"".join(m.get("body", b"") for m in messages).decode())
        return asyncio.run(run())

    def payment(self, amount=10000, status="pending", claims=()):
        with closing(app.get_conn()) as conn, conn:
            pid = conn.execute("""INSERT INTO payments
                (imported_at, received_date, payer_name, receiver_company, amount_cents, status)
                VALUES ('2026-09-20', '2026-09-20', 'Test payer', 'Test company', ?, ?)""", (amount, status)).lastrowid
            for cents in claims:
                conn.execute("""INSERT INTO claims
                    (payment_id, department, team, actor_id, actor_name, customer_project, amount_cents, status, created_at)
                    VALUES (?, ?, ?, 'u', 'User', ?, ?, 'accepted', '2026-09-20')""",
                    (pid, self.department, self.team, self.project, cents))
            return pid

    def row(self, pid):
        with closing(app.get_conn()) as conn:
            return conn.execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()

    def active(self, pid):
        with closing(app.get_conn()) as conn:
            return app.claim_totals(conn, pid)["active"]

    def claim_request(self, kind, pid, amount="100"):
        if kind == "split":
            return self.request("POST", f"/split-claim/{pid}", dict(departments=[self.department], teams=[self.team], projects=[self.project], amounts=[amount], notes=[""]))
        data = dict(department=self.department, team=self.team, customer_project=self.project)
        if kind == "batch":
            return self.request("POST", "/claim/batch", {**data, "payment_ids": [str(pid)]})
        return self.request("POST", f"/claim/{pid}", {**data, "claim_amount": amount})

    def test_concurrent_all_claim_entry_pairs_cannot_overclaim(self):
        for kinds in [("normal", "normal"), ("batch", "batch"), ("split", "split"), ("normal", "batch"), ("normal", "split"), ("batch", "split")]:
            with self.subTest(kinds=kinds):
                pid = self.payment()
                barrier = threading.Barrier(2)
                original = app.claim_totals

                def slow_totals(conn, payment_id):
                    result = original(conn, payment_id)
                    time.sleep(0.03)
                    return result

                def submit(kind):
                    barrier.wait(timeout=3)
                    return self.claim_request(kind, pid).status_code

                with patch.object(app, "claim_totals", side_effect=slow_totals), ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(submit, kinds))
                self.assertEqual(sorted(results), [303, 409])
                self.assertEqual(self.active(pid), 10000)

    def test_unauthenticated_reads_writes_and_forged_identity_are_blocked(self):
        pid = self.payment()
        for path in ["/me?department=年会&user=u&role=admin", "/search", "/admin", "/admin/export/today", "/admin/export/today-text", "/attachments/test.pdf"]:
            with self.subTest(path=path):
                response = self.request("GET", path, authenticated=False)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.headers["location"].startswith("/login?"))
                self.assertNotIn("Test payer", response.text)
        for path in [f"/claim/{pid}", "/claim/batch", f"/split-claim/{pid}", f"/admin/payments/{pid}/edit"]:
            self.assertEqual(self.request("POST", path, dict(user="u", role="admin"), False).status_code, 401)
        self.assertEqual(self.active(pid), 0)

    def test_signed_role_is_recomputed_and_form_identity_ignored(self):
        pid = self.payment()
        with closing(app.get_conn()) as conn, conn:
            conn.execute("UPDATE app_users SET managed_role = 'claimant', is_admin = 0 WHERE open_id = 'u'")
        self.assertEqual(self.request("GET", "/admin?role=admin").status_code, 403)
        data = dict(department=self.department, team=self.team, customer_project=self.project, user="someone-else", role="superadmin", claim_amount="100")
        self.assertEqual(self.request("POST", f"/claim/{pid}", data).status_code, 303)
        with closing(app.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT actor_id FROM claims WHERE payment_id = ?", (pid,)).fetchone()[0], "u")

    def test_invalid_normal_amount_is_not_full_claim(self):
        for amount in ["abc", "0", "-1", "1x2", "1.001", "NaN", "1,2", "9" * 100]:
            with self.subTest(amount=amount):
                pid = self.payment()
                self.assertEqual(self.claim_request("normal", pid, amount).status_code, 400)
                self.assertEqual(self.active(pid), 0)
        pid = self.payment(claims=[3000], status="partial_claiming")
        self.assertEqual(self.claim_request("normal", pid, "").status_code, 303)
        self.assertEqual(self.active(pid), 10000)

    def test_cancel_either_refund_offset_row_rejects_invalid_net(self):
        pid = self.payment(claims=[12000, -2000], status="claimed")
        with closing(app.get_conn()) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM claims WHERE payment_id = ?", (pid,))]
        for cid in ids:
            response = self.request("POST", f"/me/claims/{cid}/cancel")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.active(pid), 10000)

    def test_closed_rejected_and_draft_survive_refresh_and_cancel(self):
        for status in ["closed", "rejected", "draft"]:
            pid = self.payment(status=status, claims=[10000])
            with closing(app.get_conn()) as conn, conn:
                cid = conn.execute("SELECT id FROM claims WHERE payment_id = ?", (pid,)).fetchone()[0]
                app.refresh_payment_claim_status(conn, pid)
            self.assertEqual(self.row(pid)["status"], status)
            self.assertEqual(self.request("POST", f"/me/claims/{cid}/cancel").status_code, 409)
            self.assertEqual(self.row(pid)["status"], status)
            if status == "draft":
                self.assertEqual(self.request("POST", f"/admin/payments/{pid}/reject").status_code, 409)
                self.assertEqual(self.row(pid)["status"], "draft")

    def test_edit_checks_amount_and_refreshes_completion(self):
        pid = self.payment(status="claimed", claims=[10000])
        data = dict(received_date="2026-09-20", amount="50", payer_name="Test", receiver_company="Company", bank_note="")
        self.assertEqual(self.request("POST", f"/admin/payments/{pid}/edit", data).status_code, 409)
        self.assertEqual(self.row(pid)["amount_cents"], 10000)
        self.assertEqual(self.request("POST", f"/admin/payments/{pid}/edit", {**data, "amount": "200"}).status_code, 303)
        self.assertEqual(self.row(pid)["status"], "partial_claiming")

    def test_edit_and_claim_race_cannot_create_overclaim(self):
        pid = self.payment(amount=20000, status="partial_claiming", claims=[10000])
        barrier = threading.Barrier(2)
        original = app.claim_totals

        def slow_totals(conn, payment_id):
            result = original(conn, payment_id)
            time.sleep(0.03)
            return result

        def action(kind):
            barrier.wait(timeout=3)
            if kind == "claim":
                return self.claim_request("normal", pid).status_code
            return self.request("POST", f"/admin/payments/{pid}/edit", dict(amount="150", received_date="2026-09-20")).status_code

        with patch.object(app, "claim_totals", side_effect=slow_totals), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(action, ["claim", "edit"]))
        self.assertEqual(sorted(results), [303, 409])
        self.assertLessEqual(self.active(pid), self.row(pid)["amount_cents"])

    def test_reject_negative_pending_claim_keeps_net_intact(self):
        pid = self.payment(claims=[12000, -2000], status="claimed")
        with closing(app.get_conn()) as conn, conn:
            conn.execute("UPDATE claims SET status = 'pending' WHERE payment_id = ?", (pid,))
            cid = conn.execute("SELECT id FROM claims WHERE payment_id = ? AND amount_cents < 0", (pid,)).fetchone()[0]
        self.assertEqual(self.request("POST", f"/admin/claims/{cid}/reject").status_code, 409)
        self.assertEqual(self.active(pid), 10000)

    def test_resolve_validates_claims_and_explicit_rejection_preserves_history(self):
        pid = self.payment(status="claimed", claims=[10000])
        path = f"/admin/payments/{pid}/resolve"
        self.assertEqual(self.request("POST", path, {"status": "pending"}).status_code, 409)
        self.assertEqual(self.active(pid), 10000)
        self.assertEqual(self.request("POST", path, {"status": "closed"}).status_code, 303)
        self.assertEqual(self.request("POST", path, {"status": "claimed"}).status_code, 303)
        self.assertEqual(self.row(pid)["status"], "claimed")
        self.assertEqual(self.request("POST", path, {"status": "rejected"}).status_code, 303)
        self.assertEqual(self.active(pid), 0)
        self.assertEqual(self.row(pid)["status"], "pending")
        with closing(app.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM claims WHERE payment_id = ?", (pid,)).fetchone()[0], "rejected")
        self.assertEqual(self.request("POST", path, {"status": "claimed"}).status_code, 409)

    def test_cancel_queue_and_business_rollback_together(self):
        pid = self.payment(status="claimed", claims=[10000])
        with closing(app.get_conn()) as conn:
            cid = conn.execute("SELECT id FROM claims WHERE payment_id = ?", (pid,)).fetchone()[0]
        with patch.object(app, "audit", side_effect=RuntimeError("test rollback")):
            with closing(app.get_conn()) as conn:
                with self.assertRaises(RuntimeError), conn:
                    app.cancel_my_claim(conn, self.actor, cid)
        self.assertEqual(self.active(pid), 10000)
        with closing(app.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
        self.assertEqual(self.request("POST", f"/me/claims/{cid}/cancel").status_code, 303)
        with closing(app.get_conn()) as conn:
            job = conn.execute("SELECT * FROM notification_outbox").fetchone()
            self.assertEqual(job["status"], "pending")
            self.assertEqual(job["recipient_id"], "u")

    def enqueue(self):
        with closing(app.get_conn()) as conn, conn:
            app.enqueue_notification(conn, "test:1", "recipient", "Test message")

    def test_slow_send_does_not_lock_business_or_allow_duplicate_worker(self):
        self.enqueue()
        entered, release = threading.Event(), threading.Event()

        def slow_send(job):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
            return ""

        with patch.object(app, "deliver_notification", side_effect=slow_send) as send, ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(app.process_notification_outbox, 1000)
            try:
                self.assertTrue(entered.wait(3))
                with closing(sqlite3.connect(app.DB_PATH, timeout=0.1)) as conn, conn:
                    conn.execute("UPDATE app_users SET name = 'Updated' WHERE open_id = 'u'")
                app.process_notification_outbox(1001)
                self.assertEqual(send.call_count, 1)
            finally:
                release.set()
            future.result(timeout=5)
        with closing(app.get_conn()) as conn:
            job = conn.execute("SELECT * FROM notification_outbox").fetchone()
            self.assertEqual(job["status"], "sent")
            self.assertEqual(job["attempts"], 1)

    def test_failed_send_retry_and_expired_lease_reuse_request_uuid(self):
        self.enqueue()
        with patch.object(app, "deliver_notification", side_effect=["test_error", ""]) as send:
            app.process_notification_outbox(1000)
            app.process_notification_outbox(1059)
            self.assertEqual(send.call_count, 1)
            with closing(app.get_conn()) as conn:
                self.assertEqual(conn.execute("SELECT last_error FROM notification_outbox").fetchone()[0], "test_error")
            app.process_notification_outbox(1060)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args_list[0].args[0]["request_uuid"], send.call_args_list[1].args[0]["request_uuid"])
        with closing(app.get_conn()) as conn, conn:
            conn.execute("UPDATE notification_outbox SET status = 'sending', lease_until = 2000, lease_token = 'dead-worker'")
        with patch.object(app, "deliver_notification", return_value="") as send:
            app.process_notification_outbox(1999)
            send.assert_not_called()
            app.process_notification_outbox(2000)
            send.assert_called_once()

    def test_sender_uses_persisted_uuid_and_records_api_error(self):
        self.enqueue()
        with patch.object(app, "feishu_enabled", return_value=True), patch.object(app, "feishu_tenant_token", return_value="fake-test-token"), patch.object(app, "_feishu_request", return_value={"code": 999}) as send:
            app.process_notification_outbox(1000)
        with closing(app.get_conn()) as conn:
            job = conn.execute("SELECT * FROM notification_outbox").fetchone()
            self.assertEqual(job["last_error"], "send_failed: 999")
            self.assertIsNotNone(job["last_attempt_at"])
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(send.call_args.args[2]["uuid"], job["request_uuid"])
            self.assertEqual(send.call_args.args[0], app.FEISHU_SEND_MSG_URL)

    def test_draft_reminder_survives_due_time_and_legacy_snapshot_migrates(self):
        pid = self.payment(status="draft")
        with closing(app.get_conn()) as conn, conn:
            conn.execute("UPDATE payments SET reminder_due_at = '2026-09-21 17:00:00' WHERE id = ?", (pid,))
        app.process_payment_reminders("2026-09-21 17:00:00")
        self.assertIsNotNone(self.row(pid)["reminder_due_at"])
        with closing(app.get_conn()) as conn, conn:
            conn.execute("UPDATE payments SET status = 'pending' WHERE id = ?", (pid,))
        app.process_payment_reminders("2026-09-21 18:00:00")
        with closing(app.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 2)
        with closing(app.get_conn()) as conn, conn:
            conn.execute("DELETE FROM notification_outbox")
            conn.execute("UPDATE payment_reminders SET sent_json = ?", (json.dumps(["d"]),))
        with patch.object(app, "UNCLAIMED_REMINDER_RECIPIENTS", {"董芳": "", "何玲": ""}):
            app.process_payment_reminders("2026-09-21 19:00:00")
        with closing(app.get_conn()) as conn:
            jobs = conn.execute("SELECT recipient_id FROM notification_outbox").fetchall()
            self.assertEqual([r[0] for r in jobs], ["h"])

    def test_migration_is_repeatable_and_does_not_crash_on_legacy_overclaim(self):
        pid = self.payment(claims=[12000])
        with closing(app.get_conn()) as conn, conn:
            conn.execute("UPDATE claims SET status = 'pending' WHERE payment_id = ?", (pid,))
        self.enqueue()
        app.init_db()
        app.init_db()
        self.assertEqual(self.active(pid), 12000)
        with closing(app.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)

    def test_exports_share_scope_totals_and_refund_rows(self):
        for status in ["closed", "draft", "rejected"]:
            self.payment(status=status, claims=[10000])
        included = self.payment(status="partial_claiming", claims=[7000, -2000])
        response = self.request("GET", "/admin/export/today?date=2026-09-20")
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        self.assertEqual({r["ID"] for r in rows}, {str(included)})
        self.assertEqual(sum(app.parse_amount(r["认领金额"]) for r in rows), 10000)
        plain = self.request("GET", "/admin/export/today-text?date=2026-09-20")
        self.assertIn("今日合计：100.00元", plain.text)
        self.assertIn("50.00元（未认领）", plain.text)

    def test_dashboard_truncation_is_explicit(self):
        entries = [dict(payment_id=i, payer_name="Test", department="Test", amount_cents=100) for i in range(60)]
        data = app.summarize_dashboard_entries(entries)
        data.update(label="每日", start=date.today(), end=date.today())
        html = app.render_personal_dashboard(self.actor, [data])
        self.assertEqual(data["row_count"], 60)
        self.assertEqual(len(data["rows"]), 50)
        self.assertIn("当前显示前 50 条，共 60 条", html)
        self.assertIn("合计包含全部匹配款项", html)

    def test_fetch_claim_reports_success_and_preserves_validation_errors(self):
        pid = self.payment()
        data = dict(department=self.department, team=self.team, customer_project=self.project, claim_amount="abc")
        headers = [(b"x-requested-with", b"fetch")]
        result = self.request("POST", f"/claim/{pid}", data, extra_headers=headers)
        self.assertEqual(result.status_code, 400)
        self.assertIn("金额格式不正确", json.loads(result.text)["detail"])
        result = self.request("POST", f"/claim/{pid}", {**data, "claim_amount": "100"}, extra_headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.text)["redirect"], "/search")
        self.assertIn("100.00", json.loads(result.text)["message"])

    def test_fetch_batch_reports_skipped_and_unauthenticated_get_returns_401(self):
        pending, closed = self.payment(), self.payment(status="closed")
        headers = [(b"x-requested-with", b"fetch")]
        data = dict(department=self.department, team=self.team, customer_project=self.project, payment_ids=[pending, closed])
        result = self.request("POST", "/claim/batch", data, extra_headers=headers)
        message = json.loads(result.text)["message"]
        self.assertIn("已认领 1 笔", message)
        self.assertIn("跳过 1 笔", message)
        self.assertIn(f"#{closed}", message)
        self.assertEqual(self.request("GET", "/admin/export/today-text", authenticated=False, extra_headers=headers).status_code, 401)

    def test_browser_get_error_is_readable_but_api_error_stays_json(self):
        url = "/me?start_date=2026-09-20&end_date=2026-09-01"
        response = self.request("GET", url, extra_headers=[(b"accept", b"text/html")])
        self.assertEqual(response.status_code, 400)
        self.assertIn("返回上一页", response.text)
        self.assertIn("结束日期不能早于开始日期", response.text)
        response = self.request("GET", url)
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", json.loads(response.text))


if __name__ == "__main__":
    unittest.main()

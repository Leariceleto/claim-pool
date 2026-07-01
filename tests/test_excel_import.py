import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path

from openpyxl import Workbook


def install_fastapi_stub() -> None:
    try:
        import fastapi  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    fastapi_module = types.ModuleType("fastapi")
    responses_module = types.ModuleType("fastapi.responses")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda func: func

        def post(self, *args, **kwargs):
            return lambda func: func

        def middleware(self, *args, **kwargs):
            return lambda func: func

        def on_event(self, *args, **kwargs):
            return lambda func: func

    def form_or_file(default=None, *args, **kwargs):
        return default

    class Request:
        pass

    class UploadFile:
        pass

    class Response:
        pass

    class HTMLResponse(Response):
        pass

    class RedirectResponse(Response):
        pass

    class FileResponse(Response):
        pass

    fastapi_module.FastAPI = FastAPI
    fastapi_module.File = form_or_file
    fastapi_module.Form = form_or_file
    fastapi_module.HTTPException = HTTPException
    fastapi_module.Request = Request
    fastapi_module.UploadFile = UploadFile
    responses_module.FileResponse = FileResponse
    responses_module.HTMLResponse = HTMLResponse
    responses_module.RedirectResponse = RedirectResponse
    responses_module.Response = Response
    sys.modules["fastapi"] = fastapi_module
    sys.modules["fastapi.responses"] = responses_module


class ExcelImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_fastapi_stub()
        cls.app = importlib.import_module("app")

    def test_ooxml_workbook_with_xls_suffix_imports(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["到款日期", "付款方名称", "到款金额", "银行备注", "流水号"])
        sheet.append(["2026-06-25", "北京测试教育科技有限公司", 1200, "银行导出", "MX001"])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bank_export.xls"
            workbook.save(path)
            rows = self.app.rows_from_excel(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["received_date"], "2026-06-25")
        self.assertEqual(rows[0]["payer_name"], "北京测试教育科技有限公司")
        self.assertEqual(rows[0]["amount"], "1200")
        self.assertEqual(rows[0]["serial_no"], "MX001")

    def test_bank_transaction_headers_import(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["交易时间", "终端号", "交易类型", "卡号", "交易金额", "流水号", "银商订单号", "付款附言"])
        sheet.append([
            "2026-06-24 14:13:31",
            "50351488",
            "消费",
            "000000******0000",
            "999",
            "304143",
            "26062457713041431031801750",
            "上海市虹口区特教指导中心 骨干教师",
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bank_export.xls"
            workbook.save(path)
            rows = self.app.rows_from_excel(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["received_date"], "2026-06-24 14:13:31")
        self.assertEqual(rows[0]["received_time"], "14:13:31")
        self.assertEqual(rows[0]["payer_name"], "上海市虹口区特教指导中心 骨干教师")
        self.assertEqual(rows[0]["bank_note"], "上海市虹口区特教指导中心 骨干教师")
        self.assertEqual(rows[0]["amount"], "999")
        self.assertEqual(rows[0]["serial_no"], "304143")

    def test_income_header_imports_as_amount(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["到款日期", "付款方名称", "收入", "银行备注"])
        sheet.append(["2026-06-25", "广州测试学校", 1980, "报名费"])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "income_header.xlsx"
            workbook.save(path)
            rows = self.app.rows_from_excel(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "1980")

    def test_receiver_company_header_imports(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["到款日期", "付款方名称", "到款公司", "到款金额", "银行备注"])
        sheet.append(["2026-06-25", "深圳测试学校", "北京蒲公英教育科技有限公司", 3980, "参会费"])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "receiver_company.xlsx"
            workbook.save(path)
            rows = self.app.rows_from_excel(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["receiver_company"], "北京蒲公英教育科技有限公司")

    def test_bulk_close_payments_skips_closed_and_logs(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                payment_id INTEGER,
                detail_json TEXT NOT NULL,
                ip TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO payments (id, status, closed_at) VALUES (?, ?, ?)",
            [
                (1, "pending", None),
                (2, "closed", "2026-06-20 10:00:00"),
                (3, "draft", ""),
            ],
        )
        actor = {"id": "finance", "name": "财务管理员", "role": "finance"}

        detail = self.app.close_payments_bulk(conn, actor, [1, 2, 2, 3, 999])

        rows = conn.execute("SELECT id, status, closed_at FROM payments ORDER BY id").fetchall()
        self.assertEqual([row["status"] for row in rows], ["closed", "closed", "closed"])
        self.assertTrue(rows[0]["closed_at"])
        self.assertEqual(rows[1]["closed_at"], "2026-06-20 10:00:00")
        self.assertTrue(rows[2]["closed_at"])
        self.assertEqual(detail["closed_ids"], [1, 3])
        self.assertEqual(detail["skipped_closed_ids"], [2])
        self.assertEqual(detail["missing_ids"], [999])

        audit_row = conn.execute("SELECT action, detail_json FROM audit_logs").fetchone()
        self.assertEqual(audit_row["action"], "bulk_close_payments")
        audit_detail = json.loads(audit_row["detail_json"])
        self.assertEqual(audit_detail["count"], 2)

    def test_accept_payment_claims_confirms_pending_claims_and_refreshes_payment(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL,
                claimed_department TEXT,
                claimed_team TEXT,
                claimed_by TEXT,
                claimed_by_name TEXT,
                claimed_at TEXT,
                customer_project TEXT,
                claim_note TEXT,
                finance_note TEXT
            );
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                payment_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                payment_id INTEGER,
                detail_json TEXT NOT NULL,
                ip TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO payments
                (id, amount_cents, status, claimed_department, claimed_team, claimed_by,
                 claimed_by_name, claimed_at, customer_project, claim_note, finance_note)
            VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (1, 100668, "pending_confirm"),
        )
        conn.executemany(
            "INSERT INTO claims (id, payment_id, amount_cents, status) VALUES (?, ?, ?, ?)",
            [(1, 1, 100668, "pending")],
        )
        actor = {"id": "finance", "name": "财务管理员", "role": "admin"}

        detail = self.app.accept_payment_claims(conn, actor, 1)

        payment = conn.execute("SELECT status, claimed_by_name, finance_note FROM payments WHERE id = 1").fetchone()
        claim = conn.execute("SELECT status FROM claims WHERE id = 1").fetchone()
        audit_row = conn.execute("SELECT action, detail_json FROM audit_logs").fetchone()

        self.assertEqual(detail["count"], 1)
        self.assertEqual(claim["status"], "accepted")
        self.assertEqual(payment["status"], "claimed")
        self.assertEqual(payment["claimed_by_name"], "财务确认")
        self.assertEqual(payment["finance_note"], "部分认领已确认完成")
        self.assertEqual(audit_row["action"], "accept_payment_claims")

    def test_cancel_my_claim_returns_payment_to_pending(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL,
                claimed_department TEXT,
                claimed_team TEXT,
                claimed_by TEXT,
                claimed_by_name TEXT,
                claimed_at TEXT,
                customer_project TEXT,
                claim_note TEXT,
                finance_note TEXT
            );
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                payment_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                actor_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                payment_id INTEGER,
                detail_json TEXT NOT NULL,
                ip TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO payments
                (id, amount_cents, status, claimed_department, claimed_team, claimed_by,
                 claimed_by_name, claimed_at, customer_project, claim_note, finance_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                776000,
                "claimed",
                "中国教育创新年会事业部",
                "创新年会",
                "user-a",
                "测试用户",
                "2026-07-01 11:09:00",
                "报名费",
                "",
                "",
            ),
        )
        conn.execute(
            "INSERT INTO claims (id, payment_id, amount_cents, actor_id, status) VALUES (?, ?, ?, ?, ?)",
            (1, 1, 776000, "user-a", "accepted"),
        )
        actor = {"id": "user-a", "name": "测试用户", "role": "claimant"}

        detail = self.app.cancel_my_claim(conn, actor, 1)

        payment = conn.execute("SELECT status, claimed_by, claimed_department FROM payments WHERE id = 1").fetchone()
        claim = conn.execute("SELECT status FROM claims WHERE id = 1").fetchone()
        audit_row = conn.execute("SELECT action, detail_json FROM audit_logs").fetchone()

        self.assertEqual(detail["payment_status"], "pending")
        self.assertEqual(claim["status"], "canceled")
        self.assertEqual(payment["status"], "pending")
        self.assertIsNone(payment["claimed_by"])
        self.assertIsNone(payment["claimed_department"])
        self.assertEqual(audit_row["action"], "cancel_my_claim")

    def test_cancel_my_claim_rejects_other_users_claim(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL,
                claimed_department TEXT,
                claimed_team TEXT,
                claimed_by TEXT,
                claimed_by_name TEXT,
                claimed_at TEXT,
                customer_project TEXT,
                claim_note TEXT,
                finance_note TEXT
            );
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                payment_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                actor_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                payment_id INTEGER,
                detail_json TEXT NOT NULL,
                ip TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO payments
                (id, amount_cents, status, claimed_department, claimed_team, claimed_by,
                 claimed_by_name, claimed_at, customer_project, claim_note, finance_note)
            VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (1, 10000, "pending_confirm"),
        )
        conn.execute(
            "INSERT INTO claims (id, payment_id, amount_cents, actor_id, status) VALUES (?, ?, ?, ?, ?)",
            (1, 1, 10000, "user-a", "pending"),
        )

        with self.assertRaises(self.app.HTTPException) as ctx:
            self.app.cancel_my_claim(conn, {"id": "user-b", "name": "其他用户", "role": "claimant"}, 1)

        self.assertEqual(ctx.exception.status_code, 403)
        claim = conn.execute("SELECT status FROM claims WHERE id = 1").fetchone()
        self.assertEqual(claim["status"], "pending")

    def test_submit_split_claims_creates_pending_lines_and_refreshes_payment(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL,
                claimed_department TEXT,
                claimed_team TEXT,
                claimed_by TEXT,
                claimed_by_name TEXT,
                claimed_at TEXT,
                customer_project TEXT,
                claim_note TEXT,
                finance_note TEXT
            );
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER NOT NULL,
                department TEXT NOT NULL,
                team TEXT,
                amount_cents INTEGER NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                customer_project TEXT,
                contract_invoice TEXT,
                note TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                payment_id INTEGER,
                detail_json TEXT NOT NULL,
                ip TEXT
            );
            """
        )
        department = self.app.DEPARTMENTS[0]
        team = next(iter(self.app.CATALOG[department]))
        projects = self.app.CATALOG[department][team][:2]
        if len(projects) < 2:
            projects = [projects[0], projects[0]]
        conn.execute(
            """
            INSERT INTO payments
                (id, amount_cents, status, claimed_department, claimed_team, claimed_by,
                 claimed_by_name, claimed_at, customer_project, claim_note, finance_note)
            VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (1, 10000, "pending"),
        )
        actor = {"id": "split-user", "name": "分摊同事", "role": "claimant"}

        detail = self.app.submit_split_claims(
            conn,
            actor,
            1,
            [department, department],
            [team, team],
            [projects[0], projects[1]],
            ["60", "40"],
            ["2本杂志", "2个参会名额"],
        )

        payment = conn.execute("SELECT status, finance_note FROM payments WHERE id = 1").fetchone()
        claim_rows = conn.execute("SELECT amount_cents, status, note FROM claims ORDER BY id").fetchall()
        audit_row = conn.execute("SELECT action, detail_json FROM audit_logs").fetchone()

        self.assertEqual(detail["count"], 2)
        self.assertEqual(detail["amount_cents"], 10000)
        self.assertEqual(detail["payment_status"], "pending_confirm")
        self.assertEqual(payment["status"], "pending_confirm")
        self.assertEqual(payment["finance_note"], "部分认领金额已覆盖整笔到款，待管理员确认")
        self.assertEqual([row["amount_cents"] for row in claim_rows], [6000, 4000])
        self.assertEqual([row["status"] for row in claim_rows], ["pending", "pending"])
        self.assertEqual([row["note"] for row in claim_rows], ["2本杂志", "2个参会名额"])
        self.assertEqual(audit_row["action"], "split_claim_submit")

    def test_admin_payment_sort_clause_is_whitelisted(self) -> None:
        clause, sort, direction = self.app.admin_payment_order_clause("amount", "asc")
        self.assertEqual(sort, "amount")
        self.assertEqual(direction, "asc")
        self.assertEqual(clause, "amount_cents ASC, id ASC")

        clause, sort, direction = self.app.admin_payment_order_clause("bad; DROP TABLE payments", "sideways")
        self.assertEqual(sort, "id")
        self.assertEqual(direction, "desc")
        self.assertEqual(clause, "id DESC")

        clause, sort, direction = self.app.admin_payment_order_clause("status", "desc")
        self.assertEqual(sort, "status")
        self.assertEqual(direction, "desc")
        self.assertIn("CASE status", clause)
        self.assertTrue(clause.endswith("DESC, id DESC"))

    def test_admin_sort_header_supports_partial_table_refresh(self) -> None:
        header = self.app.admin_sort_th("金额", "amount", "id", "desc")

        self.assertIn('href="/admin?sort=amount&amp;dir=asc"', header)
        self.assertIn('data-table-url="/admin/payments/table?sort=amount&amp;dir=asc"', header)
        self.assertIn("sort-link", header)

    def test_general_manager_is_identity_not_admin_permission(self) -> None:
        old_db_path = self.app.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                self.app.DB_PATH = Path(tmpdir) / "roles.db"
                self.app.init_db()
                with self.app.get_conn() as conn:
                    conn.executemany(
                        """
                        INSERT INTO app_users (open_id, name, managed_role, is_admin, created_at, last_login)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            ("gm-user", "事业部总经理", "general_manager", 0, "2026-06-25 10:00:00", "2026-06-25 10:00:00"),
                            ("legacy-admin", "旧管理员", "claimant", 1, "2026-06-25 10:00:00", "2026-06-25 10:00:00"),
                        ],
                    )

                self.assertEqual(self.app.compute_role("gm-user"), "general_manager")
                self.assertEqual(self.app.compute_role("legacy-admin"), "admin")
                with self.assertRaises(self.app.HTTPException):
                    self.app.require_admin({"role": "general_manager"})
                with self.assertRaises(self.app.HTTPException):
                    self.app.require_superadmin({"role": "general_manager"})
        finally:
            self.app.DB_PATH = old_db_path

    def test_personal_dashboard_respects_role_scope(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                received_date TEXT,
                payer_name TEXT,
                receiver_company TEXT,
                bank_note TEXT,
                amount_cents INTEGER NOT NULL,
                claimed_department TEXT,
                status TEXT NOT NULL
            );
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                payment_id INTEGER NOT NULL,
                department TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO payments (id, received_date, payer_name, receiver_company, bank_note, amount_cents, claimed_department, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "2026-06-25", "未认领客户", "蒲公英教育科技", "测试摘要1", 10000, "", "pending"),
                (2, "2026-06-25", "年会客户A", "蒲公英智库", "测试摘要2", 20000, "年会事业部", "claimed"),
                (3, "2026-06-25", "混合客户", "蒲公英教育科技", "测试摘要3", 30000, "", "pending_confirm"),
                (4, "2026-06-25", "关闭客户", "蒲公英教育科技", "测试摘要4", 99900, "", "closed"),
            ],
        )
        department = self.app.DEPARTMENTS[0]
        other_department = self.app.DEPARTMENTS[1] if len(self.app.DEPARTMENTS) > 1 else "其他部门"
        conn.executemany(
            "INSERT INTO claims (id, payment_id, department, actor_id, amount_cents, status) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 2, department, "user-a", 20000, "accepted"),
                (2, 3, department, "user-b", 12000, "pending"),
                (3, 3, other_department, "user-c", 18000, "pending"),
                (4, 4, department, "user-a", 99900, "accepted"),
            ],
        )
        today = date(2026, 6, 25)

        admin_dashboard = self.app.personal_dashboard_data(
            conn,
            {"id": "admin", "role": "admin", "department": "未设置部门"},
            today,
        )[0]
        gm_dashboard = self.app.personal_dashboard_data(
            conn,
            {"id": "gm", "role": "general_manager", "department": department},
            today,
        )[0]
        user_department_dashboard = self.app.personal_dashboard_data(
            conn,
            {"id": "user-b", "role": "claimant", "department": department},
            today,
        )[0]
        user_all_dashboard = self.app.personal_dashboard_data(
            conn,
            {
                "id": "user-b",
                "role": "claimant",
                "department": department,
                "dashboard_scope_label": "全部角色",
                "dashboard_scopes": [
                    {"department": department, "team": ""},
                    {"department": other_department, "team": ""},
                ],
            },
            today,
        )[0]

        self.assertEqual(admin_dashboard["total_cents"], 60000)
        self.assertIn(("未认领", 10000), admin_dashboard["departments"])
        self.assertIn((department, 32000), admin_dashboard["departments"])
        self.assertIn((other_department, 18000), admin_dashboard["departments"])
        self.assertEqual(gm_dashboard["total_cents"], 32000)
        self.assertEqual(gm_dashboard["departments"], [(department, 32000)])
        self.assertEqual(user_department_dashboard["total_cents"], 32000)
        self.assertEqual(user_department_dashboard["departments"], [(department, 32000)])
        self.assertIn(("混合客户", 12000), user_department_dashboard["customers"])
        self.assertIn(("年会客户A", 20000), user_department_dashboard["customers"])
        self.assertTrue(all(row["department"] == department for row in user_department_dashboard["rows"]))
        self.assertFalse(any(row["department"] == other_department for row in user_department_dashboard["rows"]))
        self.assertEqual(user_all_dashboard["total_cents"], 50000)
        self.assertIn((department, 32000), user_all_dashboard["departments"])
        self.assertIn((other_department, 18000), user_all_dashboard["departments"])

    def test_profile_setup_modal_only_for_authed_users_without_complete_profile(self) -> None:
        actor = {"id": "u1", "name": "测试用户", "role": "claimant", "authed": "1"}

        missing_html = self.app.profile_setup_modal_html(actor, "", "")
        self.assertIn("首次使用，请先设置部门和中心", missing_html)
        self.assertIn('action="/me/profile"', missing_html)

        department = self.app.DEPARTMENTS[0]
        team = next(iter(self.app.CATALOG[department]))
        complete_html = self.app.profile_setup_modal_html(actor, department, team)
        demo_html = self.app.profile_setup_modal_html({**actor, "authed": ""}, "", "")

        self.assertEqual(complete_html, "")
        self.assertEqual(demo_html, "")

    def test_diagnostic_log_html_has_copyable_non_secret_context(self) -> None:
        department = self.app.DEPARTMENTS[0]
        team = next(iter(self.app.CATALOG[department]))
        html = self.app.diagnostic_log_html(
            {
                "id": "ou_test_user",
                "name": "测试用户",
                "role": "general_manager",
                "department": department,
                "team": team,
                "authed": "1",
            },
            department,
            team,
        )

        self.assertIn("问题排查日志", html)
        self.assertIn('id="diagnostic-log-base"', html)
        self.assertIn('id="copy-diagnostic-log"', html)
        self.assertNotIn("<textarea", html)
        self.assertNotIn("请用户补充", html)
        self.assertIn("事业部总经理", html)
        self.assertIn(department, html)
        self.assertNotIn("cookie", html.lower())
        self.assertNotIn("secret", html.lower())

    def test_profile_self_setup_locks_after_department_and_team_are_complete(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE user_profiles (
                open_id TEXT PRIMARY KEY,
                name TEXT,
                department TEXT,
                team TEXT,
                updated_at TEXT
            );
            """
        )

        self.assertTrue(self.app.can_self_set_profile(conn, "new-user"))
        conn.execute(
            "INSERT INTO user_profiles (open_id, name, department, team, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("partial-user", "待补全", self.app.DEPARTMENTS[0], "", "2026-06-26 10:00:00"),
        )
        self.assertTrue(self.app.can_self_set_profile(conn, "partial-user"))

        department = self.app.DEPARTMENTS[0]
        team = next(iter(self.app.CATALOG[department]))
        conn.execute(
            "INSERT INTO user_profiles (open_id, name, department, team, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("complete-user", "已完成", department, team, "2026-06-26 10:00:00"),
        )
        self.assertFalse(self.app.can_self_set_profile(conn, "complete-user"))


if __name__ == "__main__":
    unittest.main()

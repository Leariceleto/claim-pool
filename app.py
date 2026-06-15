import base64
import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse


APP_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """轻量读取 .env 到 os.environ，无第三方依赖。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(APP_DIR / ".env")

DB_PATH = Path(os.environ.get("CLAIM_POOL_DB", APP_DIR / "claim_pool.db"))
UPLOAD_DIR = Path(os.environ.get("CLAIM_UPLOAD_DIR", APP_DIR / "uploads"))
SEARCH_LIMIT = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".csv", ".xls", ".xlsx"}
BROAD_TERMS = {"公司", "有限公司", "集团", "科技", "教育", "转账", "付款", "收入"}

# 部门 → 中心/小组 → 项目 三级分类，来源：智库产品分类.xlsx
# 更新分类时直接替换 catalog.json 即可，无需改代码
CATALOG_PATH = Path(os.environ.get("CLAIM_CATALOG", APP_DIR / "catalog.json"))
CATALOG: dict[str, dict[str, list[str]]] = (
    json.loads(CATALOG_PATH.read_text("utf-8")) if CATALOG_PATH.is_file() else {}
)
DEPARTMENTS = list(CATALOG)

# ── 飞书登录配置 ──────────────────────────────────────────────
# 这些值从 .env 读取（见 .env.example）。未配置时飞书登录入口自动隐藏，
# 系统仍可用 URL 参数身份（本地开发/演示）访问。
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_REDIRECT_URI = os.environ.get("FEISHU_REDIRECT_URI", "")
FEISHU_SCOPE = os.environ.get("FEISHU_SCOPE", "contact:user.base:readonly")
# 用 open_id 白名单指定哪些人登录后是管理员（财务、总经理、部门负责人等），逗号分隔
FEISHU_ADMIN_OPEN_IDS = {
    x.strip() for x in os.environ.get("FEISHU_ADMIN_OPEN_IDS", "").split(",") if x.strip()
}
# 给会话 cookie 签名用，必须保密；未设置则随机生成（重启后旧会话失效）
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32)).encode()
SESSION_COOKIE = "claim_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 天

FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USERINFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


def feishu_enabled() -> bool:
    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_REDIRECT_URI)


app = FastAPI(title="飞书到款认领系统 MVP")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    UPLOAD_DIR.mkdir(exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                raw_count INTEGER NOT NULL DEFAULT 0,
                imported_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER,
                imported_at TEXT NOT NULL,
                confirmed_at TEXT,
                received_date TEXT,
                received_time TEXT,
                payer_name TEXT,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                bank_note TEXT,
                receiver_account TEXT,
                serial_no TEXT,
                source_ref TEXT,
                confidence REAL NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft',
                claimed_department TEXT,
                claimed_by TEXT,
                claimed_by_name TEXT,
                claimed_at TEXT,
                customer_project TEXT,
                contract_invoice TEXT,
                claim_note TEXT,
                finance_note TEXT,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER NOT NULL,
                department TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                customer_project TEXT,
                contract_invoice TEXT,
                note TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
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

            CREATE TABLE IF NOT EXISTS user_profiles (
                open_id TEXT PRIMARY KEY,
                name TEXT,
                department TEXT,
                team TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(received_date);
            CREATE INDEX IF NOT EXISTS idx_claims_payment ON claims(payment_id);
            CREATE INDEX IF NOT EXISTS idx_audit_payment ON audit_logs(payment_id);
            """
        )
        ensure_column(conn, "payments", "claimed_team", "claimed_team TEXT")
        ensure_column(conn, "claims", "team", "team TEXT")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


@app.on_event("startup")
def startup() -> None:
    init_db()


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def url(path: str, **params: str) -> str:
    return f"{path}?{urlencode(params)}"


def money(cents: Optional[int]) -> str:
    return f"{(cents or 0) / 100:,.2f}"


def parse_amount(value: Any) -> int:
    text = str(value or "").strip()
    text = text.replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text:
        return 0
    try:
        return int((Decimal(text) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return 0


def parse_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("/", "-").replace(".", "-")
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    match = re.search(r"(\d{1,2})-(\d{1,2})", text)
    if match:
        m, d = match.groups()
        return f"{datetime.now().year}-{int(m):02d}-{int(d):02d}"
    return text


def save_attachment(attachment: Optional[UploadFile]) -> str:
    if not attachment or not attachment.filename:
        return ""

    original_name = Path(attachment.filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="附件格式暂不支持")

    content = attachment.file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=400, detail="附件不能超过 20MB")

    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(original_name).stem).strip("._")
    if not safe_stem:
        safe_stem = "attachment"
    stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}_{safe_stem}{suffix}"
    target = UPLOAD_DIR / stored_name
    target.write_bytes(content)
    return f"uploads/{stored_name}"


def attachment_link(source_ref: str, actor: dict[str, str]) -> str:
    if not source_ref or not source_ref.startswith("uploads/"):
        return ""
    filename = Path(source_ref).name
    href = url(f"/attachments/{filename}", role=actor["role"], user=actor["id"], name=actor["name"])
    return f'<a href="{esc(href)}" target="_blank">查看附件</a>'


# ── 会话（签名 cookie，无第三方依赖） ───────────────────────────
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(data: dict[str, Any]) -> str:
    payload = dict(data)
    payload["exp"] = int(time.time()) + SESSION_MAX_AGE
    body = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(SESSION_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str) -> Optional[dict[str, Any]]:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(SESSION_SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(body))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


# ── 飞书 API 调用（标准库 urllib，自动尊重环境代理） ──────────────
def _feishu_request(
    url: str, method: str = "GET", json_body: Optional[dict] = None, bearer: str = ""
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"error": "http_error", "status": exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"error": "network_error", "message": str(exc)}


def feishu_exchange_token(code: str) -> str:
    resp = _feishu_request(
        FEISHU_TOKEN_URL,
        "POST",
        {
            "grant_type": "authorization_code",
            "client_id": FEISHU_APP_ID,
            "client_secret": FEISHU_APP_SECRET,
            "code": code,
            "redirect_uri": FEISHU_REDIRECT_URI,
        },
    )
    return resp.get("access_token", "")


def feishu_get_userinfo(user_token: str) -> dict[str, Any]:
    resp = _feishu_request(FEISHU_USERINFO_URL, "GET", bearer=user_token)
    return resp.get("data") or {}


def get_user_profile(open_id: str) -> Optional[sqlite3.Row]:
    if not open_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT department, team FROM user_profiles WHERE open_id = ?", (open_id,)
        ).fetchone()


def actor_from_request(request: Request) -> dict[str, str]:
    session = read_session(request.cookies.get(SESSION_COOKIE, ""))
    if session:
        open_id = session.get("id", "feishu-user")
        profile = get_user_profile(open_id)
        department = (profile["department"] if profile else "") or ""
        team = (profile["team"] if profile else "") or ""
        return {
            "id": open_id,
            "name": session.get("name", "飞书用户"),
            "role": session.get("role", "claimant"),
            "department": department or "未设置部门",
            "team": team,
            "authed": "1",
        }
    params = request.query_params
    role = params.get("role") or request.headers.get("x-role") or "claimant"
    return {
        "id": params.get("user") or request.headers.get("x-user-id") or "demo-user",
        "name": params.get("name") or request.headers.get("x-user-name") or "演示用户",
        "role": role,
        "department": params.get("department") or request.headers.get("x-department") or "未设置部门",
        "team": params.get("team") or "",
        "authed": "",
    }


def actor_from_form(
    user: str,
    name: str,
    role: str,
    department: str = "",
) -> dict[str, str]:
    return {
        "id": user or "demo-user",
        "name": name or "演示用户",
        "role": role or "claimant",
        "department": department or "未设置部门",
    }


def require_admin(actor: dict[str, str]) -> None:
    if actor["role"] not in {"finance", "admin"}:
        raise HTTPException(status_code=403, detail="只有管理员可以访问这个页面")


def audit(
    conn: sqlite3.Connection,
    actor: dict[str, str],
    action: str,
    payment_id: Optional[int] = None,
    detail: Optional[dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs
            (at, actor_id, actor_name, actor_role, action, payment_id, detail_json, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_text(),
            actor["id"],
            actor["name"],
            actor["role"],
            action,
            payment_id,
            json.dumps(detail or {}, ensure_ascii=False),
            request.client.host if request and request.client else "",
        ),
    )


BASE_CSS = """
    :root {
      color-scheme: light;
      --bg:#f3f5f8; --card:#ffffff;
      --line:#e5e8ee; --line-strong:#cfd5df;
      --text:#1a2334; --muted:#5d6b84; --faint:#8d99ad;
      --primary:#2456d6; --primary-dark:#1b44b0; --primary-soft:#e9effc;
      --radius:12px;
      --shadow:0 1px 2px rgba(23,32,51,.05), 0 1px 3px rgba(23,32,51,.06);
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text);
      font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; }
    a { color:var(--primary); text-decoration:none; }
    a:hover { text-decoration:underline; }

    header { position:sticky; top:0; z-index:10; background:rgba(255,255,255,.92); backdrop-filter:blur(8px);
      border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between;
      gap:16px; padding:10px 28px; }
    .brand { display:flex; align-items:center; gap:10px; }
    .brand-mark { width:34px; height:34px; border-radius:9px; background:linear-gradient(135deg,#2456d6,#4f7df7);
      color:#fff; display:flex; align-items:center; justify-content:center; font-size:17px; font-weight:700; }
    .brand-text { display:flex; flex-direction:column; line-height:1.3; }
    .brand-text strong { font-size:15px; }
    .brand-text small { font-size:11px; color:var(--faint); }
    nav { display:flex; gap:6px; }
    nav a { padding:7px 14px; border-radius:8px; color:var(--muted); font-weight:500; }
    nav a:hover { background:#f0f3f8; color:var(--text); text-decoration:none; }
    nav a.active { background:var(--primary-soft); color:var(--primary); }
    .login-btn { display:inline-block; padding:7px 16px; border-radius:8px; background:var(--primary);
      color:#fff; font-weight:500; white-space:nowrap; }
    .login-btn:hover { background:var(--primary-dark); text-decoration:none; }
    .user-area { white-space:nowrap; font-size:13px; }

    main { max-width:1180px; margin:0 auto; padding:28px 28px 64px; }
    .page-head { margin-bottom:20px; }
    h1 { font-size:24px; margin:0; letter-spacing:-.01em; }
    .page-sub { margin:4px 0 0; color:var(--muted); font-size:13px; }
    h2 { font-size:16px; margin:32px 0 12px; display:flex; align-items:center; gap:8px; }
    h2::before { content:""; width:4px; height:16px; border-radius:2px; background:var(--primary); }

    .panel { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
      padding:20px; margin-bottom:20px; box-shadow:var(--shadow); }
    .muted { color:var(--muted); font-size:12.5px; }
    .hint { color:var(--faint); font-size:12.5px; margin:12px 0 0; }
    .nowrap { white-space:nowrap; }

    .row { display:flex; gap:12px; flex-wrap:wrap; align-items:end; }
    .field { margin-bottom:10px; }
    label { display:block; font-size:12.5px; font-weight:500; color:var(--muted); margin:0 0 6px; }
    input, select, textarea { width:100%; border:1px solid var(--line-strong); border-radius:8px;
      padding:9px 12px; font:inherit; background:#fff; color:var(--text);
      transition:border-color .15s, box-shadow .15s; }
    input:focus, select:focus, textarea:focus { outline:none; border-color:var(--primary);
      box-shadow:0 0 0 3px rgba(36,86,214,.13); }
    textarea { min-height:140px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px; }
    input[type=file] { padding:7px; background:#fafbfd; }
    button { border:0; border-radius:8px; padding:9px 18px; background:var(--primary); color:#fff;
      font:inherit; font-weight:500; cursor:pointer; transition:background .15s; }
    button:hover { background:var(--primary-dark); }
    button.secondary { background:#fff; color:var(--text); border:1px solid var(--line-strong); }
    button.secondary:hover { background:#f4f6fa; }
    button.danger { background:#c2362b; }

    .table-wrap { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
      box-shadow:var(--shadow); margin-bottom:20px; overflow-x:auto; }
    table { width:100%; border-collapse:collapse; }
    th { padding:10px 16px; background:#f8fafc; border-bottom:1px solid var(--line);
      font-size:12px; font-weight:600; color:var(--muted); text-align:left; white-space:nowrap; }
    td { padding:13px 16px; border-bottom:1px solid var(--line); font-size:13px; vertical-align:top; }
    tbody tr:last-child td { border-bottom:0; }
    tbody tr:hover { background:#fafbfd; }
    td.num { font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap; }
    td.empty { padding:36px; text-align:center; color:var(--faint); }
    .code { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:11.5px;
      color:var(--muted); word-break:break-all; }

    .status { display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px;
      font-size:12px; font-weight:500; background:#eef1f5; color:#475569; white-space:nowrap; }
    .status::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; flex:none; }
    .status.draft { background:#e8edf5; color:#3b5275; }
    .status.pending { background:#fdf2d9; color:#9a6700; }
    .status.claimed { background:#dcf5e7; color:#13794c; }
    .status.pending_confirm { background:#fdeaec; color:#b4232e; }
    .status.rejected { background:#efeff2; color:#6b7280; }
    .status.closed { background:#e7e9fd; color:#4341c8; }

    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:14px; margin-bottom:8px; }
    .stat { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
      padding:16px 18px; box-shadow:var(--shadow); }
    .stat-label { font-size:12.5px; color:var(--muted); display:flex; align-items:center; gap:7px; }
    .stat-label::before { content:""; width:8px; height:8px; border-radius:50%; background:var(--dot,#94a3b8); flex:none; }
    .stat strong { display:block; font-size:26px; font-weight:650; margin:8px 0 2px;
      font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
    .stat-amount { font-size:12px; color:var(--faint); font-variant-numeric:tabular-nums; }

    .callout { background:#fff; border:1px solid var(--line); border-left:4px solid var(--faint);
      border-radius:10px; padding:14px 18px; margin-bottom:20px; font-size:13.5px; box-shadow:var(--shadow); }
    .callout.warn { border-left-color:#e3a008; background:#fffdf5; }
    .callout.info { border-left-color:var(--primary); }

    details.fold { border:1px solid var(--line); border-radius:10px; background:#fafbfd; margin-bottom:8px; }
    details.fold:last-child { margin-bottom:0; }
    details.fold summary { cursor:pointer; padding:8px 12px; font-size:12.5px; font-weight:500;
      color:var(--primary); list-style:none; display:flex; align-items:center; gap:6px; user-select:none; }
    details.fold summary::-webkit-details-marker { display:none; }
    details.fold summary::before { content:"\\25B8"; font-size:11px; transition:transform .15s; }
    details.fold[open] summary::before { transform:rotate(90deg); }
    details.fold .fold-body { padding:10px 12px 12px; border-top:1px dashed var(--line); }
    .actions { min-width:300px; }

    @media (max-width: 760px) {
      header { padding:10px 16px; }
      main { padding:20px 16px 48px; }
      .brand-text small { display:none; }
      table { min-width:680px; }
    }
"""


def identity_params(actor: Optional[dict[str, str]]) -> dict[str, str]:
    if not actor:
        return {}
    return {
        "user": actor["id"],
        "name": actor["name"],
        "department": actor["department"],
        "role": actor["role"],
    }


def page(
    title: str,
    body: str,
    active: str = "",
    subtitle: str = "",
    actor: Optional[dict[str, str]] = None,
) -> HTMLResponse:
    # 携带身份参数在页面间跳转，为后续接入飞书登录后透传用户身份做准备
    ident = identity_params(actor)
    search_href = url("/search", **ident) if ident else "/search"
    me_href = url("/me", **ident) if ident else "/me"
    if actor and actor["role"] in {"finance", "admin"}:
        admin_href = url("/admin", **ident)
    else:
        admin_href = url("/admin", role="admin", user="admin", name="管理员")
    nav = "".join(
        f'<a href="{esc(href)}" class="{"active" if key == active else ""}">{label}</a>'
        for href, label, key in [
            (search_href, "认领搜索", "search"),
            (me_href, "个人中心", "me"),
            (admin_href, "管理后台", "admin"),
        ]
    )
    if actor and actor.get("authed"):
        user_area = (
            f'<span class="muted">{esc(actor["name"])}</span>'
            f'<a href="/logout" style="margin-left:12px">退出</a>'
        )
    elif feishu_enabled():
        user_area = '<a href="/login" class="login-btn">飞书登录</a>'
    else:
        user_area = ""
    subtitle_html = f'<p class="page-sub">{esc(subtitle)}</p>' if subtitle else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} · 到款认领</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="brand-mark">¥</span>
      <span class="brand-text"><strong>到款认领</strong><small>到款认领管理系统</small></span>
    </div>
    <div style="display:flex; align-items:center; gap:18px">
      <nav>{nav}</nav>
      <div class="user-area">{user_area}</div>
    </div>
  </header>
  <main>
    <div class="page-head"><h1>{esc(title)}</h1>{subtitle_html}</div>
    {body}
  </main>
</body>
</html>"""
    )


HEADER_ALIASES = {
    "received_date": {"到款日期", "日期", "入账日期", "交易日期", "date"},
    "received_time": {"到款时间", "时间", "交易时间", "time"},
    "payer_name": {"付款方名称", "付款方", "对方户名", "对方名称", "客户名称", "payer", "payer_name"},
    "amount": {"到款金额", "金额", "收入金额", "贷方发生额", "入账金额", "amount"},
    "bank_note": {"银行备注", "摘要", "备注", "用途", "附言", "note", "remark"},
    "receiver_account": {"收款账户", "账号", "账户", "account"},
    "serial_no": {"流水号", "回单号", "凭证号", "serial_no", "serial"},
}


def canonical_header(header: str) -> str:
    key = header.strip().lower()
    for field, aliases in HEADER_ALIASES.items():
        if key in {alias.lower() for alias in aliases}:
            return field
    return key


def read_table(text: str) -> list[dict[str, str]]:
    sample = text.strip("\ufeff\n ")
    if not sample:
        return []
    dialect = csv.excel_tab if "\t" in sample.splitlines()[0] else csv.excel
    reader = csv.reader(io.StringIO(sample), dialect=dialect)
    rows = [list(row) for row in reader if any(str(cell).strip() for cell in row)]
    if not rows:
        return []

    first = [canonical_header(cell) for cell in rows[0]]
    has_header = bool({"received_date", "payer_name", "amount"} & set(first))
    data_rows = rows[1:] if has_header else rows
    headers = first if has_header else [
        "received_date",
        "payer_name",
        "amount",
        "bank_note",
        "serial_no",
        "receiver_account",
    ]

    result = []
    for row in data_rows:
        item = {}
        for index, value in enumerate(row):
            if index < len(headers):
                item[headers[index]] = value.strip()
        result.append(item)
    return result


def duplicate_exists(conn: sqlite3.Connection, item: dict[str, str], amount_cents: int) -> bool:
    serial_no = item.get("serial_no", "").strip()
    if serial_no:
        row = conn.execute("SELECT id FROM payments WHERE serial_no = ? LIMIT 1", (serial_no,)).fetchone()
        if row:
            return True
    row = conn.execute(
        """
        SELECT id FROM payments
        WHERE received_date = ?
          AND payer_name = ?
          AND amount_cents = ?
          AND COALESCE(bank_note, '') = ?
        LIMIT 1
        """,
        (
            parse_date(item.get("received_date")),
            item.get("payer_name", "").strip(),
            amount_cents,
            item.get("bank_note", "").strip(),
        ),
    ).fetchone()
    return row is not None


def status_badge(status: str) -> str:
    labels = {
        "draft": "待确认入池",
        "pending": "待认领",
        "claimed": "已认领",
        "pending_confirm": "待确认",
        "rejected": "已驳回",
        "closed": "已关闭",
    }
    return f'<span class="status {esc(status)}">{esc(labels.get(status, status))}</span>'


def department_select(
    name: str,
    selected: str = "",
    required: bool = False,
    allow_blank: bool = True,
    class_name: str = "",
) -> str:
    required_attr = " required" if required else ""
    class_attr = f' class="{esc(class_name)}"' if class_name else ""
    selected = selected if selected in DEPARTMENTS else ""
    options = []
    if allow_blank:
        blank_selected = " selected" if not selected else ""
        options.append(f'<option value=""{blank_selected}>请选择部门</option>')
    for department in DEPARTMENTS:
        option_selected = " selected" if department == selected else ""
        options.append(f'<option value="{esc(department)}"{option_selected}>{esc(department)}</option>')
    return f'<select name="{esc(name)}"{class_attr}{required_attr}>{"".join(options)}</select>'


def team_select(name: str, department: str, selected: str = "", required: bool = False) -> str:
    teams = list(CATALOG.get(department, {}))
    selected = selected if selected in teams else ""
    placeholder = "请选择中心/小组" if teams else "请先选择部门"
    req = " required" if required else ""
    options = [f'<option value="">{placeholder}</option>'] + [
        f'<option value="{esc(t)}"{" selected" if t == selected else ""}>{esc(t)}</option>' for t in teams
    ]
    return f'<select name="{esc(name)}" class="cs-team"{req}>{"".join(options)}</select>'


def project_select(name: str, department: str, team: str, selected: str = "", required: bool = False) -> str:
    projects = CATALOG.get(department, {}).get(team, [])
    selected = selected if selected in projects else ""
    placeholder = "请选择项目" if projects else "请先选择中心/小组"
    req = " required" if required else ""
    options = [f'<option value="">{placeholder}</option>'] + [
        f'<option value="{esc(p)}"{" selected" if p == selected else ""}>{esc(p)}</option>' for p in projects
    ]
    return f'<select name="{esc(name)}" class="cs-project"{req}>{"".join(options)}</select>'


def require_department(department: str) -> str:
    department = department.strip()
    if department not in DEPARTMENTS:
        raise HTTPException(status_code=400, detail="请选择标准部门")
    return department


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/search")


@app.get("/login")
def feishu_login(next: str = "/search") -> RedirectResponse:
    if not feishu_enabled():
        raise HTTPException(status_code=503, detail="尚未配置飞书登录")
    state = secrets.token_urlsafe(16)
    authorize = FEISHU_AUTHORIZE_URL + "?" + urlencode(
        {
            "app_id": FEISHU_APP_ID,
            "redirect_uri": FEISHU_REDIRECT_URI,
            "scope": FEISHU_SCOPE,
            "state": state,
        }
    )
    resp = RedirectResponse(authorize, status_code=302)
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax")
    safe_next = next if next.startswith("/") else "/search"
    resp.set_cookie("oauth_next", safe_next, max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/oauth/callback")
def feishu_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not feishu_enabled():
        raise HTTPException(status_code=503, detail="尚未配置飞书登录")
    saved_state = request.cookies.get("oauth_state", "")
    next_url = request.cookies.get("oauth_next", "/search")
    if not code or not state or not hmac.compare_digest(state, saved_state):
        raise HTTPException(status_code=400, detail="登录校验失败，请重新登录")
    token = feishu_exchange_token(code)
    if not token:
        raise HTTPException(status_code=400, detail="换取飞书令牌失败，请重试")
    info = feishu_get_userinfo(token)
    open_id = info.get("open_id")
    if not open_id:
        raise HTTPException(status_code=400, detail="获取飞书用户信息失败")
    name = info.get("name") or "飞书用户"
    role = "admin" if open_id in FEISHU_ADMIN_OPEN_IDS else "claimant"
    session = make_session({"id": open_id, "name": name, "role": role, "src": "feishu"})
    resp = RedirectResponse(next_url if next_url.startswith("/") else "/search", status_code=302)
    resp.set_cookie(SESSION_COOKIE, session, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    resp.delete_cookie("oauth_state")
    resp.delete_cookie("oauth_next")
    with get_conn() as conn:
        audit(conn, {"id": open_id, "name": name, "role": role}, "feishu_login", None, {"name": name}, request)
    return resp


@app.get("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/search", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/attachments/{filename}")
def view_attachment(request: Request, filename: str) -> FileResponse:
    actor = actor_from_request(request)
    require_admin(actor)
    if "/" in filename or "\\" in filename or filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="附件路径不合法")
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(path, filename=filename)


CASCADE_JS = """
function fillSelect(sel, items, placeholder) {
  if (!sel) return;
  sel.innerHTML = '';
  var opt = document.createElement('option');
  opt.value = '';
  opt.textContent = placeholder;
  sel.appendChild(opt);
  for (var i = 0; i < items.length; i++) {
    var o = document.createElement('option');
    o.value = items[i];
    o.textContent = items[i];
    sel.appendChild(o);
  }
}
document.addEventListener('change', function (e) {
  var form = e.target.closest('form');
  if (!form) return;
  if (e.target.classList.contains('cs-dept')) {
    fillSelect(form.querySelector('.cs-team'), Object.keys(CATALOG[e.target.value] || {}), '请选择中心/小组');
    fillSelect(form.querySelector('.cs-project'), [], '请先选择中心/小组');
  } else if (e.target.classList.contains('cs-team')) {
    var dept = form.querySelector('.cs-dept').value;
    fillSelect(form.querySelector('.cs-project'), (CATALOG[dept] || {})[e.target.value] || [], '请选择项目');
  }
});
"""


def catalog_script() -> str:
    return (
        "<script>var CATALOG = "
        + json.dumps(CATALOG, ensure_ascii=False)
        + ";"
        + CASCADE_JS
        + "</script>"
    )


def claim_form_html(row: sqlite3.Row, actor: dict[str, str]) -> str:
    my_dept = actor.get("department") if actor.get("department") in DEPARTMENTS else ""
    my_team = actor.get("team", "")
    dept_options = '<option value="">请选择部门</option>' + "".join(
        f'<option value="{esc(d)}"{" selected" if d == my_dept else ""}>{esc(d)}</option>' for d in DEPARTMENTS
    )
    return f"""
    <details class="fold">
      <summary>认领这笔款</summary>
      <div class="fold-body">
        <form method="post" action="/claim/{row['id']}">
          <input type="hidden" name="user" value="{esc(actor['id'])}">
          <input type="hidden" name="name" value="{esc(actor['name'])}">
          <input type="hidden" name="role" value="{esc(actor['role'])}">
          <div class="field"><label>认领部门</label><select name="department" class="cs-dept" required>{dept_options}</select></div>
          <div class="field"><label>中心 / 小组</label>{team_select("team", my_dept, my_team, required=True)}</div>
          <div class="field"><label>项目</label>{project_select("customer_project", my_dept, my_team, required=True)}</div>
          <div class="field"><label>备注说明</label><input name="note" placeholder="可选"></div>
          <button type="submit">提交认领</button>
        </form>
      </div>
    </details>
    """


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", pending_date: str = "") -> HTMLResponse:
    actor = actor_from_request(request)
    q = q.strip()
    pending_date = parse_date(pending_date) if pending_date.strip() else ""
    message = ""
    results: list[sqlite3.Row] = []

    if q:
        ok, reason = validate_search_query(q)
        with get_conn() as conn:
            if not ok:
                message = reason
                audit(conn, actor, "search_blocked", None, {"query": q, "reason": reason}, request)
            else:
                results = run_search(conn, q)
                audit(conn, actor, "search", None, {"query": q, "result_count": len(results)}, request)

    result_html = ""
    if message:
        result_html = f'<div class="callout warn"><strong>搜索被限制：</strong>{esc(message)}</div>'
    elif q and not results:
        result_html = '<div class="callout info">没有找到匹配记录。请换一个更具体的客户名、金额或备注关键词。</div>'
    elif results:
        rows = []
        for row in results:
            claim_hint = ""
            if row["status"] in {"claimed", "pending_confirm"}:
                claim_hint = f'<div class="muted">{esc(row["claimed_department"] or "")} · {esc(row["claimed_by_name"] or "")}</div>'
            rows.append(
                f"""
                <tr>
                  <td class="nowrap">{esc(row["received_date"])}</td>
                  <td class="num">¥ {money(row["amount_cents"])}</td>
                  <td><strong>{esc(row["payer_name"])}</strong></td>
                  <td>{esc(row["bank_note"])}</td>
                  <td>{status_badge(row["status"])}{claim_hint}</td>
                  <td class="actions">{claim_form_html(row, actor)}</td>
                </tr>
                """
            )
        result_html = f"""
        <div class="table-wrap">
        <table>
          <thead><tr><th>日期</th><th>金额</th><th>付款方</th><th>银行备注</th><th>状态</th><th style="width:300px">认领</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """

    with get_conn() as conn:
        if pending_date:
            pending_rows = conn.execute(
                """
                SELECT * FROM payments
                WHERE status = 'pending' AND received_date = ?
                ORDER BY id DESC
                LIMIT 50
                """,
                (pending_date,),
            ).fetchall()
            audit(conn, actor, "pending_filter", None, {"date": pending_date, "result_count": len(pending_rows)}, request)
        else:
            pending_rows = conn.execute(
                """
                SELECT * FROM payments
                WHERE status = 'pending'
                ORDER BY received_date DESC, id DESC
                LIMIT 50
                """
            ).fetchall()

    clear_href = url(
        "/search", q=q, user=actor["id"], name=actor["name"],
        department=actor["department"], role=actor["role"],
    )
    filter_form = f"""
    <form method="get" action="/search" class="row" style="margin:0 0 12px">
      <input type="hidden" name="q" value="{esc(q)}">
      <input type="hidden" name="user" value="{esc(actor['id'])}">
      <input type="hidden" name="name" value="{esc(actor['name'])}">
      <input type="hidden" name="department" value="{esc(actor['department'])}">
      <input type="hidden" name="role" value="{esc(actor['role'])}">
      <div><label>按到款日期筛选</label><input type="date" name="pending_date" value="{esc(pending_date)}"></div>
      <div><button type="submit" class="secondary">筛选</button></div>
      {f'<div><a href="{esc(clear_href)}" style="display:inline-block;padding:9px 0">清除筛选</a></div>' if pending_date else ''}
    </form>
    """

    if pending_rows:
        rows = [
            f"""
            <tr>
              <td class="nowrap">{esc(row["received_date"])}</td>
              <td><strong>{esc(row["payer_name"])}</strong></td>
              <td>{esc(row["bank_note"])}</td>
              <td>{status_badge(row["status"])}</td>
              <td class="actions">{claim_form_html(row, actor)}</td>
            </tr>
            """
            for row in pending_rows
        ]
        table_html = f"""
        <div class="table-wrap">
        <table>
          <thead><tr><th>日期</th><th>付款方</th><th>银行备注</th><th>状态</th><th style="width:300px">认领</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """
    else:
        table_html = (
            f'<div class="callout info">{esc(pending_date)} 没有待认领的款项，换个日期或清除筛选试试。</div>'
            if pending_date
            else '<div class="callout info">当前没有待认领的款项。</div>'
        )

    pending_html = f"""
    <h2>待认领列表</h2>
    <p class="hint" style="margin:-4px 0 12px">为保护金额信息，列表不显示金额。如需按金额核对，请在上方输入精确金额搜索。</p>
    {filter_form}
    {table_html}
    """

    body = f"""
    <div class="panel">
      <form method="get" action="/search">
        <div class="row">
          <div style="flex:1; min-width:250px">
            <label>关键词</label>
            <input name="q" value="{esc(q)}" placeholder="客户名、付款方、精确金额或备注，可组合搜索" autofocus>
          </div>
          <input type="hidden" name="user" value="{esc(actor['id'])}">
          <input type="hidden" name="name" value="{esc(actor['name'])}">
          <input type="hidden" name="department" value="{esc(actor['department'])}">
          <input type="hidden" name="role" value="{esc(actor['role'])}">
          <div><button type="submit">搜索</button></div>
        </div>
      </form>
      <p class="hint">搜索可看到金额；下方公开列表不显示金额。空搜索、过宽关键词、单独日期搜索会被限制，单次最多返回 {SEARCH_LIMIT} 条。</p>
    </div>
    {result_html}
    {pending_html}
    {catalog_script()}
    """
    return page("到款认领搜索", body, active="search", subtitle="输入客户名、金额或备注，找到属于你部门的到款并提交认领", actor=actor)


def validate_search_query(q: str) -> tuple[bool, str]:
    normalized = q.strip()
    if not normalized:
        return False, "关键词不能为空。"
    numeric = parse_amount(normalized) > 0
    compact = re.sub(r"\s+", "", normalized)
    if len(compact) < 2 and not numeric:
        return False, "关键词太短，请至少输入 2 个有效字符。"
    if compact in BROAD_TERMS:
        return False, "关键词过宽，请输入更具体的客户名、金额或备注。"
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", compact):
        return False, "普通用户不能只按日期搜索，请增加客户名、金额或备注。"
    return True, ""


def run_search(conn: sqlite3.Connection, q: str) -> list[sqlite3.Row]:
    terms = [term for term in re.split(r"\s+", q.strip()) if term]
    clauses = []
    params: list[Any] = []
    amount_cents = parse_amount(q)
    if amount_cents > 0 and len(terms) == 1:
        clauses.append("amount_cents = ?")
        params.append(amount_cents)
    else:
        for term in terms:
            amount = parse_amount(term)
            if amount > 0 and re.fullmatch(r"[¥￥]?\d[\d,]*(\.\d{1,2})?", term):
                clauses.append("amount_cents = ?")
                params.append(amount)
            else:
                like = f"%{term}%"
                clauses.append(
                    "(payer_name LIKE ? OR bank_note LIKE ? OR received_date LIKE ? OR serial_no LIKE ?)"
                )
                params.extend([like, like, like, like])

    where = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT *
        FROM payments
        WHERE status IN ('pending', 'claimed', 'pending_confirm')
          AND {where}
        ORDER BY received_date DESC, id DESC
        LIMIT {SEARCH_LIMIT}
        """,
        params,
    ).fetchall()


@app.post("/claim/{payment_id}")
def submit_claim(
    request: Request,
    payment_id: int,
    user: str = Form("demo-user"),
    name: str = Form("演示用户"),
    role: str = Form("claimant"),
    department: str = Form(...),
    team: str = Form(""),
    customer_project: str = Form(...),
    contract_invoice: str = Form(""),
    note: str = Form(""),
) -> RedirectResponse:
    department = require_department(department)
    team = team.strip()
    customer_project = customer_project.strip()
    if team not in CATALOG.get(department, {}):
        raise HTTPException(status_code=400, detail="请选择该部门下的中心/小组")
    if customer_project not in CATALOG[department][team]:
        raise HTTPException(status_code=400, detail="请选择该中心/小组下的项目")
    actor = actor_from_form(user, name, role, department)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row["status"] not in {"pending", "claimed", "pending_confirm"}:
            raise HTTPException(status_code=409, detail="这笔款当前状态不能认领")

        conflict = row["status"] in {"claimed", "pending_confirm"} and (
            (row["claimed_department"] or "") != department or (row["claimed_by"] or "") != actor["id"]
        )
        claim_status = "pending" if conflict else "accepted"
        conn.execute(
            """
            INSERT INTO claims
                (payment_id, department, team, actor_id, actor_name, customer_project, contract_invoice, note, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment_id,
                department,
                team.strip(),
                actor["id"],
                actor["name"],
                customer_project,
                contract_invoice,
                note,
                claim_status,
                now_text(),
            ),
        )

        if conflict:
            conn.execute(
                "UPDATE payments SET status = 'pending_confirm', finance_note = ? WHERE id = ?",
                ("存在多次认领，需要管理员确认", payment_id),
            )
            action = "claim_conflict"
        else:
            conn.execute(
                """
                UPDATE payments
                SET status = 'claimed',
                    claimed_department = ?,
                    claimed_team = ?,
                    claimed_by = ?,
                    claimed_by_name = ?,
                    claimed_at = ?,
                    customer_project = ?,
                    contract_invoice = ?,
                    claim_note = ?
                WHERE id = ?
                """,
                (
                    department,
                    team.strip(),
                    actor["id"],
                    actor["name"],
                    now_text(),
                    customer_project,
                    contract_invoice,
                    note,
                    payment_id,
                ),
            )
            action = "claim_submit"
        audit(conn, actor, action, payment_id, {"department": department, "team": team.strip(), "customer_project": customer_project}, request)

    return RedirectResponse(url("/search", user=user, name=name, department=department), status_code=303)


ROLE_LABELS = {"claimant": "普通用户", "finance": "管理员", "admin": "管理员"}


@app.get("/me", response_class=HTMLResponse)
def personal_center(request: Request) -> HTMLResponse:
    actor = actor_from_request(request)
    with get_conn() as conn:
        my_claims = conn.execute(
            """
            SELECT c.id AS c_id, c.department AS c_dept, c.team AS c_team,
                   c.customer_project AS c_proj, c.created_at AS c_at, c.status AS c_status,
                   p.received_date, p.payer_name, p.bank_note, p.status AS p_status,
                   p.claimed_by, p.claimed_by_name
            FROM claims c
            JOIN payments p ON p.id = c.payment_id
            WHERE c.actor_id = ?
            ORDER BY c.id DESC
            LIMIT 100
            """,
            (actor["id"],),
        ).fetchall()
        accepted = sum(1 for r in my_claims if r["p_status"] == "claimed" and r["claimed_by"] == actor["id"])
        waiting = sum(1 for r in my_claims if r["p_status"] == "pending_confirm")

    role_label = ROLE_LABELS.get(actor["role"], actor["role"])
    initial = esc(actor["name"][:1]) if actor["name"] else "我"
    cur_dept = actor["department"] if actor["department"] in DEPARTMENTS else ""
    cur_team = actor.get("team", "")
    id_note = (
        "已通过飞书登录"
        if actor.get("authed")
        else "当前为演示身份（URL 参数），正式使用请走飞书登录。"
    )
    dept_display = f"{esc(cur_dept)} · {esc(cur_team)}" if cur_dept else "未设置部门"
    identity_card = f"""
    <div class="panel" style="display:flex; gap:18px; align-items:center">
      <div style="width:56px; height:56px; border-radius:14px; flex:none;
        background:linear-gradient(135deg,#2456d6,#4f7df7); color:#fff;
        display:flex; align-items:center; justify-content:center; font-size:24px; font-weight:600">{initial}</div>
      <div style="flex:1">
        <div style="font-size:18px; font-weight:600">{esc(actor['name'])}</div>
        <div class="muted" style="margin-top:4px">
          {dept_display} · {esc(role_label)}
        </div>
      </div>
      <div class="muted" style="text-align:right; font-size:12px; max-width:230px">{id_note}</div>
    </div>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
      <div class="stat" style="--dot:#16a34a"><span class="stat-label">已认领</span><strong>{accepted}</strong></div>
      <div class="stat" style="--dot:#dc2626"><span class="stat-label">待管理员确认</span><strong>{waiting}</strong></div>
      <div class="stat" style="--dot:#2456d6"><span class="stat-label">认领记录总数</span><strong>{len(my_claims)}</strong></div>
    </div>
    """

    # 登录用户在这里设置自己的部门/中心，设置后认领时自动带上
    dept_setting = ""
    if actor.get("authed"):
        need = not cur_dept
        dept_opts = '<option value="">请选择部门</option>' + "".join(
            f'<option value="{esc(d)}"{" selected" if d == cur_dept else ""}>{esc(d)}</option>'
            for d in DEPARTMENTS
        )
        tip = (
            "首次使用请先设置你的部门和中心。设置后认领时会自动带上，不用每次选。"
            if need
            else "认领时会自动带上这里的部门和中心。组织调整了可随时改。"
        )
        dept_setting = f"""
        <div class="panel" style="border-left:4px solid {'#e3a008' if need else 'var(--primary)'}">
          <div style="font-weight:600; margin-bottom:4px">我的部门 / 中心</div>
          <p class="hint" style="margin:0 0 12px">{tip}</p>
          <form method="post" action="/me/profile" class="row" style="align-items:end">
            <div style="min-width:200px; flex:1"><label>部门</label><select name="department" class="cs-dept" required>{dept_opts}</select></div>
            <div style="min-width:200px; flex:1"><label>中心 / 小组</label>{team_select("team", cur_dept, cur_team, required=True)}</div>
            <div><button type="submit">保存</button></div>
          </form>
        </div>
        """

    if my_claims:
        rows = []
        for r in my_claims:
            if r["p_status"] == "claimed" and r["claimed_by"] == actor["id"]:
                state = status_badge("claimed")
            elif r["p_status"] == "pending_confirm":
                state = status_badge("pending_confirm")
            elif r["p_status"] == "rejected":
                state = status_badge("rejected")
            elif r["claimed_by"] and r["claimed_by"] != actor["id"]:
                state = f'<span class="status rejected">已归他人</span><div class="muted">{esc(r["claimed_by_name"] or "")}</div>'
            else:
                state = status_badge(r["p_status"])
            rows.append(
                f"""
                <tr>
                  <td class="nowrap">{esc(r["received_date"])}</td>
                  <td><strong>{esc(r["payer_name"])}</strong><div class="muted">{esc(r["bank_note"])}</div></td>
                  <td>{esc(r["c_dept"])}<div class="muted">{esc(r["c_team"])} · {esc(r["c_proj"])}</div></td>
                  <td class="nowrap muted">{esc(r["c_at"])}</td>
                  <td>{state}</td>
                </tr>
                """
            )
        claims_html = f"""
        <div class="table-wrap">
        <table>
          <thead><tr><th>到款日期</th><th>付款方 / 备注</th><th>部门 / 中心 / 项目</th><th>提交时间</th><th>当前状态</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        </div>
        """
    else:
        claims_html = '<div class="callout info">你还没有认领过任何到款。去「认领搜索」找到属于你的款项并提交认领吧。</div>'

    body = f"""
    {identity_card}
    {dept_setting}
    <h2>我的认领</h2>
    {claims_html}
    {catalog_script()}
    """
    return page("个人中心", body, active="me", subtitle="查看你的身份信息和认领记录", actor=actor)


@app.post("/me/profile")
def save_my_profile(
    request: Request,
    department: str = Form(...),
    team: str = Form(""),
) -> RedirectResponse:
    session = read_session(request.cookies.get(SESSION_COOKIE, ""))
    if not session:
        raise HTTPException(status_code=403, detail="请先用飞书登录")
    department = require_department(department)
    team = team.strip()
    if team not in CATALOG.get(department, {}):
        raise HTTPException(status_code=400, detail="请选择该部门下的中心/小组")
    actor = {"id": session["id"], "name": session.get("name", ""), "role": session.get("role", "claimant")}
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_profiles (open_id, name, department, team, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(open_id) DO UPDATE SET
                name = excluded.name, department = excluded.department,
                team = excluded.team, updated_at = excluded.updated_at
            """,
            (actor["id"], actor["name"], department, team, now_text()),
        )
        audit(conn, actor, "set_profile", None, {"department": department, "team": team}, request)
    return RedirectResponse("/me", status_code=303)


@app.post("/admin/profiles/{open_id}")
def admin_set_profile(
    request: Request,
    open_id: str,
    department: str = Form(...),
    team: str = Form(""),
    user: str = Form("admin"),
    name: str = Form("管理员"),
    role: str = Form("admin"),
) -> RedirectResponse:
    actor = actor_from_form(user, name, role)
    require_admin(actor)
    department = require_department(department)
    team = team.strip()
    if team not in CATALOG.get(department, {}):
        raise HTTPException(status_code=400, detail="请选择该部门下的中心/小组")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE user_profiles SET department = ?, team = ?, updated_at = ? WHERE open_id = ?",
            (department, team, now_text(), open_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
        audit(conn, actor, "admin_set_profile", None, {"open_id": open_id, "department": department, "team": team}, request)
    return RedirectResponse(url("/admin", role="admin", user=user, name=name), status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    actor = actor_from_request(request)
    require_admin(actor)
    with get_conn() as conn:
        stats = conn.execute(
            """
            SELECT status, COUNT(*) count, COALESCE(SUM(amount_cents), 0) amount
            FROM payments
            GROUP BY status
            """
        ).fetchall()
        batches = conn.execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 10").fetchall()
        payments = conn.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 100").fetchall()
        logs = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 30").fetchall()
        profiles = conn.execute(
            "SELECT open_id, name, department, team, updated_at FROM user_profiles ORDER BY updated_at DESC"
        ).fetchall()

    stat_map = {row["status"]: row for row in stats}
    stat_html = "".join(
        f'<div class="stat" style="--dot:{dot}">'
        f'<span class="stat-label">{esc(label)}</span>'
        f'<strong>{stat_map.get(status, {"count": 0})["count"] if status in stat_map else 0}</strong>'
        f'<span class="stat-amount">¥ {money(stat_map.get(status, {"amount": 0})["amount"] if status in stat_map else 0)}</span>'
        f"</div>"
        for status, label, dot in [
            ("draft", "待确认", "#64748b"),
            ("pending", "待认领", "#d97706"),
            ("claimed", "已认领", "#16a34a"),
            ("pending_confirm", "待确认异常", "#dc2626"),
            ("rejected", "已驳回", "#94a3b8"),
            ("closed", "已关闭", "#6366f1"),
        ]
    )

    batch_rows = "".join(
        f"""
        <tr>
          <td class="nowrap">#{row['id']}</td>
          <td>{esc(row['source_name'])}</td>
          <td class="nowrap">{esc(row['created_at'])}</td>
          <td class="nowrap">{esc(row['raw_count'])} / {esc(row['imported_count'])} / {esc(row['skipped_count'])}</td>
          <td>{'<span class="status claimed">已入池</span>' if row['status'] == 'confirmed' else '<span class="status draft">待确认</span>'}</td>
          <td>
            {'<span class="muted">无需操作</span>' if row['status'] == 'confirmed' else f'''
            <form method="post" action="/admin/batches/{row['id']}/confirm">
              {finance_hidden(actor)}
              <button type="submit">确认入池</button>
            </form>'''}
          </td>
        </tr>
        """
        for row in batches
    )

    payment_rows = "".join(render_admin_payment_row(row, actor) for row in payments)
    log_rows = "".join(
        f"<tr><td class='nowrap'>{esc(row['at'])}</td><td class='nowrap'>{esc(row['actor_name'])}</td><td class='nowrap'>{esc(row['action'])}</td><td>{esc(row['payment_id'])}</td><td><span class='code'>{esc(row['detail_json'])}</span></td></tr>"
        for row in logs
    )
    profile_rows = "".join(
        f"""
        <tr>
          <td><strong>{esc(p["name"] or "")}</strong><br><span class="code">{esc(p["open_id"])}</span></td>
          <td class="actions">
            <form method="post" action="/admin/profiles/{esc(p['open_id'])}" class="row" style="align-items:end">
              {finance_hidden(actor)}
              <div style="min-width:170px; flex:1"><label>部门</label>{department_select("department", p["department"] or "", required=True, class_name="cs-dept")}</div>
              <div style="min-width:170px; flex:1"><label>中心 / 小组</label>{team_select("team", p["department"] or "", p["team"] or "", required=True)}</div>
              <div><button class="secondary" type="submit">保存</button></div>
            </form>
          </td>
          <td class="nowrap muted">{esc(p["updated_at"])}</td>
        </tr>
        """
        for p in profiles
    )

    body = f"""
    <div class="grid">{stat_html}</div>

    <h2>导入流水</h2>
    <div class="panel">
      <form method="post" action="/admin/import" enctype="multipart/form-data">
        {finance_hidden(actor)}
        <div class="row">
          <div style="flex:1.4; min-width:220px">
            <label>来源名称</label>
            <input name="source_name" value="{datetime.now().strftime('%Y-%m-%d')} 银行流水">
          </div>
          <div style="flex:1; min-width:220px">
            <label>原始凭证附件</label>
            <input type="file" name="attachment" accept=".png,.jpg,.jpeg,.pdf,.csv,.xls,.xlsx">
          </div>
        </div>
        <p class="hint">可上传银行截图、PDF、CSV 或 Excel 文件。第一版只保存凭证，不自动识别附件内容。</p>
        <div class="field">
          <label>CSV / TSV / 表格文本</label>
          <textarea name="table_text" placeholder="到款日期,付款方名称,到款金额,银行备注,流水号"></textarea>
        </div>
        <button type="submit">导入为待确认</button>
      </form>
    </div>

    <h2>最近导入批次</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>ID</th><th>来源</th><th>创建时间</th><th>原始 / 导入 / 跳过</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{batch_rows or '<tr><td colspan="6" class="empty">暂无批次</td></tr>'}</tbody>
    </table>
    </div>

    <h2>全量认领池</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>ID</th><th>日期</th><th>金额</th><th>付款方 / 备注</th><th>状态 / 认领</th><th style="width:320px">管理操作</th></tr></thead>
      <tbody>{payment_rows or '<tr><td colspan="6" class="empty">暂无记录</td></tr>'}</tbody>
    </table>
    </div>

    <h2>成员部门</h2>
    <p class="hint" style="margin:-4px 0 12px">登录过并设置过部门的成员都在这里。有人选错了，管理员可直接改。</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>成员</th><th style="width:520px">部门 / 中心</th><th>更新时间</th></tr></thead>
      <tbody>{profile_rows or '<tr><td colspan="3" class="empty">还没有成员设置过部门</td></tr>'}</tbody>
    </table>
    </div>

    <h2>最近操作日志</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>时间</th><th>用户</th><th>动作</th><th>记录</th><th>详情</th></tr></thead>
      <tbody>{log_rows or '<tr><td colspan="5" class="empty">暂无日志</td></tr>'}</tbody>
    </table>
    </div>
    {catalog_script()}
    """
    return page("管理后台", body, active="admin", subtitle="导入流水、确认入池、处理认领、成员部门与操作日志", actor=actor)


def finance_hidden(actor: dict[str, str]) -> str:
    return f"""
    <input type="hidden" name="user" value="{esc(actor['id'])}">
    <input type="hidden" name="name" value="{esc(actor['name'])}">
    <input type="hidden" name="role" value="{esc(actor['role'])}">
    """


def render_admin_payment_row(row: sqlite3.Row, actor: dict[str, str]) -> str:
    claimed = ""
    if row["claimed_department"] or row["claimed_by_name"]:
        parts = [part for part in (row["claimed_department"], row["claimed_team"], row["claimed_by_name"]) if part]
        claimed = f'<div class="muted">{esc(" · ".join(parts))}<br>{esc(row["customer_project"])}</div>'
    finance_note = ""
    if row["finance_note"]:
        finance_note = f'<div class="muted">管理备注：{esc(row["finance_note"])}</div>'
    attachment = attachment_link(row["source_ref"] or "", actor)
    source = attachment or esc(row["source_ref"])
    return f"""
    <tr>
      <td class="nowrap">#{row['id']}<br><span class="muted">批次 {esc(row['batch_id'])}</span></td>
      <td class="nowrap">{esc(row['received_date'])}<br><span class="muted">{esc(row['received_time'])}</span></td>
      <td class="num">¥ {money(row['amount_cents'])}</td>
      <td>
        <strong>{esc(row['payer_name'])}</strong>
        <div>{esc(row['bank_note'])}</div>
        <div class="muted">流水号 {esc(row['serial_no'])} · 凭证 {source}</div>
      </td>
      <td>{status_badge(row['status'])}{claimed}{finance_note}</td>
      <td class="actions">
        <details class="fold">
          <summary>编辑字段</summary>
          <div class="fold-body">
            <form method="post" action="/admin/payments/{row['id']}/edit">
              {finance_hidden(actor)}
              <div class="row">
                <div style="width:115px"><label>日期</label><input name="received_date" value="{esc(row['received_date'])}"></div>
                <div style="width:110px"><label>金额</label><input name="amount" value="{money(row['amount_cents']).replace(',', '')}"></div>
              </div>
              <div class="field" style="margin-top:10px"><label>付款方</label><input name="payer_name" value="{esc(row['payer_name'])}"></div>
              <div class="field"><label>备注</label><input name="bank_note" value="{esc(row['bank_note'])}"></div>
              <button class="secondary" type="submit">保存字段</button>
            </form>
          </div>
        </details>
        <details class="fold">
          <summary>处理状态</summary>
          <div class="fold-body">
            <form method="post" action="/admin/payments/{row['id']}/resolve">
              {finance_hidden(actor)}
              <div class="field"><label>状态</label><select name="status">
                {status_options(row['status'])}
              </select></div>
              <div class="field"><label>分配部门</label>{department_select("department", row["claimed_department"], class_name="cs-dept")}</div>
              <div class="field"><label>中心 / 小组</label>{team_select("team", row["claimed_department"] or "", row["claimed_team"] or "")}</div>
              <div class="field"><label>管理备注</label><input name="finance_note" value="{esc(row['finance_note'])}"></div>
              <button type="submit">更新状态</button>
            </form>
          </div>
        </details>
      </td>
    </tr>
    """


def status_options(current: str) -> str:
    items = [
        ("pending", "待认领"),
        ("claimed", "已认领"),
        ("pending_confirm", "待确认"),
        ("rejected", "已驳回"),
        ("closed", "已关闭"),
    ]
    return "".join(
        f'<option value="{value}" {"selected" if value == current else ""}>{label}</option>'
        for value, label in items
    )


@app.post("/admin/import")
def admin_import(
    request: Request,
    source_name: str = Form(...),
    table_text: str = Form(...),
    attachment: Optional[UploadFile] = File(None),
    user: str = Form("admin"),
    name: str = Form("管理员"),
    role: str = Form("admin"),
) -> RedirectResponse:
    actor = actor_from_form(user, name, role)
    require_admin(actor)
    source_ref = save_attachment(attachment) or source_name
    rows = read_table(table_text)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO import_batches
                (source_name, created_at, created_by, raw_count, imported_count, skipped_count)
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            (source_name, now_text(), actor["name"], len(rows)),
        )
        batch_id = cur.lastrowid
        imported = 0
        skipped = 0
        for item in rows:
            amount_cents = parse_amount(item.get("amount"))
            if duplicate_exists(conn, item, amount_cents):
                skipped += 1
                continue
            received_date = parse_date(item.get("received_date"))
            payer_name = item.get("payer_name", "").strip()
            confidence = 1.0
            missing = []
            if not received_date:
                missing.append("到款日期")
            if not payer_name:
                missing.append("付款方名称")
            if amount_cents <= 0:
                missing.append("到款金额")
            if missing:
                confidence = 0.4
            conn.execute(
                """
                INSERT INTO payments
                    (batch_id, imported_at, received_date, received_time, payer_name, amount_cents,
                     bank_note, receiver_account, serial_no, source_ref, confidence, status, finance_note, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    batch_id,
                    now_text(),
                    received_date,
                    item.get("received_time", "").strip(),
                    payer_name,
                    max(amount_cents, 0),
                    item.get("bank_note", "").strip(),
                    item.get("receiver_account", "").strip(),
                    item.get("serial_no", "").strip(),
                    source_ref,
                    confidence,
                    f"字段缺失：{', '.join(missing)}" if missing else "",
                    json.dumps(item, ensure_ascii=False),
                ),
            )
            imported += 1
        conn.execute(
            "UPDATE import_batches SET imported_count = ?, skipped_count = ? WHERE id = ?",
            (imported, skipped, batch_id),
        )
        audit(
            conn,
            actor,
            "import_batch",
            None,
            {"batch_id": batch_id, "raw": len(rows), "imported": imported, "skipped": skipped, "source_ref": source_ref},
            request,
        )
    return RedirectResponse(url("/admin", role="admin", user=user, name=name), status_code=303)


@app.post("/admin/batches/{batch_id}/confirm")
def confirm_batch(
    request: Request,
    batch_id: int,
    user: str = Form("admin"),
    name: str = Form("管理员"),
    role: str = Form("admin"),
) -> RedirectResponse:
    actor = actor_from_form(user, name, role)
    require_admin(actor)
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM payments WHERE batch_id = ? AND status = 'draft'", (batch_id,)).fetchall()
        conn.execute(
            "UPDATE payments SET status = 'pending', confirmed_at = ? WHERE batch_id = ? AND status = 'draft'",
            (now_text(), batch_id),
        )
        conn.execute("UPDATE import_batches SET status = 'confirmed' WHERE id = ?", (batch_id,))
        audit(conn, actor, "confirm_batch", None, {"batch_id": batch_id, "count": len(rows)}, request)
    return RedirectResponse(url("/admin", role="admin", user=user, name=name), status_code=303)


@app.post("/admin/payments/{payment_id}/edit")
def edit_payment(
    request: Request,
    payment_id: int,
    received_date: str = Form(""),
    amount: str = Form(""),
    payer_name: str = Form(""),
    bank_note: str = Form(""),
    user: str = Form("admin"),
    name: str = Form("管理员"),
    role: str = Form("admin"),
) -> RedirectResponse:
    actor = actor_from_form(user, name, role)
    require_admin(actor)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE payments
            SET received_date = ?, amount_cents = ?, payer_name = ?, bank_note = ?
            WHERE id = ?
            """,
            (parse_date(received_date), parse_amount(amount), payer_name.strip(), bank_note.strip(), payment_id),
        )
        audit(conn, actor, "edit_payment", payment_id, {"received_date": received_date, "amount": amount}, request)
    return RedirectResponse(url("/admin", role="admin", user=user, name=name), status_code=303)


@app.post("/admin/payments/{payment_id}/resolve")
def resolve_payment(
    request: Request,
    payment_id: int,
    status: str = Form(...),
    department: str = Form(""),
    team: str = Form(""),
    finance_note: str = Form(""),
    user: str = Form("admin"),
    name: str = Form("管理员"),
    role: str = Form("admin"),
) -> RedirectResponse:
    actor = actor_from_form(user, name, role)
    require_admin(actor)
    if status not in {"pending", "claimed", "pending_confirm", "rejected", "closed"}:
        raise HTTPException(status_code=400, detail="状态不合法")
    department = department.strip()
    team = team.strip()
    if department and department not in DEPARTMENTS:
        raise HTTPException(status_code=400, detail="请选择标准部门")
    if status == "claimed" and not department:
        raise HTTPException(status_code=400, detail="标记已认领时必须选择部门")
    with get_conn() as conn:
        if team:
            row = conn.execute(
                "SELECT claimed_department FROM payments WHERE id = ?", (payment_id,)
            ).fetchone()
            effective_dept = department or (row["claimed_department"] if row else "")
            if team not in CATALOG.get(effective_dept or "", {}):
                raise HTTPException(status_code=400, detail="请选择该部门下的中心/小组")
        if status == "pending":
            conn.execute(
                """
                UPDATE payments
                SET status = 'pending',
                    claimed_department = NULL,
                    claimed_team = NULL,
                    claimed_by = NULL,
                    claimed_by_name = NULL,
                    claimed_at = NULL,
                    finance_note = ?
                WHERE id = ?
                """,
                (finance_note, payment_id),
            )
        else:
            conn.execute(
                """
                UPDATE payments
                SET status = ?,
                    claimed_department = COALESCE(NULLIF(?, ''), claimed_department),
                    claimed_team = COALESCE(NULLIF(?, ''), claimed_team),
                    finance_note = ?
                WHERE id = ?
                """,
                (status, department, team, finance_note, payment_id),
            )
        audit(conn, actor, "resolve_payment", payment_id, {"status": status, "department": department, "team": team}, request)
    return RedirectResponse(url("/admin", role="admin", user=user, name=name), status_code=303)


@app.get("/admin/payments/{payment_id}/logs", response_class=HTMLResponse)
def payment_logs(request: Request, payment_id: int) -> HTMLResponse:
    actor = actor_from_request(request)
    require_admin(actor)
    with get_conn() as conn:
        logs = conn.execute("SELECT * FROM audit_logs WHERE payment_id = ? ORDER BY id DESC", (payment_id,)).fetchall()
    rows = "".join(
        f"<tr><td class='nowrap'>{esc(row['at'])}</td><td class='nowrap'>{esc(row['actor_name'])}</td><td class='nowrap'>{esc(row['action'])}</td><td><span class='code'>{esc(row['detail_json'])}</span></td></tr>"
        for row in logs
    )
    return page(
        f"记录 #{payment_id} 操作日志",
        f"<div class='table-wrap'><table><thead><tr><th>时间</th><th>用户</th><th>动作</th><th>详情</th></tr></thead><tbody>{rows or '<tr><td colspan=4 class=empty>暂无日志</td></tr>'}</tbody></table></div>",
        active="admin",
        actor=actor,
    )

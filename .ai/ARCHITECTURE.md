# 系统架构（ARCHITECTURE）

## 总体架构

单进程 FastAPI 应用，服务端渲染 HTML，无单独前端。请求流程：

```
浏览器/飞书客户端
   │  HTTP（表单提交 / GET 导航）
   ▼
FastAPI (app.py, uvicorn)
   ├─ 身份：签名 cookie 会话 → actor_from_request() → compute_role()
   ├─ 业务路由：渲染 HTML（Python f-string）/ 写 SQLite
   ├─ 飞书 API：urllib 直连 open.feishu.cn / accounts.feishu.cn
   └─ SQLite (claim_pool.db) + 本地 uploads/
```

飞书在架构中的角色：**只提供登录授权和消息通道**，不托管本服务。同事在飞书工作台点开的是本服务的公网地址。

## 目录结构

```
财务工具/
├─ app.py              # 全部后端 + 内联 HTML/CSS/JS（约 1980 行，唯一主程序）
├─ catalog.json        # 部门→中心→项目 三级分类数据（业务数据源，改分类改这里）
├─ catalog.json.bak    # 旧分类备份（历史残留，可忽略）
├─ requirements.txt    # 3 个依赖
├─ .python-version     # 3.12
├─ Procfile            # 云平台启动命令：uvicorn app:app --host 0.0.0.0 --port $PORT
├─ .env                # 飞书密钥/会话密钥等（不进 git）
├─ .env.example        # 环境变量模板
├─ claim_pool.db       # SQLite 数据库（不进 git，运行时生成/演进）
├─ uploads/            # 上传的原始凭证附件（不进 git）
├─ README.md           # 使用/启动说明
├─ DEPLOY.md           # 给运维同事的部署说明
└─ .ai/                # 本交接文档目录
```

## app.py 内部模块职责（按文件内顺序）

| 区块 | 职责 |
|---|---|
| 配置区（顶部） | `load_dotenv` 读 `.env`；DB/上传路径、飞书配置、超管白名单、会话密钥等常量 |
| `BASE_CATALOG` / `CATALOG` | `catalog.json` 提供基础三级分类，启动时合并 SQLite 的财务项目增删覆盖层 |
| `init_db` / `ensure_column` | 建表 + 增量加列（迁移用，幂等，不破坏已有数据） |
| 会话工具 | `make_session` / `read_session`：HMAC 签名的 cookie，存 `{id, name, src, exp}`，不存角色 |
| 飞书 API | `_feishu_request`（urllib 封装）/ `feishu_exchange_token` / `feishu_get_userinfo` / `feishu_tenant_token` / `feishu_send_text` |
| 身份与鉴权 | `actor_from_request`（会话优先，无会话强制 claimant）/ `compute_role`（实时三级角色）/ `require_admin` / `require_superadmin` |
| 渲染工具 | `page`（统一页面框架/导航/登录态）/ `BASE_CSS` / `department_select` / `team_select` / `status_badge` / `catalog_script`（联动 JS） |
| 导入解析 | `read_table` / `canonical_header` / `parse_amount` / `parse_date` / `duplicate_exists` |
| 路由 | 见下表 |

## 路由清单

| 方法 路径 | 鉴权 | 说明 |
|---|---|---|
| GET `/` | 公开 | 跳 `/search` |
| GET `/login` | 公开 | 跳飞书授权页 |
| GET `/oauth/callback` | 公开 | 换 token、拿用户信息、建会话、upsert `app_users` |
| GET `/logout` | 公开 | 清会话 |
| GET `/search` | 公开（登录后归真人） | 认领搜索 + 待认领列表（日期筛选） |
| POST `/claim/{id}` | 登录优先 | 提交认领（已登录归会话身份） |
| GET `/me` | 登录 | 个人中心 |
| POST `/me/profile` | 登录 | 设置本人部门 |
| GET `/admin` | 管理员 | 管理后台 |
| POST `/admin/catalog/projects` | 管理员 | 在现有部门/中心下新增或删除项目 |
| POST `/admin/import` | 管理员 | 导入流水 |
| POST `/admin/batches/{id}/confirm` | 管理员 | 确认入池（draft→pending） |
| POST `/admin/payments/{id}/edit` | 管理员 | 编辑流水字段 |
| POST `/admin/payments/{id}/resolve` | 管理员 | 改状态/分配部门 |
| POST `/admin/payments/{id}/reject` | 管理员 | 驳回退回 + 飞书通知 |
| GET `/admin/payments/{id}/logs` | 管理员 | 单据操作日志 |
| POST `/admin/profiles/{open_id}` | 管理员 | 改某成员部门 |
| POST `/admin/admins/{open_id}` | **超级管理员** | 勾选/取消管理员 |
| GET `/attachments/{filename}` | 管理员 | 下载凭证附件 |

## 数据模型（SQLite，7 张表）

- **payments**：到款主表。`status` 取值 `draft`(待确认入池)/`pending`(待认领)/`claimed`(已认领)/`pending_confirm`(多人认领待确认)/`rejected`/`closed`。金额存 `amount_cents`（分）。认领归属字段 `claimed_department/claimed_team/claimed_by/claimed_by_name/customer_project` 等。
- **claims**：每次认领动作的流水记录（含冲突认领），关联 `payment_id`。
- **import_batches**：导入批次。
- **audit_logs**：操作审计（动作名 + JSON 详情 + 操作人角色 + IP）。
- **user_profiles**：用户的部门设置（`open_id → department, team`）。
- **app_users**：所有登录过的用户（`open_id, name, is_admin, last_login`）；`is_admin` 由超管在后台勾选。
- **catalog_project_changes**：财务对项目的新增/删除覆盖层（`department, team, project, active`）；删除是停用选项，不删历史认领文本。

## 关键数据流

1. **登录**：`/login` → 飞书授权 → `/oauth/callback` 用 code 换 user_access_token → 拿 open_id/name → `make_session` 写 cookie + upsert `app_users` → 重定向。
2. **角色判定**（每请求实时）：`actor_from_request` 读会话 → `compute_role(open_id)`：在超管白名单 `FEISHU_SUPERADMIN_OPEN_IDS` → `superadmin`；在 `app_users.is_admin` → `admin`；否则 `claimant`。**会话 cookie 不存角色**，所以改管理员后对方刷新即生效。
3. **项目目录**：启动时加载 `catalog.json` 基础目录 → 按 `catalog_project_changes` 增加或隐藏项目 → 生成运行时 `CATALOG`。
4. **认领**：选三级分类（前端联动 + 后端校验必须匹配运行时 `CATALOG`）→ 写 `claims` + 更新 `payments`。已被他人认领时进 `pending_confirm`。
5. **驳回退回**：`/admin/.../reject` → `feishu_send_text` 通知原 `claimed_by` → `payments` 置回 `pending` 并清空认领归属 → 写审计。发消息失败不阻断退回。

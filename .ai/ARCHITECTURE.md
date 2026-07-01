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
├─ app.py              # 全部后端 + 内联 HTML/CSS/JS（约 3900 行，唯一主程序）
├─ catalog.json        # 部门→中心→项目 三级分类数据（业务基础目录）
├─ catalog.json.bak    # 旧分类备份（历史残留，可忽略）
├─ requirements.txt    # 依赖
├─ .python-version     # Python 版本提示
├─ Procfile            # 云平台启动命令：uvicorn app:app --host 0.0.0.0 --port $PORT
├─ .env                # 飞书密钥/会话密钥等（不进 git）
├─ .env.example        # 环境变量模板
├─ claim_pool.db       # SQLite 数据库（不进 git，运行时生成/演进）
├─ uploads/            # 上传的原始凭证附件（不进 git）
├─ tests/              # 回归测试
├─ README.md           # 使用/启动说明
├─ DEPLOY.md           # 给运维同事的部署说明
└─ .ai/                # 接手与维护文档目录
```

## app.py 内部模块职责（按文件内顺序）

| 区块 | 职责 |
|---|---|
| 配置区（顶部） | `load_dotenv` 读 `.env`；DB/上传路径、飞书配置、超管白名单、会话密钥、限流等常量 |
| `BASE_CATALOG` / `CATALOG` | `catalog.json` 提供基础三级分类，启动时合并 SQLite 的财务项目增删覆盖层 |
| `init_db` / `ensure_column` | 建表 + 增量加列（迁移用，幂等，不破坏已有数据） |
| 会话工具 | `make_session` / `read_session`：HMAC 签名 cookie，存 `{id, name, exp}`，不存角色 |
| 飞书 API | `_feishu_request` / `feishu_exchange_token` / `feishu_get_userinfo` / `feishu_tenant_token` / `feishu_send_text` |
| 身份与鉴权 | `actor_from_request`（会话优先，无会话强制 claimant）/ `compute_role`（实时角色）/ `require_admin` / `require_superadmin` |
| 渲染工具 | `page`（统一页面框架/导航/登录态）/ `BASE_CSS` / `department_select` / `team_select` / `status_badge` / `catalog_script` |
| 导入解析 | `read_table` / `rows_from_excel` / `parse_pdf_receipts` / `canonical_header` / `parse_amount` / `parse_date` / `duplicate_exists` |
| 业务工具 | `refresh_payment_claim_status` / `claim_totals` / `submit_split_claims` / `cancel_my_claim` / `personal_dashboard_data` |
| 路由 | 见下表 |

## 路由清单

| 方法 路径 | 鉴权 | 说明 |
|---|---|---|
| GET `/` | 公开 | 跳 `/search` |
| GET `/login` | 公开 | 跳飞书授权页 |
| GET `/oauth/callback` | 公开 | 换 token、拿用户信息、建会话、upsert `app_users` |
| GET `/logout` | 公开 | 清会话 |
| GET `/attachments/{filename}` | 管理员 | 下载凭证附件 |
| GET `/search` | 登录后使用 | 认领搜索 + 待认领列表（日期筛选） |
| POST `/claim/{payment_id}` | 登录 | 提交普通认领 |
| GET `/split-claim` | 登录 | 分摊认领搜索与填写页面 |
| POST `/split-claim/{payment_id}` | 登录 | 提交一笔到款的多行分摊认领 |
| GET `/me` | 登录 | 个人中心、数据看板、我的认领 |
| POST `/me/profile` | 登录 | 设置本人部门/中心 |
| POST `/me/claims/{claim_id}/cancel` | 登录 | 本人取消自己的可取消认领 |
| GET `/admin` | 管理员 | 管理后台 |
| GET `/admin/payments/table` | 管理员 | 后台到款池表格片段 |
| GET `/admin/export/today` | 管理员 | 导出今日 CSV |
| POST `/admin/import` | 管理员 | 导入流水 |
| POST `/admin/batches/{id}/confirm` | 管理员 | 确认入池（draft→pending） |
| POST `/admin/batches/{id}/cancel` | 管理员 | 取消导入批次 |
| POST `/admin/payments/{id}/edit` | 管理员 | 编辑流水字段（含到款公司） |
| POST `/admin/payments/{id}/resolve` | 管理员 | 改状态/分配部门 |
| POST `/admin/payments/{id}/reject` | 管理员 | 驳回退回 + 飞书通知 |
| POST `/admin/payments/{id}/confirm-claims` | 管理员 | 确认部分认领完成 |
| POST `/admin/payments/bulk-close` | 管理员 | 批量关闭到款 |
| POST `/admin/claims/{id}/accept` | 管理员 | 接受某条认领 |
| POST `/admin/claims/{id}/reject` | 管理员 | 驳回某条认领 |
| POST `/admin/catalog/projects` | 管理员 | 在现有部门/中心下新增或删除项目 |
| POST `/admin/profiles/{open_id}` | 管理员 | 改某成员部门/中心 |
| POST `/admin/scopes/{open_id}/add` | 管理员 | 给成员添加额外参与范围 |
| POST `/admin/scopes/{scope_id}/deactivate` | 管理员 | 停用额外参与范围 |
| POST `/admin/users/{open_id}/role` | 管理员 | 设置成员托管身份 |
| POST `/admin/admins/{open_id}` | **超级管理员** | 兼容旧入口：勾选/取消管理员 |
| GET `/admin/payments/{id}/logs` | 管理员 | 单据操作日志 |

## 数据模型（SQLite，8 张表）

- **payments**：到款主表。金额存 `amount_cents`（分）。关键字段含 `receiver_company`、付款方、银行备注、凭证、状态、认领归属和关闭时间。`status` 取值包括 `draft`、`pending`、`partial_claiming`、`claimed`、`pending_confirm`、`rejected`、`closed`。
- **claims**：每次认领动作的流水记录，关联 `payment_id`；分摊认领会一次写入多条 `pending` claim；取消后置为 `canceled`。
- **import_batches**：导入批次。
- **audit_logs**：操作审计（动作名 + JSON 详情 + 操作人角色 + IP）。
- **user_profiles**：用户主身份的部门/中心设置（`open_id → department, team`）。
- **user_scopes**：成员额外参与范围（跨部门/跨项目协作），可由管理员添加和停用。
- **app_users**：所有登录过的用户（`open_id, name, managed_role, is_admin, last_login`）。`managed_role` 支持 `claimant`、`general_manager`、`admin`；`is_admin` 为旧字段兼容。
- **catalog_project_changes**：财务对项目的新增/删除覆盖层（`department, team, project, active`）；删除是停用选项，不删历史认领文本。

## 关键数据流

1. **登录**：`/login` → 飞书授权 → `/oauth/callback` 用 code 换 user_access_token → 拿 open_id/name → `make_session` 写 cookie + upsert `app_users` → 重定向。
2. **角色判定**（每请求实时）：`actor_from_request` 读签名会话 → `compute_role(open_id)`：超管白名单 `FEISHU_SUPERADMIN_OPEN_IDS` → `superadmin`；数据库 `managed_role` 为 `admin/general_manager` → 对应角色；旧 `is_admin=1` → `admin`；否则 `claimant`。**会话 cookie 不存角色**。
3. **项目目录**：启动时加载 `catalog.json` 基础目录 → 按 `catalog_project_changes` 增加或隐藏项目 → 生成运行时 `CATALOG`。
4. **普通认领**：用户选三级分类 → 后端校验必须匹配运行时 `CATALOG` → 写 `claims` + 根据金额和已有认领刷新 `payments.status`。
5. **部分认领**：一笔款可被多条认领覆盖。未认满保持 `partial_claiming`，认满后进入财务确认或完成状态。
6. **分摊认领**：用户在 `/split-claim` 对同一笔款填写多行部门/中心/项目/金额 → `submit_split_claims` 写多条 `claims` → 刷新到款状态 → 等财务确认。
7. **本人取消认领**：`/me/claims/{id}/cancel` 只允许认领本人取消 `pending/accepted` 状态的 claim → claim 置 `canceled` → 到款状态重新计算并回到待认领或部分认领中。
8. **跨部门参与范围**：管理员在后台写 `user_scopes`；个人中心根据“全部角色 / 主身份 / 单个参与范围”构造 `dashboard_scopes`，看板按范围汇总，`我的认领` 仍只展示本人记录。
9. **驳回退回**：管理员驳回认领 → `feishu_send_text` 通知原认领人 → 款项回到待认领或重新计算状态 → 写审计。发消息失败不阻断退回。

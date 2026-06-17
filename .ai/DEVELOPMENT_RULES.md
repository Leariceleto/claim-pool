# 开发约定（DEVELOPMENT_RULES）

## 代码风格

- Python 风格贴近现有 `app.py`：4 空格缩进，类型注解（`dict[str, str]`、`Optional[...]`），函数小而专一。
- **HTML 用 Python f-string 内联**，沿用现有写法。所有插入到 HTML 的用户数据/数据库字段**必须经过 `esc()` 转义**（防 XSS）。这是硬性要求，现有代码全程遵守。
- SQL 一律用**参数化查询**（`?` 占位 + 元组），禁止字符串拼接 SQL（防注入）。
- 注释用中文，简洁说明"为什么"而非"做什么"。
- 不引入新的第三方依赖，除非有充分理由。飞书相关一律用标准库 `urllib` 手写（与现有 `_feishu_request` 一致），不要装飞书 SDK / requests / httpx。

## 命名规范

- 路由函数用动词短语（`submit_claim`、`reject_payment`、`admin_toggle_admin`）。
- 鉴权函数 `require_xxx`；身份获取 `actor_from_request` / `actor_from_form`。
- 飞书相关函数前缀 `feishu_`。
- 数据库字段 snake_case；金额一律存「分」（`amount_cents`），展示用 `money()` 格式化。
- 角色字符串固定为 `claimant` / `admin` / `finance`(历史兼容) / `superadmin`。`finance` 是早期遗留、与 `admin` 等价，`require_admin` 同时接受，新代码用 `admin`。

## 项目约定

- **`catalog.json` 是部门/中心/项目的唯一数据源**。调整分类只改这个 JSON 文件，不改代码。三级结构：`{部门: {中心: [项目, ...]}}`。
- **数据库迁移用 `ensure_column(conn, table, column, ddl)` 模式**（幂等加列），不要写破坏性 DDL、不要丢已有数据。新表用 `CREATE TABLE IF NOT EXISTS` 加进 `init_db`。
- 启动会自动 `init_db()` 建表/补列，无需手动初始化。
- **敏感配置走 `.env`**（飞书 Secret、`SESSION_SECRET`），`.env` 已在 `.gitignore`，**绝不提交**。新增配置项同步更新 `.env.example`。
- **Git 提交**：message 用中文，每个完整功能/修复一次提交；结尾保留 `Co-Authored-By` 行。推送用 SSH（已配免密）。`.env`、`*.db`、`uploads/`、`*.bak`、`__pycache__/` 不进仓库。
- **每次提交前更新 `.ai/PROJECT_STATE.md`**：按其顶部「维护协议」刷新当前状态/最近完成/待办/已知问题，随代码一起提交。它是项目单一事实来源，必须保持最新。
- 改动涉及"可观察行为"时，习惯用 curl/浏览器手动验证（无自动化测试）。

## 不能随意修改的部分（改前务必想清楚）

1. **鉴权与角色逻辑**：`actor_from_request`、`compute_role`、`require_admin`、`require_superadmin`。
   - 铁律：**敏感路由的角色只能来自签名会话**，绝不信任表单字段或 URL `?role=` 参数。无会话时 `actor_from_request` 强制返回 `claimant`。这是堵越权的关键，曾专门加固，不要倒退。
2. **会话签名机制**：`make_session` / `read_session`（HMAC）。改了 `SESSION_SECRET` 会让所有人重新登录；不要把角色写回会话 cookie（角色必须实时算）。
3. **飞书 OAuth 流程**：`/login` → `/oauth/callback` → `feishu_exchange_token` → `feishu_get_userinfo`。端点和 scope 都是核对过的（`authen/v1/authorize`、`authen/v2/oauth/token`、`authen/v1/user_info`，scope `contact:user.base:readonly`）。改动需对照飞书最新文档。
4. **`FEISHU_REDIRECT_URI` 必须与飞书后台「重定向 URL」完全一致**，否则登录失败。换部署地址时，`.env` 和飞书后台要同步改、并在飞书重新发布版本。
5. **数据库表结构**：增改字段走 `ensure_column`，不要直接改 `CREATE TABLE` 后期望旧库自动变更（已上线库不会重建）。
6. **金额单位**：数据库存分（`amount_cents`），任何涉及金额的改动注意单位换算（用 `parse_amount` / `money`）。
7. **超级管理员根权限**只能由 `.env` 的 `FEISHU_SUPERADMIN_OPEN_IDS` 指定；普通管理员才在数据库 `app_users.is_admin` 里。不要把超管也挪进数据库（会丢失"根"）。

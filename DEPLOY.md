# 部署说明（到款认领系统）

> 给负责部署的技术同事：这份文档讲清楚怎么把本应用部署到生产、以及和飞书对接的关键点。

## 一、这是什么

飞书企业自建应用的后端：财务到款认领系统。
- 单文件 FastAPI（`app.py`）+ SQLite，无外部数据库依赖
- 飞书 OAuth2 网页登录（同事在飞书工作台点开即用）
- 三级权限：超级管理员 / 管理员 / 普通用户

## 二、运行环境

- Python 3.12（见 `.python-version`）
- 依赖：`requirements.txt`（fastapi / uvicorn[standard] / python-multipart，纯标准库实现飞书登录，无额外 SDK）
- 启动命令（已在 `Procfile` 配好）：
  ```
  uvicorn app:app --host 0.0.0.0 --port $PORT
  ```
  自建场景建议用 systemd/supervisor 常驻，前面挂 Caddy/Nginx 反代。

## 三、数据持久化（重要，否则重启丢数据）

SQLite 库和上传附件目录必须落在持久磁盘上，路径用环境变量指定：
- `CLAIM_POOL_DB`（默认 `./claim_pool.db`）→ 指向持久目录，如 `/data/claim_pool.db`
- `CLAIM_UPLOAD_DIR`（默认 `./uploads`）→ 如 `/data/uploads`

首次启动自动建表（`init_db`），无需手动初始化。部门/中心/项目分类数据在仓库 `catalog.json`，随代码走。

## 四、环境变量

| 变量 | 值 / 说明 |
|---|---|
| `FEISHU_APP_ID` | `cli_aaa4826f2ff9dbd5` |
| `FEISHU_APP_SECRET` | 敏感，找 Lear 获取（勿提交仓库） |
| `FEISHU_SCOPE` | `contact:user.base:readonly` |
| `FEISHU_REDIRECT_URI` | `https://<正式域名>/oauth/callback`（拿到域名后填） |
| `FEISHU_SUPERADMIN_OPEN_IDS` | `ou_9c5ee286ee29ba68b58b97b0dc3087d6`（Lear，超管根权限） |
| `SESSION_SECRET` | 生成一串随机：`python -c "import secrets;print(secrets.token_hex(32))"` |
| `CLAIM_POOL_DB` | 持久路径，如 `/data/claim_pool.db` |
| `CLAIM_UPLOAD_DIR` | 持久路径，如 `/data/uploads` |

参考 `.env.example`。本地用 `.env` 文件，生产用平台/系统环境变量，`.env` 已 gitignore。

## 五、飞书对接（关键约束）

1. **回调地址必须是 HTTPS 公网地址**（飞书硬性要求）。
2. 部署拿到正式域名后，三处要一致：
   - 环境变量 `FEISHU_REDIRECT_URI = https://域名/oauth/callback`
   - 飞书开放平台后台 → 安全设置 → **重定向 URL**：加 `https://域名/oauth/callback`
   - 网页应用 → **主页地址**：设 `https://域名/`
   - 改完在飞书后台 **重新发布版本**
3. 服务端需能访问 `open.feishu.cn`、`accounts.feishu.cn`（换 token、取用户信息）。云服务器直连即可。
4. 已开通权限：`contact:user.base:readonly`（登录拿姓名/open_id）。当前部门靠用户首次登录自选 + 管理员后台调整，未读飞书组织架构。

## 六、权限模型

- **超级管理员**：`FEISHU_SUPERADMIN_OPEN_IDS` 白名单（只能改环境变量）。
- **管理员**：超管登录后在「管理后台 → 管理员管理」里勾选，存数据库 `app_users` 表，**免改配置、即时生效**。
- **普通用户**：飞书登录后默认。

## 七、部署方式（二选一）

**A. 腾讯云轻量服务器（已有一台，在跑 openclaw）**
- 需要：一个域名 + SSL 证书（HTTPS）。境内服务器的域名需 ICP 备案（约 1-2 周）；香港/境外节点免备案。
- 建议：Caddy 反代（自动 Let's Encrypt HTTPS）→ uvicorn(127.0.0.1:某端口)；注意别和现有 openclaw 端口/域名冲突，给认领系统单独子域名。
- 数据库/上传目录放服务器固定路径并定期备份。

**B. Zeabur 等托管平台（最省心，免域名/备案/SSL/运维）**
- 连本 GitHub 仓库自动构建（识别 Procfile）；平台自带 HTTPS 域名。
- 挂持久卷，把 `CLAIM_POOL_DB`/`CLAIM_UPLOAD_DIR` 指向卷内路径。
- 环境变量在平台后台配置。

## 八、代码仓库

GitHub（private）：https://github.com/Leariceleto/claim-pool

有疑问联系 Lear。

## 九、2026-09-20 正确性修复升级

### 范围与准备

- 仍是单文件 FastAPI + SQLite。没有新增运行依赖、中间件或常驻服务，不需要 Redis/Celery。
- 新增同库 `notification_outbox` 表和索引，启动自动创建；补列沿用 `ensure_column`，可重复执行。旧 `payment_reminders` 快照、逐人成功记录会继续使用，不重发已记录成功的接收人。
- 不新增环境变量。保留原有非空 `SESSION_SECRET`、飞书配置、`CLAIM_POOL_DB` 和 `CLAIM_UPLOAD_DIR`。所有数据页面、导出及写接口都要求签名登录，旧 `REQUIRE_LOGIN_FOR_CLAIM=false` 不再允许匿名认领。
- 升级前停止旧进程并备份现有 SQLite（可用 sqlite3 的 `.backup`）及附件，再启动新版本；不要混跑旧通知发送器与新队列，不要清空或替换生产库。保持现有单实例部署，不为本次升级增加 workers。
- 先在独立测试库执行 `/usr/bin/python3 -m unittest discover -s tests`（生产对应 Python 命令按环境替换）。测试不会访问生产库或发送真实消息，无需额外测试组件。本地已验证 Python 3.9；生产 Python 3.12 仍需运行验收。

### 可观察的变化

- 余额校验与认领/取消/后台更正同事务。非法金额报错；单独取消退款行若使净额为负或超过到款，会拒绝，需关联修正或整笔驳回再认领。
- 普通取消不会重新打开关闭款项。有有效认领时不能直接标记待认领；退回需使用明确的“驳回退回”，保留认领历史。
- 通知不在业务请求中同步发送。写入成功意味着通知已入队，不等于已经送达；审计记录使用 `notification_queued`/`admin_queued_count`，真实结果查看队列表。
- 后台线程轮询队列；发送网络请求时不持写锁。失败从 60 秒开始指数退避，最长 1 小时；进程异常后的发送租约在 5 分钟后可重新领取。服务必须常驻。
- 次日 17 点仍未确认的草稿不发提醒，但保留调度，之后确认入池会补查。旧流水没有调度时间的不追溯；旧提醒快照的未发送接收人继续重试。
- CSV/纯文本统一排除草稿、关闭及旧 rejected 款项，只计算有效认领及剩余额度。异常净额会报错而非导出错误合计。
- 页面表单在原位显示校验错误并保留输入，成功后显示反馈；批量认领显示成功/跳过笔数。网络中断时不自动重试 POST，需先刷新核对结果。普通浏览器表单仍使用 303 跳转；带 `X-Requested-With: fetch` 的 POST 成功返回 JSON 中的 `redirect` 和 `message`。页面脚本与后端必须同时更新。

### 验收与排错

1. 未登录访问 `/me` 应跳转登录，未登录 POST 应返回 401；正常登录及管理员身份变更后权限正常。
2. 两人同时认领同一笔剩余金额，只允许符合最新余额的请求成功；拒绝时原数据不变。
3. 验证本人取消通知只发管理员、不发超管；入池群通知和董芳/何玲私信保持原接收范围。
4. 通过下列只读 SQL 检查发送状态，不要靠“HTTP 307 正常”判断机器人已送达：

```sql
SELECT status, COUNT(*) FROM notification_outbox GROUP BY status;
SELECT id, event_key, recipient_type, recipient_id, status, attempts,
       last_attempt_at, last_error, datetime(available_at, 'unixepoch') AS retry_at_utc,
       sent_at
FROM notification_outbox ORDER BY id DESC LIMIT 30;
```

上线前可只读核对历史净额异常；本次不自动改账：

```sql
SELECT p.id, p.amount_cents,
       COALESCE(SUM(CASE WHEN c.status IN ('pending', 'accepted')
                        THEN c.amount_cents ELSE 0 END), 0) AS active_cents
FROM payments p LEFT JOIN claims c ON c.payment_id = p.id
GROUP BY p.id
HAVING active_cents < 0 OR active_cents > p.amount_cents;
```

通知使用持久化 UUID 降低重试重复风险。飞书的相同 UUID 去重窗口只有 1 小时；发送成功、结果落库前崩溃且超过窗口才恢复，仍可能重复，不承诺跨系统严格恰好一次。[飞书官方 SDK 参数说明](https://larksuite.github.io/oapi-sdk-java/com/lark/oapi/service/im/v1/model/CreateMessageReqBody.Builder.html#uuid(java.lang.String))

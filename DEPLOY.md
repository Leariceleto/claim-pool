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

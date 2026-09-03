# 🚀 部署指南

## 方式一：使用 Windows `exe`

从 GitHub Releases 下载对应版本的 `OutlookEmail-windows-x64-*.zip`，解压后直接运行 `OutlookEmail.exe`。

**桌面版首次启动会自动：**
- 创建本地数据目录
- 初始化数据库
- 自动生成并持久化 `SECRET_KEY`

**Windows 默认数据目录：**
- `%APPDATA%\OutlookEmail`

默认访问地址仍为 `http://127.0.0.1:5000`。

## 方式二：使用 Docker（推荐服务器部署）

直接使用 GitHub Actions 自动构建的镜像，无需本地构建：

```bash
# 拉取最新镜像
docker pull seldomzq/email:latest

# 运行容器
docker run -d \
  --name outlook-mail-reader \
  -p 5000:5000 \
  -v $(pwd)/data:/app/data \
  -e LOGIN_PASSWORD=admin123 \
  -e SECRET_KEY=your-secret-key-here \
  seldomzq/email:latest

# 查看日志
docker logs -f outlook-mail-reader

# 停止容器
docker stop outlook-mail-reader
docker rm outlook-mail-reader
```

**首次启动会自动：**
- 创建数据目录
- 初始化数据库
- 创建默认分组和临时邮箱分组
- 设置默认密码（admin123）

## 方式三：使用 Python 直接运行

```bash
# 克隆仓库
git clone https://github.com/assast/outlookEmail.git
cd outlookEmail

# 安装依赖
pip install -r requirements.txt

# 设置环境变量
export LOGIN_PASSWORD=admin123
export SECRET_KEY=your-secret-key-here
export PORT=5000

# 运行应用
python web_outlook_app.py
```

访问 `http://localhost:5000` 即可使用。
服务器部署建议始终显式设置固定 `SECRET_KEY`。

## 运行模式说明

服务需要保持单 worker 运行。Token 刷新管理里的流式任务、导出验证等短期任务使用进程内状态保存；如果自定义部署成多个 worker，POST 初始化任务和后续 SSE 订阅可能落到不同进程，导致任务不存在或过期。

官方 Docker 镜像已固定为 Gunicorn 单 worker，并通过线程处理慢请求：

```bash
gunicorn -k gthread -w 1 --threads ${GUNICORN_THREADS:-4} ...
```

如需调整并发，请优先调整 `GUNICORN_THREADS`，不要增加 worker 数。

## Linux 服务器一键安装

安装脚本支持 Ubuntu、Debian、CentOS、RHEL、Rocky Linux 和 AlmaLinux。脚本会检测 Docker Engine 与 Docker Compose Plugin；缺少时通过 Docker 官方 CE 软件源安装，并启动 Docker 服务。

在希望部署的目录执行。脚本内嵌 `docker-compose.yml`，不会下载项目源码或要求手工创建配置文件；脚本会在当前目录生成 Compose 配置、`.env` 和 `data` 目录：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/seldom1024/email-scripts/refs/heads/master/install.sh)
```

root 用户直接运行即可；非 root 用户脚本会按需调用 `sudo`。如果使用仓库内的脚本文件，也可以执行 `bash scripts/install.sh`。需要指定其他部署目录时使用 `--install-dir PATH`（`--project-dir` 仍兼容）。默认使用 `seldomzq/email:latest`、容器名 `outlook-mail-reader` 和宿主端口 `5000`；命令末尾增加 `--v 3.6` 即部署 `seldomzq/email:3.6`，增加 `--n test-mail` 可自定义容器名，增加 `--p 5001` 可指定宿主端口。

脚本首次运行会提示输入 `LOGIN_PASSWORD` 和 `SECRET_KEY`。直接回车会生成安全随机值，并将配置保存到 `.env`（权限 `600`）。已有非空值会在重复运行时复用，不会因为重新安装而更换 `SECRET_KEY`。

默认宿主端口为 `5000`。如果端口已被占用，脚本会要求输入 `1024-65535` 范围内的可用端口；也可以通过 `--p PORT` 显式指定端口。显式指定的端口无效或已被占用时脚本会直接失败，不会等待交互输入；容器内部端口始终为 `5000`。

每次运行都会执行：

```bash
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
```

即使本地已有 `seldomzq/email:latest`，也会检查 Docker Hub 的最新镜像。远程拉取失败时脚本会停止，不会静默使用旧本地镜像。脚本不会删除其他容器、镜像、数据目录或数据卷。

安装完成后，脚本会输出访问 URL、容器名称、登录密码和 `SECRET_KEY`。升级时保留 `.env` 与 `./data`，再次执行同一脚本或运行上面的 Compose 命令即可。

## 分布式只读副本

这套副本复制协议不依赖 Nginx Proxy Manager。主节点默认仍按现有方式运行；副本节点只需要单独指定 `NODE_ROLE=replica`、`MASTER_URL` 和本地独立的 `SECRET_KEY`。

副本的首次安装可以直接在空目录执行：

```bash
bash scripts/install-node.sh --master https://PRIMARY_HOST --node-id NODE_ID --master-fingerprint SHA256:...
```

脚本会在当前目录生成 `docker-compose.yml`、`.env` 和 `data/cluster/identity.db`，并在重复运行时复用已有身份、重新拉取所选版本的 `seldomzq/email` 镜像。增加 `--v 3.6` 可部署 `seldomzq/email:3.6`，增加 `--n test-mail` 可自定义副本容器名，增加 `--p 5001` 可指定宿主端口，省略时分别使用 `latest`、`outlook-mail-reader` 和 `5000`。`LOGIN_PASSWORD` 不参与副本运行；`SECRET_KEY` 为空时会自动生成并保存。

副本只同步 `/api/v1/mailboxes/messages` 所需的数据。公共 API Key 只保留鉴权与绑定所需的最小字段，不会同步可恢复密钥、名称或备注；因此副本列表页只应依赖摘要、后缀、绑定账号和过期信息。

副本在首次快照前不会提供邮件查询；如果最后一次成功同步距当前超过 24 小时，邮件查询也会停止。可用的健康接口是：

```text
/health/live
/health/ready
/api/v1/cluster/status
```

### 复制协议 v3 升级

协议 v3 会把邮箱分享链接的稳定路径段随账号数据同步。主节点与副本节点可以继续使用各自独立的 `SECRET_KEY`；主节点生成的 `/show/...` 和 `/query/...` 链接在副本节点上也能使用。

协议版本必须一致，不能混用 v2 与 v3。升级时应安排同一维护窗口：先升级主节点，再升级所有副本节点，并等待每个副本的 `/health/ready` 恢复为 `200`。已有副本首次以 v3 启动时会清除旧的同步成功标记并强制拉取完整快照；快照完成前相关查询返回 `503 replica_not_ready`，这是预期行为。升级过程中保留各节点原有的 `.env`、`SECRET_KEY`、数据目录和副本身份库，不要把主节点密钥复制到副本。

### Enrollment、凭据轮换与恢复

主节点设置页的“节点管理”可以创建节点、生成一次性 enrollment token，并显示包含 `MASTER_URL`、节点 ID 和主节点指纹的安装命令。token 只在结果窗口中显示一次，不会写入 `.env` 或 Compose 文件。副本安装脚本首次运行时从标准输入读取 token；重复运行会校验并复用本地身份，然后先拉取最新镜像再启动。

节点凭据轮换由主节点发起，副本在收到新版本并成功提交后再确认；撤销会立即阻止该节点继续同步，只有状态为 `revoked` 的节点才允许删除。删除或损坏副本的 `data/cluster/identity.db` 后，必须重新创建/签发节点并执行一次新的 enrollment，不能把另一台机器的身份库复制过来。

主节点会按事件保留周期清理增量事件。副本发现游标早于保留窗口时会收到 `snapshot_required`，自动重新拉取完整快照。备份时应将 `.env`、`SECRET_KEY` 和 `data/cluster/identity.db` 视为密钥材料单独保护；不要把副本身份库恢复到另一节点，也不要把 enrollment token 放入备份。更换副本主机时使用新的本地 `SECRET_KEY` 并重新 enrollment。

同步请求和响应在应用层使用 X25519/HKDF 派生密钥、AES-GCM 加密和 HMAC 请求签名，因此即使节点之间暂时通过公网 HTTP，邮箱凭据、API key 和邮件数据也不会以明文出现在同步载荷中。但 HTTP 仍会暴露流量元数据并允许主动网络攻击，生产环境应优先使用 HTTPS 或可信专网；应用层加密不是 TLS 的替代品。

## Nginx Proxy Manager 一键安装

如果希望由 Nginx Proxy Manager（NPM）提供唯一公网入口，请在一个独立的部署目录执行：

```bash
mkdir outlook-email-npm
cd outlook-email-npm
bash <(curl -fsSL https://raw.githubusercontent.com/seldom1024/Email/refs/heads/feature/public-mailbox-messages/scripts/install-with-npm.sh)
```

NPM 安装命令支持可选的 `--v VERSION` 和 `--n CONTAINER_NAME` 参数，例如 `--v 3.6 --n test-mail`；省略时分别使用 `seldomzq/email:latest` 和 `outlook-mail-reader`。自定义容器名后，Proxy Host 的 Forward Hostname / IP 也应填写该名称。

仓库内脚本也可直接运行：

```bash
bash scripts/install-with-npm.sh
```

脚本支持与普通一键安装相同的 Linux 发行版、Docker 自动安装、root/sudo 判断、`--install-dir PATH` 和 `--project-dir PATH`。它会在安装目录生成：

```text
docker-compose.yml
.env
npm/data/
npm/letsencrypt/
email/data/
```

其中 NPM 与邮件服务的数据目录相互隔离。`.env` 权限为 `600`；已有 `LOGIN_PASSWORD` 和 `SECRET_KEY` 会在重复执行时复用。

### 网络和端口

两个容器加入名为 `npm` 的可附加 Docker 网络。只有 NPM 发布宿主机端口：

- `80`: HTTP 代理入口
- `443`: HTTPS 代理入口
- `81`: NPM 管理页面

邮件服务不映射宿主机 `5000` 端口，因此不能访问 `http://SERVER_IP:5000`。它只能由同一 Docker 网络中的 NPM 通过 `outlook-mail-reader:5000` 访问。

启动前脚本会检查 `80`、`443` 和 `81`。只要任一端口被其他进程或容器占用，脚本就会列出冲突端口并停止；不会关闭占用服务，也不会询问替代端口。重复执行时，由脚本自己的 `nginx-proxy-manager` 容器占用这些端口属于正常状态。

### NPM 配置

安装完成后打开：

```text
http://SERVER_IP:81
```

完成 NPM 首次初始化，然后创建 Proxy Host。域名或公网 IP 按实际部署填写，上游必须使用 Docker 容器名称，而不是 `localhost`：

```text
Scheme: http
Forward Hostname / IP: outlook-mail-reader
Forward Port: 5000
```

在 Proxy Host 创建完成前，邮件服务不会从公网直接访问。只有公网 IP、没有域名时可以先使用 HTTP；申请受信任的 HTTPS 证书通常需要一个解析到该服务器的域名。

### 启动与升级顺序

每次运行脚本都会按以下顺序执行：

1. 拉取 `jc21/nginx-proxy-manager:latest`。
2. 启动 NPM，并等待管理端口 `81` 可用。
3. 拉取 `seldomzq/email:latest`。
4. 启动 `outlook-mail-reader`，并从容器内部检查端口 `5000`。

本地已有镜像时仍会拉取远程最新版本。任何镜像拉取失败都会停止，不会使用缓存旧镜像继续启动。邮件服务启动失败时，已经正常运行的 NPM 会保留，脚本不会执行破坏性回滚。

脚本会覆盖安装目录中的 `docker-compose.yml`，因此不要在包含其他 Compose 项目的目录运行。重复执行不会删除 `npm/data`、`npm/letsencrypt`、`email/data`、镜像或数据卷。

排查命令：

```bash
docker compose ps
docker compose logs --tail 100 npm
docker compose logs --tail 100 outlook-mail-reader
```

## 使用 Docker Compose

```yaml
version: '3.8'

services:
  outlook-mail-reader:
    image: seldomzq/email:latest
    container_name: outlook-mail-reader
    ports:
      - "5000:5000"
    volumes:
      - ./data:/app/data
    environment:
      - LOGIN_PASSWORD=admin123
      - SECRET_KEY=your-secret-key-here
      - FLASK_ENV=production
      - GPTMAIL_API_KEY=your-api-key
    restart: unless-stopped
```

```bash
# 启动服务
docker-compose up -d

# 查看定时任务启动日志（应出现“定时任务已启动”）
docker-compose logs -f

# 停止服务
docker-compose down
```

## 定时刷新说明

- 应用在 `python web_outlook_app.py`、Docker、Docker Compose、Gunicorn 单 worker 模式下都会自动初始化定时任务。
- 如需确认定时任务是否已启动，可执行 `docker-compose logs -f`，日志中应出现“定时任务已启动”。
- 若使用 Cron 模式，请确认已在系统设置中开启 `use_cron_schedule`，并填写正确的 5 段 Cron 表达式。

## 环境变量配置

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `SECRET_KEY` | Session 密钥（服务器部署强烈建议固定设置） | Windows `exe` 首次启动会自动生成并持久化；Docker / Python / 生产环境请显式设置固定值，不要随意修改，否则会导致已存储敏感数据无法解密 |
| `LOGIN_PASSWORD` | 登录密码 | `admin123` |
| `FLASK_ENV` | 运行环境 | `production` |
| `PORT` | 应用端口 | `5000` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `DATABASE_PATH` | 数据库路径 | `data/outlook_accounts.db` |
| `GPTMAIL_BASE_URL` | GPTMail API 地址 | `https://mail.chatgpt.org.uk` |
| `GPTMAIL_API_KEY` | GPTMail API Key | `gpt-test` |
| `DUCKMAIL_BASE_URL` | DuckMail API 地址 | `https://api.duckmail.sbs` |
| `DUCKMAIL_API_KEY` | DuckMail API Key | 空 |
| `CLOUDFLARE_WORKER_DOMAIN` | Cloudflare Temp Email Worker 域名，也兼容读取 `WORKER_DOMAIN` | 空 |
| `CLOUDFLARE_EMAIL_DOMAINS` | Cloudflare 临时邮箱域名列表，逗号分隔，也兼容读取 `EMAIL_DOMAIN` | 空 |
| `CLOUDFLARE_ADMIN_PASSWORD` | Cloudflare 管理密码，也兼容读取 `ADMIN_PASSWORD` | 空 |
| `OAUTH_CLIENT_ID` | OAuth 客户端 ID | `建议使用自己的，如果实在搞不到不填的话会使用默认的` |
| `OAUTH_REDIRECT_URI` | OAuth 重定向 URI | `建议使用自己的，如果实在搞不到不填的话会使用默认的` |

**生成 SECRET_KEY：**
```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

## 数据持久化

数据库文件存储在 `./data` 目录中，通过 Docker Volume 挂载实现持久化。

自定义外观皮肤文件存储在数据库文件同目录下的 `skins/` 目录。默认数据库路径是 `data/outlook_accounts.db` 时，皮肤目录为 `data/skins/`。Docker 部署应继续挂载整个 `./data:/app/data`，不要只备份单个数据库文件，否则自定义皮肤文件会丢失并回退到内置 `classic` 皮肤。

数据库包含以下表：
- `settings` - 系统设置（登录密码、API Key 等）
- `groups` - 邮箱分组
- `accounts` - Outlook 邮箱账号
- `account_refresh_logs` - 账号刷新记录
- `temp_emails` - 临时邮箱
- `temp_email_messages` - 临时邮箱的邮件

## 端口映射

默认映射 5000 端口，可以在 `docker-compose.yml` 中修改：

```yaml
ports:
  - "8080:5000"  # 将容器的 5000 端口映射到主机的 8080 端口
```

## 镜像说明

项目使用 GitHub Actions 自动构建并推送 Docker 镜像，支持稳定版、开发版和正式版本标签。

### 可用镜像标签

- `seldomzq/email:latest` - 默认分支最近一次符合条件的稳定构建
- `seldomzq/email:main` - `main` 分支最近一次符合条件的构建
- `seldomzq/email:dev` - `dev` 分支最近一次符合条件的构建
- `seldomzq/email:vX.Y.Z` - 指定正式版本镜像，由手动发版工作流生成

GitHub Actions 发布到 Docker Hub 需要在仓库的 Actions secrets 中配置：

- `DOCKERHUB_USERNAME`: `seldomzq`
- `DOCKERHUB_TOKEN`: Docker Hub Access Token（不要填写账户密码）

补充说明：

- 文档改动不会触发 Docker 镜像重建
- 正式发版时建议优先使用 `vX.Y.Z` 明确版本标签
- 具体发版流程见仓库根目录的 `RELEASE.md`

### 更新镜像

```bash
docker pull seldomzq/email:latest
docker-compose down
docker-compose up -d
```

### 自己构建镜像（可选）

```bash
docker build -t outlookemail:local .
docker run -d \
  --name outlook-mail-reader \
  -p 5000:5000 \
  -v $(pwd)/data:/app/data \
  -e LOGIN_PASSWORD=admin123 \
  outlookemail:local
```

## 生产环境部署

### 使用 Nginx + HTTPS

**1. 安装 Nginx**
```bash
sudo apt install nginx certbot python3-certbot-nginx -y
```

**2. 配置 Nginx** `/etc/nginx/sites-available/outlook-mail-reader`
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 支持（如果需要）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

**3. 启用配置**
```bash
sudo ln -s /etc/nginx/sites-available/outlook-mail-reader /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

**4. 配置 HTTPS**
```bash
sudo certbot --nginx -d your-domain.com
```

### 使用 Caddy（更简单）

```bash
sudo apt install caddy -y

# 配置 /etc/caddy/Caddyfile
your-domain.com {
    reverse_proxy localhost:5000
}

# 重载（自动 HTTPS）
sudo systemctl reload caddy
```

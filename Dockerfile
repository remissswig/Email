# 使用 Python 3.11 作为基础镜像
FROM python:3.11-slim

ARG BUILD_GIT_SHA=unknown

# 设置工作目录
WORKDIR /app

# 安装 curl（用于健康检查）
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    BUILD_GIT_SHA=$BUILD_GIT_SHA \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GUNICORN_TIMEOUT=300 \
    GUNICORN_THREADS=32 \
    GUNICORN_BACKLOG=2048 \
    IMAP_TIMEOUT=45 \
    PUBLIC_MAILBOX_UPSTREAM_CONCURRENCY=100 \
    PUBLIC_MAILBOX_UPSTREAM_WORKERS=32 \
    PUBLIC_MAILBOX_UPSTREAM_ACQUIRE_TIMEOUT_SECONDS=5.0 \
    PUBLIC_MAILBOX_ACCOUNT_LOCK_ACQUIRE_TIMEOUT_SECONDS=3.0 \
    PUBLIC_MAILBOX_RESULT_CACHE_SECONDS=8 \
    PUBLIC_MAILBOX_BUSY_ERROR_CACHE_SECONDS=0.5 \
    PUBLIC_MAILBOX_ACCOUNT_SNAPSHOT_CACHE_SECONDS=8 \
    PUBLIC_MAILBOX_ACCOUNT_SNAPSHOT_COALESCE_SECONDS=0.05 \
    PUBLIC_MAILBOX_REDIS_POOL_SIZE=8 \
    PUBLIC_MAILBOX_REDIS_TIMEOUT_SECONDS=1.5 \
    PUBLIC_MAILBOX_STICKY_POOL_SIZE=16 \
    PUBLIC_MAILBOX_STICKY_POOL_BLOCK=true \
    PUBLIC_MAILBOX_STICKY_NODE_COOLDOWN_SECONDS=5 \
    PUBLIC_MAILBOX_EMPTY_CACHE_SECONDS=5 \
    PUBLIC_MAILBOX_ERROR_CACHE_SECONDS=10 \
    PUBLIC_MAILBOX_IMAP_AUTH_ERROR_CACHE_SECONDS=60

# 复制依赖文件
COPY requirements.txt .

# 安装依赖（包括生产服务器）
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install gunicorn

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 5000

# 启动应用（保持单 worker，使用线程提升慢请求容错）
CMD ["sh", "-c", "gunicorn -k gthread -w 1 --threads ${GUNICORN_THREADS:-32} --backlog ${GUNICORN_BACKLOG:-2048} -b 0.0.0.0:5000 --timeout ${GUNICORN_TIMEOUT:-300} --graceful-timeout 30 --access-logfile - --error-logfile - --capture-output web_outlook_app:app"]

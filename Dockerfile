FROM node:22-slim

# OCI 标签（GHCR 页面上会显示这些信息）
LABEL org.opencontainers.image.title="MusicSync" \
      org.opencontainers.image.description="网易云歌单同步：自动下载、整理标签/封面/歌词、备份到网易云云盘" \
      org.opencontainers.image.source="https://github.com/yq487900/music-sync" \
      org.opencontainers.image.url="https://github.com/yq487900/music-sync"

# 构建期镜像源（本机 pypi.org 不可达；换源可覆盖，如 --build-arg PIP_INDEX=https://pypi.org/simple）
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com

# Python 与运行时依赖（Debian 12 → Python 3.11）
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg jq supervisor ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 网易云接口服务（纯 JS，无原生模块，装进同一容器，仅监听本机回环）
RUN mkdir -p /opt/ncm-api \
    && cd /opt/ncm-api \
    && npm install --omit=dev --no-audit --no-fund --registry="$NPM_REGISTRY" \
         @neteasecloudmusicapienhanced/api@4.40.1 \
    && npm cache clean --force

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX" -r requirements.txt

COPY app/ ./app
COPY sources/ ./sources
COPY supervisord.conf entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

ENV APP_PORT=13570 \
    NCM_API=http://127.0.0.1:3000 \
    LX_URL=http://127.0.0.1:3100

EXPOSE 13570

# 健康检查：/api/status 不需要登录，容器内自探（不用 curl，alpine/debian 都可能没装）
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD ["python3", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:13570/api/status', timeout=5).status == 200 else 1)"]

ENTRYPOINT ["/app/entrypoint.sh"]

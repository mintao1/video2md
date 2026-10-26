# video2pdf 容器镜像：python3.12-slim + ffmpeg + 项目依赖（不含 whisper/torch——
# 转写/OCR/剪辑默认走 Colab CLI，见 README 的 Colab CLI GPU 配置）。
# 在目标架构上构建（服务器 x86：直接在目标机上 docker build，避免 Mac arm64 交叉构建；
# CI/registry 走 buildx 多架构，见 .github/workflows/ci.yml 的 release job）。

# 同一镜像携带生产 Web bundle，避免公网 Nginx 落回默认欢迎页；API 仍可
# 单独运行，bundle 不存在时不会改变 API-only 行为。
FROM node:24-alpine AS web-build
WORKDIR /web
COPY frontend/web/package.json frontend/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/web/ ./
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL="${VITE_API_BASE_URL}"
RUN npm run build

FROM python:3.12-slim

# 版本注入（CI release job 传 --build-arg；本地构建缺省空，运行时版本以 pyproject 兜底为准）
ARG VERSION=""
ARG GIT_SHA=""
LABEL org.opencontainers.image.title="video2pdf" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/mintao1/video2md" \
      org.opencontainers.image.vendor="lcode-platform"

# Debian 源换清华镜像（国内服务器从 deb.debian.org 拉 ffmpeg 依赖龟速实测 ~60KB/s；
# Mac/海外构建也无害——tuna 全球可达）
# PDF 导出（weasyprint）：Pango 排版 + 中文字体；构建期生成字体缓存，只读根文件系统下无需再写
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig fonts-noto-cjk \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

# uv 以 pip 包安装（ghcr.io 在国内服务器拉不动；PyPI 走镜像站即可）。
# 构建参数 PIP_INDEX_URL：服务器上传清华镜像（https://pypi.tuna.tsinghua.edu.cn/simple），
# uv 装包走同一镜像（UV_INDEX_URL 跟随）。
ARG PIP_INDEX_URL=""
# UV_HTTP_TIMEOUT=300：tuna 大 wheel（regex/greenlet 级）实测超缺省 30s——
# 2026-09-27 服务器构建两次同因超时后入册
ENV PIP_INDEX_URL="${PIP_INDEX_URL}" \
    UV_INDEX_URL="${PIP_INDEX_URL}" \
    UV_HTTP_TIMEOUT=300
RUN pip install --no-cache-dir uv

WORKDIR /app
# 依赖层先行：源码变动不击穿缓存。package=false 项目，uv sync 只装依赖
# --extra evals：离线评测旁路（backend/scripts/eval_notes.py）在容器内可跑（deepeval/ragas/langfuse SDK）
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/vendor/ ./vendor/
RUN uv sync --frozen --no-dev --extra evals
# 无 mac extra：pyobjc 是 macOS 专属，Linux 镜像天然不含

COPY backend/video2pdf/ ./video2pdf/
# 改名过渡期垫片：旧清单的 `python -m video2md` 仍可用
COPY backend/video2md/ ./video2md/
COPY backend/acctkit/ ./acctkit/
COPY backend/gpu_plugins/ ./gpu_plugins/
COPY --from=web-build /web/dist/ ./web-dist/
COPY README.md ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    V2PDF_HOST=0.0.0.0 \
    V2PDF_STATE_FILE=/app/data/state.json \
    V2PDF_RUNS_FILE=/app/data/runs.json \
    V2PDF_PREFS_FILE=/app/data/prefs.json \
    V2PDF_LOG_DIR=/app/data/logs \
    XDG_CACHE_HOME=/tmp/.cache

VOLUME ["/app/data", "/vault"]
EXPOSE 8790

# 非 root 运行（平台基线 L0：ADR-0020）
RUN useradd -u 10001 -m -d /home/appuser appuser \
    && mkdir -p /app/data /vault \
    && chown -R appuser:appuser /home/appuser /app/data /vault
USER 10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=4)"]

CMD ["python", "-m", "video2pdf", "api", "--port", "8790"]

# video2md 容器镜像：python3.12-slim + ffmpeg + 项目依赖（不含 whisper/torch——
# 转写/OCR/剪辑走 gpu_router 远程池，见 docker-compose.yml 的 V2MD_*_REMOTE）。
# 在目标架构上构建（服务器 x86：直接在 lcode-4c 上 docker build，避免 Mac arm64 交叉构建）。
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
# 依赖层先行：源码变动不击穿缓存。package=false 项目，uv sync 只装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# 无 mac extra：pyobjc 是 macOS 专属，Linux 镜像天然不含

COPY video2md/ ./video2md/
COPY README.md ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    V2MD_HOST=0.0.0.0 \
    V2MD_STATE_FILE=/app/data/state.json \
    V2MD_RUNS_FILE=/app/data/runs.json \
    V2MD_LOG_DIR=/app/data/logs

VOLUME ["/app/data", "/vault"]
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=4)"]

CMD ["python", "-m", "video2md", "api", "--port", "8790"]

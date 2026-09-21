# video2md 容器镜像：python3.12-slim + ffmpeg + 项目依赖（不含 whisper/torch——
# 转写/OCR/剪辑走 gpu_router 远程池，见 docker-compose.yml 的 V2MD_*_REMOTE）。
# 在目标架构上构建（服务器 x86：直接在 lcode-4c 上 docker build，避免 Mac arm64 交叉构建）。
FROM python:3.12-slim

# Debian 源换清华镜像（国内服务器从 deb.debian.org 拉 ffmpeg 依赖龟速实测 ~60KB/s；
# Mac/海外构建也无害——tuna 全球可达）
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# uv 以 pip 包安装（ghcr.io 在国内服务器拉不动；PyPI 走镜像站即可）。
# 构建参数 PIP_INDEX_URL：服务器上传清华镜像（https://pypi.tuna.tsinghua.edu.cn/simple），
# uv 装包走同一镜像（UV_INDEX_URL 跟随）。
ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL="${PIP_INDEX_URL}" \
    UV_INDEX_URL="${PIP_INDEX_URL}"
RUN pip install --no-cache-dir uv

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

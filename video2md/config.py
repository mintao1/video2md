"""配置与基座：项目 .env（非机密）+ 凭证文件（机密）+ 路径常量。"""
from __future__ import annotations

import os
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent  # 包目录的上一级 = 项目根（state.json/runs.json/.env 都在根上）


MAX_TRANSCRIPT_CHARS = 60000  # 约等于 3 小时密集语音


LOW_CONF = 0.5


BLUR_SHARP_MIN = 2.5  # 中心裁剪区边缘均值；白底幻灯片约 3-5，真模糊帧 <2


FRAME_MAE_MAX = 2.0   # 相邻帧 160x90 灰度 MAE：重复帧 <1，不同文字幻灯片 >3


FRAME_DEDUP_EPS = 0.4   # 候选帧微去重（秒）：网格/场景两路同一瞬间重复采样才去重，内容去重留给选帧




CLIP_SPAN = 10        # 视频节段：帧时间戳 ±10s 剪 20s 可播片段（对齐参考笔记的内嵌 mp4）



# ---------------------------------------------------------------- env / paths
def _read_env_file(p: Path) -> dict[str, str]:
    """读 KEY=VALUE 行（忽略注释/空行；值可带引号）。"""
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out



# 机密凭证 SSOT：~/.config/video2md.env（0600；LLM_API_KEY 等）。
# Dify 抽离（2026-09-21）后不再经 dify_kit 注入 dify-zcode.env。
SECRETS_FILE = Path(ENV_SECRETS_OVERRIDE) if (ENV_SECRETS_OVERRIDE := os.environ.get("V2MD_SECRETS_FILE")) \
    else Path.home() / ".config" / "video2md.env"



def load_env() -> dict[str, str]:
    """合并优先级：项目 .env（非机密兜底）< 凭证文件 < 真实环境变量。"""
    env = _read_env_file(PROJ / ".env")
    env.update(_read_env_file(SECRETS_FILE))
    env.update({k: v for k, v in os.environ.items() if v})  # 真实环境变量优先
    return env



ENV = load_env()


# 持久化文件位置：容器里由 compose 指到 data 卷（/app/data/…），本机默认项目根
STATE_FILE = Path(ENV.get("V2MD_STATE_FILE", str(PROJ / "state.json")))


LOG_DIR = Path(ENV.get("V2MD_LOG_DIR", str(PROJ / "logs")))


# 代理键透传给子进程/httpx（YouTube 等境外源下载；国内站代码层强制直连）。
# 真实环境变量优先，.env 只作兜底。
# （Colab/Google OAuth 流量已随算力池迁出本进程，见 gpu_router 项目 config.load_dotenv_proxy）
for _pk in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
    if not os.environ.get(_pk) and ENV.get(_pk):
        os.environ.setdefault(_pk, ENV[_pk])


# 身份表（多用户鉴权：(provider, external_id) → 稳定内部 uid）：容器里指到 data 卷
USERS_FILE = Path(ENV.get("V2MD_USERS_FILE", str(PROJ / "users.json")))

# 鉴权配置解析已收进 acctkit.build_from_env（V2MD_AUTH / V2MD_AUTH_PROVIDERS /
# GOOGLE_CLIENT_ID 语义见 acctkit/engine.py 与 README「acctkit」节；off 缺省保证
# e2e/batch 无 token 也能跑），接线在 video2md/auth/__init__.py，此处不再镜像一份。
# CORS 放行 origin（逗号分隔；GUI 前端域名）。空=不加 CORS 中间件（同源/隧道开发够用）
CORS_ORIGINS = [o.strip() for o in (ENV.get("V2MD_CORS_ORIGINS", "") or "").split(",") if o.strip()]


VAULT = Path(ENV.get("OBSIDIAN_VAULT", str(Path.home() / "Documents/Obsidian Vault")))


OUT_DIR = VAULT / "资料库" / "视频笔记"


INDEX_FILE = OUT_DIR / "索引.md"


ATTACH_DIR = OUT_DIR / "attachments"      # 嵌入帧（![[attachments/...]] 按文件名全库可解析）


TRANSCRIPT_DIR = OUT_DIR / "transcripts"  # 转录本 SSOT（info callout wikilink）


# 产物落点模式（缝在 artsink.py，写盘函数不感知）：
#   vault（默认）= 直写 vault 卷 + Syncthing 同步（现状逐字节不变）
#   deliver      = 多用户「云端不储存」：产物进 per-run outbox，终态后搬到 OUTBOX_DIR，
#                  只经 bundle/ACK HTTP 端点发送到用户端，TTL 到期/确认收完即焚（api.py）
ARTIFACT_MODE = (ENV.get("V2MD_ARTIFACT_MODE") or "vault").strip().lower()
if ARTIFACT_MODE not in ("vault", "deliver"):
    raise RuntimeError(f"未知 V2MD_ARTIFACT_MODE：{ARTIFACT_MODE}（支持 vault|deliver）")

# deliver 在架根（容器指 data 卷）与保留窗口（小时，到期自动烧）
OUTBOX_DIR = Path(ENV.get("V2MD_OUTBOX_DIR", str(PROJ / "data" / "outbox")))
OUTBOX_TTL_H = float(ENV.get("V2MD_OUTBOX_TTL_H", "24") or 24)


# run 卡死看门狗（watchdog.py）：心跳超时判定。单条 3h 转录属正常，默认 2h 无任何活动才判。
WATCHDOG_TIMEOUT_S = float(ENV.get("V2MD_WATCHDOG_TIMEOUT_S", "7200") or 7200)
WATCHDOG_PERIOD_S = float(ENV.get("V2MD_WATCHDOG_PERIOD_S", "60") or 60)
WATCHDOG_ACTION = (ENV.get("V2MD_WATCHDOG_ACTION") or "mark").strip().lower()
if WATCHDOG_ACTION not in ("mark", "exit"):
    raise RuntimeError(f"未知 V2MD_WATCHDOG_ACTION：{WATCHDOG_ACTION}（支持 mark|exit）")

# 执行层并发与配额（终局布局·执行层/限流配额的进程内落地）：
#   并发闸门默认 1 = 单 worker 语义；生产多用户调大——并发>1 时 checkpointer 建议
#   切 postgres（sqlite 单连接弱）、vault 自托管姿势建议保持 1。三个上限 0 = 不设限；
#   提交限流按 uid 60s 滑窗（入队前闸，429 信封）。
MAX_CONCURRENT_RUNS = max(1, int(ENV.get("V2MD_MAX_CONCURRENT_RUNS", "1") or 1))
QUEUE_MAX = int(ENV.get("V2MD_QUEUE_MAX", "100") or 0)
USER_QUEUE_MAX = int(ENV.get("V2MD_USER_QUEUE_MAX", "5") or 0)
USER_SUBMIT_PER_MIN = int(ENV.get("V2MD_USER_SUBMIT_PER_MIN", "30") or 0)

# 上传面（多用户专用 POST /uploads）：落地根/单文件上限/每用户配额/TTL 惰性清扫。
# 容器里 UPLOADS_DIR 指到 data 卷；TTL 到期物理删——「云端不储存」口径。
UPLOADS_DIR = Path(ENV.get("V2MD_UPLOADS_DIR", str(PROJ / "data" / "uploads")))
UPLOAD_MAX_GB = float(ENV.get("V2MD_UPLOAD_MAX_GB", "2") or 2)
UPLOAD_QUOTA_GB = float(ENV.get("V2MD_UPLOAD_QUOTA_GB", "5") or 5)
UPLOADS_TTL_H = float(ENV.get("V2MD_UPLOADS_TTL_H", "24") or 24)

# 本地路径输入策略（POST /runs 的 url 收非 http 时）：auto = AUTH=off 放行（开发/
# 自托管），AUTH≠off 只认本人 uploads 白名单内路径；allow/deny 显式覆盖。
LOCAL_PATHS = (ENV.get("V2MD_LOCAL_PATHS") or "auto").strip().lower()
if LOCAL_PATHS not in ("auto", "allow", "deny"):
    raise RuntimeError(f"未知 V2MD_LOCAL_PATHS：{LOCAL_PATHS}（支持 auto|allow|deny）")

# 版本单源：dist metadata（已安装时）→ pyproject.toml（源码/镜像直跑，Dockerfile 有 COPY）
# → "0.0.0"。FastAPI version 与 /healthz 都读这里，不手工双写。
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    VERSION = _pkg_version("video2md")
except PackageNotFoundError:
    try:
        import tomllib
        with (PROJ / "pyproject.toml").open("rb") as _pf:
            VERSION = str(tomllib.load(_pf)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        VERSION = "0.0.0"



# ---------------------------------------------------------------- fin
# dify_kit / DIFY_* / dataset_api 已随 Dify 抽离（2026-09-21）摘除：
# 知识库入库（kb 阶段）取消；LLM 凭证由 SECRETS_FILE 提供（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）。

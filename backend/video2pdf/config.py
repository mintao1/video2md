"""配置与基座：项目 .env（非机密）+ 凭证文件（机密）+ 路径常量。

P1-9 起配置是**契约**：键/类型/默认/校验器集中登记于 _SPEC 一表——新键=加一行，
别处零改动；启动期统一解析，非法值**一次性汇总报错**（非首错即停，运维一次改对）。

合并优先级：项目 .env（非机密兜底）< 凭证文件 < 真实环境变量。真实环境变量
**含空串**参与覆盖：空串 = 显式清空该键（回落表内默认）——修掉旧「空串被丢弃
→ .env 真值渗入」的隔离缺口（test_api_contract 实证踩坑：V2PDF_REMOTE="" 打不住
.env 的 9700）。消费者侧 `ENV.get(k, d) or d` 的 or 兜底语义不变。
"""
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


# 改名兼容（video2md → video2pdf）：旧前缀 V2MD_* 仍可用，新键未设时按旧键补齐。
# 真实环境变量同步写回 os.environ，直读 os.environ 的模块与子进程也看得到新键名。
LEGACY_ENV_PREFIX, ENV_PREFIX = "V2MD_", "V2PDF_"


def _alias_legacy(env: dict[str, str]) -> list[str]:
    """把 V2MD_X 补成 V2PDF_X（新键已存在则新键优先），返回被补齐的旧键名。"""
    aliased = []
    for k in [k for k in env if k.startswith(LEGACY_ENV_PREFIX)]:
        nk = ENV_PREFIX + k[len(LEGACY_ENV_PREFIX):]
        if nk not in env:
            env[nk] = env[k]
            aliased.append(k)
    return aliased


_LEGACY_ENV_KEYS = _alias_legacy(os.environ)  # type: ignore[arg-type]


# 机密凭证 SSOT：~/.config/video2pdf.env（0600；LLM_API_KEY 等）；旧文件名
# ~/.config/video2md.env 在新文件不存在时兜底。
# Dify 抽离（2026-09-21）后不再经 dify_kit 注入 dify-zcode.env。
def _default_secrets_file() -> Path:
    new, old = Path.home() / ".config" / "video2pdf.env", Path.home() / ".config" / "video2md.env"
    return old if not new.exists() and old.exists() else new


SECRETS_FILE = Path(ENV_SECRETS_OVERRIDE) if (ENV_SECRETS_OVERRIDE := os.environ.get("V2PDF_SECRETS_FILE")) \
    else _default_secrets_file()


def load_env() -> dict[str, str]:
    """合并优先级：项目 .env（非机密兜底）< 凭证文件 < 真实环境变量。

    真实环境变量含空串一并覆盖（= 显式清空 → 解析回落默认）；.env/凭证文件里的
    空串同样按「未设值」处理（_spec 解析统一口径）。
    """
    files = _read_env_file(PROJ / ".env")
    files.update(_read_env_file(SECRETS_FILE))
    _LEGACY_ENV_KEYS.extend(_alias_legacy(files))
    files.update(dict(os.environ))
    return files


ENV = load_env()
if _LEGACY_ENV_KEYS:
    import sys
    print(f"⚠️ 已弃用的 {LEGACY_ENV_PREFIX}* 配置仍在使用（{len(_LEGACY_ENV_KEYS)} 个，如 "
          f"{sorted(_LEGACY_ENV_KEYS)[0]}），请改为 {ENV_PREFIX}*", file=sys.stderr)


# ---------------------------------------------------------------- 配置契约表（P1-9）
# 行 = (类型, 默认, 约束/校验)。类型：path/str/int/float/enum/csv。
# 新键加一行即可；解析在 import 期完成（fail-fast 启动闸）——「import 期加载」的
# 原批判点在散落与静默，不在时机：启动期一次性全量报错正是要的语义。
_SPEC: dict[str, tuple] = {
    # 运行档位
    "V2PDF_ENV":             ("str", "development", None),
    "V2PDF_COLAB_USERS_DIR": ("path", str(PROJ / "data" / "colab-users"), None),
    # 持久化/身份/凭证（容器里指 data 卷；凭证文件只在此处定位）
    "V2PDF_STATE_FILE":      ("path", str(PROJ / "state.json"), None),
    "V2PDF_RUNS_FILE":       ("path", str(PROJ / "runs.json"), None),
    "V2PDF_USERS_FILE":      ("path", str(PROJ / "users.json"), None),
    "V2PDF_PG_DSN":          ("raw", "", None),
    "V2PDF_PG_TIMEOUT_S":    ("float", 10.0, ("min", 1.0)),
    "V2PDF_RUNS_STORE":      ("enum", "json", ("json", "postgres")),
    "V2PDF_STATE_STORE":     ("enum", "json", ("json", "postgres")),
    "V2PDF_USERS_STORE":     ("enum", "json", ("json", "postgres")),
    "V2PDF_EVENTS":          ("enum", "local", ("local", "pg")),
    "V2PDF_CHECKPOINTER":    ("enum", "sqlite", ("sqlite", "postgres")),
    "V2PDF_LOG_DIR":         ("path", str(PROJ / "logs"), None),
    "V2PDF_SECRETS_FILE":    ("path", str(SECRETS_FILE), None),
    "OBSIDIAN_VAULT":       ("path", str(Path.home() / "Documents/Obsidian Vault"), None),
    # 鉴权/CORS（V2PDF_AUTH 语义在 acctkit.build_from_env，此处不镜像——接线 auth/__init__.py）
    "V2PDF_CORS_ORIGINS":    ("csv", [], None),
    # 产物落点
    "V2PDF_ARTIFACT_MODE":   ("enum", "vault", ("vault", "deliver")),
    "V2PDF_OUTBOX_DIR":      ("path", str(PROJ / "data" / "outbox"), None),
    "V2PDF_OUTBOX_TTL_H":    ("float", 24.0, ("gt", 0.0)),
    # run 卡死看门狗
    "V2PDF_WATCHDOG_TIMEOUT_S": ("float", 7200.0, ("gt", 0.0)),
    "V2PDF_WATCHDOG_PERIOD_S":  ("float", 60.0, ("gt", 0.0)),
    "V2PDF_WATCHDOG_ACTION":    ("enum", "mark", ("mark", "exit")),
    # 执行层并发与配额（0 = 该项不设限）
    "V2PDF_MAX_CONCURRENT_RUNS": ("int", 1, ("min", 1)),
    "V2PDF_QUEUE_MAX":           ("int", 100, ("min", 0)),
    "V2PDF_USER_QUEUE_MAX":      ("int", 5, ("min", 0)),
    "V2PDF_USER_SUBMIT_PER_MIN": ("int", 30, ("min", 0)),
    # 上传面
    "V2PDF_UPLOADS_DIR":     ("path", str(PROJ / "data" / "uploads"), None),
    "V2PDF_UPLOAD_MAX_GB":   ("float", 2.0, ("gt", 0.0)),
    "V2PDF_UPLOAD_QUOTA_GB": ("float", 5.0, ("gt", 0.0)),
    "V2PDF_UPLOADS_TTL_H":   ("float", 24.0, ("gt", 0.0)),
    # 任务表真相源（P4b·多副本档）
    "V2PDF_TASKS_STORE":     ("enum", "json", ("json", "postgres")),
    # 教材偏好存储（收藏/阅读进度，(uid,run_id) 维度；web 教材库页数据源）
    "V2PDF_PREFS_FILE":      ("path", str(PROJ / "prefs.json"), None),
    "V2PDF_LLM_SETTINGS_FILE": ("str", "", None),  # 可选路径；空=跟随 PREFS_FILE 同目录
    "V2PDF_PREFS_STORE":     ("enum", "json", ("json", "postgres")),
    "V2PDF_COLAB_SESSION":   ("str", "v2pdf-whisper", None),
    "V2PDF_COLAB_BIN":       ("str", "", None),
    "V2PDF_COLAB_LOCK":      ("path", str(PROJ / "data" / "colab.lock"), None),
    "V2PDF_COLAB_MAX_WAIT_S": ("int", 2700, ("min", 30)),
    "V2PDF_COLAB_CPU_FALLBACK": ("enum", "off", ("on", "off")),
    # 本地路径输入策略（auto/allow/deny 语义见 _validate_run_url）
    "V2PDF_LOCAL_PATHS":     ("enum", "auto", ("auto", "allow", "deny")),
    # 匿名面限流（P0-2）：设备流两端点按 IP 共享滑窗（0=不设限）。缺省 30/min 而非
    # ROADMAP 示例的 10——token 端点按 Google 建议间隔（~5s）轮询，合法单用户登录
    # 就要 12 次/min，10 会误伤正常轮询。V2PDF_TRUST_XFF=on 时客户端 IP 取
    # X-Forwarded-For 末位（单层反代拓扑：api 绑回环、唯一入口=仓内 nginx，尾位即真实客户端）
    "V2PDF_DEVICE_RATE_PER_MIN": ("int", 30, ("min", 0)),
    "V2PDF_TRUST_XFF":           ("enum", "off", ("off", "on")),
    # 成本归因→预算闸（P2-2）：单价表全 0 = 不定价（cost_usd 恒 0，只记 usage 不计钱）；
    # 预算 0 = 不设闸。操作者按真实账单填价（参考行见 08 篇），口径：
    #   LLM 单价 = USD/1M tokens（in/out 分列）；GPU 单价 = USD/远程调用分钟（时长上界口径）
    "V2PDF_BUDGET_MONTH_USD":     ("float", 0.0, ("min", 0.0)),
    "V2PDF_PRICE_LLM_IN_MTOK":    ("float", 0.0, ("min", 0.0)),
    "V2PDF_PRICE_LLM_OUT_MTOK":   ("float", 0.0, ("min", 0.0)),
    "V2PDF_PRICE_LLM_CACHED_IN_MTOK": ("float", 0.0, ("min", 0.0)),
    "V2PDF_PRICE_GPU_MIN":        ("float", 0.0, ("min", 0.0)),
    "V2PDF_GPU_PRICE_POLICY":     ("enum", "metered", ("metered", "free")),
}


def _parse_spec(env: dict[str, str]) -> tuple[dict, list[str]]:
    """按 _SPEC 全量解析：返回 (键→终值, 错误清单)。错误攒齐不首停（P1-9 验收口径）。"""
    out: dict = {}
    errs: list[str] = []
    for key, (typ, default, rule) in _SPEC.items():
        raw = env.get(key)
        if raw is None or str(raw).strip() == "":     # 未设/空串=显式清空 → 默认
            out[key] = Path(default) if (typ == "path" and not isinstance(default, Path)) else default
            continue
        raw = str(raw).strip()
        try:
            if typ == "path":
                out[key] = Path(raw)
            elif typ == "str":
                out[key] = raw.lower()
            elif typ == "raw":
                out[key] = raw
            elif typ == "int":
                out[key] = int(raw)
            elif typ == "float":
                out[key] = float(raw)
            elif typ == "enum":
                v = raw.lower()
                allowed = rule or ()
                if v not in allowed:
                    errs.append(f"{key}={raw!r} 未知（支持 {'|'.join(allowed)}）")
                    out[key] = default
                    continue
                out[key] = v
            elif typ == "csv":
                out[key] = [o.strip() for o in raw.split(",") if o.strip()]
        except ValueError:
            errs.append(f"{key}={raw!r} 不是合法 {typ}")
            out[key] = default
            continue
        if rule and typ in ("int", "float") and isinstance(rule, tuple):
            kind, bound = rule
            val = out[key]
            if (kind == "gt" and not val > bound) or (kind == "min" and not val >= bound):
                errs.append(f"{key}={raw!r} 越界（要求 {kind} {bound}）")
    return out, errs


_PARSED, _ERRS = _parse_spec(ENV)

# Production deployments must provide a real authentication provider.
if ENV.get("V2PDF_ENV", "development").strip().lower() == "production" \
        and (ENV.get("V2PDF_AUTH", "off") or "off").strip().lower() in ("", "off", "none"):
    raise RuntimeError("生产环境禁止 V2PDF_AUTH=off；请配置 V2PDF_AUTH=google 或其它真实 provider")

# Google 网页登录的 redirect_uri 与回跳地址由这两个键拼出；生产缺省会回落到 localhost，
# 登录必坏且只在真人点击时暴露，所以启动期就拒绝。
if ENV.get("V2PDF_ENV", "development").strip().lower() == "production" \
        and "google" in (ENV.get("V2PDF_AUTH", "") or "").strip().lower():
    for _k in ("V2PDF_PUBLIC_BASE_URL", "V2PDF_WEB_BASE_URL"):
        if not (ENV.get(_k) or "").strip().lower().startswith("https://"):
            raise RuntimeError(f"生产环境 V2PDF_AUTH=google 须配置 https 的 {_k}（当前：{ENV.get(_k)!r}）")

if _ERRS:
    raise RuntimeError("配置错误（共 {} 处，一次改全）：\n  - ".format(len(_ERRS))
                       + "\n  - ".join(_ERRS))


# ---------------------------------------------------------------- 解析产物（消费面零改动）
DEPLOY_ENV: str = _PARSED["V2PDF_ENV"]

# 持久化文件位置：容器里由 compose 指到 data 卷（/app/data/…），本机默认项目根
STATE_FILE: Path = _PARSED["V2PDF_STATE_FILE"]
RUNS_FILE: Path = _PARSED["V2PDF_RUNS_FILE"]
LOG_DIR: Path = _PARSED["V2PDF_LOG_DIR"]

# 代理键透传给子进程/httpx（YouTube 等境外源下载；国内站代码层强制直连）。
# 真实环境变量优先，.env 只作兜底。
# （Colab/Google OAuth 流量已随算力池迁出本进程，见 gpu_router 项目 config.load_dotenv_proxy）
for _pk in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
    if not os.environ.get(_pk) and ENV.get(_pk):
        os.environ.setdefault(_pk, ENV[_pk])


# 身份表（多用户鉴权：(provider, external_id) → 稳定内部 uid）：容器里指到 data 卷
USERS_FILE: Path = _PARSED["V2PDF_USERS_FILE"]

# 鉴权配置解析已收进 acctkit.build_from_env（V2PDF_AUTH / V2PDF_AUTH_PROVIDERS /
# GOOGLE_CLIENT_ID 语义见 acctkit/engine.py 与 README「acctkit」节；off 缺省保证
# e2e/batch 无 token 也能跑），接线在 video2pdf/auth/__init__.py，此处不再镜像一份。
# CORS 放行 origin（逗号分隔；GUI 前端域名）。空=不加 CORS 中间件（同源/隧道开发够用）
CORS_ORIGINS: list[str] = _PARSED["V2PDF_CORS_ORIGINS"]

VAULT: Path = _PARSED["OBSIDIAN_VAULT"]

OUT_DIR = VAULT / "资料库" / "视频笔记"


INDEX_FILE = OUT_DIR / "索引.md"

ATTACH_DIR = OUT_DIR / "attachments"      # 嵌入帧（![[attachments/...]] 按文件名全库可解析）

TRANSCRIPT_DIR = OUT_DIR / "transcripts"  # 转录本 SSOT（info callout wikilink）


# 产物落点模式（缝在 artsink.py，写盘函数不感知）：
#   vault（默认）= 直写 vault 卷 + Syncthing 同步（现状逐字节不变）
#   deliver      = 多用户「云端不储存」：产物进 per-run outbox，终态后搬到 OUTBOX_DIR，
#                  只经 bundle/ACK HTTP 端点发送到用户端，TTL 到期/确认收完即焚（api.py）
ARTIFACT_MODE: str = _PARSED["V2PDF_ARTIFACT_MODE"]

# deliver 在架根（容器指 data 卷）与保留窗口（小时，到期自动烧）
OUTBOX_DIR: Path = _PARSED["V2PDF_OUTBOX_DIR"]
OUTBOX_TTL_H: float = _PARSED["V2PDF_OUTBOX_TTL_H"]


# run 卡死看门狗（watchdog.py）：心跳超时判定。单条 3h 转录属正常，默认 2h 无任何活动才判。
WATCHDOG_TIMEOUT_S: float = _PARSED["V2PDF_WATCHDOG_TIMEOUT_S"]
WATCHDOG_PERIOD_S: float = _PARSED["V2PDF_WATCHDOG_PERIOD_S"]
WATCHDOG_ACTION: str = _PARSED["V2PDF_WATCHDOG_ACTION"]

# 执行层并发与配额（终局布局·执行层/限流配额的进程内落地）：
#   并发闸门默认 1 = 单 worker 语义；生产多用户调大——并发>1 时 checkpointer 建议
#   切 postgres（sqlite 单连接弱）、vault 自托管姿势建议保持 1。三个上限 0 = 不设限；
#   提交限流按 uid 60s 滑窗（入队前闸，429 信封）。
MAX_CONCURRENT_RUNS: int = _PARSED["V2PDF_MAX_CONCURRENT_RUNS"]
QUEUE_MAX: int = _PARSED["V2PDF_QUEUE_MAX"]
USER_QUEUE_MAX: int = _PARSED["V2PDF_USER_QUEUE_MAX"]
USER_SUBMIT_PER_MIN: int = _PARSED["V2PDF_USER_SUBMIT_PER_MIN"]

# 上传面（多用户专用 POST /uploads）：落地根/单文件上限/每用户配额/TTL 惰性清扫。
# 容器里 UPLOADS_DIR 指到 data 卷；TTL 到期物理删——「云端不储存」口径。
UPLOADS_DIR: Path = _PARSED["V2PDF_UPLOADS_DIR"]
UPLOAD_MAX_GB: float = _PARSED["V2PDF_UPLOAD_MAX_GB"]
UPLOAD_QUOTA_GB: float = _PARSED["V2PDF_UPLOAD_QUOTA_GB"]
UPLOADS_TTL_H: float = _PARSED["V2PDF_UPLOADS_TTL_H"]

# 任务表真相源（P4b·多副本档）：json 缺省 = _TASKS 进程内视图即现状零变化；
# postgres = /queue、配额闸、dedup 判活、watchdog 巡检、启动恢复认领读 v2pdf_runs
# 提取列（record jsonb 仍是状态唯一真相，status 列是其可查镜像）。需先切 RUNS_STORE。
TASKS_STORE: str = _PARSED["V2PDF_TASKS_STORE"]

# 教材偏好（收藏/阅读进度）：json 缺省指 PROJ/prefs.json（容器指 data 卷）；
# postgres = v2pdf_prefs 表（migrations/0003，共用 V2PDF_PG_DSN）——云上多副本切此档
PREFS_FILE: Path = _PARSED["V2PDF_PREFS_FILE"]
PREFS_STORE: str = _PARSED["V2PDF_PREFS_STORE"]

# 本地路径输入策略（POST /runs 的 url 收非 http 时）：auto = AUTH=off 放行（开发/
# 自托管），AUTH≠off 只认本人 uploads 白名单内路径；allow/deny 显式覆盖。
LOCAL_PATHS: str = _PARSED["V2PDF_LOCAL_PATHS"]

# 匿名面限流（P0-2）：设备流两端点按 IP 共享滑窗；TRUST_XFF=on 取 XFF 末位作客户端 IP
# （仅仓内反代拓扑下开——api 绑回环时唯一入口是 nginx，尾位可信；直连开发档保持 off）
DEVICE_RATE_PER_MIN: int = _PARSED["V2PDF_DEVICE_RATE_PER_MIN"]
TRUST_XFF: str = _PARSED["V2PDF_TRUST_XFF"]

# 成本归因→预算闸（P2-2）
BUDGET_MONTH_USD: float = _PARSED["V2PDF_BUDGET_MONTH_USD"]
PRICE_LLM_IN_MTOK: float = _PARSED["V2PDF_PRICE_LLM_IN_MTOK"]
PRICE_LLM_OUT_MTOK: float = _PARSED["V2PDF_PRICE_LLM_OUT_MTOK"]
PRICE_GPU_MIN: float = _PARSED["V2PDF_PRICE_GPU_MIN"]
PRICE_LLM_CACHED_IN_MTOK: float = _PARSED["V2PDF_PRICE_LLM_CACHED_IN_MTOK"]

# 版本单源：dist metadata（已安装时）→ pyproject.toml（源码/镜像直跑，Dockerfile 有 COPY）
# → "0.0.0"。FastAPI version 与 /healthz 都读这里，不手工双写。
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version
    VERSION = _pkg_version("video2pdf")
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
# PG 域键统一在本表解析；pg.py 仍从 ENV 读取，保留测试运行时切档能力。

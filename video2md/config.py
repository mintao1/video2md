"""配置与基座：项目 .env（非机密）+ dify_kit SSOT 注入 + 路径常量。"""
from __future__ import annotations

import os
import sys
from pathlib import Path



PROJ = Path(__file__).resolve().parent.parent  # 包目录的上一级 = 项目根（state.json/runs.json/.env 都在根上）


STATE_FILE = PROJ / "state.json"


LOG_DIR = PROJ / "logs"


MAX_TRANSCRIPT_CHARS = 60000  # 约等于 3 小时密集语音


LOW_CONF = 0.5


BLUR_SHARP_MIN = 2.5  # 中心裁剪区边缘均值；白底幻灯片约 3-5，真模糊帧 <2


FRAME_MAE_MAX = 2.0   # 相邻帧 160x90 灰度 MAE：重复帧 <1，不同文字幻灯片 >3


FRAME_DEDUP_EPS = 0.4   # 候选帧微去重（秒）：网格/场景两路同一瞬间重复采样才去重，内容去重留给选帧


KB_NAME = "视频知识库"


CLIP_SPAN = 10        # 视频节段：帧时间戳 ±10s 剪 20s 可播片段（对齐参考笔记的内嵌 mp4）



# ---------------------------------------------------------------- env / paths
def load_env() -> dict[str, str]:
    """读项目 .env（只放本机非机密配置），真实环境变量优先。

    key/凭证 SSOT 是 ~/.config/dify-zcode.env，由 dify_kit 基座 load_env 注入 os.environ
    （见下方 kit_config.load_env()）；历史上 daoxe/.env 作 GEMINI_API_KEY 兜底，
    该文件已无此键，兜底链已摘除。"""
    env: dict[str, str] = {}
    for p in (PROJ / ".env",):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    env.update({k: v for k, v in os.environ.items() if v})  # 真实环境变量优先
    return env



ENV = load_env()


# 代理键透传给子进程/httpx（YouTube 等境外源下载；国内站代码层强制直连）。
# 真实环境变量优先，.env 只作兜底。
# （Colab/Google OAuth 流量已随算力池迁出本进程，见 gpu_router 项目 config.load_dotenv_proxy）
for _pk in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
    if not os.environ.get(_pk) and ENV.get(_pk):
        os.environ.setdefault(_pk, ENV[_pk])


VAULT = Path(ENV.get("OBSIDIAN_VAULT", str(Path.home() / "Documents/Obsidian Vault")))


OUT_DIR = VAULT / "资料库" / "视频笔记"


INDEX_FILE = OUT_DIR / "索引.md"


ATTACH_DIR = OUT_DIR / "attachments"      # 嵌入帧（![[attachments/...]] 按文件名全库可解析）


TRANSCRIPT_DIR = OUT_DIR / "transcripts"  # 转录本 SSOT（info callout wikilink）



# ---------------------------------------------------------------- dify_kit 基座
def _load_dify_kit():
    kit_home = Path(ENV.get("DIFY_KIT_HOME",
                            str(Path.home() / "Desktop/开发项目/dify_kit"))).expanduser()
    if not kit_home.exists():
        sys.exit(f"❌ dify_kit 基座不存在: {kit_home}（设 DIFY_KIT_HOME 指向基座目录）")
    sys.path.insert(0, str(kit_home))
    from dify_kit.client import AppClient, DatasetAPI, DifyClient  # noqa: F401
    from dify_kit import config as kit_config
    return kit_config



kit_config = _load_dify_kit()


kit_config.load_env()  # 基座文件 → os.environ（setdefault，不覆盖真实环境变量）


if not os.environ.get("DIFY_APP_API_KEY") and ENV.get("DIFY_API_KEY") \
        and not ENV["DIFY_API_KEY"].startswith("app-xxxx"):
    os.environ.setdefault("DIFY_APP_API_KEY", ENV["DIFY_API_KEY"])


if not os.environ.get("DIFY_DATASET_API_KEY") and ENV.get("DIFY_DATASET_API_KEY") \
        and not ENV["DIFY_DATASET_API_KEY"].startswith("dataset-xxxx"):
    os.environ.setdefault("DIFY_DATASET_API_KEY", ENV["DIFY_DATASET_API_KEY"])


if not os.environ.get("DIFY_DATASET_ID") and ENV.get("DIFY_DATASET_ID"):
    os.environ.setdefault("DIFY_DATASET_ID", ENV["DIFY_DATASET_ID"])


_APP_CFG = kit_config.load_app_config()          # base_url + app_key（缺则 SystemExit）


DIFY_BASE = _APP_CFG["base_url"].rstrip("/")


_dataset_api = None





def dataset_api():
    global _dataset_api
    if _dataset_api is None:
        from dify_kit.client import DatasetAPI, DifyClient
        ds_key = os.environ.get("DIFY_DATASET_API_KEY", "")
        if not ds_key:
            sys.exit("DIFY_DATASET_API_KEY 未配置：写入 ~/.config/dify-zcode.env 或环境变量")
        _dataset_api = DatasetAPI(DifyClient(DIFY_BASE, ds_key, timeout=120))
    return _dataset_api

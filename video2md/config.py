"""配置与基座：项目 .env（非机密）+ 凭证文件（机密）+ 路径常量。"""
from __future__ import annotations

import os
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



# ---------------------------------------------------------------- fin
# dify_kit / DIFY_* / dataset_api 已随 Dify 抽离（2026-09-21）摘除：
# 知识库入库（kb 阶段）取消；LLM 凭证由 SECRETS_FILE 提供（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）。

"""HTTP 契约模型（Phase 1 契约硬化）：请求校验 + 响应形状 + 统一错误信封。

纪律：响应字段与旧 api 逐一对齐（test_e2e.sh / batch_csv.py 读 run_id/total/
all_skipped/status/task.status/entries[].done/queue_len/note），只做类型化不破坏。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator


class LlmSettingsUpdate(BaseModel):
    base_url: str = Field(..., min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(None, max_length=4096)
    model: str = Field("", max_length=200)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        from .llm_settings import normalize_url
        return normalize_url(value)

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        text = value.get_secret_value().strip()
        if not text:
            return None  # Blank preserves the saved key; DELETE explicitly clears it.
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ValueError("API Key 不能包含控制字符")
        return SecretStr(text)


class LlmSettingsOut(BaseModel):
    base_url: str
    model: str
    has_api_key: bool
    configured: bool

# ---------------------------------------------------------------- POST /runs

class RunCreate(BaseModel):
    url: str = Field(..., min_length=1, description="B站/YouTube/抖音/小红书链接或容器内本地文件路径")
    part_items: str = Field("", description="只跑指定条目，如 '1,3-5'")
    force: bool = Field(False, description="忽略 state 幂等全量重跑")
    review: bool = Field(False, description="启用校对 LLM（更准更慢）")
    max_frames: int = Field(60, ge=1, le=1000, description="候选帧池上限")
    resolve_only: bool = Field(False, description="只解析建 run 不驱动（批量分窗预飞）")


class EntryBrief(BaseModel):
    index: int
    url: str
    skipped: bool
    from_run: str | None = Field(
        None, description="deliver 模式幂等跳过的来源 run_id（产物还在架上，直接拉旧 bundle）")


class RunCreatedOut(BaseModel):
    run_id: str
    total: int
    entries: list[EntryBrief]
    all_skipped: bool
    status: str = Field(description="queued / created(resolve_only) / 复用旧 run 的当前态")
    dedup: bool = False


# ---------------------------------------------------------------- GET /runs/{id}

class RunTask(BaseModel):
    status: str | None = None
    url: str | None = None
    created: str | None = None
    started: str | None = None
    finished: str | None = None
    errors: list[str] | None = None


class EntryStatus(BaseModel):
    index: int | None = None
    url: str
    skipped: bool = False
    note: str | None = None
    done: dict[str, bool] = Field(default_factory=dict)
    from_run: str | None = None


class ArtifactsInfo(BaseModel):
    """deliver 模式产物在架状态（vault 模式此块为 None——契约零扰动）。"""
    mode: str = "deliver"
    present: bool = Field(description="outbox 在架（未 ACK、未过期被清）")
    expires: str | None = Field(None, description="在架到期时刻（iso）；到点即焚")


class RunStatusOut(BaseModel):
    run_id: str
    task: RunTask | None = None
    url: str | None = None
    status: str | None = None
    total: int | None = None
    stages: dict[str, Any] | None = None
    partial_failures: dict[str, Any] | None = None
    errors: list[str] | None = None   # 记录级留痕（P1-4：跨副本可见——task.errors 只在驱动进程）
    entries: list[EntryStatus] | None = None
    artifacts: ArtifactsInfo | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    cost_pricing: dict[str, Any] | None = None
    diagnostic: dict[str, str] | None = None


class ArtifactsAckOut(BaseModel):
    run_id: str
    status: str = Field(description="burned=已即时销毁（ACK 即焚）")


# ---------------------------------------------------------------- GET /runs（列表 · 插件收件箱发现用）

class RunListItem(BaseModel):
    run_id: str
    url: str
    title: str | None = Field(None, description="教材展示名：art.title → entry.title → url 主机名（教材库页数据源）")
    status: str | None = None
    queue_pos: int | None = None
    created: str | None = None
    total: int | None = None
    done_entries: int = Field(0, description="note 产物已写成的条目数（skipped 不计入）")
    artifacts: ArtifactsInfo | None = Field(
        None, description="deliver 模式在架状态；vault 模式为 None")
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    cost_pricing: dict[str, Any] | None = None
    diagnostic: dict[str, str] | None = None


class RunListOut(BaseModel):
    items: list[RunListItem]
    mode: str = Field(description="当前产物形态 vault|deliver（客户端据此决定拉不拉 bundle）")


# ---------------------------------------------------------------- /prefs（教材偏好：收藏/阅读进度）

class PrefsItem(BaseModel):
    run_id: str
    fav: bool = False
    progress: int = Field(0, ge=0, le=100, description="阅读进度 0-100（阅读器页接入后写入）")


class PrefsOut(BaseModel):
    items: list[PrefsItem]


class PrefsUpdate(BaseModel):
    fav: bool | None = None       # None = 本字段不动（部分更新）
    progress: int | None = Field(None, ge=0, le=100)


# ---------------------------------------------------------------- /auth/device/*（设备流登录桥）

class DeviceCodeOut(BaseModel):
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


class DeviceTokenBody(BaseModel):
    device_code: str = Field(..., min_length=1)


class DeviceTokenOut(BaseModel):
    id_token: str = Field(description="验签后即等价 Bearer 凭据，插件存本地复用")


# ---------------------------------------------------------------- note / queue / health / preflight

class NoteItem(BaseModel):
    title: str = ""
    path: str = ""
    md: str = ""


class RunNotesOut(BaseModel):
    run_id: str
    status: str | None = None
    notes: list[NoteItem] = Field(default_factory=list)


class CancelOut(BaseModel):
    """POST /runs/{id}/cancel：排队项立即撤销回 error；运行中/他副本驱动回 cancelling
    （协作式——本进程循环头或 cmd 通道生效后翻终态，轮询/SSE 可见）。"""
    run_id: str
    status: str
    remote: bool = False


class QueueOut(BaseModel):
    queue_len: int
    running: int
    limits: dict[str, int] | None = Field(
        None, description="执行层档位（max_concurrent/queue_max/user_queue_max；0=不设限）")


class CostOut(BaseModel):
    """GET /cost：本人本月成本归因（P2-2）。spend 为已结算 cost_usd 之和；prices 回显
    当前单价表（USD）；budget 0=未设闸。GPU 口径=远程调用墙钟上界（见 08 篇）。"""
    uid: str
    month: str = Field(description="统计月起（ISO，本地时区）")
    spend_usd: float
    budget_usd: float
    usage: dict[str, Any] = Field(description="llm_in_tok/llm_out_tok/gpu_s/runs_n 月度合计")
    prices: dict[str, float] = Field(description="llm_in_mtok/llm_out_mtok/gpu_min 当前单价")
    gpu_policy: str = "metered"
    pricing_configured: bool = False
    runs: list[dict[str, Any]] = Field(default_factory=list)


class QuotaStats(BaseModel):
    """GET /quota 的统计三格（web 个人页资料卡下排）。"""
    books: int = 0
    knowledge_points: int = 0
    accuracy: float = 0


class QuotaOut(BaseModel):
    """GET /quota：本人会员配额与统计（展示档，来自 v2pdf_membership；不参与提交强制）。
    PG 关（无 DSN）或无 membership 行 → 全 0 默认值。"""
    ai_hours_used: float
    ai_hours_total: float
    storage_gb_used: float
    storage_gb_total: float
    stats: QuotaStats


class MeOut(BaseModel):
    """GET /me：本人资料卡。plan 取 membership 行（无行/PG 关 → free）；
    id_label 由 uid 派生不落库。"""
    uid: str
    name: str
    display_name: str
    avatar_hue: int
    plan: str = Field("free", description="free|pro|svip")
    id_label: str


class HealthOut(BaseModel):
    ok: bool
    ts: str
    version: str = Field("", description="运行中版本（pyproject dist metadata 单源）")


class UploadOut(BaseModel):
    path: str = Field(description="落地路径（容器内绝对路径，直接作为 POST /runs 的 url）")
    size: int = Field(description="字节数")
    name: str = Field(description="原始文件名（sanitize 后）")


class PreflightCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class PreflightOut(BaseModel):
    ok: bool
    checks: list[PreflightCheck]
    ts: str


# ---------------------------------------------------------------- 统一错误信封

class ErrorBody(BaseModel):
    code: int
    message: str
    details: Any | None = None


class ErrorOut(BaseModel):
    error: ErrorBody

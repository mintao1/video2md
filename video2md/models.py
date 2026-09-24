"""HTTP 契约模型（Phase 1 契约硬化）：请求校验 + 响应形状 + 统一错误信封。

纪律：响应字段与旧 api 逐一对齐（test_e2e.sh / batch_csv.py 读 run_id/total/
all_skipped/status/task.status/entries[].done/queue_len/note），只做类型化不破坏。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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
    entries: list[EntryStatus] | None = None
    artifacts: ArtifactsInfo | None = None


class ArtifactsAckOut(BaseModel):
    run_id: str
    status: str = Field(description="burned=已即时销毁（ACK 即焚）")


# ---------------------------------------------------------------- GET /runs（列表 · 插件收件箱发现用）

class RunListItem(BaseModel):
    run_id: str
    url: str
    status: str | None = None
    created: str | None = None
    total: int | None = None
    done_entries: int = Field(0, description="note 产物已写成的条目数（skipped 不计入）")
    artifacts: ArtifactsInfo | None = Field(
        None, description="deliver 模式在架状态；vault 模式为 None")


class RunListOut(BaseModel):
    items: list[RunListItem]
    mode: str = Field(description="当前产物形态 vault|deliver（客户端据此决定拉不拉 bundle）")


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

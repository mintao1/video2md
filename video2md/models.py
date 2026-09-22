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


class RunStatusOut(BaseModel):
    run_id: str
    task: RunTask | None = None
    url: str | None = None
    status: str | None = None
    total: int | None = None
    stages: dict[str, Any] | None = None
    partial_failures: dict[str, Any] | None = None
    entries: list[EntryStatus] | None = None


# ---------------------------------------------------------------- note / queue / health / preflight

class NoteItem(BaseModel):
    title: str = ""
    path: str = ""
    md: str = ""


class RunNotesOut(BaseModel):
    run_id: str
    status: str | None = None
    notes: list[NoteItem] = Field(default_factory=list)


class QueueOut(BaseModel):
    queue_len: int
    running: int


class HealthOut(BaseModel):
    ok: bool
    ts: str


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

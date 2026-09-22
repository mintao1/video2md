"""产物落点（artifact sink）：vault 与 deliver 两种模式的唯一缝。

写盘函数（note/frames/clips）不感知 run 也不感知模式——一律经本模块访问器拿目录；
激活点在阶段执行前包 `with sink_for(run):`（runs._run_stage 与 graph.node_note_entry
的 note 直调）：

- vault 模式（默认）：访问器 = config 常量，行为与现状逐字节相同（vault 卷 + Syncthing）
- deliver 模式（多用户「云端不储存」产品形态）：产物全落 `<run.tmp>/outbox{,/attachments,
  /transcripts}`，索引行进 `outbox/索引-append.md`（客户端自行并库）；finalize 把 outbox
  整体搬家到 OUTBOX_DIR/<run_id>，此后 TTL 到期 / 客户端 ACK 拉完即焚（端点在 api.py）

ContextVar 天然够隔离：阶段跑在 asyncio.to_thread 工作线程 + 单执行位信号量串行，
不会串 run 的 sink。deliver 模式下漏激活点会**炸 RuntimeError 而不是静默写进 vault**
——「云端不储存」的口径不容兜底漏网。模式与路径都经 config 模块属性动态读
（测试可 monkeypatch），本模块不做 import 时快照。
"""
from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path

from . import config

_SINK = contextvars.ContextVar("v2md_art_sink", default=None)


def current_mode() -> str:
    return config.ARTIFACT_MODE


def outbox_dir() -> Path:
    """deliver 模式产物在架根（终态后 outbox 搬进这里，TTL/ACK 烧毁）。"""
    return config.OUTBOX_DIR


def outbox_ttl_h() -> float:
    return config.OUTBOX_TTL_H


def _require() -> dict:
    s = _SINK.get()
    if s is None:
        raise RuntimeError("deliver 模式的产物写盘必须发生在 sink_for(run) 作用域内"
                           "（激活点丢失？宁可炸也不静默写进 vault）")
    return s


@contextlib.contextmanager
def sink_for(run: dict):
    """deliver 模式下给本 run 开 outbox 落点；vault 模式 no-op 直通（零开销零行为差）。"""
    if current_mode() != "deliver":
        yield None
        return
    tmp = run.get("tmp") or ""
    if not tmp:
        raise RuntimeError(f"run {run.get('run_id')} 缺 tmp，deliver 模式无处落 outbox")
    root = Path(tmp) / "outbox"
    (root / "attachments").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(parents=True, exist_ok=True)
    tok = _SINK.set({"root": root, "attach": root / "attachments",
                     "transcript": root / "transcripts",
                     "index": root / "索引-append.md"})
    try:
        yield root
    finally:
        _SINK.reset(tok)


# ---- 写盘函数唯一取目录的四个访问器（vault = config 常量；deliver = 本 run outbox）----

def note_root() -> Path:
    return config.OUT_DIR if current_mode() != "deliver" else _require()["root"]


def attach_dir() -> Path:
    return config.ATTACH_DIR if current_mode() != "deliver" else _require()["attach"]


def transcript_dir() -> Path:
    return config.TRANSCRIPT_DIR if current_mode() != "deliver" else _require()["transcript"]


def index_file() -> Path:
    return config.INDEX_FILE if current_mode() != "deliver" else _require()["index"]

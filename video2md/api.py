"""FastAPI 服务：LangGraph 编排的 HTTP 入口（替代 serve.py + Dify 轮询）。

Dify 时代的双进程分工（Dify 画布轮询 ←HTTP→ serve.py 阶段打工）收拢为单进程：
POST /runs 入队 → 后台任务内嵌跑完整张 LangGraph（阶段即节点），客户端轮询
GET /runs/{id} 看终态。串行纪律不变：asyncio 信号量=1，对齐旧单 worker 模型
（4C 小机上 ffmpeg/whisper 是 CPU 密集，并行只会互相拖死）。

端点契约：
  POST /runs          {url, part_items?, force?, review?, max_frames?} → {run_id, status}
  GET  /runs/{id}     run 记录 + 任务状态 + 逐条目产物进度（沿用 serve 的形状）
  GET  /runs/{id}/note  产出 markdown（标题/路径/正文），供云上调取
  GET  /queue         {queue_len, running}——gpu_router 的 colab 看门狗回读这里
  GET  /healthz       存活探针（Docker healthcheck）
  GET  /preflight     开跑前体检（复用 serve._preflight：ffmpeg/磁盘/vault/算力池）
  GET  /checkpoint    对账快照（复用 serve._checkpoint 的算力池部分）

恢复语义：进程重启时把 runs.json 里 created/running 的 run 重新入队（同 run_id），
graph 的 resolve 节点幂等复用该 run，阶段节点按产物字段跳过已完成条目——
与旧 _load_jobs 语义一致，只是 jobs.json 不再需要。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from .config import ENV, LOG_DIR, PROJ
from .graph import make_checkpointer, run_graph
from .runs import _load_runs, _runs_file_lock, _runs_merge
from .serve import _checkpoint, _cleanup_stale_runs, _preflight, _prune_orphan_tmps
from .util import redact

# ---------------------------------------------------------------- 任务表
_TASKS: dict[str, dict] = {}          # run_id → {status, url, created, started, finished, errors}
_SEMA = asyncio.Semaphore(1)          # 串行执行：单 worker 语义
_CHECKPOINTER = None                  # lifespan 注入的 SqliteSaver

_TASKS_KEEP = 200                     # 内存任务表上限（超出裁最老，runs.json 才是真相）


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _req_log(method: str, path: str, code: int, **fields) -> None:
    """请求审计（沿用 serve 的 jsonl 口径：证明调用方是否真的打到服务）。"""
    import json
    rec = {"ts": _now(), "method": method, "path": path, "code": code, **fields}
    print(f"[req] {method} {path} → {code} " + " ".join(f"{k}={str(v)[:60]}" for k, v in fields.items()))
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with (LOG_DIR / "serve-requests.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass



# ---------------------------------------------------------------- 后台执行

async def _drive(run_id: str, url: str, part_items: str, force: bool,
                 review: bool, max_frames: int) -> None:
    """排队等信号量 → 线程里跑同步 graph（stage 函数全是同步阻塞，不能堵事件循环）。"""
    async with _SEMA:
        _TASKS[run_id].update(status="running", started=_now())
        try:
            final = await asyncio.to_thread(run_graph, url, part_items=part_items,
                                            force=force, review=review,
                                            max_frames=max_frames, run_id=run_id,
                                            checkpointer=_CHECKPOINTER)
            _TASKS[run_id].update(status=final.get("status") or "done",
                                  finished=_now(), errors=(final.get("errors") or [])[-10:])
        except Exception as exc:  # graph 节点已全捕获，走到这是框架级异常
            _TASKS[run_id].update(status="error", finished=_now(),
                                  errors=[redact(str(exc))[:300]])



def _enqueue(url: str, part_items: str, force: bool, review: bool,
             max_frames: int, run_id: str = "") -> dict:
    run_id = run_id or uuid.uuid4().hex[:8]
    _TASKS[run_id] = {"status": "queued", "url": url, "created": _now()}
    if len(_TASKS) > _TASKS_KEEP:  # 裁最老的已完成记录（queued/running 永不裁）
        done = sorted((k for k, v in _TASKS.items() if v.get("finished")),
                      key=lambda k: _TASKS[k]["finished"])
        for k in done[: len(_TASKS) - _TASKS_KEEP]:
            _TASKS.pop(k, None)
    asyncio.create_task(_drive(run_id, url, part_items, force, review, max_frames))
    return {"run_id": run_id, "status": "queued"}



# ---------------------------------------------------------------- app

@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _CHECKPOINTER
    _CHECKPOINTER = make_checkpointer()
    _cleanup_stale_runs()
    _prune_orphan_tmps()
    # 恢复中断的 run（对齐旧 _load_jobs 的语义；error 的不自动重跑，避免双份产物）。
    # tmp 已没了的 run 不可恢复（产物文件随目录一起没了，续跑只会拿空产物硬跑）：
    # 标记 error 收场，不占用执行信号量。
    resumed = 0
    for r in _load_runs().values():
        if r.get("status") not in ("created", "running") or not r.get("url"):
            continue
        if r.get("tmp") and not Path(r["tmp"]).exists() and not r.get("tmp_cleaned"):
            r["status"] = "error"
            r["errors"] = [*(r.get("errors") or []), "临时目录已被清理，无法续跑（重发同 URL 新建 run）"]
            with _runs_file_lock():
                _runs_merge(r)
            continue
        _enqueue(r["url"], r.get("part_items", ""), bool(r.get("force")), False, 60,
                 run_id=r["run_id"])
        resumed += 1
    if resumed:
        print(f"♻️ 恢复 {resumed} 个中断 run（上次服务退出前未跑完）")
    yield


app = FastAPI(title="video2md", lifespan=_lifespan, docs_url=None, redoc_url=None)



@app.post("/runs")
async def post_runs(request: Request) -> dict:
    data = await request.json()
    url = str(data.get("url", "")).strip()
    part_items = str(data.get("part_items", "")).strip()
    if not url:
        _req_log("POST", "/runs", 400, error="缺 url")
        raise HTTPException(400, "缺 url")
    # API 层去重（便宜预检；graph resolve 节点还有一道 runs.json 真去重）
    dup = next((k for k, v in _TASKS.items()
                if v.get("url") == url and v.get("part_items", "") == part_items
                and v.get("status") in ("queued", "running")), None)
    if dup:
        _req_log("POST", "/runs", 200, run_id=dup, dedup=True)
        return {"run_id": dup, "status": _TASKS[dup]["status"], "dedup": True}
    out = _enqueue(url, part_items, bool(data.get("force")), bool(data.get("review")),
                   int(data.get("max_frames") or 60))
    _TASKS[out["run_id"]]["part_items"] = part_items
    _req_log("POST", "/runs", 200, run_id=out["run_id"], url=url[:80])
    return out



@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = _load_runs().get(run_id)
    task = _TASKS.get(run_id)
    if not run and not task:
        raise HTTPException(404, f"run {run_id} 不存在")
    out: dict = {"run_id": run_id}
    if task:
        out["task"] = {k: task.get(k) for k in ("status", "url", "created", "started",
                                                "finished", "errors") if task.get(k) is not None}
    if run:
        entries = []
        for e in run["entries"]:
            art = run["artifacts"].get(e["url"], {})
            entries.append({
                "index": e.get("playlist_index"), "url": e["url"],
                "skipped": bool(art.get("skipped")),
                "note": Path(art["note"]).name if art.get("note") else None,
                "done": {s: bool(art.get({"transcribe": "segs", "frames": "candidates",
                                          "ocr": "ocr_raw", "select": "published",
                                          "clips": "clips_done", "note": "note",
                                          "download": "video"}.get(s)))
                         for s in ("download", "transcribe", "frames", "ocr",
                                   "select", "clips", "note")},
            })
        out.update(url=run.get("url"), status=run.get("status"), total=run.get("total"),
                   stages=run.get("stages"), partial_failures=run.get("partial_failures"),
                   entries=entries)
    return out



@app.get("/runs/{run_id}/note")
def get_run_notes(run_id: str) -> dict:
    """产出的 markdown（云上调取口）：标题 + vault 路径 + 正文。"""
    run = _load_runs().get(run_id)
    if not run:
        raise HTTPException(404, f"run {run_id} 不存在")
    notes = [{"title": a.get("title", ""), "path": a.get("note", ""), "md": a.get("md", "")}
             for a in (run.get("artifacts") or {}).values() if a.get("note")]
    return {"run_id": run_id, "status": run.get("status"), "notes": notes}



@app.get("/queue")
def get_queue() -> dict:
    """gpu_router 的 colab 看门狗回读（queue_url 指这里）——判队列保温。"""
    return {"queue_len": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
            "running": sum(1 for v in _TASKS.values() if v.get("status") == "running")}



@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ts": _now()}



@app.get("/preflight")
def preflight() -> dict:
    return _preflight()



@app.get("/checkpoint")
def checkpoint() -> dict:
    return _checkpoint()



def cmd_api(port: int) -> None:
    # launchd/容器环境 PATH 极简，补齐框架/homebrew 路径让子进程找得到 yt-dlp/whisper/ffmpeg
    os.environ["PATH"] = ":".join([
        "/opt/homebrew/bin", "/usr/local/bin",
        "/Library/Frameworks/Python.framework/Versions/3.14/bin",
        str(Path.home() / ".local/bin"),
    ]) + ":" + os.environ.get("PATH", "")
    host = ENV.get("V2MD_HOST", "127.0.0.1")  # 容器内设 0.0.0.0；无认证，别暴露公网
    print(f"🎬 video2md LangGraph 服务 → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")

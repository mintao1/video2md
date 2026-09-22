"""FastAPI 服务：LangGraph 编排的 HTTP 入口（替代 serve.py + Dify 轮询）。

Dify 时代的双进程分工（Dify 画布轮询 ←HTTP→ serve.py 阶段打工）收拢为单进程：
POST /runs 入队 → 后台任务内嵌跑完整张 LangGraph（阶段即节点），客户端轮询
GET /runs/{id} 看终态。串行纪律不变：asyncio 信号量=1，对齐旧单 worker 模型
（4C 小机上 ffmpeg/whisper 是 CPU 密集，并行只会互相拖死）。

Phase 1（多用户后端契约硬化）：鉴权可插拔（默认 off，生产 google ID-Token）、
Pydantic 请求/响应契约、重开 OpenAPI、run 按内部 uid 归属隔离（越权 403）、
SSE 实时进度（GET /runs/{id}/events）与轮询并存。核心 run_graph 不动，本层只是传输。

端点契约：
  POST /runs            {url, part_items?, force?, review?, max_frames?, resolve_only?} → {run_id, total, ...}
  GET  /runs/{id}       run 记录 + 任务状态 + 逐条目产物进度（沿用 serve 的形状）
  GET  /runs/{id}/note  产出 markdown（标题/路径/正文），供云上调取
  GET  /runs/{id}/events SSE 实时进度（graph:*/stage 事件流；轮询 GET /runs/{id} 仍在）
  GET  /queue           {queue_len, running}——gpu_router 的 colab 看门狗回读这里（运维探针，不鉴权）
  GET  /healthz         存活探针（Docker healthcheck，不鉴权）
  GET  /preflight       开跑前体检（ffmpeg/磁盘/vault/算力池，不鉴权）

恢复语义：进程重启时把 runs.json 里 running 的 run 重新入队（同 run_id、同 owner），
graph 的 resolve 节点幂等复用该 run，阶段节点按产物字段跳过已完成条目。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import CORS_ORIGINS, ENV, LOG_DIR, OUT_DIR
from . import events
from .auth import AuthError, Identity, current_identity
from .graph import make_checkpointer, run_graph
from .models import (PreflightOut, QueueOut, RunCreate, RunCreatedOut, RunNotesOut,
                     RunStatusOut, HealthOut)
from .runs import _load_runs, _runs_file_lock, _runs_merge, _save_runs
from . import observability

from .util import redact

# ---------------------------------------------------------------- 运维函数（原 serve.py）

def _preflight() -> dict:
    """开跑前的环境体检：把「全局必炸」在阶段入队前暴露，免得 27 条各自白烧才发现同一死法。"""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "未安装")
    try:
        free_gb = shutil.disk_usage(tempfile.gettempdir()).free / 1e9
        add("disk", free_gb >= 2, f"临时卷剩余 {free_gb:.1f}GB")
    except Exception as exc:
        add("disk", False, str(exc)[:80])
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)  # 新鲜 vault（云上空卷）先建目录再测写
        add("vault", os.access(OUT_DIR, os.W_OK), str(OUT_DIR))
    except Exception as exc:
        add("vault", False, f"{OUT_DIR}: {str(exc)[:80]}")
    remote = (ENV.get("V2MD_REMOTE") or "").strip()
    if remote.lower() in ("colab", "colab-cli"):
        add("gpu_pool", False, "V2MD_REMOTE=colab 已抽离：起 ~/Desktop/开发项目/gpu_router "
                               "后改 V2MD_REMOTE=http://127.0.0.1:9700")
    elif remote.startswith(("http://", "https://")):
        # GPU 算力池/远程转录服务：/status（池子）或 /ping（裸参考服务端）二选一认
        base = remote.rstrip("/")
        ok, detail = False, ""
        try:
            d = httpx.get(f"{base}/status", timeout=4).json()
            ok = bool(d.get("ok"))
            usable = [p["name"] for p in d.get("providers", [])
                      if p.get("enabled") and p.get("healthy")
                      and not p.get("cooldown_remaining_s")]
            detail = f"算力池可用 provider: {', '.join(usable) or '无'}"
        except Exception:
            try:
                d = httpx.get(f"{base}/ping", timeout=4).json()
                ok, detail = bool(d.get("ok")), "裸转录服务 /ping 通过"
            except Exception as exc:
                detail = f"够不到 {base}: {str(exc)[:100]}"
        add("gpu_pool", ok, detail)
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "ts": datetime.now().isoformat(timespec="seconds")}



def _cleanup_stale_runs(max_age_days: int = 30, idle_hours: float = 24.0) -> None:
    """清陈旧/失活 run 的临时目录：run 生命周期随 finalize 节点结束清，这里兜底异常中断。

    两类会清：
    - 超龄（>30 天）——历史保险丝
    - 闲置 >24h——阶段失败后 tmp 无人清理（一个合集能占几百 MB）；「闲置」按最近阶段
      落盘时间算，正常批次阶段间隔以分钟计，撞不上
    """
    with _runs_file_lock():
        runs = _load_runs()
        now = time.time()
        changed = False
        for rid, r in list(runs.items()):
            if r.get("tmp_cleaned"):
                continue
            try:
                age = now - datetime.fromisoformat(r.get("created", "2000-01-01")).timestamp()
                last = max((datetime.fromisoformat(s["ts"]).timestamp()
                            for s in r.get("stages", {}).values() if s.get("ts")), default=0.0)
                idle = now - last if last else age
            except (ValueError, TypeError):
                age, idle = 1e9, 1e9
            if age > max_age_days * 86400 or idle > idle_hours * 3600:
                shutil.rmtree(r.get("tmp", ""), ignore_errors=True)
                r["tmp_cleaned"] = True
                changed = True
        if changed:
            _save_runs(runs)



def _prune_orphan_tmps(min_age_h: float = 6.0) -> None:
    """清没人认领的 run 临时目录：runs.json 记录丢失或进程被 SIGKILL 时，tmp 目录永久残留，
    30 天兜底等于不清。只动 `video2md-run-*`（CLI 的 `video2md-*` 由 finally 自清）。

    6 小时内动过的目录视为可能在跑（目录 mtime 随文件增删刷新），不清。
    """
    tdir = Path(tempfile.gettempdir())
    claimed = {r.get("tmp") for r in _load_runs().values()}
    n = 0
    for d in tdir.glob("video2md-run-*"):
        if str(d) in claimed:
            continue
        try:
            if time.time() - d.stat().st_mtime < min_age_h * 3600:
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    if n:
        print(f"🧹 清理孤儿临时目录 {n} 个（无 run 记录认领，>{int(min_age_h)}h 未动）")


# ---------------------------------------------------------------- 任务表
_TASKS: dict[str, dict] = {}          # run_id → {status, url, owner, created, started, finished, errors}
_SEMA = asyncio.Semaphore(1)          # 串行执行：单 worker 语义
_CHECKPOINTER = None                  # lifespan 注入的 SqliteSaver

_TASKS_KEEP = 200                     # 内存任务表上限（超出裁最老，runs.json 才是真相）

_TERMINAL_STATUSES = ("done", "partial", "error")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _req_log(method: str, path: str, code: int, **fields) -> None:
    """请求审计 jsonl（证明调用方是否真的打到服务；2026-09-22 前叫 serve-requests.jsonl）。"""
    rec = {"ts": _now(), "method": method, "path": path, "code": code, **fields}
    print(f"[req] {method} {path} → {code} " + " ".join(f"{k}={str(v)[:60]}" for k, v in fields.items()))
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with (LOG_DIR / "api-requests.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _require_run_access(run_id: str, ident: Identity) -> dict | None:
    """run 归属校验（越权 403 / 不存在 404），返回 runs.json 记录（可能仅有内存任务）。

    owner 恒为内部 uid（run 建时写入）；AUTH=off 下所有无 token 请求共享同一 dev uid，
    故 e2e/batch 自起自取照常通过。
    """
    run = _load_runs().get(run_id)
    task = _TASKS.get(run_id)
    if not run and not task:
        raise HTTPException(404, f"run {run_id} 不存在")
    owner = (run.get("owner", "") if run else task.get("owner", "")) or ""
    if owner != ident.uid:
        raise HTTPException(403, f"无权访问 run {run_id}")
    return run


# ---------------------------------------------------------------- 后台执行

async def _drive(run_id: str, url: str, part_items: str, force: bool,
                 review: bool, max_frames: int, owner: str = "") -> None:
    """排队等信号量 → 线程里跑同步 graph（stage 函数全是同步阻塞，不能堵事件循环）。"""
    async with _SEMA:
        _TASKS[run_id].update(status="running", started=_now())
        run = _load_runs().get(run_id)  # created → running（启动恢复只认 running）
        if run and run.get("status") == "created":
            run["status"] = "running"
            with _runs_file_lock():
                _runs_merge(run)
        try:
            final = await asyncio.to_thread(run_graph, url, part_items=part_items,
                                            force=force, review=review,
                                            max_frames=max_frames, run_id=run_id,
                                            checkpointer=_CHECKPOINTER, owner=owner)
            _TASKS[run_id].update(status=final.get("status") or "done",
                                  finished=_now(), errors=(final.get("errors") or [])[-10:])
        except Exception as exc:  # graph 节点已全捕获，走到这是框架级异常
            _TASKS[run_id].update(status="error", finished=_now(),
                                  errors=[redact(str(exc))[:300]])
        finally:
            observability.metric_run(_TASKS[run_id].get("status", "error"))
            observability.close_run(run_id)  # 兜底闭合 run 级 root span（各 stage 已各自闭合）
            observability.flush()



def _enqueue(url: str, part_items: str, force: bool, review: bool,
             max_frames: int, run_id: str = "", owner: str = "") -> dict:
    run_id = run_id or uuid.uuid4().hex[:8]
    _TASKS[run_id] = {"status": "queued", "url": url, "owner": owner, "created": _now()}
    if len(_TASKS) > _TASKS_KEEP:  # 裁最老的已完成记录（queued/running 永不裁）
        done = sorted((k for k, v in _TASKS.items() if v.get("finished")),
                      key=lambda k: _TASKS[k]["finished"])
        for k in done[: len(_TASKS) - _TASKS_KEEP]:
            _TASKS.pop(k, None)
    asyncio.create_task(_drive(run_id, url, part_items, force, review, max_frames, owner))
    return {"run_id": run_id, "status": "queued"}


# ---------------------------------------------------------------- app

@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _CHECKPOINTER
    events.bind_loop(asyncio.get_running_loop())  # SSE 总线钉住主循环（工作线程据此投递）
    _CHECKPOINTER = make_checkpointer()
    # 遥测出口：OTEL_EXPORTER_OTLP_ENDPOINT（collector）> LANGFUSE 直连 > 纯本地 jsonl
    observability.init()
    observability.register_runtime_gauges(lambda: {
        "queued": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
        "active": sum(1 for v in _TASKS.values() if v.get("status") == "running")})
    _cleanup_stale_runs()
    _prune_orphan_tmps()
    # 恢复中断的 run（对齐旧 _load_jobs 的语义；error 的不自动重跑，避免双份产物）。
    # tmp 已没了的 run 不可恢复（产物文件随目录一起没了，续跑只会拿空产物硬跑）：
    # 标记 error 收场，不占用执行信号量。
    resumed = 0
    for r in _load_runs().values():
        if r.get("status") != "running" or not r.get("url"):
            continue
        if r.get("tmp") and not Path(r["tmp"]).exists() and not r.get("tmp_cleaned"):
            r["status"] = "error"
            r["errors"] = [*(r.get("errors") or []), "临时目录已被清理，无法续跑（重发同 URL 新建 run）"]
            with _runs_file_lock():
                _runs_merge(r)
            continue
        _enqueue(r["url"], r.get("part_items", ""), bool(r.get("force")), False, 60,
                 run_id=r["run_id"], owner=r.get("owner", ""))
        resumed += 1
    if resumed:
        print(f"♻️ 恢复 {resumed} 个中断 run（上次服务退出前未跑完）")
    yield


app = FastAPI(title="video2md", version="0.2.0", lifespan=_lifespan,
              docs_url="/docs", redoc_url=None, openapi_url="/openapi.json")

# CORS：仅当配了 origin 才挂（同源/SSH 隧道开发不需要，也避免误开 *）
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------- 统一错误信封

def _err(code: int, message: str, details=None) -> JSONResponse:
    return JSONResponse(status_code=code,
                        content={"error": {"code": code, "message": message,
                                           "details": details}})


@app.exception_handler(AuthError)
async def _on_auth_error(request: Request, exc: AuthError) -> JSONResponse:
    return _err(exc.status, exc.message)


# fastapi.HTTPException 比 starlette 基类更具体（端点里 raise 的都是它）——单独注册确保走统一信封
@app.exception_handler(HTTPException)
async def _on_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return _err(exc.status_code, str(exc.detail),
                details=exc.headers.get("X-Error") if exc.headers else None)


@app.exception_handler(StarletteHTTPException)
async def _on_starlette_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _err(422, "请求校验失败", details=exc.errors())


# ---------------------------------------------------------------- 用户级端点（鉴权 + 归属）

@app.post("/runs", response_model=RunCreatedOut,
          summary="发起一次视频转文档（异步作业：同步解析建 run，后台驱动整张图）")
async def post_runs(body: RunCreate, ident: Identity = Depends(current_identity)) -> dict:
    """同步解析建 run（旧 serve 语义：响应即知 total/all_skipped，批量驱动靠它分窗），
    随后异步驱动整张图。解析慢点（抖音分享页抓取 70s+）所以在线程里跑，不堵事件循环。"""
    from .graph import resolve_run
    from .resolve import extract_url

    url = extract_url((body.url or "").strip())
    part_items = (body.part_items or "").strip()
    if not url:
        _req_log("POST", "/runs", 400, user=ident.uid, error="缺 url")
        raise HTTPException(400, "缺 url")
    run_id = uuid.uuid4().hex[:8]
    run, err, is_dup = await asyncio.to_thread(
        resolve_run, url, part_items, body.force, run_id, ident.uid)
    if err:
        _req_log("POST", "/runs", 400, user=ident.uid, url=url[:80], error=err[:120])
        raise HTTPException(400, err)
    skipped = [bool(a.get("skipped")) for a in run["artifacts"].values()]
    resp = {"run_id": run["run_id"], "total": run["total"],
            "entries": [{"index": i, "url": e["url"], "skipped": s}
                        for i, (e, s) in enumerate(zip(run["entries"], skipped), 1)],
            "all_skipped": bool(skipped) and all(skipped)}
    if body.resolve_only:
        # 只解析不驱动（批量脚本分窗预飞）；后续同 URL+part_items+owner 的正式 POST
        # 会 dedup 命中这条记录并入队驱动——探测残档不会被启动恢复捡走（只认 running）
        _req_log("POST", "/runs", 200, run_id=run["run_id"], resolve_only=True, user=ident.uid)
        return {**resp, "status": "created"}
    if is_dup and _TASKS.get(run["run_id"], {}).get("status") in ("queued", "running"):
        _req_log("POST", "/runs", 200, run_id=run["run_id"], dedup=True, user=ident.uid)
        return {**resp, "status": _TASKS[run["run_id"]]["status"], "dedup": True}
    # 新 run 或「旧 run 驱动已死」的 dedup 残档——都入队驱动（图内幂等重入）
    _TASKS[run["run_id"]] = {"status": "queued", "url": url, "part_items": part_items,
                             "owner": ident.uid, "created": _now()}
    asyncio.create_task(_drive(run["run_id"], url, part_items, body.force,
                               body.review, body.max_frames, ident.uid))
    _req_log("POST", "/runs", 200, run_id=run["run_id"], total=run["total"],
             dedup=is_dup, user=ident.uid)
    return {**resp, "status": "queued", "dedup": is_dup}



@app.get("/runs/{run_id}", response_model=RunStatusOut, summary="run 终态 + 逐条目产物进度")
def get_run(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    run = _require_run_access(run_id, ident)
    task = _TASKS.get(run_id)
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



@app.get("/runs/{run_id}/note", response_model=RunNotesOut,
         summary="产出的 markdown（标题 + vault 路径 + 正文）")
def get_run_notes(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    run = _require_run_access(run_id, ident)
    if not run:
        return {"run_id": run_id, "status": None, "notes": []}
    notes = [{"title": a.get("title", ""), "path": a.get("note", ""), "md": a.get("md", "")}
             for a in (run.get("artifacts") or {}).values() if a.get("note")]
    return {"run_id": run_id, "status": run.get("status"), "notes": notes}



def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _run_current_status(run_id: str) -> tuple[str | None, str | None]:
    """(status, url) 快照：内存任务优先（实时），回退 runs.json。"""
    task = _TASKS.get(run_id)
    if task:
        return task.get("status"), task.get("url")
    run = _load_runs().get(run_id)
    if run:
        return run.get("status"), run.get("url")
    return None, None



@app.get("/runs/{run_id}/events", summary="SSE 实时进度（graph:*/stage 事件流）")
async def run_events(run_id: str, request: Request,
                     ident: Identity = Depends(current_identity)) -> StreamingResponse:
    """连上即发一条 connected 快照，随后把该 run 的事件总线原样转发；见收尾事件即关流。

    图在工作线程里 publish → 主循环 call_soon_threadsafe 投递；慢/断客户端靠 keepalive +
    队列上限丢弃兜底，绝不反压主管线。轮询 GET /runs/{id} 仍并存（前端可只用其一）。
    """
    _require_run_access(run_id, ident)
    q = events.subscribe(run_id)

    async def gen():
        try:
            st, url = _run_current_status(run_id)
            yield _sse({"event": "connected", "run_id": run_id, "status": st, "url": url})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # 注释行保活，穿透反代空闲超时
                    continue
                yield _sse(rec)
                if events.is_terminal(rec):
                    break
                cur, _ = _run_current_status(run_id)
                if cur in _TERMINAL_STATUSES:
                    yield _sse({"event": "run_finalized", "run_id": run_id, "status": cur})
                    break
        finally:
            events.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 运维探针（不鉴权：容器 healthcheck / gpu_router 看门狗）

@app.get("/queue", response_model=QueueOut)
def get_queue() -> dict:
    """gpu_router 的 colab 看门狗回读（queue_url 指这里）——判队列保温。"""
    return {"queue_len": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
            "running": sum(1 for v in _TASKS.values() if v.get("status") == "running")}



@app.get("/healthz", response_model=HealthOut)
def healthz() -> dict:
    return {"ok": True, "ts": _now()}



@app.get("/preflight", response_model=PreflightOut)
def preflight() -> dict:
    return _preflight()

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

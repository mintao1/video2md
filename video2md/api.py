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
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from .config import ENV, LOG_DIR, OUT_DIR, PROJ
from .graph import make_checkpointer, run_graph
from .runs import _load_runs, _runs_file_lock, _runs_merge

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



def _checkpoint() -> dict:
    """对账快照：任务表计数 + 算力池可达性 + 临时目录残留。"""
    counts: dict[str, int] = {}
    for t in _TASKS.values():
        counts[t.get("status", "?")] = counts.get(t.get("status", "?"), 0) + 1
    snap = {"ts": _now(), "counts": counts,
            "queue_len": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
            "running": sum(1 for v in _TASKS.values() if v.get("status") == "running"),
            "tmp_leftovers": len(list(Path(tempfile.gettempdir()).glob("video2md-*")))}
    remote = (ENV.get("V2MD_REMOTE") or "").strip()
    if remote.startswith(("http://", "https://")):
        try:
            snap["gpu_pool"] = httpx.get(f"{remote.rstrip('/')}/status", timeout=2).json()
        except Exception as exc:
            snap["gpu_pool"] = {"ok": False, "error": str(exc)[:100]}
    return snap


def _cleanup_stale_runs(max_age_days: int = 30, idle_hours: float = 24.0) -> None:
    """清陈旧/失活 run 的临时目录：run 生命周期随 kb 阶段结束清，这里兜底异常中断。

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
        run = _load_runs().get(run_id)  # created → running（启动恢复只认 running）
        if run and run.get("status") == "created":
            run["status"] = "running"
            with _runs_file_lock():
                _runs_merge(run)
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
        if r.get("status") != "running" or not r.get("url"):
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
    """同步解析建 run（旧 serve 语义：响应即知 total/all_skipped，批量驱动靠它分窗），
    随后异步驱动整张图。解析慢点（抖音分享页抓取 70s+）所以在线程里跑，不堵事件循环。"""
    from .graph import resolve_run
    from .resolve import extract_url

    data = await request.json()
    url = extract_url(str(data.get("url", "")).strip())
    part_items = str(data.get("part_items", "")).strip()
    force = bool(data.get("force"))
    if not url:
        _req_log("POST", "/runs", 400, error="缺 url")
        raise HTTPException(400, "缺 url")
    run_id = uuid.uuid4().hex[:8]
    run, err, is_dup = await asyncio.to_thread(resolve_run, url, part_items, force, run_id)
    if err:
        _req_log("POST", "/runs", 400, url=url[:80], error=err[:120])
        raise HTTPException(400, err)
    skipped = [bool(a.get("skipped")) for a in run["artifacts"].values()]
    resp = {"run_id": run["run_id"], "total": run["total"],
            "entries": [{"index": i, "url": e["url"], "skipped": s}
                        for i, (e, s) in enumerate(zip(run["entries"], skipped), 1)],
            "all_skipped": bool(skipped) and all(skipped)}
    if data.get("resolve_only"):
        # 只解析不驱动（批量脚本分窗预飞）；后续同 URL+part_items 的正式 POST
        # 会 dedup 命中这条记录并入队驱动——探测残档不会被启动恢复捡走（只认 running）
        _req_log("POST", "/runs", 200, run_id=run["run_id"], resolve_only=True)
        return {**resp, "status": "created"}
    if is_dup and _TASKS.get(run["run_id"], {}).get("status") in ("queued", "running"):
        _req_log("POST", "/runs", 200, run_id=run["run_id"], dedup=True)
        return {**resp, "status": _TASKS[run["run_id"]]["status"], "dedup": True}
    # 新 run 或「旧 run 驱动已死」的 dedup 残档——都入队驱动（图内幂等重入）
    _TASKS[run["run_id"]] = {"status": "queued", "url": url, "part_items": part_items,
                             "created": _now()}
    asyncio.create_task(_drive(run["run_id"], url, part_items, force,
                               bool(data.get("review")), int(data.get("max_frames") or 60)))
    _req_log("POST", "/runs", 200, run_id=run["run_id"], total=run["total"], dedup=is_dup)
    return {**resp, "status": "queued", "dedup": is_dup}



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

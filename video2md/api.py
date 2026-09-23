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
  GET  /runs/{id}/bundle   deliver 模式产物整包 zip（笔记/帧/转录/节段/索引-append）——云端不
                           储存，只在此窗口发送到用户端；已焚/过期 410
  DELETE /runs/{id}/artifacts deliver 模式 ACK 即焚：客户端确认收完，云端立刻销毁产物
  GET  /queue           {queue_len, running}——gpu_router 的 colab 看门狗回读这里（运维探针，不鉴权）
  GET  /healthz         存活探针（Docker healthcheck，不鉴权）
  GET  /preflight       开跑前体检（ffmpeg/磁盘/vault 或 outbox/算力池，不鉴权）

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
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import artsink, events, observability, watchdog
from .auth import AuthError, Identity, current_identity
from .config import CORS_ORIGINS, ENV, LOG_DIR, OUT_DIR, WATCHDOG_PERIOD_S
from .graph import make_checkpointer_from_env, run_graph
from .models import (
    ArtifactsAckOut,
    DeviceCodeOut,
    DeviceTokenBody,
    DeviceTokenOut,
    HealthOut,
    PreflightOut,
    QueueOut,
    RunCreate,
    RunCreatedOut,
    RunListOut,
    RunNotesOut,
    RunStatusOut,
)
from .runs import _load_runs, _runs_file_lock, _runs_merge, _save_runs
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
        if artsink.current_mode() == "deliver":
            ob = artsink.outbox_dir()
            ob.mkdir(parents=True, exist_ok=True)  # 产物不落 vault，体检对象换成在架根
            add("outbox", os.access(ob, os.W_OK),
                f"{ob}（deliver 模式，TTL {artsink.outbox_ttl_h():.0f}h 即焚）")
        else:
            OUT_DIR.mkdir(parents=True, exist_ok=True)  # 新鲜 vault（云上空卷）先建目录再测写
            add("vault", os.access(OUT_DIR, os.W_OK), str(OUT_DIR))
    except Exception as exc:
        add("outbox" if artsink.current_mode() == "deliver" else "vault",
            False, str(exc)[:120])
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


def _sweep_outbox() -> int:
    """deliver TTL 清扫：过期/盘上已丢的 outbox 物理删掉并记 burned（启动跑一遍 + 周期兜底）。

    过期的权威判定在 _outbox_live（请求时惰性焚，410 不掺假）；本函数负责让**文件真的没了**
    ——「云端不储存」是物理口径不是视图口径。
    """
    n = 0
    now = datetime.now()
    with _runs_file_lock():
        runs = _load_runs()
        changed = False
        for r in runs.values():
            ob = r.get("outbox")
            if not ob or r.get("artifacts_burned"):
                continue
            try:
                expired = datetime.fromisoformat(r.get("artifacts_expire", "2000-01-01")) <= now
            except (ValueError, TypeError):
                expired = True  # 到期时间缺失/非法（脏数据）：宁可烧也不永久 shelving
            if expired or not Path(ob).is_dir():
                shutil.rmtree(ob, ignore_errors=True)
                r["artifacts_burned"] = True
                r["burned_reason"] = "ttl" if expired else "missing"
                changed = True
                n += 1
        if changed:
            _save_runs(runs)
    if n:
        print(f"🔥 outbox 清扫：焚毁 {n} 个 run 的产物（TTL {artsink.outbox_ttl_h():.0f}h 到期/盘上已丢）")
    return n


# ---------------------------------------------------------------- 任务表
_TASKS: dict[str, dict] = {}          # run_id → {status, url, owner, created, started, finished, errors}
_SEMA = asyncio.Semaphore(1)          # 串行执行：单 worker 语义
_CHECKPOINTER = None                  # lifespan 注入（sqlite/postgres 由 V2MD_CHECKPOINTER 定）
def _ck_closer() -> None:
    pass  # checkpointer 资源释放钩子（PG 分支关连接池）

_TASKS_KEEP = 200                     # 内存任务表上限（超出裁最老，runs.json 才是真相）

_TERMINAL_STATUSES = ("done", "partial", "error")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _unlink_quiet(path: str) -> None:
    """BackgroundTask 收尾：zip 发完即删（deliver 的「不留副本」到临时打包文件这一层）。"""
    with suppress(OSError):
        os.unlink(path)


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
    owner = (run.get("owner", "") if run else (task or {}).get("owner", "")) or ""
    if owner != ident.uid:
        raise HTTPException(403, f"无权访问 run {run_id}")
    return run


# ---------------------------------------------------------------- deliver 产物在架态（TTL/ACK 即焚）

def _burn_run(run_id: str, run: dict, reason: str) -> None:
    """即时销毁在架产物并留痕（run 内存副本同步更新，响应口径一致）。"""
    if run.get("outbox"):
        shutil.rmtree(run["outbox"], ignore_errors=True)
    with _runs_file_lock():
        fresh = _load_runs().get(run_id)
        if fresh:
            fresh["artifacts_burned"] = True
            fresh["burned_reason"] = reason
            _runs_merge(fresh)
    run["artifacts_burned"] = True
    run["burned_reason"] = reason


def _outbox_live(run: dict) -> tuple[Path | None, str | None]:
    """(在架目录, 到期时刻)；None=不在架。过期在读取时惰性焚——410 不是视图口径。"""
    exp = run.get("artifacts_expire") or ""
    rid = run.get("run_id", "")
    if run.get("artifacts_burned") or not run.get("outbox"):
        return None, exp or None
    try:
        expired = datetime.fromisoformat(exp) <= datetime.now() if exp else False
    except (ValueError, TypeError):
        expired = True
    if expired:
        _burn_run(rid, run, "ttl")
        return None, exp or None
    p = Path(run["outbox"])
    if not p.is_dir():  # 目录被外部挪走：盘上一致性优先，视同已焚
        _burn_run(rid, run, "missing")
        return None, exp or None
    return p, exp or None


def _deliver_outbox(run_id: str, ident: Identity) -> Path:
    """/bundle 与 DELETE /artifacts 的共同访问闸：409(模式) / 404(无 run 或无产物) /
    403(越权，_require_run_access 里) / 410(已焚)。"""
    if artsink.current_mode() != "deliver":
        raise HTTPException(409, f"当前产物模式为 {artsink.current_mode()}："
                                 "bundle/artifacts 端点仅 deliver 模式提供")
    run = _require_run_access(run_id, ident)
    if not run:
        raise HTTPException(404, f"run {run_id} 无落盘记录（产物无从取架）")
    path, _exp = _outbox_live(run)
    if path is None:
        if run.get("artifacts_burned"):
            raise HTTPException(410, f"产物已焚（{run.get('burned_reason', 'ttl')}），"
                                     "云端不保留——需要请重发 URL 生成新 run")
        raise HTTPException(404, "产物尚未生成（run 进行中或没有写出笔记）")
    return path


async def _outbox_sweeper(period_s: float = 600.0) -> None:
    """deliver 周期 TTL 清扫：启动扫一遍管冷启动，长驻进程还得把窗口里陆续到期的烧掉。"""
    while True:
        await asyncio.sleep(period_s)
        try:
            await asyncio.to_thread(_sweep_outbox)
        except Exception as exc:
            print(f"⚠ outbox 清扫异常（下轮重试）：{redact(str(exc))[:160]}")


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
            watchdog.forget(run_id)  # 终态清心跳（_HEARTBEATS 不随 run 数增长）



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
    global _CHECKPOINTER, _ck_closer
    events.bind_loop(asyncio.get_running_loop())  # SSE 总线钉住主循环（工作线程据此投递）
    _CHECKPOINTER, _ck_closer = make_checkpointer_from_env(ENV)
    # 遥测出口：OTEL_EXPORTER_OTLP_ENDPOINT（collector）> LANGFUSE 直连 > 纯本地 jsonl
    observability.init()
    observability.register_runtime_gauges(lambda: {
        "queued": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
        "active": sum(1 for v in _TASKS.values() if v.get("status") == "running")})
    _cleanup_stale_runs()
    _prune_orphan_tmps()
    if artsink.current_mode() == "deliver":
        _sweep_outbox()  # 停机期间到期的产物，起来先物理焚掉（冷启动口径）
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
    sweeper = (asyncio.create_task(_outbox_sweeper())
               if artsink.current_mode() == "deliver" else None)
    # run 卡死看门狗：周期扫内存任务表，心跳超时按 V2MD_WATCHDOG_ACTION 处置
    # （mark=标 error 等人工；exit=进程退出靠容器重启自愈，见 watchdog.py 模块注释）
    wd = asyncio.create_task(watchdog.loop(_TASKS, WATCHDOG_PERIOD_S))
    yield
    wd.cancel()
    if sweeper:
        sweeper.cancel()
    try:  # 释放 checkpointer 资源（PG 分支关池；sqlite 是 no-op）
        _ck_closer()
    except Exception as exc:
        print(f"⚠ checkpointer 关闭异常（退出流程不拦）：{exc}")


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

_GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"


@app.post("/auth/device/code", response_model=DeviceCodeOut,
          summary="设备流登录第一步：向 Google 申请 device_code/user_code")
async def auth_device_code() -> dict:
    """给没有浏览器重定向能力的客户端（Obsidian 插件、CLI）用：服务端桥。"""
    d = await _google_device_code()
    _req_log("POST", "/auth/device/code", 200, user="(anon)")
    return d


@app.post("/auth/device/token", response_model=DeviceTokenOut,
          summary="设备流轮询：device_code → id_token（用户授权前 401 pending）")
async def auth_device_token(body: DeviceTokenBody) -> dict:
    """acctkit 的 authenticate 是 headers 入参契约，换票逻辑复用其私有 _exchange；
    成功即回 id_token——插件此后所有请求带 `Authorization: Bearer <id_token>`，
    与网页端同一条 GoogleProvider 验签路径（external_id=sub 归并同一 uid）。"""
    from acctkit.providers import GoogleDeviceProvider

    cid = ENV.get("GOOGLE_CLIENT_ID", "").strip()
    secret = ENV.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        raise HTTPException(500, "服务端未配置 GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET，"
                                 "无法提供设备流登录")
    p = GoogleDeviceProvider(cid, secret)
    try:
        tok = await asyncio.to_thread(p._exchange, body.device_code)
    except AuthError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    idt = str(tok.get("id_token") or "")
    if not idt:
        raise HTTPException(401, "Google 设备流未返回 id_token")
    _req_log("POST", "/auth/device/token", 200, user="(anon)")
    return {"id_token": idt}


async def _google_device_code() -> dict:
    cid = ENV.get("GOOGLE_CLIENT_ID", "").strip()
    secret = ENV.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        raise HTTPException(500, "服务端未配置 GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET，"
                                 "无法提供设备流登录")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_GOOGLE_DEVICE_CODE_URL,
                             data={"client_id": cid, "client_secret": secret,
                                   "scope": "openid email profile"})
    except Exception as exc:
        raise HTTPException(502, f"Google device/code 不可达：{type(exc).__name__}") from exc
    d = r.json()
    if r.status_code != 200:
        raise HTTPException(502, f"Google device/code 拒绝：{str(d)[:200]}")
    return {"device_code": d["device_code"], "user_code": d["user_code"],
            "verification_url": d["verification_url"],
            "expires_in": int(d.get("expires_in") or 0), "interval": int(d.get("interval") or 5)}


@app.get("/runs", response_model=RunListOut,
         summary="列出本人名下的 run（插件收件箱发现新产物用）")
def get_runs(status: str = "", limit: int = 50,
             ident: Identity = Depends(current_identity)) -> dict:
    """owner 过滤同 _require_run_access 口径；created 倒序。deliver 模式下逐条标
    在架状态（_outbox_live 含惰性焚——过期即 present=false，不掺假）。"""
    limit = max(1, min(limit, 500))
    mode = artsink.current_mode()
    rows = []
    for r in sorted(_load_runs().values(), key=lambda x: str(x.get("created") or ""), reverse=True):
        if (r.get("owner", "") or "") != ident.uid:
            continue
        if status and r.get("status") != status:
            continue
        done_n = sum(1 for a in (r.get("artifacts") or {}).values()
                     if a.get("note") and not a.get("skipped"))
        arts = None
        if mode == "deliver":
            path, exp = _outbox_live(r)
            arts = {"mode": "deliver", "present": path is not None, "expires": exp}
        rows.append({"run_id": r.get("run_id"), "url": r.get("url"),
                     "status": r.get("status"), "created": r.get("created"),
                     "total": r.get("total"), "done_entries": done_n, "artifacts": arts})
        if len(rows) >= limit:
            break
    return {"items": rows, "mode": mode}


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
            "entries": [{"index": i, "url": e["url"], "skipped": s,
                         "from_run": ((run["artifacts"].get(e["url"]) or {})
                                      .get("skipped_from") or {}).get("run_id") or None}
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
                "from_run": (art.get("skipped_from") or {}).get("run_id") or None,
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
    if run and artsink.current_mode() == "deliver":
        path, exp = _outbox_live(run)  # 客户端凭它知道拉取窗口（present=false 即已焚）
        out["artifacts"] = {"mode": "deliver", "present": path is not None, "expires": exp}
    return out



@app.get("/runs/{run_id}/note", response_model=RunNotesOut,
         summary="产出的 markdown（标题 + 产物路径 + 正文），供云上调取")
def get_run_notes(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    """vault 模式：正文常驻 runs.json 记录（原语义）。deliver 模式：finalize 已把全文
    剥出 runs.json，这里从在架 outbox 的文件读——已焚即 410，「云端不储存」不掺假。"""
    run = _require_run_access(run_id, ident)
    if not run:
        return {"run_id": run_id, "status": None, "notes": []}
    deliver = artsink.current_mode() == "deliver"
    shelf, _exp = _outbox_live(run) if deliver else (None, None)
    notes = []
    for a in (run.get("artifacts") or {}).values():
        if not a.get("note"):
            continue
        md = a.get("md")
        if not md and deliver:
            p = Path(a["note"])
            if shelf is None or not p.is_file():
                raise HTTPException(410, "产物已焚（ACK/TTL 即焚），云端不留正文——"
                                         "需要请重发 URL 生成新 run")
            md = p.read_text(encoding="utf-8")
        notes.append({"title": a.get("title", ""), "path": a.get("note", ""), "md": md or ""})
    return {"run_id": run_id, "status": run.get("status"), "notes": notes}


def _zip_dir(src: Path) -> tuple[str, int]:
    """目录现打包 zip（temp 文件，发送方负责删）→ (路径, 字节数)。结构原样保留。"""
    fd, zpath = tempfile.mkstemp(prefix=f"v2md-bundle-{src.name}-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    zf.write(f, arcname=str(f.relative_to(src)))
        return zpath, os.path.getsize(zpath)
    except OSError:
        with suppress(OSError):
            os.unlink(zpath)
        raise


@app.get("/runs/{run_id}/bundle", summary="deliver 产物整包 zip（笔记+帧图+转录+节段+索引-append）",
         response_class=FileResponse)
def get_run_bundle(run_id: str, ident: Identity = Depends(current_identity)) -> FileResponse:
    """「发送到用户端」的投递主体：在架 outbox 整包（wikilink 相对结构原样，客户端
    解进本机 vault 即成品）。zip 现打包、发完即删（BackgroundTask），云端不留副本。

    410=已焚/过期（重发 URL 生成新 run）；404=run 不存在、越权 403、非 deliver 模式 409。
    """
    src = _deliver_outbox(run_id, ident)
    zpath, size = _zip_dir(src)
    _req_log("GET", f"/runs/{run_id}/bundle", 200, user=ident.uid, zip_bytes=size)
    return FileResponse(zpath, media_type="application/zip",
                        filename=f"video2md-{run_id}.zip",
                        background=BackgroundTask(_unlink_quiet, zpath))


@app.delete("/runs/{run_id}/artifacts", response_model=ArtifactsAckOut,
            summary="ACK 即焚：客户端确认收完，云端立刻销毁产物（不等 TTL）")
def delete_run_artifacts(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    path = _deliver_outbox(run_id, ident)
    run = _load_runs().get(run_id) or {"run_id": run_id, "outbox": str(path)}
    _burn_run(run_id, run, "ack")
    return {"run_id": run_id, "status": "burned"}



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

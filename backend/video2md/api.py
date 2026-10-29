"""FastAPI 服务：LangGraph 编排的 HTTP 入口（替代 serve.py + Dify 轮询）。

Dify 时代的双进程分工（Dify 画布轮询 ←HTTP→ serve.py 阶段打工）收拢为单进程：
POST /runs 入队 → 后台任务内嵌跑完整张 LangGraph（阶段即节点），客户端轮询
GET /runs/{id} 看终态。并发上限纪律：asyncio 信号量=V2MD_MAX_CONCURRENT_RUNS
（默认 1 = 单 worker 语义；生产多用户调大，checkpointer 建议切 postgres，
vault 自托管姿势建议保持 1）。入队前配额闸：总队列/每用户在途/60s 滑窗提交频率。

Phase 1（多用户后端契约硬化）：鉴权可插拔（默认 off，生产 google ID-Token）、
Pydantic 请求/响应契约、重开 OpenAPI、run 按内部 uid 归属隔离（越权 403）、
SSE 实时进度（GET /runs/{id}/events）与轮询并存。核心 run_graph 不动，本层只是传输。

端点契约：
  POST /runs            {url, part_items?, force?, review?, max_frames?, resolve_only?} → {run_id, total, ...}
  POST /uploads         multipart 上传视频/音频 → {path,size,name}（path 直接喂 /runs；多用户上传面）
  GET  /runs/{id}       run 记录 + 任务状态 + 逐条目产物进度（沿用 serve 的形状）
  GET  /runs/{id}/note  产出 markdown（标题/路径/正文），供云上调取
  GET  /runs/{id}/events SSE 实时进度（graph:*/stage 事件流；轮询 GET /runs/{id} 仍在）
  GET  /runs/{id}/bundle   deliver 模式产物整包 zip（笔记/帧/转录/节段/索引-append）——云端不
                           储存，只在此窗口发送到用户端；已焚/过期 410
  DELETE /runs/{id}/artifacts deliver 模式 ACK 即焚：客户端确认收完，云端立刻销毁产物
  POST /runs/{id}/cancel 撤销进行中 run（P1-2）：排队立即 error(cancelled)；运行中协作式
                        （循环头拦截）；他副本驱动走 cmd 广播；终态 409
  GET  /cost            本人本月成本归因（P2-2：usage/单价/预算；预算超限 POST /runs 429 BUDGET_EXCEEDED）
  GET  /queue           {queue_len, running, limits}——gpu_router 的 colab 看门狗回读这里（运维探针，不鉴权）
  GET  /healthz         存活探针（Docker healthcheck，不鉴权）
  GET  /preflight       开跑前体检（ffmpeg/磁盘/vault 或 outbox/算力池；匿名面 detail 脱敏，P0-3）

恢复语义：进程重启时把 runs.json 里 running 的 run 重新入队（同 run_id、同 owner），
graph 的 resolve 节点幂等复用该 run，阶段节点按产物字段跳过已完成条目。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import artsink, events, observability, watchdog
from .auth import AuthError, Identity, current_identity, optional_identity
from .colab_auth import router as colab_auth_router
from .config import (
    BUDGET_MONTH_USD,
    CORS_ORIGINS,
    DEVICE_RATE_PER_MIN,
    ENV,
    LOCAL_PATHS,
    LOG_DIR,
    MAX_CONCURRENT_RUNS,
    OUT_DIR,
    PRICE_GPU_MIN,
    PRICE_LLM_IN_MTOK,
    PRICE_LLM_OUT_MTOK,
    QUEUE_MAX,
    TRUST_XFF,
    UPLOAD_MAX_GB,
    UPLOAD_QUOTA_GB,
    UPLOADS_DIR,
    UPLOADS_TTL_H,
    USER_QUEUE_MAX,
    USER_SUBMIT_PER_MIN,
    VERSION,
    WATCHDOG_PERIOD_S,
)
from .graph import make_checkpointer_from_env, run_graph
from .models import (
    ArtifactsAckOut,
    CancelOut,
    CostOut,
    DeviceCodeOut,
    DeviceTokenBody,
    DeviceTokenOut,
    HealthOut,
    LlmSettingsOut,
    LlmSettingsUpdate,
    MeOut,
    PreflightOut,
    PrefsItem,
    PrefsOut,
    PrefsUpdate,
    QueueOut,
    QuotaOut,
    RunCreate,
    RunCreatedOut,
    RunListOut,
    RunNotesOut,
    RunStatusOut,
    UploadOut,
)
from .prefs import prefs_store
from .runs import (
    _load_runs,
    _runs_file_lock,
    _runs_merge,
    cancel_requested,
    delete_terminal_run,
    forget_cancel,
    month_rollup,
    patch_owned_run,
    request_cancel,
    settle_cost,
)
from .util import redact, sanitize_filename

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
        from .colab import health
        ok, detail = health()
        add("colab_cli", ok, detail)
    elif remote.startswith(("http://", "https://")):
        # GPU 算力池/远程转录服务：/status（池子）或 /ping（裸参考服务端）二选一认
        base = remote.rstrip("/")
        # 池子 token 闸门在产（gpu_router B4a）：裸调 /status 得 401 JSON——providers
        # 解析为空列表造成「可用 provider: 无」假阴性（2026-09-27 实测入册；
        # token 未设/空 = 不带头，本地裸池向后兼容，同 util 池客户端口径）
        _tok = (ENV.get("V2MD_POOL_TOKEN") or "").strip()
        _hdr = {"Authorization": f"Bearer {_tok}"} if _tok else {}
        ok, detail = False, ""
        try:
            d = httpx.get(f"{base}/status", timeout=4, headers=_hdr).json()
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


def _preflight_public(payload: dict) -> dict:
    """匿名视图（P0-3）：checks[].detail 全部剥除——detail 含 ffmpeg 绝对路径、磁盘
    余量、vault/outbox 路径、算力池 provider 名与内网 base URL，匿名面只回结论。"""
    return {"ok": payload["ok"], "ts": payload["ts"],
            "checks": [{"name": c["name"], "ok": c["ok"]} for c in payload["checks"]]}


def _preflight_privileged(client_host: str, authed: bool) -> bool:
    """完整 detail 判权：回环（deploy.sh / 容器内 Dockerfile 体检路径）或真实认证。
    off 档 authenticate 恒过（acctkit providers=[fake,dev] 兜底），不构成凭证语义 →
    非回环一律脱敏，公网暴露时 off 档 detail 亦不外泄。"""
    return client_host in ("127.0.0.1", "::1", "localhost") or authed



def _cleanup_stale_runs(max_age_days: int = 30, idle_hours: float = 24.0) -> None:
    """清陈旧/失活 run 的临时目录：run 生命周期随 finalize 节点结束清，这里兜底异常中断。

    两类会清：
    - 超龄（>30 天）——历史保险丝
    - 闲置 >24h——阶段失败后 tmp 无人清理（一个合集能占几百 MB）；「闲置」按最近阶段
      落盘时间算，正常批次阶段间隔以分钟计，撞不上

    落盘逐条 merge（json 档=锁内读改写循环，与原 save_all 等价；PG 档=行级原子，
    save_all 在多实例下会覆盖他进程写入故禁用）。多副本纪律：在途（queued/running）
    一律跳过——他进程正驱动的 tmp 不是垃圾，翻终态后再由下一轮清扫收走。
    """
    now = time.time()
    for rid, r in list(_load_runs().items()):
        if r.get("tmp_cleaned"):
            continue
        if r.get("status") in ("queued", "running"):
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
            with _runs_file_lock():
                _runs_merge(r)



def _prune_orphan_tmps(min_age_h: float = 6.0) -> None:
    """清没人认领的 run 临时目录：runs.json 记录丢失或进程被 SIGKILL 时，tmp 目录永久残留，
    30 天兜底等于不清。只动 `video2md-run-*`（CLI 的 `video2md-*` 由 finally 自清）。

    6 小时内动过的目录视为可能在跑（目录 mtime 随文件增删刷新），不清。
    """
    tdir = Path(tempfile.gettempdir())
    claimed = {r.get("tmp") for r in _load_runs().values() if not r.get("deleted")}
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
    for r in list(_load_runs().values()):
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
            with _runs_file_lock():
                _runs_merge(r)  # 逐条落盘：PG 档行级原子，多实例安全
            n += 1
    if n:
        print(f"🔥 outbox 清扫：焚毁 {n} 个 run 的产物（TTL {artsink.outbox_ttl_h():.0f}h 到期/盘上已丢）")
    return n


# ---------------------------------------------------------------- 任务表
_TASKS: dict[str, dict] = {}          # run_id → {status, url, owner, created, started, finished, errors}
_SEMA = asyncio.Semaphore(MAX_CONCURRENT_RUNS)   # 并发闸门：默认 1=单 worker 语义（V2MD_MAX_CONCURRENT_RUNS）
_CHECKPOINTER = None                  # lifespan 注入（sqlite/postgres 由 V2MD_CHECKPOINTER 定）
def _ck_closer() -> None:
    pass  # checkpointer 资源释放钩子（PG 分支关连接池）

_TASKS_KEEP = 200                     # 内存任务表上限（超出裁最老，runs.json 才是真相）

_PROC_ID = uuid.uuid4().hex[:8]       # 副本身份：claimed_by 钉名 / watchdog steal 用

# P4b 多副本档（V2MD_TASKS_STORE=postgres）：_TASKS 写路径保留不动（本进程实时视图），
# 读侧换 PG 真相源——/queue、配额两闸+滑窗、dedup 判活、watchdog 大盘巡检、启动恢复
# 认领全部走 v2md_runs（record jsonb 是状态唯一真相，status 提取列是其可查镜像）。
# 缺省 json 档 _tasks_backend() 恒 None，下列新代码路径整体不激活。
_TASKS_PG = None            # PostgresTaskStore | None（测试可直改本属性切档）
_tasks_pg_resolved = False


def _tasks_backend():
    global _TASKS_PG, _tasks_pg_resolved
    if not _tasks_pg_resolved:
        _tasks_pg_resolved = True
        if (ENV.get("V2MD_TASKS_STORE") or "").strip().lower() == "postgres":
            from . import pg, pgstore
            _TASKS_PG = pgstore.PostgresTaskStore(pg.shared_pool())
    return _TASKS_PG


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
    if run and run.get("deleted"):
        raise HTTPException(404, f"run {run_id} 不存在")
    if not run and not task:
        raise HTTPException(404, f"run {run_id} 不存在")
    owner = (run.get("owner", "") if run else (task or {}).get("owner", "")) or ""
    if owner != ident.uid:
        raise HTTPException(403, f"无权访问 run {run_id}")
    return run


# ---------------------------------------------------------------- 配额闸（终局布局·限流配额）

_USER_SUBMIT_TS: dict[str, deque] = {}  # uid → 60s 滑窗提交时间戳（monotonic）

# 匿名面滑窗（P0-2）：IP → 60s 时间戳。设备流两端点共享一桶（code 发起 + token 轮询
# 是同一次登录的两半）。进程内记账起步（多副本时各副本独立桶——nginx limit_req 是
# 跨副本兜底层，见 deploy/nginx.conf）。
_DEVICE_WIN: dict[str, deque] = {}


def _client_ip(request: Request) -> str:
    """客户端 IP：TRUST_XFF=on 取 X-Forwarded-For 末位，否则 socket 对端。

    末位语义=单层反代（仓内 nginx）append 的 $remote_addr——客户端伪造前置链也
    尾位可信。仅 api 绑回环（唯一入口=本机反代）拓扑下开 TRUST_XFF，直连开发档
    保持 off（socket 直读，伪造 XFF 无效）。
    """
    if TRUST_XFF == "on":
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[-1].strip()
    return (request.client.host if request.client else "") or "unknown"


def _admit_device(request: Request, *, label: str = "POST /auth/device/*") -> str:
    """设备流两端点的 IP 滑窗闸（P0-2）：匿名零频控可刷 OAuth 配额/被 Google 封 client_id。

    打在调 Google 之前（护的是出网配额）；0 = 不设限。返回客户端 IP 供审计日志。
    label 供复用方（Google 授权码流登录端点）改审计行的方法名——默认值=设备流原口径。
    """
    ip = _client_ip(request)
    if not DEVICE_RATE_PER_MIN:
        return ip
    now = time.monotonic()
    dq = _DEVICE_WIN.setdefault(ip, deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= DEVICE_RATE_PER_MIN:
        method, path = label.split(" ", 1)
        _req_log(method, path, 429, user=f"(anon:{ip})")
        raise HTTPException(429, f"请求过频（限 {DEVICE_RATE_PER_MIN} 次/分钟/IP），稍后重试",
                            headers={"X-Error": "RATE_LIMITED"})
    dq.append(now)
    return ip


def _month_start_iso() -> str:
    return datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) \
        .isoformat(timespec="seconds")


def _budget_spend(uid: str, be) -> float:
    """uid 本月已结算成本（PG 档 SQL 求和 / json 档全表扫——与 runs.month_rollup 同源）。"""
    since = _month_start_iso()
    if be is not None:
        return be.month_spend(uid, since)
    return month_rollup(uid, since)["spend_usd"]


def _admit_submission(uid: str) -> None:
    """POST /runs 入队前的配额闸：总队列 / 每用户在途 / 滑窗提交频率 / 月度预算（P2-2）。

    超限一律 429 + X-Error 符号码（错误信封统一转译）；0 = 该项不设限。
    PG 档（V2MD_TASKS_STORE=postgres，多副本）各闸全读 DB 真相源——各副本计数一致、
    滑窗跨进程不丢；json 档逐字节现状（进程内视图）。
    """
    be = _tasks_backend()
    if BUDGET_MONTH_USD:
        spent = _budget_spend(uid, be)
        if spent >= BUDGET_MONTH_USD:
            raise HTTPException(
                429, f"本月预算已用尽（${spent:.2f}/${BUDGET_MONTH_USD:.2f}），"
                     f"下月自动重置或联系管理员调整 V2MD_BUDGET_MONTH_USD",
                headers={"X-Error": "BUDGET_EXCEEDED"})
    if be is not None:
        if QUEUE_MAX:
            n = be.total_active()
            if n >= QUEUE_MAX:
                raise HTTPException(429, f"队列已满（{n}/{QUEUE_MAX}），稍后重试",
                                    headers={"X-Error": "QUEUE_FULL"})
        if USER_QUEUE_MAX:
            mine = be.active_for(uid)
            if mine >= USER_QUEUE_MAX:
                raise HTTPException(429, f"你在途/排队的 run 已达上限（{mine}/{USER_QUEUE_MAX}）",
                                    headers={"X-Error": "USER_QUEUE_FULL"})
        if USER_SUBMIT_PER_MIN:
            if be.submit_window_count(uid) >= USER_SUBMIT_PER_MIN:
                raise HTTPException(429, f"提交过频（限 {USER_SUBMIT_PER_MIN} 次/分钟），稍后重试",
                                    headers={"X-Error": "RATE_LIMITED"})
        return
    active = [v for v in _TASKS.values() if v.get("status") in ("queued", "running")]
    if QUEUE_MAX and len(active) >= QUEUE_MAX:
        raise HTTPException(429, f"队列已满（{len(active)}/{QUEUE_MAX}），稍后重试",
                            headers={"X-Error": "QUEUE_FULL"})
    if USER_QUEUE_MAX:
        mine = sum(1 for v in active if v.get("owner") == uid)
        if mine >= USER_QUEUE_MAX:
            raise HTTPException(429, f"你在途/排队的 run 已达上限（{mine}/{USER_QUEUE_MAX}）",
                                headers={"X-Error": "USER_QUEUE_FULL"})
    if USER_SUBMIT_PER_MIN:
        now = time.monotonic()
        dq = _USER_SUBMIT_TS.setdefault(uid, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= USER_SUBMIT_PER_MIN:
            raise HTTPException(429, f"提交过频（限 {USER_SUBMIT_PER_MIN} 次/分钟），稍后重试",
                                headers={"X-Error": "RATE_LIMITED"})
        dq.append(now)


# ---------------------------------------------------------------- 本地路径白名单 + 上传面

_UPLOAD_EXT_OK = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}

# 内容嗅探（P0-6）：后缀白名单可被改名绕过，任意内容会进 ffprobe/ffmpeg 解析路径——
# 流式写盘后读首 16 字节比对容器魔数，不符即删 + 422（与 413 分支同款回滚）。
# 与 _UPLOAD_EXT_OK 一一映射；取保守白名单：现代编码器（ffmpeg/yt-dlp 产物）必带标准头，
# 误杀优先于漏放。mp3 兼容 ID3v2 前缀与裸帧同步（0xFF 0xEx）两种起点。
_UPLOAD_MAGIC: dict[str, Callable[[bytes], bool]] = {
    ".mp4": lambda b: len(b) >= 8 and b[4:8] == b"ftyp",
    ".mov": lambda b: len(b) >= 8 and b[4:8] == b"ftyp",
    ".m4a": lambda b: len(b) >= 8 and b[4:8] == b"ftyp",
    ".mkv": lambda b: b.startswith(b"\x1a\x45\xdf\xa3"),          # EBML
    ".webm": lambda b: b.startswith(b"\x1a\x45\xdf\xa3"),
    ".wav": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE",
    ".mp3": lambda b: b.startswith(b"ID3")
                     or (len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0),
}


def _upload_magic_ok(path: Path, ext: str) -> bool:
    """首 16 字节容器头比对（容器魔数全在前 16 字节内，一次便宜的同步读）。"""
    check = _UPLOAD_MAGIC.get(ext)
    if check is None:
        return True  # 白名单外后缀在入口已 422，此处防御性兜底
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return bool(check(head))


def _validate_run_url(url: str, uid: str) -> None:
    """非 http 输入的白名单闸（终局布局·产物层：不收裸本地路径）。

    V2MD_LOCAL_PATHS：auto（默认）= AUTH=off 放行（开发/自托管），AUTH≠off 只认本人
    uploads 目录内路径；allow/deny 显式覆盖。拒绝 → 422 LOCAL_PATH_FORBIDDEN。
    """
    if url.startswith(("http://", "https://")):
        return
    if LOCAL_PATHS == "allow":
        return
    if LOCAL_PATHS == "auto" and (ENV.get("V2MD_AUTH") or "off").strip().lower() == "off":
        return  # 开发/自托管姿势：本地路径输入是文档化能力（e2e/batch/容器内路径）
    inside = False
    try:
        p = Path(url).expanduser().resolve()
        owned = (UPLOADS_DIR / uid).resolve()
        inside = p == owned or owned in p.parents
    except (OSError, RuntimeError):
        inside = False
    if inside:
        return
    hint = ("本地路径输入已被禁用（V2MD_LOCAL_PATHS=deny）" if LOCAL_PATHS == "deny"
            else "多用户档不收裸本地路径：先 POST /uploads 上传，或自托管设 V2MD_LOCAL_PATHS=allow")
    raise HTTPException(422, hint, headers={"X-Error": "LOCAL_PATH_FORBIDDEN"})


def _du_bytes(root: Path) -> int:
    """上传配额用的目录体积（walk 求和；上传频率低，遍历可接受）。"""
    total = 0
    for f in root.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _sweep_uploads() -> int:
    """uploads TTL 清扫：超龄文件物理删、空目录回收（启动跑一遍 + 周期兜底）。"""
    if not UPLOADS_DIR.is_dir():
        return 0
    cutoff = time.time() - UPLOADS_TTL_H * 3600
    n = 0
    for f in UPLOADS_DIR.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            continue
    for d in sorted((p for p in UPLOADS_DIR.rglob("*") if p.is_dir()), reverse=True):
        with suppress(OSError):
            d.rmdir()  # 只删空目录（rmdir 非递归，有文件自然失败）
    return n


async def _uploads_sweeper(period_s: float = 600.0) -> None:
    """上传面周期 TTL 清扫（与 outbox sweeper 同款纪律：异常只留痕不终止）。"""
    while True:
        await asyncio.sleep(period_s)
        try:
            n = await asyncio.to_thread(_sweep_uploads)
            if n:
                print(f"🧹 uploads 清扫：删 {n} 个超龄文件（TTL {UPLOADS_TTL_H:g}h）")
        except Exception as exc:
            print(f"⚠ uploads 清扫异常（下轮重试）：{redact(str(exc))[:160]}")


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

_DRIVES: dict[str, asyncio.Task] = {}
_RUNNING_DRIVES: set[str] = set()


@asynccontextmanager
async def _execution_slot(run_id: str):
    # Recheck persisted priority after acquiring: promotion can reorder existing
    # waiters while async with guarantees release on cancellation or storage errors.
    while True:
        async with _SEMA:
            records = _load_runs()
            waiting = [(rid, task) for rid, task in _TASKS.items()
                       if task.get("status") == "queued" and (rid in _DRIVES or rid == run_id)]
            waiting.sort(key=lambda item: (
                -int((records.get(item[0]) or {}).get("queue_priority") or 0),
                int(item[1].get("queued_at") or 0), item[1].get("created", "")))
            task = _TASKS.get(run_id) or {}
            if task.get("status") != "queued" or not waiting or waiting[0][0] == run_id:
                yield
                return
        await asyncio.sleep(0.05)


async def _drive(run_id: str, url: str, part_items: str, force: bool,
                 review: bool, max_frames: int, owner: str = "") -> None:
    """排队等信号量 → 线程里跑同步 graph（stage 函数全是同步阻塞，不能堵事件循环）。

    P1-1：①拿到信号量先 still_mine 复核——排队期间租约被大盘 watchdog 划给别的副本
    时静默让位（接管方重投驱动，本进程不双跑）；②created/queued 皆翻 running
    （排队接管路径 DB 态是 queued，原 created→running 翻转对它不命中）。
    """
    async with _execution_slot(run_id):
        be = _tasks_backend()
        if be is not None and not be.still_mine(run_id, _PROC_ID):
            _TASKS.pop(run_id, None)   # 已被他副本接管：本进程视图让位，DB 真相由接管者续写
            watchdog.forget(run_id)
            forget_cancel(run_id)
            print(f"↪ run {run_id} 排队期间租约被其他副本接管，本进程让位不驱动")
            return
        if cancel_requested(run_id):
            # P1-2：排队期被撤销。远端 cmd 撤销（本进程仍持 queued）由此处代翻终态——
            # 记录写入归持有者，单写者纪律；翻完静默退场，不占信号量跑图。
            t = _TASKS.get(run_id)
            if t and t.get("status") in ("queued", "running"):
                _flip_cancelled(run_id, "用户取消（cancelled）：排队期间撤销")
            forget_cancel(run_id)
            watchdog.forget(run_id)
            return
        t = _TASKS.get(run_id)
        if t is None or t.get("status") not in ("queued", "running"):
            # 端点本地撤销已翻终态（取消标志归本协程 finally 清——端点刻意不清，防
            # 「标志先清、_drive 后醒」竞态复活已撤销的 run）：醒来时任务已非排队/
            # 运行态（或已被裁剪）→ 静默退场。
            forget_cancel(run_id)
            watchdog.forget(run_id)
            return
        _TASKS[run_id].update(status="running", started=_now())
        run = _load_runs().get(run_id)  # created/queued → running（启动恢复与排队接管都汇到这）
        if run and run.get("status") in ("created", "queued"):
            run["status"] = "running"
            with _runs_file_lock():
                _runs_merge(run)
        if be is not None:
            be.note_claimed_by(run_id, _PROC_ID)  # 大盘 watchdog 据此区分本进程/他进程残档
        try:
            _RUNNING_DRIVES.add(run_id)
            final = await asyncio.to_thread(run_graph, url, part_items=part_items,
                                            force=force, review=review,
                                            max_frames=max_frames, run_id=run_id,
                                            checkpointer=_CHECKPOINTER, owner=owner)
            _TASKS[run_id].update(status=final.get("status") or "done",
                                  finished=_now(), errors=(final.get("errors") or [])[-10:])
        except Exception as exc:  # graph 节点已全捕获，走到这是框架级异常
            _TASKS[run_id].update(status="error", finished=_now(),
                                  errors=[redact(str(exc))[:300]])
            patch_owned_run(run_id, owner, ("created", "queued", "running", "done", "partial", "error"),
                            {"status": "error", "errors": [redact(str(exc))[:300]]})
        finally:
            _RUNNING_DRIVES.discard(run_id)
            observability.metric_run(_TASKS[run_id].get("status", "error"))
            observability.close_run(run_id)  # 兜底闭合 run 级 root span（各 stage 已各自闭合）
            observability.flush()
            watchdog.forget(run_id)  # 终态清心跳（_HEARTBEATS 不随 run 数增长）
            forget_cancel(run_id)    # P1-2：清取消标志（同款防增长纪律）
            try:
                settle_cost(run_id)  # P2-2：终态结算 usage×单价 → cost_usd 落记录
            except Exception as exc:  # 结算绝不影响终态语义
                print(f"⚠ 成本结算异常（忽略）：{redact(str(exc))[:120]}")



def _flip_cancelled(run_id: str, msg: str) -> None:
    """终态翻转 error(cancelled)：内存任务表 + runs 记录同步留痕（SSE/轮询面自动生效）。"""
    t = _TASKS.get(run_id)
    if t is not None:
        t.update(status="error", finished=_now(), errors=[*(t.get("errors") or []), msg][-10:])
    with _runs_file_lock():
        run = _load_runs().get(run_id)
        if run:
            run["status"] = "error"
            run["errors"] = [*(run.get("errors") or []), msg][-20:]
            _runs_merge(run)


def _enqueue(url: str, part_items: str, force: bool, review: bool,
             max_frames: int, run_id: str = "", owner: str = "") -> dict:
    run_id = run_id or uuid.uuid4().hex[:8]
    queued_at = time.time_ns()
    _TASKS[run_id] = {"status": "queued", "url": url, "owner": owner, "created": _now(),
                      "queued_at": queued_at}
    with _runs_file_lock():
        record = _load_runs().get(run_id)
        if record:
            if record.get("status") != "queued" or not record.get("queued_at"):
                record.update(queued_at=queued_at, queue_priority=0)
            _TASKS[run_id]["queued_at"] = record["queued_at"]
            record.update(status="queued", review=review, max_frames=max_frames)
            _runs_merge(record)
            if record.get("cancel_requested"):
                request_cancel(run_id)
    if len(_TASKS) > _TASKS_KEEP:  # 裁最老的已完成记录（queued/running 永不裁）
        done = sorted((k for k, v in _TASKS.items() if v.get("finished")),
                      key=lambda k: _TASKS[k]["finished"])
        for k in done[: len(_TASKS) - _TASKS_KEEP]:
            _TASKS.pop(k, None)
    be = _tasks_backend()
    if be is not None:
        # P1-1：入队即落库+钉名——PG 档配额闸/判活/queue gauge/大盘接管从此看得见排队项
        # （此前 queued 只在内存 _TASKS，DB 停在 created：跨副本双投、重启丢排队 run）
        be.mark_queued(run_id, _PROC_ID)
    drive = asyncio.create_task(_drive(run_id, url, part_items, force, review, max_frames, owner))
    _DRIVES[run_id] = drive

    def finished(task):
        if _DRIVES.get(run_id) is task:
            _DRIVES.pop(run_id, None)

    drive.add_done_callback(finished)
    return {"run_id": run_id, "status": "queued"}


async def _queue_hb_loop() -> None:
    """排队项心跳（P1-1）：每 WATCHDOG_PERIOD_S 扫内存 queued 项打点。

    单一常驻任务（非 per-run tasker）：生命周期随 lifespan、零泄漏。touch 的 DB 段
    是同步 SQL → to_thread 不堵事件循环。json 档 touch 退化为本地打点（无 DB/广播），
    整循环无害。心跳让「排队久」（信号量前排长队，健康）与「无主停滞」（属主死）
    靠 updated 新鲜度可区分——大盘 watchdog 用短租约（3×period）接管后者。
    """
    while True:
        await asyncio.sleep(WATCHDOG_PERIOD_S)
        try:
            for rid, task in list(_TASKS.items()):
                if task.get("status") == "queued":
                    await asyncio.to_thread(watchdog.touch, rid)
        except Exception as exc:  # 心跳循环绝不反压主管线
            print(f"⚠ 排队心跳异常（下轮重试）：{redact(str(exc))[:160]}")


async def _proc_hb_loop() -> None:
    """进程心跳（P1-4）：周期广播 phb——他副本启动恢复的属主甄别依据。

    无条件广播（run 级 hb 只在有活 run 时发，空闲副本也得可被甄别为活）。
    json 档 publish_cmd 为本地 no-op，整循环无害。
    """
    while True:
        await asyncio.sleep(watchdog.PHB_PERIOD_S)
        try:
            await asyncio.to_thread(events.publish_cmd, "phb", "")
        except Exception as exc:
            print(f"⚠ 进程心跳广播异常（下轮重试）：{redact(str(exc))[:160]}")


def _tmp_missing_run(rec: dict) -> bool:
    return bool(rec.get("tmp")) and not Path(str(rec["tmp"])).exists() \
        and not rec.get("tmp_cleaned")


async def _recover_interrupted(be) -> int:
    """PG 档启动恢复（P1-4 属主甄别版，后台任务——甄别窗不拖 boot）。

    语义（替代旧 recover_candidates 全量 SKIP LOCKED 抢占，批判补遗②）：
    - tmp 缺失 → mark_stuck_error 翻 error（记录级事实，与属主无关）；
    - 无主（claimed_by=''）→ claim_dead 原子认领重投；
    - 有主且属主活着（phb 注册表闻声）→ 跳过：**B 重启不抢 A 的活 run**；
    - 有主但持续无声 → 甄别窗（≤PHB_ALIVE_S）后 claim_dead 认领重投——单副本重启
      自愈走此径（旧属主已死必无声；活属主的下一次 phb ≤PHB_PERIOD_S 必达窗内）。
    并发竞争由 claim_dead 条件 UPDATE 原子互斥（claimed_by 仍=预期值才翻）。
    """
    from . import watchdog as wd

    def _requeue(rec: dict) -> None:
        _enqueue(rec["url"], rec.get("part_items", ""), bool(rec.get("force")), False, 60,
                 run_id=rec["run_id"], owner=rec.get("owner", ""))

    resumed = 0
    try:
        rows = await asyncio.to_thread(be.recovery_scan)
    except Exception as exc:
        print(f"⚠ 启动恢复扫描失败（按无恢复处理）：{redact(str(exc))[:160]}")
        return 0
    orphans: list[tuple[str, dict]] = []
    for row in rows:
        rid = str(row.get("run_id") or "")
        rec = row.get("record") or {}
        if not rid or not rec.get("url"):
            continue
        if _tmp_missing_run(rec):
            try:
                await asyncio.to_thread(
                    be.mark_stuck_error, rid, "临时目录已被清理，无法续跑（重发同 URL 新建 run）")
            except Exception as exc:
                print(f"⚠ 恢复翻 error 失败 {rid}：{redact(str(exc))[:120]}")
            continue
        owner = str(row.get("claimed_by") or "")
        if not owner:
            got = None
            try:
                got = await asyncio.to_thread(be.claim_dead, rid, "", _PROC_ID)
            except Exception as exc:
                print(f"⚠ 无主认领失败 {rid}：{redact(str(exc))[:120]}")
            if got:
                _requeue(got)
                resumed += 1
        elif not wd.proc_alive(owner):
            orphans.append((owner, rec))  # 属主甄别窗内二次确认
    # 甄别窗：等活属主的 phb（≤PHB_ALIVE_S），窗外仍无声 → 认领重投
    deadline = time.monotonic() + watchdog.PHB_ALIVE_S
    for owner, rec in orphans:
        while time.monotonic() < deadline and not wd.proc_alive(owner):
            await asyncio.sleep(1.0)
        if wd.proc_alive(owner):
            continue  # 属主活着（窗内闻声）：不抢
        got = None
        try:
            got = await asyncio.to_thread(be.claim_dead, rec["run_id"], owner, _PROC_ID)
        except Exception as exc:
            print(f"⚠ 死属主认领失败 {rec.get('run_id')}：{redact(str(exc))[:120]}")
        if got:
            print(f"♻️ 认领死属主 {owner} 的中断 run {rec['run_id']}")
            _requeue(got)
            resumed += 1
    return resumed


# ---------------------------------------------------------------- app

def _ensure_database_ready() -> None:
    """Fail fast when a PostgreSQL-backed component is configured.

    Previously the stores were resolved lazily, so a process could report healthy
    while still pointing at JSON files (or fail only on the first request).  A
    production process must either have a complete PostgreSQL configuration or
    explicitly use the JSON/SQLite development defaults.
    """
    pg_keys = ("V2MD_RUNS_STORE", "V2MD_STATE_STORE", "V2MD_USERS_STORE",
               "V2MD_TASKS_STORE", "V2MD_PREFS_STORE", "V2MD_EVENTS",
               "V2MD_CHECKPOINTER")
    wants_pg = any((ENV.get(key) or "").strip().lower() == "postgres" or
                    (key == "V2MD_EVENTS" and (ENV.get(key) or "").strip().lower() == "pg")
                    for key in pg_keys)
    if not wants_pg:
        return
    from . import pg
    if not pg.pg_enabled():
        raise RuntimeError("已启用 PostgreSQL 存储，但未配置 V2MD_PG_DSN")
    # shared_pool validates the migration version before accepting traffic.
    pg.shared_pool()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _CHECKPOINTER, _ck_closer
    _ensure_database_ready()
    events.bind_loop(asyncio.get_running_loop())  # SSE 总线钉住主循环（工作线程据此投递）
    _CHECKPOINTER, _ck_closer = make_checkpointer_from_env(ENV)
    # 遥测出口：OTEL_EXPORTER_OTLP_ENDPOINT（collector）> LANGFUSE 直连 > 纯本地 jsonl
    observability.init()
    def _gauge_counts() -> dict:
        """队列仪表：PG 档读全盘真相（多副本合计），异常回退本进程视图——gauge 不许炸。"""
        be = _tasks_backend()
        if be is not None:
            try:
                c = be.counts()
                return {"queued": c["queued"], "active": c["running"]}
            except Exception:
                pass
        return {"queued": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
                "active": sum(1 for v in _TASKS.values() if v.get("status") == "running")}
    observability.register_runtime_gauges(_gauge_counts)
    _cleanup_stale_runs()
    _prune_orphan_tmps()
    if artsink.current_mode() == "deliver":
        _sweep_outbox()  # 停机期间到期的产物，起来先物理焚掉（冷启动口径）
    _sweep_uploads()    # 上传面同款：停机期间超龄的源文件先清
    # 恢复中断的 run（对齐旧 _load_jobs 的语义；error 的不自动重跑，避免双份产物）。
    # tmp 已没了的 run 不可恢复（产物文件随目录一起没了，续跑只会拿空产物硬跑）：
    # 标记 error 收场，不占用执行信号量。
    # PG 档（P1-4 属主甄别版）：无主即领、有主过 phb 甄别窗——B 重启不抢 A 的活 run
    # （批判补遗②），死属主（单副本重启自愈）窗后认领。后台任务执行，甄别窗不拖 boot。
    be = _tasks_backend()
    resumed = 0
    if be is not None:
        def _report_resumed(t) -> None:
            if t.cancelled() or t.exception() is not None:
                return  # 取消（重启）/异常：恢复静默放弃，异常已在任务内打印过
            if t.result():
                print(f"♻️ 恢复 {t.result()} 个中断 run（属主甄别后认领）")
        rec_task = asyncio.create_task(_recover_interrupted(be))
        rec_task.add_done_callback(_report_resumed)
    else:
        for r in _load_runs().values():
            if r.get("deleted") or r.get("status") not in ("queued", "running") or not r.get("url"):
                continue
            if _tmp_missing_run(r):
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
    watchdog.set_stealer(lambda rec: _enqueue(
        rec["url"], rec.get("part_items", ""), bool(rec.get("force")), False, 60,
        run_id=rec["run_id"], owner=rec.get("owner", "")))
    sweeper = (asyncio.create_task(_outbox_sweeper())
               if artsink.current_mode() == "deliver" else None)
    uploads_sw = asyncio.create_task(_uploads_sweeper())
    # run 卡死看门狗：周期扫内存任务表，心跳超时按 V2MD_WATCHDOG_ACTION 处置
    # （mark=标 error 等人工；exit=进程退出靠容器重启自愈，见 watchdog.py 模块注释）
    wd = asyncio.create_task(watchdog.loop(_TASKS, WATCHDOG_PERIOD_S))
    qhb = asyncio.create_task(_queue_hb_loop())  # 排队项心跳（P1-1）：大盘接管判据的续约源
    phb = asyncio.create_task(_proc_hb_loop())   # 进程心跳（P1-4）：他副本属主甄别依据
    yield
    wd.cancel()
    qhb.cancel()
    phb.cancel()
    uploads_sw.cancel()
    if sweeper:
        sweeper.cancel()
    try:  # 释放 checkpointer 资源（sqlite no-op；postgres 池归 pg 模块统一关）
        _ck_closer()
    except Exception as exc:
        print(f"⚠ checkpointer 关闭异常（退出流程不拦）：{exc}")
    events.shutdown()  # 停 PG LISTEN 泵线程（local 档 no-op）
    from . import pg
    if pg.pg_enabled():
        pg.close_shared_pool()  # 三存储/checkpointer/事件扇出共用的进程级池


_production = (ENV.get("V2MD_ENV") or "").strip().lower() == "production"
app = FastAPI(title="video2md", version=VERSION, lifespan=_lifespan,
              docs_url=None if _production else "/docs", redoc_url=None,
              openapi_url=None if _production else "/openapi.json")
app.include_router(colab_auth_router)

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
    if request.url.path == "/settings/llm":
        # Pydantic errors can contain the entire submitted body, including an API key.
        return _err(422, "LLM 配置校验失败", details=[
            {"loc": e["loc"], "type": e["type"],
             "msg": "请检查 API URL、Key 或模型格式"} for e in exc.errors()])
    if request.url.path.startswith("/settings/colab"):
        return _err(422, "Colab 授权参数无效，请重新开始授权")
    return _err(422, "请求校验失败", details=exc.errors())


# PG 不可达/超预算 → 集中 503（P1-8：快速失败而非挂死；统一信封 PG_UNAVAILABLE）。
# QueryCanceled 单列：语句超时既可能是 PG 病了也可能是本查询慢——两者对调用方都表现为
# 「存储暂时不可用，稍后重试」。刻意不捕 DatabaseError 基类：UndefinedTable 等编程错误
# 仍是 500 级（带栈排查），不许被 503 吞掉。
async def _on_pg_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return _err(503, "存储暂时不可用（PG 超时/不可达），稍后重试", details="PG_UNAVAILABLE")


def _register_pg_503() -> None:
    import psycopg.errors as _pg_err
    from psycopg_pool import PoolTimeout
    for _cls in (PoolTimeout, _pg_err.OperationalError, _pg_err.QueryCanceled):
        app.add_exception_handler(_cls, _on_pg_unavailable)  # 逐类精确注册（starlette 按 MRO 查表）


_register_pg_503()


# ---------------------------------------------------------------- 用户级端点（鉴权 + 归属）

@app.get("/settings/llm", response_model=LlmSettingsOut, summary="读取本人的 LLM 配置（不回传 Key）")
def get_llm_settings(ident: Identity = Depends(current_identity)) -> dict:
    from . import llm_settings
    return llm_settings.public(llm_settings.load(ident.uid))


@app.put("/settings/llm", response_model=LlmSettingsOut, summary="保存本人的 OpenAI 兼容 LLM 配置")
def put_llm_settings(body: LlmSettingsUpdate, ident: Identity = Depends(current_identity)) -> dict:
    from . import llm_settings
    key = body.api_key.get_secret_value() if body.api_key is not None else None
    if key is None and not llm_settings.load(ident.uid).get("api_key"):
        raise HTTPException(422, "首次配置请填写 API Key")
    row = llm_settings.update(ident.uid, base_url=body.base_url, api_key=key, model=body.model.strip())
    return llm_settings.public(row)


@app.delete("/settings/llm", response_model=LlmSettingsOut, summary="清除本人配置，恢复服务默认 LLM")
def delete_llm_settings(ident: Identity = Depends(current_identity)) -> dict:
    from . import llm_settings
    return llm_settings.public(llm_settings.update(ident.uid, clear=True))


@app.post("/uploads", response_model=UploadOut,
          summary="上传视频/音频文件（多用户专用上传面：落地后 path 直接喂 POST /runs）")
async def post_upload(file: UploadFile, ident: Identity = Depends(current_identity)) -> dict:
    """流式落盘 data/uploads/<uid>/（不进内存）；单文件上限/每用户配额/类型白名单，
    TTL 到期惰性清扫——「云端不储存」口径延伸到源文件。"""
    name = sanitize_filename(file.filename or "upload")
    ext = Path(name).suffix.lower()
    if ext not in _UPLOAD_EXT_OK:
        raise HTTPException(422, f"不支持的文件类型 {ext or '(无后缀)'}："
                                 f"{'/'.join(sorted(_UPLOAD_EXT_OK))}",
                            headers={"X-Error": "UNSUPPORTED_TYPE"})
    dest_dir = UPLOADS_DIR / ident.uid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    max_bytes = int(UPLOAD_MAX_GB * 1e9)
    n = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1 << 20):
                n += len(chunk)
                if n > max_bytes:
                    raise HTTPException(413, f"文件超上限 {UPLOAD_MAX_GB:g}GB",
                                        headers={"X-Error": "FILE_TOO_LARGE"})
                out.write(chunk)
    except HTTPException:
        with suppress(OSError):
            dest.unlink()
        raise
    finally:
        await file.close()
    if UPLOAD_QUOTA_GB and _du_bytes(dest_dir) > UPLOAD_QUOTA_GB * 1e9:
        with suppress(OSError):
            dest.unlink()
        raise HTTPException(413, f"上传配额已满（{UPLOAD_QUOTA_GB:g}GB/用户），清理旧文件后重试",
                            headers={"X-Error": "QUOTA_EXCEEDED"})
    if not _upload_magic_ok(dest, ext):  # P0-6：后缀过了内容也得过，防改名绕过进 ffmpeg
        with suppress(OSError):
            dest.unlink()
        raise HTTPException(422, f"文件内容与 {ext} 容器格式不符（魔数校验失败）",
                            headers={"X-Error": "UNSUPPORTED_TYPE"})
    _req_log("POST", "/uploads", 200, user=ident.uid, name=name, bytes=n)
    return {"path": str(dest), "size": n, "name": name}


_GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"


@app.post("/auth/device/code", response_model=DeviceCodeOut,
          summary="设备流登录第一步：向 Google 申请 device_code/user_code")
async def auth_device_code(request: Request) -> dict:
    """给没有浏览器重定向能力的客户端（Obsidian 插件、CLI）用：服务端桥。"""
    ip = _admit_device(request)  # P0-2：IP 滑窗闸在调 Google 之前（护出网配额）
    d = await _google_device_code()
    _req_log("POST", "/auth/device/code", 200, user=f"(anon:{ip})")
    return d


@app.post("/auth/device/token", response_model=DeviceTokenOut,
          summary="设备流轮询：device_code → id_token（用户授权前 401 pending）")
async def auth_device_token(body: DeviceTokenBody, request: Request) -> dict:
    """acctkit 的 authenticate 是 headers 入参契约，换票逻辑复用其私有 _exchange；
    成功即回 id_token——插件此后所有请求带 `Authorization: Bearer <id_token>`，
    与网页端同一条 GoogleProvider 验签路径（external_id=sub 归并同一 uid）。"""
    ip = _admit_device(request)  # P0-2：轮询也过闸（与 code 共享一桶=同一次登录的两半）
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
    _req_log("POST", "/auth/device/token", 200, user=f"(anon:{ip})")
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


# ------------------------------------------- Google 授权码重定向登录（网页端）
_GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_STATE_TTL_S = 600
_GOOGLE_OAUTH_STATES: dict[str, float] = {}  # state → 过期时刻（monotonic）。进程内记账
_GOOGLE_OAUTH_REDIRECTS: dict[str, str] = {}  # state → 登录后回到的站内路径（随 state 一并消费）


def _google_oauth_timeout_s() -> float:
    """Token exchange timeout, bounded so a bad deployment cannot hang callbacks."""
    try:
        return min(60.0, max(3.0, float(ENV.get("V2MD_GOOGLE_OAUTH_TIMEOUT_S", "15"))))
    except (TypeError, ValueError):
        return 15.0


def _google_oauth_proxy() -> str | None:
    """Optional dedicated OAuth proxy; reject the retired 17898 endpoint."""
    proxy = (ENV.get("V2MD_GOOGLE_OAUTH_PROXY_URL") or "").strip()
    if not proxy:
        return None
    if ":17898" in proxy or "127.0.0.1:17898" in proxy:
        raise RuntimeError("V2MD_GOOGLE_OAUTH_PROXY_URL 指向已弃用的 17898 代理")
    return proxy
# 口径同 _DEVICE_WIN：单节点单副本安全；多副本各副本独立 dict 会挂——扩副本前先挪共享存储。


def _google_web_creds() -> tuple[str, str]:
    """网页端授权码流凭据：优先 GOOGLE_WEB_*（OAuth 应用里「Web」型 client），缺省回落
    GOOGLE_CLIENT_*。设备流的 GOOGLE_CLIENT_* 是「TV 与 Limited Input」型（Web 型无设备码
    端点），两者并存时必须分两套 env——本函数只管发起/换票侧取哪对；验签侧的多 audience
    收口在 video2md/auth/ext_google.py。回落语义保证单 client 开发档零配置。"""
    cid = (ENV.get("GOOGLE_WEB_CLIENT_ID") or ENV.get("GOOGLE_CLIENT_ID") or "").strip()
    secret = (ENV.get("GOOGLE_WEB_CLIENT_SECRET") or ENV.get("GOOGLE_CLIENT_SECRET") or "").strip()
    return cid, secret


def _google_public_base() -> str:
    """对外可达的 api scheme+host（redirect_uri 前缀）。生产=网关 https 域名，
    Google 对 redirect_uri 在授权页与换票时逐字比对、且须与控制台登记一致。"""
    return (ENV.get("V2MD_PUBLIC_BASE_URL") or "http://127.0.0.1:8790").rstrip("/")


def _google_web_base() -> str:
    """前端 origin：成功 302 回品牌首页 #id_token=…，失败 #auth_error=…（fragment
    不进服务端日志/代理）。"""
    return (ENV.get("V2MD_WEB_BASE_URL") or "http://localhost:5173").rstrip("/")


def _google_state_purge() -> None:
    now = time.monotonic()
    for k in [k for k, exp in _GOOGLE_OAUTH_STATES.items() if exp <= now]:
        _GOOGLE_OAUTH_STATES.pop(k, None)
    for k in [k for k in _GOOGLE_OAUTH_REDIRECTS if k not in _GOOGLE_OAUTH_STATES]:
        _GOOGLE_OAUTH_REDIRECTS.pop(k, None)


def _google_safe_redirect(path: str) -> str:
    """只接受站内绝对路径：拒 `//host`、`/\\host`（浏览器按协议相对处理）与控制字符，
    防开放重定向。不合格一律回空串（= 前端默认页）。"""
    path = (path or "").strip()
    if not path.startswith("/") or path.startswith(("//", "/\\")) or len(path) > 512:
        return ""
    if any(ord(ch) < 0x20 or ch == "\\" for ch in path):
        return ""
    return path


def _google_login_url(redirect: str) -> str:
    base = f"{_google_web_base()}/"
    return f"{base}?{urllib.parse.urlencode({'redirect': redirect})}" if redirect else base


def _google_error_redirect(msg: str, back: str = "") -> RedirectResponse:
    """浏览器整页导航端点的错误出口：JSON 错误信封对 302 跳转无意义，
    统一 302 回品牌首页 fragment，前端消费。back=站内回跳路径，重试时不丢。"""
    return RedirectResponse(f"{_google_login_url(back)}"
                            f"#auth_error={urllib.parse.quote(msg)}", status_code=302)


def _google_finish(resp: RedirectResponse) -> RedirectResponse:
    """所有出口统一清 CSRF state cookie（一次性，用完即焚）。"""
    resp.delete_cookie("v2md_oauth_state", path="/auth/google/callback")
    return resp


@app.get("/auth/google/login", summary="Google 授权码流发起：302 到 Google 授权页")
async def auth_google_login(request: Request, redirect: str = "", terms_accepted: bool = False) -> RedirectResponse:
    """网页端登录入口（整页跳转）：与设备流并存的第二条 Google 登录路——本端点只负责
    发起，真正换票/验签在 /auth/google/callback。IP 滑窗复用设备流闸（护 Google 出网
    配额；超限也走 fragment 错误出口——429 JSON 对浏览器导航无意义）。"""
    back = _google_safe_redirect(redirect)
    if not terms_accepted:
        _req_log("GET", "/auth/google/login", 400,
                 user=f"(anon:{_client_ip(request)})", error_type="terms_not_accepted")
        return _google_error_redirect("请先阅读并同意《用户服务协议》与《隐私保护政策》", back)
    try:
        ip = _admit_device(request, label="GET /auth/google/login")
    except HTTPException:
        return _google_error_redirect(f"请求过频（限 {DEVICE_RATE_PER_MIN} 次/分钟/IP），稍后重试",
                                      _google_safe_redirect(redirect))
    cid, secret = _google_web_creds()
    if not (cid and secret):
        _req_log("GET", "/auth/google/login", 500, user=f"(anon:{ip})")
        return _google_error_redirect("服务端未配置 Google 登录凭据", back)
    _google_state_purge()
    state = secrets.token_urlsafe(32)
    _GOOGLE_OAUTH_STATES[state] = time.monotonic() + _GOOGLE_STATE_TTL_S
    if back:
        _GOOGLE_OAUTH_REDIRECTS[state] = back
    q = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": f"{_google_public_base()}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",  # 重登强制账号选择器，避免静默选中旧账号
    })
    _req_log("GET", "/auth/google/login", 302, user=f"(anon:{ip})")
    resp = RedirectResponse(f"{_GOOGLE_OAUTH_AUTH_URL}?{q}", status_code=302)
    # 登录 CSRF 闸：state 与浏览器绑定（HttpOnly 防脚本读；SameSite=Lax 保证 Google 顶层
    # 跳回本端点时仍携带；Path 只对回调生效，不污染其它请求）。
    resp.set_cookie("v2md_oauth_state", state, max_age=_GOOGLE_STATE_TTL_S,
                    httponly=True, samesite="lax", path="/auth/google/callback")
    return resp


@app.get("/auth/google/callback", summary="Google 回调：换票验签后 302 回前端（fragment 带 id_token）")
async def auth_google_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    """授权码 → id_token（服务端换票，client secret 不出后端）→ 302 回前端 fragment。
    state 单次消费 + cookie 比对（防登录 CSRF：攻击者拿自己的 code+state 钓受害者回跳
    即可把受害者登录成攻击者账号——cookie 绑定后该向量关闭）。验 aud=cid 早失败早提示；
    后续 /me 的验签走引擎侧多 audience provider（ext_google），两条路 external_id=sub
    同口径归并同一 uid。"""
    ip = _client_ip(request)
    exp = _GOOGLE_OAUTH_STATES.pop(state, None) if state else None
    back = _GOOGLE_OAUTH_REDIRECTS.pop(state, "") if state else ""
    cookie_state = request.cookies.get("v2md_oauth_state") or ""
    if exp is None or exp <= time.monotonic() or cookie_state != state:
        _req_log("GET", "/auth/google/callback", 400, user=f"(anon:{ip})")
        return _google_finish(_google_error_redirect("登录状态已过期或校验失败，请重新登录", back))
    if not code:
        return _google_finish(_google_error_redirect("Google 未返回授权码（用户可能取消了授权）", back))
    cid, secret = _google_web_creds()
    redirect_uri = f"{_google_public_base()}/auth/google/callback"
    try:
        async with httpx.AsyncClient(timeout=_google_oauth_timeout_s(),
                                     proxy=_google_oauth_proxy()) as c:
            r = await c.post(_GOOGLE_OAUTH_TOKEN_URL,
                             data={"code": code, "client_id": cid, "client_secret": secret,
                                   "redirect_uri": redirect_uri,
                                   "grant_type": "authorization_code"})
    except Exception as exc:
        # Keep the failure actionable without ever logging code/state/credentials.
        _req_log("POST", "/auth/google/token", 503, user=f"(anon:{ip})",
                 error_type=type(exc).__name__, timeout_s=_google_oauth_timeout_s())
        return _google_finish(_google_error_redirect(f"Google 换票不可达（{type(exc).__name__}），请重新登录", back))
    if r.status_code != 200:
        _req_log("POST", "/auth/google/token", r.status_code, user=f"(anon:{ip})",
                 error_type="oauth_http_error")
        return _google_finish(_google_error_redirect("Google 授权码无效或已使用，请重新登录", back))
    idt = str(r.json().get("id_token") or "")
    if not idt:
        return _google_finish(_google_error_redirect("Google 未返回 id_token", back))
    from acctkit.providers import GoogleProvider

    try:
        await asyncio.to_thread(
            GoogleProvider(cid, proxy_url=_google_oauth_proxy() or "")._verify,
            idt,
        )
    except Exception as exc:
        cause = exc.__cause__ or exc.__context__
        _req_log("POST", "/auth/google/verify", 401, user=f"(anon:{ip})",
                 error_type=type(exc).__name__,
                 error_detail=str(exc)[:240],
                 cause_type=type(cause).__name__ if cause else "",
                 cause_detail=str(cause)[:240] if cause else "")
        return _google_finish(_google_error_redirect(
            f"Google ID-Token 校验失败（{type(exc).__name__}）", back))
    _req_log("GET", "/auth/google/callback", 302, user=f"(anon:{ip})")
    # id_token 是 base64url JWT（fragment 字符集安全，无需再编码）；fragment 不落服务端日志。
    return _google_finish(RedirectResponse(f"{_google_login_url(back)}#id_token={idt}",
                                           status_code=302))


def _run_title(r: dict) -> str:
    """教材展示名（GET /runs 列表，web 教材库页数据源）：note 阶段的 LLM 标题优先
    （art.title；deliver 剥 md 但 title 留存）→ entry.title → url 主机名。永不空串。"""
    for a in (r.get("artifacts") or {}).values():
        if isinstance(a, dict) and (a.get("title") or "").strip():
            return str(a["title"]).strip()
    for e in (r.get("entries") or []):
        if (e.get("title") or "").strip():
            return str(e["title"]).strip()
    return urllib.parse.urlparse(r.get("url") or "").hostname or r.get("url") or ""


def _public_run_diagnostic(run: dict) -> dict | None:
    """Never forward provider bodies, prompts, credentials or paths to the UI."""
    from .note import MIN_NOTE_TRANSCRIPT_CHARS

    errors = run.get("errors") or []
    if not errors and (run.get('partial_failures') or {}).get('note'):
        short = any(len(str(a.get('transcript') or '').strip()) < MIN_NOTE_TRANSCRIPT_CHARS
                    for a in (run.get('artifacts') or {}).values() if not a.get('note'))
        return {'stage': 'note', 'message': ('转录内容过短，未达到教材生成质量门槛；请使用内容更完整的视频。'
                if short else '部分条目未生成教材，请检查视频内容或重试。')}
    if not errors:
        return None
    error = str(errors[-1])
    stage = next((name for name in ("note", "select_frames", "transcribe", "download", "frames", "ocr", "clips")
                  if name in error), "unknown")
    if "依赖阶段" in error or "clips 阶段无节段产物" in error:
        failed = run.get("partial_failures") or {}
        dependency = next((name for name in ("clips", "ocr", "select", "frames", "transcribe", "download")
                           if name in error and failed.get(name)), None)
        if dependency:
            label = {"clips": "视频剪辑", "ocr": "画面文字识别", "select": "画面选择", "frames": "抽帧",
                     "transcribe": "语音转写", "download": "视频下载"}[dependency]
            return {"stage": dependency, "message": f"{label}未完成，暂时无法生成教材；请补跑失败条目。"}
    status = re.search(r"HTTP\s+(\d{3})", error)
    if status:
        code = status.group(1)
        message = {"402": "模型账户余额不足，请补足余额或更换可用配置。",
                   "403": "模型渠道拒绝访问，请检查渠道是否启用及账号权限。",
                   "401": "模型身份验证失败，请检查服务配置。",
                   "429": "模型请求受限，请稍后重试。"}.get(code, "上游服务请求失败，请稍后重试。")
        message = f"{message}（HTTP {code}）"
    elif '转录本过短' in error or '无转录本' in error:
        message = '转录内容过短或为空，未达到教材生成质量门槛；请使用内容更完整的视频。'
    elif "cancelled" in error or "用户取消" in error:
        message = "任务已取消（cancelled）。"
    elif "timeout" in error.lower() or "超时" in error:
        message = "处理阶段超时，请稍后重试。"
    else:
        message = "处理未完成，请重试或凭任务编号联系支持。"
    return {"stage": stage, "message": message}


def _run_accounting(run: dict) -> dict:
    return {key: run.get(key) for key in ("usage", "cost_usd", "cost_pricing")}


@app.get("/runs", response_model=RunListOut,
         summary="列出本人名下的 run（插件收件箱发现新产物用）")
def get_runs(status: str = "", limit: int = 50,
             ident: Identity = Depends(current_identity)) -> dict:
    """owner 过滤同 _require_run_access 口径；created 倒序。deliver 模式下逐条标
    在架状态（_outbox_live 含惰性焚——过期即 present=false，不掺假）。"""
    limit = max(1, min(limit, 500))
    mode = artsink.current_mode()
    rows = []
    records = _load_runs()
    queued = sorted((r for r in records.values() if r.get("status") == "queued"),
                    key=lambda r: (-int(r.get("queue_priority") or 0), int(r.get("queued_at") or 0),
                                   r.get("created") or "", r["run_id"]))
    positions = {r["run_id"]: i for i, r in enumerate(queued, 1)}
    for r in sorted(records.values(), key=lambda x: str(x.get("created") or ""), reverse=True):
        if r.get("deleted"):
            continue
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
        visible_status = ("cancelling" if r.get("cancel_requested") and r.get("status") in ("running", "queued")
                          else r.get("status"))
        rows.append({"run_id": r.get("run_id"), "url": r.get("url"),
                     "title": _run_title(r),
                     "status": visible_status, "created": r.get("created"),
                     "queue_pos": positions.get(r["run_id"]),
                     "total": r.get("total"), "done_entries": done_n, "artifacts": arts,
                     **_run_accounting(r), "diagnostic": _public_run_diagnostic(r)})
        if len(rows) >= limit:
            break
    return {"items": rows, "mode": mode}


@app.get("/prefs", response_model=PrefsOut,
         summary="本人教材偏好（收藏/阅读进度；web 教材库页数据源）")
def get_prefs(ident: Identity = Depends(current_identity)) -> dict:
    rows = prefs_store().list_uid(ident.uid)
    return {"items": [{"run_id": rid,
                       "fav": bool(p.get("fav")),
                       "progress": int(p.get("progress") or 0)}
                      for rid, p in sorted(rows.items())]}


@app.put("/prefs/{run_id}", response_model=PrefsItem,
         summary="更新本人教材偏好（部分更新 upsert；收藏/阅读进度）")
def put_prefs(run_id: str, body: PrefsUpdate,
              ident: Identity = Depends(current_identity)) -> dict:
    _require_run_access(run_id, ident)  # 404 不存在 / 403 越权
    row = prefs_store().upsert(ident.uid, run_id, fav=body.fav, progress=body.progress)
    return {"run_id": run_id, "fav": bool(row["fav"]), "progress": int(row["progress"])}


@app.post("/runs", response_model=RunCreatedOut,
          summary="发起一次视频转文档（异步作业：同步解析建 run，后台驱动整张图）")
async def post_runs(body: RunCreate, ident: Identity = Depends(current_identity)) -> dict:
    """同步解析建 run（旧 serve 语义：响应即知 total/all_skipped，批量驱动靠它分窗），
    随后异步驱动整张图。解析慢点（抖音分享页抓取 70s+）所以在线程里跑，不堵事件循环。"""
    from .graph import resolve_run
    from .resolve import extract_url, normalize_url

    url = extract_url((body.url or "").strip())
    part_items = (body.part_items or "").strip()
    if not url:
        _req_log("POST", "/runs", 400, user=ident.uid, error="缺 url")
        raise HTTPException(400, "缺 url")
    # Reopening an existing completed artifact is a read, not a billable rerun.
    # Explicit force, expired delivery, missing files, other owners and other
    # partitions all retain the normal admission/resolve path below.
    if not body.force:
        for previous in _load_runs().values():
            if (previous.get("deleted") or previous.get("status") != "done"
                    or previous.get("owner") != ident.uid
                    or normalize_url(previous.get("url") or "") != normalize_url(url)
                    or (previous.get("part_items") or "").strip() != part_items):
                continue
            notes = [a.get("note") for a in (previous.get("artifacts") or {}).values() if a.get("note")]
            reusable = (bool(notes) and all(Path(p).is_file() for p in notes))
            if artsink.current_mode() == "deliver":
                reusable = bool(notes) and _outbox_live(previous)[0] is not None
            if reusable:
                return {"run_id": previous["run_id"], "total": previous.get("total", 0),
                        "entries": [{"index": i, "url": e["url"], "skipped": True,
                                     "from_run": previous["run_id"]}
                                    for i, e in enumerate(previous.get("entries") or [], 1)],
                        "all_skipped": True, "status": "done", "dedup": True}
    _admit_submission(ident.uid)   # 配额闸（队列/每用户在途/滑窗频率）——便宜检查放最前
    _validate_run_url(url, ident.uid)  # 非 http 输入的白名单闸（多用户档不收裸本地路径）
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
    # PG 档判活读 DB（他进程驱动也算活，不重投）；本进程有活条目仍优先取实时状态。
    alive = bool(_TASKS.get(run["run_id"], {}).get("status") in ("queued", "running"))
    be = _tasks_backend() if is_dup else None
    db_alive = False
    if is_dup and not alive and be is not None:
        try:
            db_alive = be.is_alive(run["run_id"])
        except Exception:
            db_alive = False
    if is_dup and (alive or db_alive):
        # dedup 命中：旧 run 必在 created/running（resolve_run 语义）；本进程有活条目取实时态，
        # 否则 DB 判活即他进程驱动中，回 running（queued 瞬态由轮询/SSE 下一拍修正）。
        cur = _TASKS.get(run["run_id"], {}).get("status") or run.get("status") or "running"
        _req_log("POST", "/runs", 200, run_id=run["run_id"], dedup=True, user=ident.uid)
        return {**resp, "status": cur, "dedup": True}
    # 新 run 或「旧 run 驱动已死」的 dedup 残档——都入队驱动（图内幂等重入）；
    # POST 与 lifespan 恢复共用同一条入队路径（dedup 判活在上面，_enqueue 只管排队）
    _enqueue(url, part_items, body.force, body.review, body.max_frames,
             run_id=run["run_id"], owner=ident.uid)
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
        diagnostic = _public_run_diagnostic(run)
        out.update(**_run_accounting(run), diagnostic=diagnostic)
        if task and task.get("errors"):
            out["task"]["errors"] = [diagnostic["message"]] if diagnostic else []
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
                   errors=[diagnostic["message"]] if diagnostic else [],
                   entries=entries)
        if run.get("cancel_requested") and run.get("status") in ("running", "queued"):
            out["status"] = "cancelling"
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


@app.get("/runs/{run_id}/media/{name}", summary="读取本人笔记引用的图片或视频")
def get_run_media(run_id: str, name: str, ident: Identity = Depends(current_identity)):
    run = _require_run_access(run_id, ident)
    if not run or Path(name).name != name or "\\" in name:
        raise HTTPException(404, "媒体不存在")
    suffix = Path(name).suffix.lower()
    media_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                   ".webp": "image/webp", ".mp4": "video/mp4"}
    if suffix not in media_types:
        raise HTTPException(404, "媒体不存在")
    deliver = artsink.current_mode() == "deliver"
    shelf = _deliver_outbox(run_id, ident) if deliver else OUT_DIR
    notes = get_run_notes(run_id, ident)["notes"]
    referenced = any(name in re.findall(r"!\[\[attachments/([^\]|]+)(?:\|[^\]]*)?\]\]", n["md"])
                     for n in notes)
    path = shelf / "attachments" / name
    allowed_root = (shelf / "attachments").resolve()
    if not referenced or not path.resolve().is_relative_to(allowed_root) or not path.is_file():
        raise HTTPException(404, "媒体不存在")
    return FileResponse(path, media_type=media_types[suffix], headers={"Cache-Control": "no-store"})


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



@app.post("/runs/{run_id}/cancel", response_model=CancelOut,
          summary="撤销进行中的 run（排队项立即生效；运行中协作式；跨副本走 cmd 广播）")
def cancel_run(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    """P1-2 跨进程取消。动作端点而非 DELETE：记录留痕不删除（终态 error(cancelled)），
    与 DELETE /artifacts 的真删除语义区分（AIP-135 口径）。

    - 本进程 queued：无在途工作，立即翻终态（_drive 醒来见标志静默退场）；
    - 本进程 running：登记标志回 cancelling，协作式生效——下一个条目循环头
      SystemExit 走既有熔断通道，终态由 run_graph 留痕（长单阶段内无取消点）；
    - 他副本驱动（PG 档判 DB）：cmd 广播 cancel，目标副本只对本地驱动落标志；
    - 终态/未驱动：409 NOT_CANCELLABLE。
    """
    _require_run_access(run_id, ident)  # 404 不存在 / 403 越权
    task = _TASKS.get(run_id)
    local = (task or {}).get("status")
    if local in ("queued", "running"):
        patch_owned_run(run_id, ident.uid, ("queued", "running"), {"cancel_requested": True})
        request_cancel(run_id)
        _req_log("POST", f"/runs/{run_id}/cancel", 200, run_id=run_id, user=ident.uid,
                 local=local)
        if local == "queued":
            _flip_cancelled(run_id, "用户取消（cancelled）：排队期间撤销")
            # 不清取消标志：挂起的 _drive 醒来按标志/终态静默退场（标志归其 finally 清）。
            # 此处先清会有「标志先清、_drive 后醒」竞态——已撤销的 run 被复活驱动。
            watchdog.forget(run_id)
            return {"run_id": run_id, "status": "error"}
        return {"run_id": run_id, "status": "cancelling"}
    st = None
    be = _tasks_backend()
    if be is not None:
        try:
            st, _url = be.get_status_url(run_id)
        except Exception:
            st = None
        if st in ("queued", "running"):
            patch_owned_run(run_id, ident.uid, ("queued", "running"), {"cancel_requested": True})
            _req_log("POST", f"/runs/{run_id}/cancel", 200, run_id=run_id, user=ident.uid,
                     remote=True)
            events.publish_cmd("cancel", run_id)  # 目标副本 _on_cmd → 本地驱动才落标志
            return {"run_id": run_id, "status": "cancelling", "remote": True}
    raise HTTPException(409, f"run {run_id} 无可取消的进行中任务"
                             f"（当前 {local or st or '无任务记录'}）",
                        headers={"X-Error": "NOT_CANCELLABLE"})


async def _discard_waiting_drive(run_id: str) -> None:
    """A cancelled queued coroutine must retire before reusing its run ID."""
    old = _DRIVES.get(run_id)
    if old is not None and not old.done():
        old.cancel()
        with suppress(asyncio.CancelledError):
            await old


@app.post("/runs/{run_id}/resume", response_model=CancelOut, summary="重试失败或部分完成的任务")
async def resume_run(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    run = _require_run_access(run_id, ident)
    if not run or run.get("status") not in ("error", "partial"):
        raise HTTPException(409, "只有失败或部分完成的任务可以重试")
    task = _TASKS.get(run_id) or {}
    if run_id in _RUNNING_DRIVES or task.get("status") in ("running", "cancelling"):
        raise HTTPException(409, "原任务仍在结束中，请稍后重试")
    _admit_submission(ident.uid)
    run = patch_owned_run(run_id, ident.uid, ("error", "partial"),
                          {"status": "created", "cancel_requested": False})
    if run is None:
        raise HTTPException(409, "任务已被其他请求重试或删除")
    be = _tasks_backend()
    if be is not None and not be.release_claim_for_retry(run_id):
        raise HTTPException(409, "任务重试租约重置失败，请刷新后重试")
    await _discard_waiting_drive(run_id)
    forget_cancel(run_id)
    # 新建临时目录时复制仍在架的 deliver 产物，finalize 替换旧架时不能丢掉已完成笔记。
    fresh = None
    try:
        if run.get("tmp_cleaned") or not Path(run.get("tmp") or "/nonexistent").exists():
            fresh = Path(tempfile.mkdtemp(prefix="video2md-run-"))
            shelf, _expires = _outbox_live(run) if artsink.current_mode() == "deliver" else (None, None)
            if shelf is not None:
                shutil.copytree(shelf, fresh / "outbox")
                for art in run.get("artifacts", {}).values():
                    for key in ("note", "transcript_path"):
                        if art.get(key) and Path(art[key]).is_relative_to(shelf):
                            art[key] = str(fresh / "outbox" / Path(art[key]).relative_to(shelf))
            run["tmp"] = str(fresh)
            run["tmp_cleaned"] = False
            for art in run.get("artifacts", {}).values():
                if art.get("note") and not Path(art["note"]).is_file():
                    art.pop("note", None)
                    art.pop("md", None)
                for key in tuple(art):
                    if key not in ("note", "md", "title", "fp", "transcript_path"):
                        art.pop(key, None)
        run.update(status="created", errors=[], partial_failures={})
        with _runs_file_lock():
            _runs_merge(run)
        return _enqueue(run["url"], run.get("part_items", ""), False,
                        bool(run.get("review")), int(run.get("max_frames") or 60),
                        run_id=run_id, owner=ident.uid)
    except Exception:
        if fresh is not None:
            shutil.rmtree(fresh, ignore_errors=True)
        patch_owned_run(run_id, ident.uid, ("created", "queued"),
                        {"status": "error", "errors": ["重试准备失败，请稍后重试"]})
        raise



@app.post("/runs/{run_id}/promote", response_model=CancelOut, summary="将排队任务设为优先")
async def promote_run(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    run = _require_run_access(run_id, ident)
    if not run or run.get("status") != "queued":
        raise HTTPException(409, "只有排队任务可以设为优先")
    if patch_owned_run(run_id, ident.uid, ("queued",), {"queue_priority": time.time_ns()}) is None:
        raise HTTPException(409, "任务已开始，不能调整顺序")
    return {"run_id": run_id, "status": "queued"}


@app.delete("/runs/{run_id}", summary="删除已结束的任务记录（不删除本地 vault 笔记）")
async def delete_run(run_id: str, ident: Identity = Depends(current_identity)) -> dict:
    run = _require_run_access(run_id, ident)
    if not run or run.get("status") not in ("done", "partial", "error"):
        raise HTTPException(409, "请先取消任务并等待结束后再删除")
    if run_id in _RUNNING_DRIVES or (_TASKS.get(run_id) or {}).get("status") in ("running", "cancelling"):
        raise HTTPException(409, "任务仍在结束中，请稍后再删除")
    await _discard_waiting_drive(run_id)
    if artsink.current_mode() == "deliver" and run.get("outbox"):
        _burn_run(run_id, run, "deleted")
    if not delete_terminal_run(run_id, ident.uid):
        raise HTTPException(409, "任务状态已改变，无法删除")
    _TASKS.pop(run_id, None)
    forget_cancel(run_id)
    return {"run_id": run_id, "deleted": True}


@app.get("/runs/{run_id}/events", summary="SSE 实时进度（graph:*/stage 事件流）")
async def run_events(run_id: str, request: Request,
                     ident: Identity = Depends(current_identity)) -> StreamingResponse:
    """连上即发一条 connected 快照，随后把该 run 的事件总线原样转发；见收尾事件即关流。

    图在工作线程里 publish → 主循环 call_soon_threadsafe 投递；慢/断客户端靠 keepalive +
    队列上限丢弃兜底，绝不反压主管线。轮询 GET /runs/{id} 仍并存（前端可只用其一）。
    V2MD_EVENTS=pg 档订阅会拉起 events 的 LISTEN 泵：别的副本驱动该 run 时，终态事件经
    跨进程扇出到达本端点——run_finalized 关流不要求 SSE 连接与驱动进程同侧。
    """
    _require_run_access(run_id, ident)
    q = events.subscribe(run_id)

    async def gen():
        _t0: float | None = time.monotonic()  # SLI 起点：connected→首个业务事件（None=已记）
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
                if _t0 is not None:  # SLI（P1-5）：connected→首个业务事件延迟，SLO p95<3s
                    observability.metric_sse_first_event(time.monotonic() - _t0)
                    _t0 = None
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

@app.get("/cost", response_model=CostOut,
          summary="本人本月成本归因（P2-2：usage/单价/预算余量；明细导出走 CLI cost）")
def get_cost(ident: Identity = Depends(current_identity)) -> dict:
    be = _tasks_backend()
    since = _month_start_iso()
    u = be.month_rollup(ident.uid, since) if be is not None else month_rollup(ident.uid, since)
    items = [{"run_id": r["run_id"], "title": _run_title(r), "status": r.get("status"),
              "created": r.get("created"), "deleted": bool(r.get("deleted")), **_run_accounting(r)}
             for r in _load_runs().values()
             if r.get("owner") == ident.uid and (r.get("created") or "") >= since]
    items.sort(key=lambda r: r.get("created") or "", reverse=True)
    policy = ENV.get("V2MD_GPU_PRICE_POLICY", "metered")
    return {"uid": ident.uid, "month": since, "spend_usd": u["spend_usd"],
            "budget_usd": BUDGET_MONTH_USD, "usage": u,
            "gpu_policy": policy, "runs": items,
            "pricing_configured": PRICE_LLM_IN_MTOK > 0 and PRICE_LLM_OUT_MTOK > 0
                and (PRICE_GPU_MIN > 0 or (PRICE_GPU_MIN == 0 and policy == "free")),
            "prices": {"llm_in_mtok": PRICE_LLM_IN_MTOK, "llm_out_mtok": PRICE_LLM_OUT_MTOK,
                       "gpu_min": PRICE_GPU_MIN,
                       "llm_cached_in_mtok": float(ENV.get("V2MD_PRICE_LLM_CACHED_IN_MTOK") or 0)}}


# ---------------------------------------------------------------- 会员/配额展示档（web 个人页）

_MEMBERSHIP_STORE = None
_membership_resolved = False


def _membership_row(uid: str) -> dict | None:
    """v2md_membership 单行读（惰性分派仿 _tasks_backend）：PG 关（无 DSN）→ None，
    端点据此回 free/全 0 默认视图；运行期 PG 异常由既有 503 处理器兜住。"""
    global _MEMBERSHIP_STORE, _membership_resolved
    if not _membership_resolved:
        _membership_resolved = True
        from . import pg, pgstore
        if pg.pg_enabled():
            _MEMBERSHIP_STORE = pgstore.PostgresMembershipStore(pg.shared_pool())
    if _MEMBERSHIP_STORE is None:
        return None
    return _MEMBERSHIP_STORE.get(uid)


@app.get("/me", response_model=MeOut,
          summary="本人资料卡（web 个人页：plan/昵称/头像色；无 membership 行回 free 默认档）")
def get_me(ident: Identity = Depends(current_identity)) -> dict:
    row = _membership_row(ident.uid)
    display = (row or {}).get("display_name") or ""
    name = (ident.name or "").strip() or display or ident.uid
    return {"uid": ident.uid,
            "name": name,
            "display_name": display or name,
            "avatar_hue": int((row or {}).get("avatar_hue") or 258),
            "plan": (row or {}).get("plan") or "free",
            "id_label": f"ID: {ident.uid.removeprefix('u_')}"}


@app.get("/quota", response_model=QuotaOut,
          summary="本人会员配额与统计（展示档，来自 v2md_membership；不参与提交强制）")
def get_quota(ident: Identity = Depends(current_identity)) -> dict:
    row = _membership_row(ident.uid) or {}
    # 已生成教材数取 runs 真相源（与教材库同口径：本人、未删、done/partial），
    # 不读 membership.stats_books——该列无写入方，恒为 0。
    books = sum(1 for r in _load_runs().values()
                if (r.get("owner", "") or "") == ident.uid and not r.get("deleted")
                and r.get("status") in ("done", "partial")
                and any(a.get('note') for a in (r.get('artifacts') or {}).values()))
    return {"ai_hours_used": float(row.get("ai_hours_used") or 0),
            "ai_hours_total": float(row.get("ai_hours_total") or 0),
            "storage_gb_used": float(row.get("storage_gb_used") or 0),
            "storage_gb_total": float(row.get("storage_gb_total") or 0),
            "stats": {"books": books,
                      "knowledge_points": int(row.get("stats_knowledge_points") or 0),
                      "accuracy": float(row.get("stats_accuracy") or 0)}}


@app.get("/queue", response_model=QueueOut)
def get_queue() -> dict:
    """gpu_router 的 colab 看门狗回读（queue_url 指这里）——判队列保温；
    limits 顺带公开并发/队列档位（探针口径，无敏感信息）。
    PG 档读全盘真相源（多副本合计），查询异常回退本进程视图——探针不许炸。"""
    be = _tasks_backend()
    if be is not None:
        try:
            c = be.counts()
            return {"queue_len": c["queued"], "running": c["running"],
                    "limits": {"max_concurrent": MAX_CONCURRENT_RUNS, "queue_max": QUEUE_MAX,
                               "user_queue_max": USER_QUEUE_MAX}}
        except Exception:
            pass
    return {"queue_len": sum(1 for v in _TASKS.values() if v.get("status") == "queued"),
            "running": sum(1 for v in _TASKS.values() if v.get("status") == "running"),
            "limits": {"max_concurrent": MAX_CONCURRENT_RUNS, "queue_max": QUEUE_MAX,
                       "user_queue_max": USER_QUEUE_MAX}}



@app.get("/healthz", response_model=HealthOut)
def healthz() -> dict:
    return {"ok": True, "ts": _now(), "version": VERSION}



@app.get("/preflight", response_model=PreflightOut, summary="开跑前体检（匿名面脱敏，P0-3：detail 仅回环/真实认证可见）")
def preflight(request: Request, ident: Identity | None = Depends(optional_identity)) -> dict:
    full = _preflight()
    host = (request.client.host if request.client else "") or ""
    real_auth = (ENV.get("V2MD_AUTH") or "off").strip().lower() not in ("", "off", "none")
    return full if _preflight_privileged(host, real_auth and ident is not None) \
        else _preflight_public(full)


# The production image also contains the Vite bundle. Keep the API at the root
# paths for backwards compatibility, and let the final catch-all serve Vue
# history routes such as / and /read/:id. If the bundle is not present
# (tests and API-only development images), these handlers remain ordinary 404s.
_WEB_DIST = Path(ENV.get("V2MD_WEB_DIST") or "/app/web-dist").resolve()
_API_PATH_PREFIXES = (
    "auth/", "runs", "uploads", "settings/", "prefs", "healthz", "preflight",
    "queue", "me", "quota", "cost", "docs", "openapi", "redoc",
)


def _web_file(path: str) -> FileResponse:
    candidate = (_WEB_DIST / path).resolve()
    if _WEB_DIST not in candidate.parents and candidate != _WEB_DIST:
        raise HTTPException(status_code=404, detail="Not Found")
    if candidate.is_file():
        return FileResponse(candidate)
    if (index := _WEB_DIST / "index.html").is_file():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Not Found")


@app.get("/", include_in_schema=False)
def web_root() -> FileResponse:
    return _web_file("index.html")


@app.get("/{path:path}", include_in_schema=False)
def web_fallback(path: str) -> FileResponse:
    if path.startswith(_API_PATH_PREFIXES):
        raise HTTPException(status_code=404, detail="Not Found")
    return _web_file(path)

def configure_subprocess_path() -> str:
    """Put this interpreter's scripts first so packaged CLIs remain executable."""
    os.environ["PATH"] = os.pathsep.join([
        str(Path(sys.executable).parent),
        "/opt/homebrew/bin", "/usr/local/bin",
        "/Library/Frameworks/Python.framework/Versions/3.14/bin",
        str(Path.home() / ".local/bin"),
        os.environ.get("PATH", ""),
    ])
    return os.environ["PATH"]


def cmd_api(port: int) -> None:
    # launchd/容器环境 PATH 极简；镜像 CLI 安装在当前虚拟环境的 bin 目录。
    configure_subprocess_path()
    host = ENV.get("V2MD_HOST", "127.0.0.1")  # 容器内设 0.0.0.0；无认证，别暴露公网
    print(f"🎬 video2md LangGraph 服务 → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")

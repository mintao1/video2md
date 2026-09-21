"""本地作业服务：HTTP 端点（/runs /stage /preflight…）+ job 编排。

GPU 算力（含 Colab 会话管理与看门狗）已抽离到 ~/Desktop/开发项目/gpu_router 算力池，
本服务只经 HTTP 契约（V2MD_*_REMOTE=<池子 URL>）消费算力，不管任何平台细节。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

import httpx

from .config import ENV, LOG_DIR, OUT_DIR, PROJ
from .util import redact
from .resolve import ensure_scheme
from .runs import STAGES, _load_runs, _run_outputs, _runs_file_lock, _runs_merge, _save_runs, create_run



# ---------------------------------------------------------------- ask（dify_kit AppClient.chat）


# ---------------------------------------------------------------- serve（Dify UI 入口的本地作业服务）
# Dify Chatflow 的 HTTP 节点经 host.docker.internal:8790 调用：
#   POST /jobs        {"url": "https://..."}  → {"job_id"}（入队，串行 worker 一次跑一个）
#   GET  /jobs/{id}   作业详情（状态 / 日志尾 / 产物）
#   GET  /jobs/latest 最近一个作业；GET /jobs 最近列表
_JOBS: dict[str, dict] = {}


_JOBS_LOCK = threading.RLock()  # 可重入：_post_run_stage 持锁调 _stage_dep_block 会再进锁


_JOB_QUEUE: list[str] = []


_JOB_TAIL = 40  # 每个作业保留的日志尾行数


# 子进程输出标记 → 阶段名（工作流轮询节点据此显示「现在在干什么」）




def _progress_line(job: dict) -> str:
    """一句话进度：阶段 · 条目 N/M · 已用时长 —— 工作流轮询节点直接展示这个。"""
    total = job.get("total") or 1
    done = (job.get("completed") or 0) + (job.get("failed") or 0)
    cur = (job.get("current") or {}).get("index") or min(done + 1, total)
    elapsed = ""
    if job.get("created"):
        try:
            dt = datetime.now() - datetime.fromisoformat(job["created"])
            elapsed = f" · 已用 {int(dt.total_seconds())}s"
        except (ValueError, TypeError):
            pass
    part = f" · 条目 {cur}/{total}" if total > 1 else ""
    name = job.get("stage") or "排队中"
    if job.get("run_id") and job.get("stage") and not name.startswith("阶段"):
        name = f"阶段 {name}"  # 阶段 job 展示带前缀；job["stage"] 保持原始名（去重/子进程按原始名匹配）
    return f"{name}{part}{elapsed}"


def _raw_stage(job: dict) -> str:
    """job 的原始阶段名：新 job 存的就是原始名；历史 job 存过 '阶段 xx' 展示名，剥前缀。"""
    s = job.get("stage") or ""
    return s[3:] if s.startswith("阶段 ") else s



# ---------------------------------------------------------------- 自适应轮询窗
# 预算 = 阶段基础开销 + Σ条目估算（download 后时长已知，按实长折算；未知按 180s 默认）。
# wait_hint = budget/80（clamp 5..60，/wait 端点上限 60s）→ Dify loop_count 100 轮硬限
# 覆盖 ≈1.25×预算，恒小于 over_budget 熔断线（预算×grace）——超预算一定先于轮次耗尽触发，
# 轮询窗跟着「条目数×时长」走，不再用固定间隔×100 轮的统一窗口。
_STAGE_BASE_S = {"download": 30, "transcribe": 300, "frames": 20, "ocr": 15,
                 "select": 10, "clips": 300, "note": 30, "kb": 20}


def _entry_est(stage: str, dur: float) -> float:
    """单条目阶段耗时估算（秒）。池子 GPU 转录 ≈0.1×实时+上传调度；剪辑按实测合集节奏。"""
    if stage == "download":
        return min(90.0, 30 + 0.15 * dur)
    if stage == "transcribe":
        return 60 + 0.15 * dur
    if stage == "frames":
        return 15 + 0.08 * dur
    if stage == "ocr":
        return 45.0
    if stage == "select":
        return 20.0
    if stage == "clips":
        return 150.0
    if stage == "note":
        return 60.0
    if stage == "kb":
        return 45.0
    return 60.0


def _adaptive(job: dict) -> dict:
    """轮询快照的自适应字段：elapsed_s（执行钟，不含排队）/ budget_s / wait_hint / over_budget。

    预算只烧执行时间：status==queued 时 elapsed 恒 0——worker 串行，排队等前序 job
    不该消耗本 job 的预算（started 由 _job_run 在真正开跑时落）。
    """
    stage = _raw_stage(job)
    run = _load_runs().get(job.get("run_id") or "") or {}
    durs: list[float] = []
    for e in run.get("entries", []):
        art = (run.get("artifacts") or {}).get(e.get("url"), {}) or {}
        if art.get("skipped"):
            continue  # 幂等跳过的条目秒过，不占预算
        durs.append(float(art.get("duration") or 0.0))
    if not durs:
        durs = [0.0]
    budget = _STAGE_BASE_S.get(stage, 30) + sum(
        _entry_est(stage, d if d > 0 else 180.0) for d in durs)
    grace = float(ENV.get("V2MD_ADAPTIVE_GRACE", "1.5"))
    started = job.get("started") or ""
    elapsed = 0.0
    if job.get("status") == "running" and started:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
        except (ValueError, TypeError):
            elapsed = 0.0
    over = bool(job.get("status") == "running" and started and elapsed > budget * grace)
    if job.get("status") == "queued":
        hint = 8
    elif job.get("status") == "running":
        hint = max(5, min(60, round(budget / 80)))
    else:
        hint = 3
    return {"elapsed_s": int(elapsed), "budget_s": int(budget),
            "wait_hint": hint, "over_budget": over}


def _job_snapshot(job: dict) -> dict:
    """阶段 job 的轮询快照（POST 阶段端点/GET /jobs/{id} 共用）：工作流 code_progress 直接解析。"""
    return {"job_id": job.get("id"), "run_id": job.get("run_id"), "stage": job.get("stage"),
            "status": job.get("status", "none"), "progress": job.get("progress") or "",
            "note": job.get("note", ""), "error": job.get("error", ""),
            "selfcheck": job.get("selfcheck", ""), "partial_fail": job.get("partial_fail", ""),
            **_adaptive(job)}



_JOBS_FILE = PROJ / "jobs.json"  # 队列落盘：服务重启不丢排队任务



def _save_jobs() -> None:
    """把作业与队列持久化（服务重启/崩溃后可见、可恢复）。

    快照+写 tmp+rename 整段持锁：两个线程并发写同一 .tmp 会让 rename 扑空
    （FileNotFoundError），丢一次落盘。
    """
    with _JOBS_LOCK:
        snap = {"jobs": {k: {kk: vv for kk, vv in v.items()} for k, v in _JOBS.items()},
                "queue": list(_JOB_QUEUE)}
        tmp = _JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
        tmp.replace(_JOBS_FILE)



def _load_jobs() -> None:
    """启动时恢复：排队中的任务重新排队；中断在跑的标记失败（不自动重跑，避免双份产物）。"""
    if not _JOBS_FILE.exists():
        return
    try:
        snap = json.loads(_JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    for jid, job in (snap.get("jobs") or {}).items():
        if job.get("status") in ("queued", "running"):
            job["status"] = "queued"
            job["created"] = datetime.now().isoformat(timespec="seconds")  # 计时从本次重启起
            _JOBS[jid] = job
            _JOB_QUEUE.append(jid)
        else:
            # 已完成/已失败的历史记录只读保留，不因服务重启改写状态
            _JOBS[jid] = job
    if _JOB_QUEUE:
        print(f"♻️ 恢复 {len(_JOB_QUEUE)} 个排队任务（上次服务退出前未跑完）")



def _job_run(job: dict) -> None:
    """跑一条阶段作业子进程，逐行收集 stdout 作为日志尾（Dify 轮询展示用）。

    作业只有一种：单阶段（run_id+stage，由 Dify workflow 全阶段编排驱动）。
    """
    job["status"] = "running"
    job["started"] = datetime.now().isoformat(timespec="seconds")  # 执行钟起点：自适应预算只烧执行时间，不含排队
    _save_jobs()
    raw_stage = job.get("stage", "")  # 存储名即原始阶段名；展示前缀由 _progress_line 加
    job["total"] = 1
    job["completed"] = 0
    job["failed"] = 0
    job["current"] = {"index": 1, "id": raw_stage, "url": ""}
    job["progress"] = _progress_line(job)
    _save_jobs()
    # 阶段子进程：python -m video2md __stage <run> <stage> <args-json>
    cmd = [sys.executable, "-u", "-m", "video2md", "__stage",
           job["run_id"], raw_stage, json.dumps(job.get("stage_args") or {})]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", cwd=PROJ)
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            with _JOBS_LOCK:
                job["log"] = (job["log"] + [redact(line)])[-_JOB_TAIL:]
                flat = line.strip()  # 子进程输出带缩进，判前缀前先去空白
                if flat.startswith("📝 "):
                    job["note"] = flat[2:].strip()
                if flat.startswith("SELF-CHECK"):
                    job["selfcheck"] = flat.split(" ", 1)[-1]
                if flat.startswith("PARTIAL-FAIL"):
                    job["partial_fail"] = flat.split(" ", 1)[-1].strip()
            job["progress"] = _progress_line(job)
        rc = p.wait()
        with _JOBS_LOCK:
            if rc == 0:
                job["completed"] = 1
            else:
                job["failed"] = 1
                job["error"] = job["log"][-1] if job["log"] else "阶段任务失败"
            job["status"] = "done" if rc == 0 else "error"
        _save_jobs()
    except Exception as exc:
        with _JOBS_LOCK:
            job["status"], job["error"] = "error", redact(str(exc))[:300]
        _save_jobs()



def _job_worker() -> None:
    while True:
        with _JOBS_LOCK:
            jid = _JOB_QUEUE.pop(0) if _JOB_QUEUE else None
        if jid is None:
            time.sleep(0.5)
            continue
        job = _JOBS.get(jid)
        if job:
            _job_run(job)



_REQ_LOG = LOG_DIR / "serve-requests.jsonl"  # 入队/查询请求审计（证明 Dify 侧是否真的打到服务）



def _req_log(method: str, path: str, code: int, **fields) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "method": method,
           "path": path, "code": code, **fields}
    line = json.dumps(rec, ensure_ascii=False)
    print(f"[req] {method} {path} → {code} " + " ".join(f"{k}={str(v)[:60]}" for k, v in fields.items()))
    try:
        with _REQ_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass



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
    add("vault", os.access(OUT_DIR, os.W_OK), str(OUT_DIR))
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
    """一次快照：队列/作业/算力池/临时目录残留，用于事后对账与排障。"""
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j.get("created", ""), reverse=True)
        counts: dict[str, int] = {}
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        snap = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "queue_len": len(_JOB_QUEUE),
            "counts": counts,
            "recent": [{k: j.get(k) for k in ("id", "status", "url", "total", "completed", "failed", "note")}
                       for j in jobs[:5]],
            "jobs_file_age_s": round(time.time() - _JOBS_FILE.stat().st_mtime) if _JOBS_FILE.exists() else None,
            "tmp_leftovers": len(list(Path(tempfile.gettempdir()).glob("video2md-*"))),
        }
    # GPU 算力池快照（配了 URL 型 V2MD_REMOTE 才有；2s 超时尽力而为，不拖慢对账）
    remote = (ENV.get("V2MD_REMOTE") or "").strip()
    if remote.startswith(("http://", "https://")):
        try:
            snap["gpu_pool"] = httpx.get(f"{remote.rstrip('/')}/status", timeout=2).json()
        except Exception as exc:
            snap["gpu_pool"] = {"ok": False, "error": str(exc)[:100]}
    return snap



# 阶段硬依赖：前置阶段没 done 就拒绝入队（409 PRECONDITION）。
# 事故链：transcribe 还在 running（实际在空转失败），工作流 loop 上限耗尽就前进到 frames——
# 工作流侧修 loop 是前台，这里拒造无前置的 job 是底线。{"force": true} 显式绕过。
# clips 依赖 select 的 published 帧清单（不是只到 download）；kb 只要求 note 已启动
# （worker 串行 = 入队序即执行序，note 各条目排在 kb 前自然先跑完），要求全 done 反而会
# 409 掉「note 循环发完即入 kb」的正常编排。
_STAGE_DEPS: dict[str, tuple[str, ...]] = {
    "transcribe": ("download",),
    "frames": ("download",),
    "ocr": ("frames",),
    "select": ("ocr", "transcribe"),
    "clips": ("select",),
    "note": ("select",),
}



def _stage_dep_block(run_id: str, stage: str) -> str:
    """返回阻止入队的原因；可入队返回空串。"""
    with _JOBS_LOCK:
        jobs = [j for j in _JOBS.values() if j.get("run_id") == run_id]
        if stage == "kb":
            if not any(_raw_stage(j) == "note" for j in jobs):
                # 全跳过 run（state 幂等全命中）：note 循环对 skipped 条目快进、永不 POST /note，
                # 不存在 note job 是正常态而非编排错误——放行（kb 阶段对 skipped 条目同样秒过）
                run = _load_runs().get(run_id) or {}
                arts = run.get("artifacts") or {}
                if run.get("entries") and all((arts.get(e["url"]) or {}).get("skipped")
                                              for e in run["entries"]):
                    return ""
                return "前置阶段 note 尚未入队；等 note 循环发起再入队，或显式 {\"force\": true} 绕过"
            return ""
        for dep in _STAGE_DEPS.get(stage, ()):
            dep_jobs = [j for j in jobs if _raw_stage(j) == dep]
            if not any(j.get("status") == "done" for j in dep_jobs):
                cur = dep_jobs[-1].get("status") if dep_jobs else "未入队"
                return (f"前置阶段 {dep} 未完成（当前 {cur}）；等它 done 再入队，"
                        f"或显式 {{\"force\": true}} 绕过")
    return ""



class _JobHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端提前断开（轮询超时等）不该刷栈

    def do_POST(self) -> None:
        if self.path == "/jobs":
            self._post_jobs()
            return
        if self.path == "/runs":
            self._post_runs()
            return
        m = re.fullmatch(r"/runs/([^/]+)/(download|transcribe|frames|ocr|select|clips|note|kb)", self.path)
        if m:
            self._post_run_stage(m.group(1), m.group(2))
            return
        _req_log("POST", self.path, 404, remote=self.client_address[0])
        return self._send(404, {"error": "not found"})

    def _read_json(self) -> dict:
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _post_jobs(self) -> None:
        """URL 入队已废弃：本地 CLI 直跑链路已删，管线由 Dify workflow「视频转文档」驱动。
        （工作流内部走 POST /runs + /runs/{id}/{stage}；本端点只回迁移指引。）"""
        _req_log("POST", self.path, 410, remote=self.client_address[0], error="gone")
        self._send(410, {"error": "POST /jobs 已废弃：请通过 Dify workflow「视频转文档」发起处理"})


    def _post_runs(self) -> None:
        """建 run：resolve 展开条目；同链接同分区的进行中 run 复用（去重）。

        dedup 必须带上 part_items：同一合集的分区间补跑（2-5 / 6-10 / …）共享 URL，
        只按 URL dedup 会把新的分区错误地并进旧 run，导致那几集永远不被处理。
        """
        data = self._read_json()
        url = ensure_scheme(str(data.get("url", "")).strip())
        part_items = str(data.get("part_items", "")).strip()
        force = bool(data.get("force"))
        dify_run_id = (self.headers.get("X-Dify-Workflow-Run-Id") or "").strip()
        if not url:
            _req_log("POST", self.path, 400, remote=self.client_address[0], error="缺 url")
            return self._send(400, {"error": "缺 url"})
        with _JOBS_LOCK:
            dup = next((r for r in _load_runs().values()
                        if r.get("url") == url and str(r.get("part_items", "")).strip() == part_items
                        and r.get("status") in ("created", "running")), None)
            if dup:
                _req_log("POST", self.path, 200, remote=self.client_address[0], url=url[:80],
                         run_id=dup["run_id"], dedup=True)
                return self._send(200, {"run_id": dup["run_id"], "total": dup["total"], "dedup": True})
        try:
            run = create_run(url, part_items, force=force, dify_run_id=dify_run_id)
        except RuntimeError as exc:
            _req_log("POST", self.path, 400, remote=self.client_address[0], url=url[:80], error=str(exc)[:120])
            return self._send(400, {"error": str(exc)[:300]})
        with _runs_file_lock():
            _runs_merge(run)
        skipped = [bool(a.get("skipped")) for a in run["artifacts"].values()]
        _req_log("POST", self.path, 200, remote=self.client_address[0], url=url[:80],
                 run_id=run["run_id"], total=run["total"])
        self._send(200, {
            "run_id": run["run_id"], "total": run["total"],
            "entries": [{"index": i, "url": e["url"], "skipped": bool(run["artifacts"][e["url"]].get("skipped"))}
                        for i, e in enumerate(run["entries"], 1)],
            "all_skipped": bool(skipped) and all(skipped),
        })

    def _post_run_stage(self, run_id: str, stage: str) -> None:
        """阶段触发：入队一个阶段 job（worker 子进程跑 __stage）。

        幂等去重：同一 run 的同一阶段已有 queued/running job 时复用并回带其当前状态
        （工作流 loop 每轮重 POST 该端点轮询，直到 status 进入 done/error）。
        响应即 code_progress 节点要解析的完整快照：{job_id, status, progress, note, error}。
        """
        run = _load_runs().get(run_id)
        if not run:
            return self._send(404, {"error": f"run {run_id} 不存在"})
        body = self._read_json()
        want_entry = str(body.get("entry", "")).strip()
        with _JOBS_LOCK:
            # 幂等去重：同一 run + 同阶段 + 同条目 复用现有 job。
            # 逐条目循环（note 阶段）里 entry 是 dedup 的一部分，否则第 2..N 条会被误并到第 1 条的 job。
            # 多条匹配取**最新**（created 最大）：error 旧档 + 人工 retry 新档并存时，
            # 工作流无 retry 的轮询必须落在「活」的那条上，否则恢复链路被旧 error 卡死
            dups = [j for j in _JOBS.values()
                    if j.get("run_id") == run_id and _raw_stage(j) == stage
                    and str((j.get("stage_args") or {}).get("entry", "")).strip() == want_entry]
            dup = max(dups, key=lambda j: j.get("created", ""), default=None)
            if dup and dup["status"] in ("queued", "running"):
                # 轮询复用现有 job，不新建
                _req_log("POST", self.path, 200, remote=self.client_address[0], run_id=run_id,
                         stage=stage, job_id=dup["id"], dedup=True)
                return self._send(200, {**_job_snapshot(dup), "dedup": True})
            if dup and dup["status"] == "done" and not body.get("retry"):
                # 幂等回显；显式 {"retry": true} 可重跑 done 阶段（补部分失败条目——
                # 条目产物幂等，已完成条目跳过，只补没产物的）
                _req_log("POST", self.path, 200, remote=self.client_address[0], run_id=run_id,
                         stage=stage, job_id=dup["id"], dedup=True)
                return self._send(200, {**_job_snapshot(dup), "dedup": True})
            if dup and dup["status"] == "error":
                # 失败回带快照（工作流 loop 看到 error 即停）；显式 {"retry": true} 才重跑
                if not body.get("retry"):
                    _req_log("POST", self.path, 200, remote=self.client_address[0], run_id=run_id,
                             stage=stage, job_id=dup["id"], dedup=True)
                    return self._send(200, _job_snapshot(dup))
            block = "" if body.get("force") else _stage_dep_block(run_id, stage)
            if block:
                _req_log("POST", self.path, 409, remote=self.client_address[0], run_id=run_id,
                         stage=stage, error=block[:80])
                return self._send(409, {"error": block, "code": "PRECONDITION",
                                        "run_id": run_id, "stage": stage})
            jid = uuid.uuid4().hex[:8]
            job = {"id": jid, "run_id": run_id, "stage": stage, "stage_args": body,
                   "status": "queued", "log": [], "progress": f"阶段 {stage} 排队中",
                   "created": datetime.now().isoformat(timespec="seconds"), "total": 1}
            _JOBS[jid] = job
            _JOB_QUEUE.append(jid)
        _save_jobs()
        _req_log("POST", self.path, 200, remote=self.client_address[0], run_id=run_id,
                 stage=stage, job_id=jid)
        self._send(200, _job_snapshot(job))

    def do_GET(self) -> None:
        if self.path.startswith("/wait"):
            # 给 Dify 循环节点做延时：sleep 放在服务端（sandbox code 节点里 sleep 会撑爆 worker）
            try:
                n = int((self.path.split("seconds=")[-1].split("&")[0] or "10")[:3])
            except (ValueError, IndexError):
                n = 10
            n = max(1, min(n, 60))
            time.sleep(n)
            return self._send(200, {"slept": n})
        if self.path == "/preflight":
            # 开跑前体检（ffmpeg/磁盘/vault/算力池可达性），工作流第一个节点调它，不过即终止
            snap = _preflight()
            _req_log("GET", self.path, 200, remote=self.client_address[0], ok=snap["ok"])
            return self._send(200, snap)
        if self.path == "/checkpoint":
            snap = _checkpoint()
            _req_log("GET", self.path, 200, remote=self.client_address[0],
                     queue_len=snap["queue_len"])
            return self._send(200, snap)
        if self.path == "/queue":
            # 真实队列计数——GPU 算力池的 colab provider 据此判队列保温（queue_url 指这里）；
            # GET /jobs 只有最近 10 条
            with _JOBS_LOCK:
                return self._send(200, {"queue_len": len(_JOB_QUEUE),
                                        "running": sum(1 for j in _JOBS.values()
                                                       if j.get("status") == "running")})
        if self.path == "/jobs/latest":
            with _JOBS_LOCK:
                jobs = sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)
            return self._send(200, jobs[0] if jobs else {"status": "none"})
        if self.path == "/jobs":
            with _JOBS_LOCK:
                jobs = sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)[:10]
            return self._send(200, {"jobs": [{k: j.get(k) for k in ("id", "url", "status", "created", "note",
                                                                   "total", "completed", "failed", "current",
                                                                   "stage", "progress", "run_id", "stage")}
                                             for j in jobs]})
        m = re.fullmatch(r"/runs/([^/]+)/stages/(transcript|candidates|selected)",
                         self.path.partition("?")[0])
        if m:
            run_id, which = m.group(1), m.group(2)
            run = _load_runs().get(run_id)
            if not run:
                return self._send(404, {"error": f"run {run_id} 不存在"})
            # 产物门控：产出阶段没 done 就 409——阶段半路产出的空清单喂给 LLM 只会出垃圾选帧/正文
            need = {"transcript": "transcribe", "candidates": "ocr", "selected": "select"}[which]
            with _JOBS_LOCK:
                need_done = any(j.get("run_id") == run_id and _raw_stage(j) == need
                                and j.get("status") == "done" for j in _JOBS.values())
            if not need_done:
                _req_log("GET", self.path, 409, remote=self.client_address[0],
                         run_id=run_id, stage=need, error="产物未就绪")
                return self._send(409, {"error": f"阶段 {need} 未 done，{which} 清单还不可用",
                                        "code": "PRECONDITION"})
            qs = self.path.partition("?")[2]
            entry = next((v for k, _, v in (kv.partition("=") for kv in qs.split("&")) if k == "entry"),
                         None)
            return self._send(200, _run_outputs(run, which, entry or None))
        m = re.fullmatch(r"/runs/([^/]+)", self.path)
        if m:
            run_id = m.group(1)
            run = _load_runs().get(run_id)
            if not run:
                return self._send(404, {"error": f"run {run_id} 不存在"})
            entries = []
            for e in run["entries"]:
                art = run["artifacts"].get(e["url"], {})
                entries.append({
                    "index": e.get("playlist_index"),
                    "url": e["url"],
                    "skipped": bool(art.get("skipped")),
                    "video": Path(art["video"]).name if art.get("video") else None,
                    "done": {s: bool(art.get({"transcribe": "segs", "frames": "candidates",
                                              "ocr": "ocr_raw", "select": "published",
                                              "clips": "clips_done", "note": "note",
                                              "kb": "doc_id", "download": "video"}.get(s)))
                             for s in STAGES},
                })
            return self._send(200, {
                "run_id": run["run_id"], "url": run["url"], "status": run.get("status"),
                "total": run["total"], "stages": run["stages"], "entries": entries,
            })
        job = _JOBS.get(self.path.removeprefix("/jobs/"))
        if not job:
            return self._send(404, {"error": "job 不存在"})
        self._send(200, job)

    def log_message(self, *_a) -> None:  # 静默访问日志；作业轨迹在 logs/run-*.jsonl
        pass



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



def cmd_serve(port: int) -> None:
    # launchd 环境 PATH 极简，补齐框架/homebrew 路径让子进程找得到 yt-dlp/whisper/ffmpeg
    os.environ["PATH"] = ":".join([
        "/opt/homebrew/bin", "/usr/local/bin",
        "/Library/Frameworks/Python.framework/Versions/3.14/bin",
        str(Path.home() / ".local/bin"),
    ]) + ":" + os.environ.get("PATH", "")
    _load_jobs()  # 服务重启后把排队任务捡回来
    _cleanup_stale_runs()
    _prune_orphan_tmps()  # 无人认领的 tmp 目录（run 记录丢失/SIGKILL 残留）也一并清
    threading.Thread(target=_job_worker, daemon=True).start()
    # Colab 会话闲置看门狗已随算力池抽离（gpu_router 进程内 autostop_watchdog）
    print(f"🎬 video2md 作业服务 → http://127.0.0.1:{port}（Dify 经 host.docker.internal:{port} 调用）")
    print("📋 checkpoint: " + json.dumps(_checkpoint(), ensure_ascii=False))
    ThreadingHTTPServer(("127.0.0.1", port), _JobHandler).serve_forever()

"""google-colab-cli 会话机械（自包含副本）：建/复用会话、上传、远端后台执行、轮询产物、
token 体检、lease 心跳、队列感知保温。

本文件是 Colab 会话机械的**唯一 SSOT**（2026-09-21 抽离完成：video2md 侧的
同名实现已删除，算力全收口到本池子）。
与 video2md 的协同约定（可经 provider extra 配置；不配则用完即关）：
同名会话（默认 v2md-whisper）；lease 文件心跳（watchdog 三信号之一）；
queue_url 指 video2md serve 的 /queue（队列有货就保温）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import httpx



# ---------------------------------------------------------------- 参数（provider extra 可覆盖）
SESSION = "v2md-whisper"

LEASE_FILE = ""     # 默认由 provider 注入；空 = 仅用进程内临时 lease（看门狗协同关闭）

QUEUE_URL = ""      # video2md serve /queue；空 = 不感知队列（用完即关）

MAX_WAIT_S = 2700

AUTOSTOP_IDLE_S = 60   # 队列+在途连续空闲这么久才关会话（接管原 serve 的看门狗职责）



# ---------------------------------------------------------------- 远端三件套（launcher 秒回 / worker 真跑 / args）
# 为什么异步：长音频上 `colab exec` 的回复等待会先于任务完成而超时（社区已知坑）；
# 为什么不用 __file__：Colab exec 在内核命名空间执行，__file__ 未定义（实测 NameError）。
_COLAB_WORKER = '''
import json
import os

args = json.load(open("/content/v2md_args.json"))
try:  # import 也放进 try：依赖缺失/显存不足等启动期异常必须写进 status，否则本地空等
    from faster_whisper import WhisperModel

    try:
        import torch  # 设备自适应：GPU 会话被配额打满时降级 CPU（int8）
        _dev, _ct = ("cuda", "float16") if torch.cuda.is_available() else ("cpu", "int8")
    except Exception:
        _dev, _ct = "cpu", "int8"
    _model = args.get("model") or "turbo"
    if _dev == "cpu" and _model in ("turbo", "large", "large-v3", "large-v3-turbo"):
        _model = "small"  # CPU 上 large 级模型等于不跑：降级 small 保可用性
    m = WhisperModel(_model, device=_dev, compute_type=_ct)
    kw = dict(language=None if args.get("language") == "auto" else args.get("language"),
              condition_on_previous_text=False, vad_filter=True,
              initial_prompt="\\u4ee5\\u4e0b\\u662f\\u666e\\u901a\\u8bdd\\u7684\\u7b80\\u4f53\\u4e2d\\u6587\\u5185\\u5bb9\\u3002"
                  if (args.get("language") or "").startswith("zh") else None)
    try:
        # 批处理推理：同一张卡上并行跑多个 30s 窗口，吞吐约 2–4×；版本不支持则回退
        from faster_whisper import BatchedInferencePipeline

        segs_iter, info = BatchedInferencePipeline(model=m).transcribe(
            "/content/in.m4a", batch_size=16, **kw)
    except Exception:
        segs_iter, info = m.transcribe("/content/in.m4a", **kw)
    segments = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                 "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio,
                 "avg_logprob": s.avg_logprob} for s in segs_iter]
    with open("/content/out.json.tmp", "w") as fh:
        json.dump({"model": args.get("model") or "turbo", "language": info.language,
                   "duration": info.duration, "segments": segments}, fh)
    os.replace("/content/out.json.tmp", "/content/out.json")
    json.dump({"state": "done", "segments": len(segments)}, open("/content/status.json", "w"))
except Exception as exc:
    json.dump({"state": "error", "error": str(exc)[:500]}, open("/content/status.json", "w"))
'''


_COLAB_JOB = '''
import json
import subprocess
import sys

json.dump({"state": "running"}, open("/content/status.json", "w"))
with open("/content/worker.log", "w") as log:
    p = subprocess.Popen([sys.executable, "/content/v2md_worker.py"],
                         stdout=log, stderr=subprocess.STDOUT)
print("STARTED", p.pid)
'''


# OCR worker：rapidocr（ONNX，免 paddle 重型依赖）。产物文件名与 ASR worker 一致，
# 直接复用同一套轮询。
_COLAB_OCR_WORKER = r'''
import json
import os
import tarfile

try:
    from rapidocr import RapidOCR

    with tarfile.open("/content/frames.tar") as tf:
        tf.extractall("/content/frames_in")
    ocr = RapidOCR()
    results = []
    for name in sorted(os.listdir("/content/frames_in")):
        p = os.path.join("/content/frames_in", name)
        try:
            out = ocr(p)
            # rapidocr 版本间返回形状不一：v3 是带 txts/scores 的对象，旧版是 (items, elapse)
            if hasattr(out, "txts"):
                txts, scores = list(out.txts or []), [float(s) for s in (out.scores or [])]
            else:
                items = (out[0] if isinstance(out, tuple) else out) or []
                txts = [str(it[1]) for it in items]
                scores = [float(it[2]) for it in items]
            results.append({"name": name, "text": "\n".join(txts),
                            "conf": (sum(scores) / len(scores) if scores else 0.0)})
        except Exception as exc:
            results.append({"name": name, "text": "", "conf": 0.0, "err": str(exc)[:200]})
    with open("/content/out.json.tmp", "w") as fh:
        json.dump({"results": results}, fh)
    os.replace("/content/out.json.tmp", "/content/out.json")
    json.dump({"state": "done", "n": len(results)}, open("/content/status.json", "w"))
except Exception as exc:
    json.dump({"state": "error", "error": str(exc)[:500]}, open("/content/status.json", "w"))
'''


# 剪辑 worker：johnvansickle 静态 ffmpeg + nvenc 实测探测 + 双线程批切。
_COLAB_CLIP_JOB = '''
import json
import subprocess
import sys

json.dump({"state": "starting"}, open("/content/clip_status.json", "w"))
with open("/content/clip_worker.log", "w") as log:
    p = subprocess.Popen([sys.executable, "/content/clip_worker.py"],
                         stdout=log, stderr=subprocess.STDOUT)
print("STARTED", p.pid)
'''


_COLAB_CLIP_WORKER = r'''
import concurrent.futures as cf
import glob
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request

ST = "/content/clip_status.json"


def status(**kw):
    json.dump(kw, open(ST, "w"))


try:
    job = json.load(open("/content/clip_job.json"))
    clips = job["clips"]
    status(state="running", done=0, total=len(clips), note="prep")

    # 源片：优先用本地已上传的；没有且给了 direct_url 就由 Colab 直连 CDN 下载（省调用方上行流量）
    src = "/content/clip-src.mp4"
    durl = job.get("direct_url") or ""
    if not os.path.exists(src) and durl:
        status(state="running", done=0, total=len(clips), note="cdn-download")
        req = urllib.request.Request(durl, headers={"User-Agent": "Mozilla/5.0",
                                                    "Referer": "https://www.douyin.com/"})
        with urllib.request.urlopen(req, timeout=60) as r, open(src, "wb") as f:
            shutil.copyfileobj(r, f)
    if not os.path.exists(src) or os.path.getsize(src) == 0:
        raise RuntimeError("源视频缺失：未上传且 direct_url 下载失败/未提供")

    # johnvansickle 静态构建自带 nvenc；下载失败退回系统 ffmpeg（CPU）
    FF = (glob.glob("/content/ffmpeg-*-amd64-static/ffmpeg") or [None])[0]
    if not FF:
        try:
            urllib.request.urlretrieve(
                "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
                "/content/ff.tar.xz")
            subprocess.run(["tar", "-xf", "/content/ff.tar.xz", "-C", "/content/"],
                           check=True, capture_output=True)
            FF = glob.glob("/content/ffmpeg-*-amd64-static/ffmpeg")[0]
            os.chmod(FF, 0o755)
        except Exception:
            FF = "ffmpeg"

    # 编码器实测探测：列表里有 ≠ 能编码（参数集随构建漂移，实测 -cq 在部分构建上不存在）。
    def probe(enc):
        q = subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=black:s=320x240:d=1:r=25",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=1",
                            *enc, "-c:a", "aac", "/tmp/probe.mp4"],
                           capture_output=True, text=True)
        return q.returncode == 0 and os.path.exists("/tmp/probe.mp4")

    enc = None
    for cand in (["-c:v", "h264_nvenc", "-preset", "p4", "-qp", "26"],
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]):
        if probe(cand):
            enc = cand
            break
    if not enc:
        raise RuntimeError("远端无可用 H.264 编码器（nvenc/x264 探测全灭）")
    status(state="running", done=0, total=len(clips),
           note="encoder=" + ("nvenc" if "nvenc" in enc[1] else "x264"))

    os.makedirs("/content/clips_out", exist_ok=True)
    results, counter = {}, {"n": 0}

    def cut(c):
        dest = "/content/clips_out/" + c["out"]
        q = subprocess.run(
            [FF, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "%.2f" % c["s0"], "-t", "%.2f" % (c["s1"] - c["s0"]),
             "-i", "/content/clip-src.mp4",
             *enc, "-c:a", "aac", "-movflags", "+faststart", dest],
            capture_output=True, text=True)
        ok = q.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0
        if not ok:
            try:
                os.remove(dest)
            except OSError:
                pass
        return c["out"], ok

    with cf.ThreadPoolExecutor(max_workers=2) as pool:  # 2 vCPU 恰好打满
        for out, ok in pool.map(cut, clips):
            results[out] = ok
            counter["n"] += 1
            if counter["n"] % 5 == 0:
                status(state="running", done=counter["n"], total=len(clips))

    with tarfile.open("/content/clips.tar", "w") as tf:
        for out, ok in results.items():
            if ok:
                tf.add("/content/clips_out/" + out, arcname=out)
    json.dump(results, open("/content/clips_result.json", "w"))
    status(state="done", done=len(clips), total=len(clips),
           ok=sum(1 for v in results.values() if v))
except Exception as exc:
    status(state="error", error=str(exc)[:500])
'''



# ---------------------------------------------------------------- CLI 基础
def _colab_bin() -> str:
    """定位 colab CLI（uv tool 默认装到 ~/.local/bin，服务进程 PATH 里常没有）。"""
    return shutil.which("colab") or next(
        (str(p) for p in (Path.home() / ".local/bin/colab",
                          Path("/opt/homebrew/bin/colab"),
                          Path("/usr/local/bin/colab")) if p.exists()), "colab")



def _colab(args: list[str], timeout: int = 600, retries: int = 1, stdin: str | None = None) -> str:
    """官方 google-colab-cli 子进程。按已知坑兜底：
    exec --timeout 只管远端内核不管本地进程 → 用 subprocess timeout 双保险；
    代理 token 约 60min 过期且被误报为会话丢失 → 失败重试一次。"""
    for attempt in range(retries + 1):
        try:
            p = subprocess.run([_colab_bin(), *args], capture_output=True, text=True,
                               timeout=timeout, input=stdin)
        except subprocess.TimeoutExpired:
            if attempt < retries:
                continue
            raise RuntimeError(f"colab {' '.join(args[:2])} 超时（>{timeout}s）")
        if p.returncode == 0:
            return (p.stdout or "") + (p.stderr or "")
        if attempt < retries:
            time.sleep(3)
            continue
        # 保留 3000 字符：CLI 的 Rich 面板会把核心错误（如 412 TooManyAssignments）
        # 裹进重复的长 traceback，截太短只剩边框，下游按错误码分类（配额/降级）会失明
        raise RuntimeError(f"colab {' '.join(args[:2])} 失败: {(p.stdout or '')[-1500:] + (p.stderr or '')[-1500:]}")
    return ""



def _ensure_colab_session(deps_pkg: str = "") -> None:
    """建/复用会话；deps_pkg 给了就做依赖自检（缺则装）。探测幂等只读、允许重试；
    标记串在源码里分段拼接，防 exec 回显造成假阳性。"""
    if f"[{SESSION}]" not in _colab(["sessions"], timeout=90, retries=2):
        print(f"  ⏳ 创建 Colab T4 会话 {SESSION}（免费档 GPU 以配额为准）…")
        try:
            _colab(["new", "-s", SESSION, "--gpu", "T4"], timeout=420)
        except RuntimeError as exc:
            # GPU 配额/供给耗尽的信号集：412 TooManyAssignments 或 503 Service Unavailable
            # （assign 端点两种都返回过；只在 --gpu 尝试失败时降级，CPU 会话自身失败不递归）
            msg = str(exc)
            if not any(s in msg for s in ("412", "TooManyAssignments", "503", "Service Unavailable")):
                raise
            print(f"  ⚠️ T4 不可用（GPU assign 被拒），降级创建 CPU 会话 {SESSION}"
                  f"（whisper 自动 int8/small，慢但能跑）…")
            _colab(["new", "-s", SESSION], timeout=420)
    if not deps_pkg:
        return
    probe = _colab(["exec", "-s", SESSION], timeout=300, retries=2,
                   stdin=f"import {deps_pkg}\nprint('DEPS' + '-OK')")
    if "DEPS-OK" not in probe:
        pkg = {"faster_whisper": "faster-whisper>=1.1"}.get(deps_pkg, deps_pkg)
        print(f"  ⏳ 会话内安装 {pkg}…")
        _colab(["install", "-s", SESSION, pkg], timeout=600)



def _colab_session_active(max_age: float = 10.0) -> bool:
    try:
        out = subprocess.run([_colab_bin(), "sessions"], capture_output=True, text=True, timeout=8)
        return f"[{SESSION}]" in (out.stdout + out.stderr)
    except Exception:
        return False



def _colab_stop_session(reason: str = "") -> bool:
    """关会话（幂等：不存在也返回 True）。任何异常都吞掉，不能因清理动作影响主流程。"""
    try:
        subprocess.run([_colab_bin(), "stop", "-s", SESSION],
                       capture_output=True, text=True, timeout=120)
        if reason:
            print(f"  🛑 关闭 Colab 会话（{reason}）")
        return True
    except Exception:
        return False



def _colab_token_check(margin_min: int = 15) -> tuple[bool, str]:
    """Colab OAuth token 体检：余量 ≥ margin 直接过；不足就当场刷新（走当前进程代理环境）。"""
    p = Path.home() / ".config/colab-cli/token.json"
    if not p.exists():
        return False, "token.json 不存在：先交互式跑一次 colab 完成授权"
    try:
        d = json.loads(p.read_text())
    except Exception as exc:
        return False, f"token.json 读不动：{exc}"
    try:
        exp = datetime.strptime(str(d.get("expiry") or ""), "%Y-%m-%dT%H:%M:%S.%fZ")
        left = (exp - datetime.utcnow()).total_seconds() / 60
    except ValueError:
        left = -1
    if left >= margin_min:
        return True, f"有效（余 {left:.0f}min）"
    if not d.get("refresh_token"):
        return False, "无 refresh_token：先交互式跑一次 colab 完成授权"
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode({
            "client_id": d.get("client_id", ""), "client_secret": d.get("client_secret", ""),
            "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode())
    with urllib.request.urlopen(req, timeout=20) as r:  # urllib 默认认 HTTPS_PROXY 环境
        j = json.load(r)
    d["token"] = j["access_token"]
    if j.get("refresh_token"):
        d["refresh_token"] = j["refresh_token"]
    d["expiry"] = (datetime.utcnow() + timedelta(seconds=int(j.get("expires_in", 3599)))
                   ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p.write_text(json.dumps(d, indent=2))
    return True, f"已刷新（余 {int(j.get('expires_in', 3599)) // 60}min）"



# ---------------------------------------------------------------- lease / 队列协同
def _lease_touch() -> None:
    """标记「有人在用 Colab 会话」——video2md serve 看门狗据此让路。"""
    if not LEASE_FILE:
        return
    try:
        lp = Path(LEASE_FILE).expanduser()
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    except OSError:
        pass



def _lease_release() -> None:
    if not LEASE_FILE:
        return
    try:
        Path(LEASE_FILE).expanduser().unlink(missing_ok=True)
    except OSError:
        pass



def _more_jobs_waiting() -> bool:
    """video2md 作业队列是否还有排队任务——决定任务完是否保留热会话。未配置视为没有。"""
    if not QUEUE_URL:
        return False
    try:
        d = httpx.get(QUEUE_URL, timeout=5).json()
        return bool(d.get("queue_len") or d.get("running"))
    except Exception:
        return False



def _lease_fresh(max_age: float = 180.0) -> bool:
    """lease 心跳是否新鲜（有任务正占着会话跑）。"""
    if not LEASE_FILE:
        return False
    try:
        return time.time() - Path(LEASE_FILE).expanduser().stat().st_mtime < max_age
    except OSError:
        return False



def _session_epilogue() -> None:
    """任务收尾：队列有货就保温（省下一条 1–2min 冷启动），见底即关（不烧闲置配额）。"""
    _lease_release()
    if _more_jobs_waiting():
        print("  ♻️ 队列还有排队任务，保留 Colab 会话给下一条复用")
    else:
        _colab_stop_session("任务结束且队列见底")



def autostop_watchdog(is_inflight, idle_s: int | None = None) -> None:
    """空闲看门狗（接管原 video2md serve 的职责）：队列见底且路由无在途任务就关会话。

    存在的意义是兜底「任务被 SIGKILL / 进程重启」等 finally 跑不到的情况——
    路由是长命组件，由它保证会话最终一定关。busy = 路由在途（is_inflight()）
    ∨ 下游队列非空（_more_jobs_waiting()）；lease 新鲜 = 有任务正在会话里跑，让路。
    """
    idle_need = idle_s if idle_s is not None else AUTOSTOP_IDLE_S
    idle_since: float | None = None
    while True:
        time.sleep(15)
        busy = bool(is_inflight()) or _more_jobs_waiting()
        if busy:
            idle_since = None
            continue
        idle_since = idle_since or time.time()
        if time.time() - idle_since >= idle_need and not _lease_fresh() and _colab_session_active():
            _colab_stop_session("队列空闲自动关闭，省配额")
            idle_since = None



# ---------------------------------------------------------------- 轮询
def _colab_poll_result(t0: float, deadline: float, out_json: Path, status_json: Path,
                       worker_log: Path) -> dict:
    """轮询 Colab 产物直到 out.json 就绪/报错/超时。"""
    def fetch(remote: str, local: Path) -> bool:
        try:
            _colab(["download", "-s", SESSION, remote, str(local)], timeout=90, retries=0)
            return True
        except Exception:
            return False

    last_hb = time.time()
    gone = 0
    while True:
        if time.time() >= deadline:
            raise RuntimeError(f"Colab 任务超时（>{MAX_WAIT_S // 60}min）")
        time.sleep(10)
        _lease_touch()  # 心跳：长任务不摸租约会被看门狗当闲置杀会话
        if fetch("/content/out.json", out_json):
            try:
                data = json.loads(out_json.read_text())
                print(f"  ✅ Colab 往返 {time.time() - t0:.0f}s")
                return data
            except json.JSONDecodeError:
                pass
        if fetch("/content/status.json", status_json):
            try:
                st = json.loads(status_json.read_text())
            except json.JSONDecodeError:
                st = {}
            if st.get("state") == "error":
                raise RuntimeError(f"远端执行失败: {str(st.get('error'))[:300]}")
        # 会话没了（被回收/被谁关了）就别等到超时：连续两次探测不到即快速失败
        if not _colab_session_active():
            gone += 1
            if gone >= 2:
                raise RuntimeError("Colab 会话已消失（被回收或被其他进程关闭），请重试")
        else:
            gone = 0
        # 兜底：worker 若被硬杀（status 停在 running），从 worker.log 里捞 Traceback 快速失败
        if time.time() - last_hb > 25:
            last_hb = time.time()
            if fetch("/content/worker.log", worker_log):
                try:
                    txt = worker_log.read_text()
                except OSError:
                    txt = ""
                if "Traceback" in txt:
                    tail = txt.strip().splitlines()[-3:]
                    raise RuntimeError("远端 worker 异常退出: " + " / ".join(tail)[:300])
        print(f"    … Colab 执行中 {time.time() - t0:.0f}s")



# ---------------------------------------------------------------- 三个负载的完整生命周期
def colab_run_asr(audio: Path, tmp: Path, model: str, language: str) -> dict:
    """音频（aac/m4a）→ Colab T4 faster-whisper → whisper 同形 JSON。会话用完按队列节奏收放。"""
    job = tmp / "v2md_job.py"
    job.write_text(_COLAB_JOB)
    worker = tmp / "v2md_worker.py"
    worker.write_text(_COLAB_WORKER)
    args_json = tmp / "v2md_args.json"
    args_json.write_text(json.dumps({"model": model, "language": language}))
    print(f"  ☁️ Colab GPU 转录（会话 {SESSION}）…")
    _lease_touch()
    t0 = time.time()
    try:
        _ensure_colab_session("faster_whisper")
        _colab(["upload", "-s", SESSION, str(audio), "/content/in.m4a"], timeout=600)
        for local, remote in ((worker, "/content/v2md_worker.py"),
                              (args_json, "/content/v2md_args.json")):
            _colab(["upload", "-s", SESSION, str(local), remote], timeout=300)
        _colab(["exec", "-s", SESSION, "-f", str(job)], timeout=300)
        return _colab_poll_result(t0, t0 + MAX_WAIT_S, tmp / "colab-out.json",
                                  tmp / "colab-status.json", tmp / "colab-worker.log")
    finally:
        _session_epilogue()



def colab_run_ocr(images: list[Path], tmp: Path) -> list[dict]:
    """帧图集 → tar 上 Colab → rapidocr → [{name,text,conf}]。"""
    import tarfile
    tar = tmp / "frames.tar"
    with tarfile.open(tar, "w") as tf:
        for p in images:  # 调用方保证文件名平铺唯一
            tf.add(p, arcname=p.name)
    worker = tmp / "v2md_worker.py"
    worker.write_text(_COLAB_OCR_WORKER)
    job = tmp / "v2md_job.py"
    job.write_text(_COLAB_JOB)
    _lease_touch()
    t0 = time.time()
    try:
        _ensure_colab_session()
        probe = _colab(["exec", "-s", SESSION], timeout=300, retries=2,
                       stdin="import rapidocr\nprint('DEPS' + '-OK')")
        if "DEPS-OK" not in probe:
            print("  ⏳ 会话内安装 rapidocr…")
            _colab(["install", "-s", SESSION, "rapidocr"], timeout=600)
        for local, remote in ((tar, "/content/frames.tar"),
                              (worker, "/content/v2md_worker.py")):
            _colab(["upload", "-s", SESSION, str(local), remote], timeout=600)
        # 上轮残留会造成假完成
        _colab(["exec", "-s", SESSION], timeout=90, stdin=(
            "import os\n"
            "for f in ('/content/out.json', '/content/status.json'):\n"
            "    if os.path.exists(f):\n        os.remove(f)\nprint('clean')"))
        _colab(["exec", "-s", SESSION, "-f", str(job)], timeout=300)
        data = _colab_poll_result(t0, t0 + MAX_WAIT_S, tmp / "ocr-out.json",
                                  tmp / "ocr-status.json", tmp / "ocr-worker.log")
        print(f"  ✅ Colab OCR 往返 {time.time() - t0:.0f}s（{len(images)} 帧）")
        return data.get("results", [])
    finally:
        _session_epilogue()



def colab_exec_clips(video: Path | None, direct_url: str, specs: list[dict],
                     tmp: Path, out_dir: Path) -> dict[str, bool]:
    """spec 驱动的远端剪辑：清场 → 供源（上传 video 或 direct_url 直连 CDN）→
    nvenc 批切 → tar 拉回 → 成功段落进 out_dir。返回 {out_name: ok}。"""
    import tarfile
    _lease_touch()
    t0 = time.time()
    try:
        _ensure_colab_session()
        # 旧产物清场（会话跨任务复用，上轮 status/tar/源片残留会造成假完成）——先清再传
        _colab(["exec", "-s", SESSION], timeout=90, stdin=(
            "import os\n"
            "for f in ('/content/clip_status.json', '/content/clips.tar',"
            " '/content/clips_result.json', '/content/clip-src.mp4'):\n"
            "    if os.path.exists(f):\n        os.remove(f)\nprint('clean')"))
        if video is None:
            print("    ☁️ 源片由 Colab 直连 CDN 下载（省本地上行）…")
        else:
            # 大文件上行慢：超时按体积折算（保守 0.5MB/s 计）
            size_mb = video.stat().st_size / 1e6
            _colab(["upload", "-s", SESSION, str(video), "/content/clip-src.mp4"],
                   timeout=max(900, int(size_mb * 8)), retries=2)
        worker, launcher = tmp / "clip_worker.py", tmp / "clip_launcher.py"
        worker.write_text(_COLAB_CLIP_WORKER)
        launcher.write_text(_COLAB_CLIP_JOB)
        spec = tmp / "clip_job.json"
        spec.write_text(json.dumps({"direct_url": direct_url, "clips": specs}))
        for local, remote in ((worker, "/content/clip_worker.py"),
                              (launcher, "/content/clip_launcher.py"),
                              (spec, "/content/clip_job.json")):
            _colab(["upload", "-s", SESSION, str(local), remote], timeout=300)
        _colab(["exec", "-s", SESSION, "-f", str(launcher)], timeout=120)
        status_json = tmp / "clip_status.json"
        deadline = time.time() + MAX_WAIT_S
        note = ""
        while time.time() < deadline:
            time.sleep(10)
            _lease_touch()
            try:
                _colab(["download", "-s", SESSION, "/content/clip_status.json",
                        str(status_json)], timeout=90, retries=0)
                st = json.loads(status_json.read_text())
            except Exception:
                st = {}
            if st.get("note") and st["note"] != note:
                note = st["note"]
                print(f"    … Colab {note}")
            if st.get("state") == "done":
                print(f"    … Colab 剪辑 {st.get('ok')}/{st.get('total')} 段")
                break
            if st.get("state") == "error":
                raise RuntimeError(f"远端剪辑失败: {str(st.get('error'))[:200]}")
        else:
            raise RuntimeError("Colab 剪辑超时")
        tar_local = tmp / "clips.tar"
        _colab(["download", "-s", SESSION, "/content/clips.tar", str(tar_local)],
               timeout=600, retries=2)
        in_dir = tmp / "clips_in"
        in_dir.mkdir(exist_ok=True)
        with tarfile.open(tar_local) as tf:  # 条目名全部由调用方清单生成，无路径注入面
            tf.extractall(in_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, bool] = {}
        for c in specs:
            src = in_dir / c["out"]
            ok = src.exists() and src.stat().st_size > 0
            if ok:
                shutil.move(str(src), str(out_dir / c["out"]))
            results[c["out"]] = ok
        print(f"  ✅ Colab 剪辑往返 {time.time() - t0:.0f}s"
              f"（{sum(results.values())}/{len(specs)} 段）")
        return results
    finally:
        _session_epilogue()

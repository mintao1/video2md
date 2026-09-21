"""作业服务（阶段作业模型）：_job_run 子进程编排 / 队列恢复 / _run_stage 熔断 的单测。

不联网。跑法：python3 test_jobs.py
（Colab 会话保温用例已随算力池抽离迁往 ~/Desktop/开发项目/gpu_router/tests/test_colab_machinery.py）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from video2md import resolve, runs, serve

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---- 隔离：作业文件指到临时目录，避免污染线上 jobs.json
serve._JOBS_FILE = Path(tempfile.mkdtemp()) / "jobs.json"


class FakeProc:
    """假子进程：固定日志行，rc 由构造参数决定。"""

    def __init__(self, rc):
        self._rc = rc
        self.stdout = iter(["  📝 note.md\n", "SELF-CHECK PASS\n"])

    def wait(self):
        return self._rc


captured: list[list[str]] = []


def fake_popen_ok(cmd, **kw):
    captured.append(cmd)
    return FakeProc(rc=0)


def fake_popen_fail(cmd, **kw):
    captured.append(cmd)
    return FakeProc(rc=1)


print("== 1. 阶段作业：spawn 命令 + 成功路径 ==")
orig_popen = serve.subprocess.Popen
serve.subprocess.Popen = fake_popen_ok
job = {"id": "s1", "run_id": "abc12345", "stage": "download", "stage_args": {},
       "status": "queued", "log": [], "created": "t"}
with serve._JOBS_LOCK:
    serve._JOBS["s1"] = job
serve._job_run(job)
check("status=done", job["status"] == "done", str(job["status"]))
check("completed=1", job.get("completed") == 1)
cmd = captured[0]
check("以包模块拉起（-m video2md）", "-m" in cmd and "video2md" in cmd, str(cmd))
check("__stage + run_id + stage 传参",
      "__stage" in cmd and "abc12345" in cmd and "download" in cmd, str(cmd))
check("日志尾落 job", any("note.md" in ln for ln in job["log"]))
check("持久化落盘", serve._JOBS_FILE.exists())

print("== 2. 阶段作业：失败路径（rc!=1 → error + 日志尾作错误） ==")
captured.clear()
serve.subprocess.Popen = fake_popen_fail
job2 = {"id": "s2", "run_id": "abc12345", "stage": "transcribe", "stage_args": {},
        "status": "queued", "log": [], "created": "t"}
with serve._JOBS_LOCK:
    serve._JOBS["s2"] = job2
serve._job_run(job2)
check("status=error", job2["status"] == "error")
check("failed=1", job2.get("failed") == 1)
check("error 取自日志尾", "SELF-CHECK" in (job2.get("error") or ""), str(job2.get("error")))
serve.subprocess.Popen = orig_popen

print("== 3. 队列恢复（服务重启语义）==")
serve._JOBS.clear(); serve._JOB_QUEUE.clear()
serve._JOBS.update({"qa": {"id": "qa", "run_id": "r", "stage": "download", "status": "queued", "log": [], "created": "t"},
                    "rb": {"id": "rb", "run_id": "r", "stage": "ocr", "status": "running", "log": [], "created": "t"},
                    "dc": {"id": "dc", "run_id": "r", "stage": "kb", "status": "done", "log": [], "created": "t", "note": "n.md"}})
serve._JOB_QUEUE.append("qa"); serve._save_jobs()
serve._JOBS.clear(); serve._JOB_QUEUE.clear(); serve._load_jobs()
check("排队+中断都重排", serve._JOB_QUEUE == ["qa", "rb"], str(serve._JOB_QUEUE))
check("完成记录原样", serve._JOBS["dc"]["status"] == "done")

print("== 4.（会话保温用例已随 Colab 抽离迁往 gpu_router tests/test_colab_machinery.py）==")

print("== 5. ensure_scheme：裸域名补 https://，本地路径不动 ==")
check("裸域名补 scheme", resolve.ensure_scheme("www.bilibili.com/video/BV1xx")
      == "https://www.bilibili.com/video/BV1xx")
check("已带 scheme 不动", resolve.ensure_scheme("https://x.com/a") == "https://x.com/a")
check("空白不动", resolve.ensure_scheme("") == "")
check("不存在的相对路径不补", resolve.ensure_scheme("no_such_file_xyz.mp4") == "no_such_file_xyz.mp4")
_p = Path(tempfile.mkdtemp()) / "real.mp4"
_p.write_text("x")
check("存在的本地文件不补", resolve.ensure_scheme(str(_p)) == str(_p))

print("== 6. _run_stage：部分失败留痕 + 关键阶段全灭也先落盘 ==")
runs.RUNS_FILE = Path(tempfile.mkdtemp()) / "runs.json"
runs._RUNS_LOCKFILE = runs.RUNS_FILE.with_name("runs.lock")


def _mkrun(rid, urls):
    return {"run_id": rid, "url": urls[0], "part_items": "", "created": "2026-01-01T00:00:00",
            "tmp": tempfile.mkdtemp(prefix="video2md-run-t-"), "total": len(urls),
            "entries": [{"url": u} for u in urls], "artifacts": {}, "stages": {}, "status": "created"}


_orig_download = runs._STAGE_FNS["download"]


def fake_download(run, entry, art, tmp, args):
    if entry["url"] == "u2":
        raise SystemExit("下载失败: 模拟")
    art.update(video=str(tmp / f"{entry['url']}.mp4"), duration=1.0, width=1, height=1, fp="fp")


runs._STAGE_FNS["download"] = fake_download
runs._run_stage(_mkrun("r1", ["u1", "u2", "u3"]), "download", {})
saved = runs._load_runs()["r1"]
check("失败条目留痕", saved["stages"]["download"].get("failed_entries") == ["2"],
      str(saved["stages"]))
check("partial_failures 落盘", saved["partial_failures"]["download"] == ["2"])
check("成功条目产物已落盘", saved["artifacts"]["u1"].get("video", "").endswith(".mp4"))


def always_fail(run, entry, art, tmp, args):
    raise SystemExit("全灭模拟")


runs._STAGE_FNS["download"] = always_fail
_thrown = False
try:
    runs._run_stage(_mkrun("r2", ["u2"]), "download", {})
except SystemExit:
    _thrown = True
check("关键阶段全灭 → SystemExit", _thrown)
check("全灭也先落盘（stages 留痕）", "download" in runs._load_runs()["r2"]["stages"])
runs._STAGE_FNS["download"] = _orig_download

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：" + "; ".join(FAILED))
    sys.exit(1)
print("✅ 全部通过")

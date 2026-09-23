#!/usr/bin/env python3
"""半成品 run 抢救器：把「download 成功、transcribe 被 colab 会话回收熔断烧掉」的合集
run 按 part_items 逐条补跑，tmp 里的视频文件直接搬进新 run 复用（不重复下载）。

背景（2026-09-23 实测）：colab 免费档会话会在长批次中途被回收，router 侧表现为瞬时
502；runs._run_stage 的全局错误 2 连即熔 → 整 run error，但已下载的 mp4 留在 tmp。
API 没有单条目重驱动端点，故走 graph-run CLI 子进程：同 URL+part_items=N 建的新 run
里，本脚本先把旧 tmp 的视频搬到新 run tmp 并预置 art["video"]——_stage_download 见
已有产物即「⏭ 已下载，跳过」，管线从 transcribe 续起。

用法（容器内跑，指到 data 卷的 runs.json/state.json/tmp）:
  python scripts/rescue_partial.py <run_id> [--dry-run] [--max-entries N]
前置：api 服务空闲（graph-run 与在线 worker 并行写 runs.json 会打架）。
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video2md import config, runs  # noqa: E402
from video2md.graph import resolve_run  # noqa: E402
from video2md.resolve import normalize_url  # noqa: E402
from video2md.util import load_state  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-entries", type=int, default=0, help="最多补几条（0=不限）")
    a = ap.parse_args()

    db = runs._load_runs()
    old = db.get(a.run_id)
    if not old:
        print(f"❌ run {a.run_id} 不存在")
        return 1
    url = old.get("url", "")
    entries = old.get("entries", [])
    arts = old.get("artifacts", {})
    state_done = {str(v.get("url", "")).rstrip("/") for v in load_state().values()
                  if not v.get("dry_run") and v.get("url")}

    todo = []
    for e in entries:
        idx = e.get("playlist_index") or 0
        art = arts.get(e["url"], {})
        if art.get("skipped") or art.get("note"):
            continue
        if normalize_url(e["url"]).rstrip("/") in state_done:
            continue
        has_video = bool(art.get("video")) and Path(art.get("video", "")).is_file()
        todo.append((idx, e["url"], has_video))
    if a.max_entries:
        todo = todo[:a.max_entries]

    print(f"run {a.run_id}（{url}）：{len(entries)} 条目，待补 {len(todo)}"
          f"（其中 {sum(1 for _, _, h in todo if h)} 条视频已在旧 tmp 可复用）")
    for idx, u, h in todo[:10]:
        print(f"  #{idx:<3} {'♻️' if h else '🆕'} {u[:80]}")
    if len(todo) > 10:
        print(f"  …共 {len(todo)} 条")
    if a.dry_run or not todo:
        return 0

    ok_n = fail_n = 0
    for n, (idx, _u, has_video) in enumerate(todo, 1):
        print(f"\n[{time.strftime('%H:%M:%S')}] ▶ 补条目 #{idx}（{n}/{len(todo)}）"
              + ("，复用已下载视频" if has_video else "，将重新下载"), flush=True)
        t0 = time.time()
        # 1) 预建 run（owner="" 与旧 dev uid 同口径 dedup：error 终态不命中，必新建）
        run, err, _dup = resolve_run(url, str(idx), force=False)
        if err:
            print(f"  ❌ 建 run 失败：{err}")
            fail_n += 1
            continue
        # 2) create_run 的 state-fp 命中会把旧 tmp 视频路径直接带进新 art（_stage_*
        #    见产物即跳过）；旧文件被清或没带进来时，手动搬一份进新 run tmp 兜底。
        au = run["entries"][0]["url"]
        art_new = run["artifacts"].get(au, {})
        cur = Path(art_new.get("video", ""))
        if not cur.is_file():
            old_art = arts.get(_u) or {}
            src = Path(old_art.get("video", ""))
            if src.is_file():
                dst = Path(run["tmp"]) / src.name
                shutil.copy2(src, dst)
                fresh = runs._load_runs()[run["run_id"]]
                fresh["artifacts"].setdefault(au, {}).update(
                    video=str(dst), duration=float(old_art.get("duration") or 0),
                    width=int(old_art.get("width") or 0),
                    height=int(old_art.get("height") or 0),
                    fp=old_art.get("fp", ""), uploader=old_art.get("uploader", ""))
                with runs._runs_file_lock():
                    runs._runs_merge(fresh)
                print(f"  ♻️ 视频搬运就位 {dst.name}")
        # 3) 图直跑该条目（同步阻塞；假 LLM 无关，生产凭据走 secrets）
        import subprocess
        p = subprocess.run(
            [sys.executable, "-m", "video2md", "graph-run", url, "--part-items", str(idx)],
            capture_output=True, text=True, timeout=3 * 3600,
            cwd=str(Path(config.__file__).parent.parent))
        tail = (p.stdout or "").strip().splitlines()[-3:]
        print("\n".join(f"  {t}" for t in tail))
        if p.returncode == 0:
            ok_n += 1
            print(f"  ✅ #{idx} 用时 {time.time() - t0:.0f}s")
        else:
            fail_n += 1
            print(f"  ❌ #{idx} 失败（rc={p.returncode}）{(p.stderr or '')[-200:]}")
    print(f"\n==== 汇总：✅{ok_n} ❌{fail_n} ====")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

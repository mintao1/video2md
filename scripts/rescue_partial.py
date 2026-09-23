#!/usr/bin/env python3
"""半成品 run 抢救器：把「download 成功、transcribe 被 colab 会话回收熔断烧掉」的合集
run 按 part_items 逐条补跑，tmp 里的视频文件直接搬进新 run 复用（不重复下载）。

背景（2026-09-23 实测）：colab 免费档会话会在长批次中途被回收，router 侧表现为瞬时
502；runs._run_stage 全局错误 2 连即熔 → 整 run error，但已下载的 mp4 留在 tmp。
本脚本对每条未出笔记的条目：resolve_run 预建单条目 run → 把旧 tmp 视频复制进新 run
tmp 并预置 art["video"]（_stage_download 见已有产物即「⏭ 已下载，跳过」，管线从
transcribe 续起）→ 经 api._enqueue 在**本进程**驱动整张图。

为什么必须停 API 再跑：worker 串行信号量只活在 API 进程里，跨进程无法互斥——API
活着时双进程并发写同一个 vault 笔记文件会互相覆盖。所以流程是 docker stop api →
本脚本逐条跑完 → docker start api。产物按服务器当前 artifact_mode 正常落
vault/outbox。state.json 幂等：已出笔记的条目 resolve 时自动标 skipped。

用法（容器内跑，环境变量沿用 compose：V2MD_RUNS_FILE/V2MD_STATE_FILE/OBSIDIAN_VAULT）:
  docker stop video2md-video2md-1
  docker exec video2md-video2md-1 python scripts/rescue_partial.py <run_id> [--dry-run] [--max-entries N]
  docker start video2md-video2md-1
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video2md import runs  # noqa: E402
from video2md.graph import resolve_run  # noqa: E402
from video2md.resolve import normalize_url  # noqa: E402
from video2md.util import load_state  # noqa: E402

_CP_CLOSER = None  # checkpointer 关闭句柄（drive_and_wait 首次建时写入）


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

    # owner 沿用旧 run：migrate 收编后产物仍归同一用户，插件列表可见；
    # 本进程驱动不经 API 归属校验，没有 uid 不同源问题。
    api_owner = old.get("owner", "")
    ok_n = fail_n = 0
    for n, (idx, _u, has_video) in enumerate(todo, 1):
        print(f"\n[{time.strftime('%H:%M:%S')}] ▶ 补条目 #{idx}（{n}/{len(todo)}）"
              + ("，复用已下载视频" if has_video else "，将重新下载"), flush=True)
        t0 = time.time()
        # 1) 预建单条目 run（dedup 只认 created/running，error 旧 run 不命中，必新建）
        run, err, _dup = resolve_run(url, str(idx), force=False, owner=api_owner)
        if err:
            print(f"  ❌ 建 run 失败：{err}")
            fail_n += 1
            continue
        rid = run["run_id"]
        # 2) 把旧 tmp 的视频搬进新 run tmp 并预置 art["video"]——_stage_download 见
        #    已有产物即「⏭ 已下载，跳过」，管线从 transcribe 续起。state.json 里躺着
        #    已完成的同 URL 记录时 create_run 会整条标 skipped（vault 旧语义），故无条
        #    件 pop skip 再写 video（skip 的条目 worker 连搬都不搬就白建了）。
        old_art = arts.get(_u) or {}
        src = Path(old_art.get("video", ""))
        dst = None
        if src.is_file():
            dst = Path(run["tmp"]) / src.name
            shutil.copy2(src, dst)
        elif has_video:
            print(f"  ⚠ 旧视频 {src} 已被清理，退化为重新下载")
        # 3) 本进程驱动整张图（api._enqueue + 事件循环里跑 _drive），轮询到该条目出
        #    note。逐条串行：上一条不落地不投下一条，state.json 落盘后天然幂等。
        done = drive_and_wait(url, rid, idx, api_owner, dst, old_art)
        if done:
            ok_n += 1
            print(f"  ✅ #{idx} 用时 {time.time() - t0:.0f}s")
        else:
            fail_n += 1
            cur = runs._load_runs().get(rid, {})
            print(f"  ❌ #{idx} 未出笔记（status={cur.get('status')}）")
    print(f"\n==== 汇总：✅{ok_n} ❌{fail_n} ====")
    if _CP_CLOSER:
        _CP_CLOSER()  # sqlite/postgres checkpointer 句柄收尾（postgres 不关会挂进程）
    return 0 if fail_n == 0 else 1


def drive_and_wait(url: str, rid: str, idx: int, owner: str,
                   dst: Path | None, old_art: dict) -> bool:
    """入队驱动 rid 并轮询到该条目出 note；colab 类熔断 error 冷却后重投一次。

    走 api._enqueue 而不是 POST /runs：本进程即执行体，不依赖 API 活着。_drive→
    run_graph(run_id=rid) 里 resolve 节点 dedup 命中我们预建的 created run（同
    url+part_items+owner），随后逐阶段续跑；搬好的视频经 runs.json 被 _stage_node
    的 _reload 读回（artifacts dict 引用同一对象，_run_stage 原地更新无覆盖问题）。
    """
    import asyncio

    from video2md import api as v2api

    def _entry_done(d: dict) -> bool:
        art = (d.get("artifacts") or {}).get(next(
            (e["url"] for e in (d.get("entries") or []) if e.get("playlist_index") == idx), ""), {})
        return bool(art.get("note"))

    def _patch() -> None:
        # 搬视频须在入队之前：_run_stage 每阶段从 runs.json reload，先入队可能把
        # 预置的 video path 冲掉。锁内读-改-写。
        if dst is None:
            return
        with runs._runs_file_lock():
            fresh = runs._load_runs()[rid]
            a = fresh["artifacts"].setdefault(fresh["entries"][0]["url"], {})
            a.pop("skipped", None)
            a.update(video=str(dst), duration=float(old_art.get("duration") or 0),
                     width=int(old_art.get("width") or 0),
                     height=int(old_art.get("height") or 0),
                     fp=old_art.get("fp", ""), uploader=old_art.get("uploader", ""))
            runs._runs_merge(fresh)

    async def _submit() -> None:
        # 假 api 壳最小初始化：模块级 _SEMA 绑在导入时 loop 上（换新环须重建）；
        # _CHECKPOINTER 正常由 lifespan 注入，这里补一次——单条目 run 从头跑也要它
        # 做 thread_id 记账。_patch 必须在入队前：resolve_run 节点会按 url+part_items+
        # owner dedup 命中我们预建并搬好视频的 created run。
        v2api._SEMA = asyncio.Semaphore(1)
        if v2api._CHECKPOINTER is None:
            from video2md.config import ENV as _ENV
            from video2md.graph import make_checkpointer_from_env
            cp, closer = make_checkpointer_from_env(_ENV)
            global _CP_CLOSER
            _CP_CLOSER = closer
            v2api._CHECKPOINTER = cp
        _patch()
        v2api._enqueue(url, str(idx), False, False, 60, run_id=rid, owner=owner)

    async def _wait(max_wait_s: int) -> tuple[str, dict]:
        """轮询到 note 出现 / 任务终态 / 超时。返回 ('ok'|'terminal'|'timeout', d)。"""
        t0 = time.time()
        last = ""
        while time.time() - t0 < max_wait_s:
            await asyncio.sleep(20)
            d = runs._load_runs().get(rid, {})
            st = (v2api._TASKS.get(rid) or {}).get("status") or d.get("status")
            line = f"  …#{idx} {st} {int((time.time() - t0) // 60)}min"
            if line != last:
                print(line, flush=True)
                last = line
            if _entry_done(d):
                return "ok", d
            if st in ("done", "partial", "error"):
                return "terminal", d
        return "timeout", runs._load_runs().get(rid, {})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        for attempt in (1, 2):
            loop.run_until_complete(_submit())
            outcome, d = loop.run_until_complete(_wait(4 * 3600))
            if outcome == "ok":
                return True
            if outcome == "timeout":
                print(f"  ⏰ #{idx} 等待超时（4h）")
                return False
            errs = json.dumps({"t": (v2api._TASKS.get(rid) or {}).get("errors"),
                               "s": {k: v.get("breaker")
                                     for k, v in (d.get("stages") or {}).items()
                                     if isinstance(v, dict) and v.get("breaker")}},
                              ensure_ascii=False)
            breaker = any(p in errs for p in ("熔断", "provider", "colab", "502"))
            if attempt == 1 and breaker:
                print(f"  🔁 全局故障熔断（{errs[:90]}…），等 5min 冷却后重投")
                time.sleep(300)
                continue
            return _entry_done(d)
        return False
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    sys.exit(main())

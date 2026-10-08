#!/usr/bin/env python3
"""scripts/otel_replay.py — 历史遥测回放：logs/run-*.jsonl → OTLP spans → Langfuse Cloud。

把本地 jsonl 真相（span started/completed 事件对）拼回 OTel span 树重投远端——
Langfuse 网页里就能翻看历史 run 的完整 trace（root→stage→entry→llm），
含 evals/deadman 写进同一 jsonl 的事件（按 run_id 分组自动归位同一条 trace）。

约定与 video2md/observability.py 逐字对齐（在线/回放同一棵树）：
- trace_id = sha256(trace_prefix+run_id)[:32]（确定性；evals 分数回挂同一约定。
  前缀 2026-09-22 起为 "video2md-"；更早的 run 用 --trace-prefix serve- 回放才能
  落回 Langfuse 里的旧 trace）
- root 锚点 span_id = sha256("root-"+run_id)[:16]（伪造远端 SpanContext，锚点永不上报）
- 属性映射复用 observability._scalar_attrs（llm:* → gen_ai.* → Langfuse generation）
- info 点事件 → 零时长 span 挂当前最深开口 span；悬空闭合（丢 started）补零时长 span

回放与在线的有意差异：
- 时间戳用事件行 ts（写入时的本地时钟，秒级精度；span 起止差与 durationMs 有 ≤1s 误差）
- 悬空 started（进程崩溃点）强制闭合并标 ERROR + replay.dangling=true
  （在线 close_run 只 end 不标错；回放是事后复盘，崩溃点要显式可见）
- root 属性带 replayed=true / run_status（runs.json 联表）/ replay.source

端点：默认 observability._endpoint()；本机 SSOT 已按「只追踪生产」决策不持久存 Langfuse 键
（生产遥测走服务器 collector），按需回放用真实环境变量传凭证：
  LANGFUSE_PK=… LANGFUSE_SK=… uv run python scripts/otel_replay.py
--endpoint http://127.0.0.1:4318 可改投任意 collector（服务器上 docker exec 跑同款）。

核验注意：2026-09-16 后新建的 Langfuse 组织 legacy 读 API（/api/public/traces）404、
v2 observations 读路径不可靠（数据短暂可见后查空）——落库核验以网页 UI 为准。

⚠️ 一次性操作：重复回放同一批文件会产生重复 span（Langfuse 不去重）。

运行：uv run python scripts/otel_replay.py                     # 全部 logs/run-*.jsonl
      uv run python scripts/otel_replay.py --run-id da76c99b   # 单个 run
      uv run python scripts/otel_replay.py --since 2026-09-21 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)
from platform_otel_sdk._otel import _endpoint  # _endpoint 随 otel 迁移入 SDK（033ae72）

from video2md.observability import _scalar_attrs

PROJ = Path(__file__).resolve().parent.parent

# OTLP/HTTP exporter 底层是 requests：会吃 os.environ 里的代理键。
# 本机直连 cloud.langfuse.com 实测 200，而 17898 出口对它 TLS 掐断 → 回放进程剥代理直连。
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _ns(ts: str) -> int:
    """ISO 秒级时间戳（本地时钟写入）→ ns since epoch。"""
    return int(datetime.fromisoformat(ts).timestamp() * 1e9)


def load_runs_meta(path: Path) -> dict:
    """runs.json → {run_id: {"name": url, "status": 终态}}（root span 名 / 回放留痕）。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"⚠️ runs.json 解析失败（{path}），root 名回退 run_id", file=sys.stderr)
        return {}
    out = {}
    for rid, r in raw.items():
        if isinstance(r, dict):
            out[rid] = {"name": r.get("url") or "", "status": r.get("status") or ""}
    return out


def iter_records(paths: list[Path]) -> tuple[list[dict], int]:
    """逐行读 jsonl → 记录列表（保持 文件序×行序）。坏行跳过并计数。"""
    recs, bad = [], 0
    for p in paths:
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(r, dict) and r.get("ts") and r.get("run_id"):
                recs.append(r)
            else:
                bad += 1
    return recs, bad


def group_by_run(recs: list[dict]) -> "OrderedDict[str, list[dict]]":
    """按 run_id 分组；组内按 ts 稳定排序（同秒保序：started 行本就先于 completed 行）。"""
    runs: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in recs:
        runs.setdefault(r["run_id"], []).append(r)
    for rs in runs.values():
        rs.sort(key=lambda r: r["ts"])
    return runs


def replay_run(tracer, run_id: str, recs: list[dict], meta: dict,
               source: str = "", trace_prefix: str = "video2md-") -> dict | None:
    """一个 run 的 jsonl 记录 → 一条 trace 的完整 span 树（镜像 record() 的映射）。

    meta: {name, status}（runs.json 联表；缺省 name 回退 run_id）。
    trace_prefix: 确定性 traceId 前缀（现行 video2md-；2026-09-22 前的 run 用 serve-）。
    返回 stats（含 trace_id 十六进制）；recs 为空返回 None。
    """
    if not recs:
        return None
    tid = int(hashlib.sha256(f"{trace_prefix}{run_id}".encode()).hexdigest()[:32], 16)
    sid = int(hashlib.sha256(f"root-{run_id}".encode()).hexdigest()[:16], 16)
    anchor = SpanContext(trace_id=tid, span_id=sid, is_remote=True,
                         trace_flags=TraceFlags(TraceFlags.SAMPLED))
    parent_ctx = set_span_in_context(NonRecordingSpan(anchor))

    name = (meta or {}).get("name") or run_id
    attrs = {"langfuse.trace.name": name, "run_id": run_id, "replayed": True}
    if (meta or {}).get("status"):
        attrs["run_status"] = meta["status"]
    if source:
        attrs["replay.source"] = source[:200]
    first_ns, last_ns = _ns(recs[0]["ts"]), _ns(recs[-1]["ts"])
    root = tracer.start_span(name, context=parent_ctx, attributes=attrs,
                             start_time=first_ns)

    open_spans: dict[str, object] = {}   # spanId → SDK span（插入序 = 深度序，同 record()）
    info_n = synth_n = 0
    for rec in recs:
        ts_ns = _ns(rec["ts"])
        span_id, status = rec.get("spanId"), rec.get("status")
        event = str(rec.get("event") or "")
        if not span_id:
            # info 点事件 → 零时长 span 挂当前最深开口 span（无则 root）
            parent = list(open_spans.values())[-1] if open_spans else root
            sp = tracer.start_span(event, context=set_span_in_context(parent),
                                   attributes=_scalar_attrs(rec), start_time=ts_ns)
            sp.end(end_time=ts_ns)
            info_n += 1
        elif status == "started":
            parent = open_spans.get(rec.get("parentSpanId") or "") or root
            open_spans[span_id] = tracer.start_span(
                event, context=set_span_in_context(parent),
                attributes=_scalar_attrs(rec), start_time=ts_ns)
        else:
            # completed/failed/exited → 闭合；悬空闭合（丢 started）补零时长 span 留痕
            sp = open_spans.pop(span_id, None)
            if sp is None:
                sp = tracer.start_span(event, context=set_span_in_context(root),
                                       attributes=_scalar_attrs(rec), start_time=ts_ns)
                synth_n += 1
            for k, v in _scalar_attrs(rec).items():
                sp.set_attribute(k, v)
            if status in ("failed", "exited"):
                sp.set_status(Status(StatusCode.ERROR, str(rec.get("error") or "")[:300]))
            else:
                sp.set_status(Status(StatusCode.OK))
            sp.end(end_time=ts_ns)
    # 悬空 started = 崩溃点：强制闭合标 ERROR（在线 close_run 只 end；回放要显式可见）
    dangling = len(open_spans)
    for sp in open_spans.values():
        sp.set_attribute("replay.dangling", True)
        sp.set_status(Status(StatusCode.ERROR, "dangling started（进程崩溃点，回放强制闭合）"))
        sp.end(end_time=last_ns)
    root.end(end_time=last_ns)
    return {"trace_id": f"{tid:032x}", "name": name, "spans": len(recs),
            "info": info_n, "synth": synth_n, "dangling": dangling}


def _default_paths() -> list[Path]:
    return sorted((PROJ / "logs").glob("run-*.jsonl"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="历史 jsonl 遥测回放（→ Langfuse Cloud / 任意 OTLP collector）")
    ap.add_argument("paths", nargs="*", type=Path, help="jsonl 文件（缺省：logs/run-*.jsonl 全部）")
    ap.add_argument("--run-id", help="只回放指定 run_id")
    ap.add_argument("--since", help="只回放该日期（YYYY-MM-DD 或 ISO ts）之后的事件")
    ap.add_argument("--runs-json", type=Path, default=PROJ / "runs.json",
                    help="run_id→url 联表（root span 名；缺省项目根 runs.json）")
    ap.add_argument("--endpoint", help="OTLP base 覆盖（如 http://127.0.0.1:4318）；缺省走 LANGFUSE 直连")
    ap.add_argument("--trace-prefix", default="video2md-",
                    help="确定性 traceId 前缀（2026-09-22 前的历史 run 用 serve-）")
    ap.add_argument("--dry-run", action="store_true", help="InMemory 试跑，不出网络，只印统计")
    args = ap.parse_args(argv)

    for k in _PROXY_KEYS:  # 直连纪律见模块 docstring
        os.environ.pop(k, None)

    paths = args.paths or _default_paths()
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("没有可回放的 jsonl 文件（logs/run-*.jsonl 不存在？）", file=sys.stderr)
        return 1
    recs, bad = iter_records(paths)
    if args.since:
        recs = [r for r in recs if r["ts"] >= args.since]
    if args.run_id:
        recs = [r for r in recs if r["run_id"] == args.run_id]
    runs = group_by_run(recs)
    if not runs:
        print(f"过滤后无记录（since={args.since} run_id={args.run_id}）", file=sys.stderr)
        return 1
    runs_meta = load_runs_meta(args.runs_json)
    source = ",".join(p.name for p in paths)

    # 与在线同一 resource（Langfuse 同一 service 归组）；span 侧带 replayed 区分
    provider = TracerProvider(resource=Resource.create(
        {"service.name": "video2md-serve", "service.namespace": "video2md"}))
    if args.dry_run:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    else:
        if args.endpoint:
            base, hdrs = args.endpoint.rstrip("/"), {}
        else:
            cfg = _endpoint()
            if cfg is None:
                print("未配置端点：设 --endpoint，或在 ~/.config/video2md.env 配 LANGFUSE_PK/SK",
                      file=sys.stderr)
                return 1
            base, hdrs, _ = cfg
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        # 回放是批处理不是边跑边导：大批量少握手（队列拉高防静默丢 span）
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=hdrs),
            max_queue_size=200000, max_export_batch_size=512, schedule_delay_millis=2000))
        print(f"导出 → {base}/v1/traces")
    tracer = provider.get_tracer("video2md")

    total = {"spans": 0, "info": 0, "synth": 0, "dangling": 0}
    for run_id, rs in runs.items():
        st = replay_run(tracer, run_id, rs, runs_meta.get(run_id), source=source,
                        trace_prefix=args.trace_prefix)
        if st is None:
            continue
        for k in total:
            total[k] += st[k]
        print(f"{run_id}  trace=0x{st['trace_id']}  events={st['spans']} "
              f"(info={st['info']} synth={st['synth']} dangling={st['dangling']})  {st['name'][:60]}")
    try:
        provider.shutdown()   # SimpleSpanProcessor：同步导出，shutdown 即落库
    except Exception as e:
        print(f"❌ 导出失败：{e}", file=sys.stderr)
        return 1
    print(f"✅ 回放 {len(runs)} 个 run / {total['spans']} 事件 "
          f"(info={total['info']} synth={total['synth']} 崩溃点={total['dangling']})"
          + ("（dry-run，未出网络）" if args.dry_run else ""))
    if bad:
        print(f"⚠️ 跳过坏行 {bad} 行", file=sys.stderr)
    print("⚠️ 一次性操作：重复回放会产生重复 span", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

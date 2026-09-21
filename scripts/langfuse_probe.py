#!/usr/bin/env python3
"""Langfuse 一键监测：聚合最近 N 小时的 trace（Dify 节点级 + serve 阶段级）。

用法：python3 scripts/langfuse_probe.py [小时数，默认 24]
凭据：~/.config/dify-zcode.env 的 LANGFUSE_PK/SK/HOST（dify_kit SSOT）。

注意（2026-09-20 实测）：
- 新组织 legacy 查询 API（/api/public/traces）已 410，只能用 /api/public/v2/observations
- 索引有分钟级延迟；Dify 侧 trace 在 workflow 跑完才整批可见（unified_trace 事后构建）
- serve 侧 trace 的 traceId 可算：sha256("serve-"+run_id)[:32]
"""
import collections
import datetime
import os
import statistics
import sys

import requests

SSOT = os.path.expanduser("~/.config/dify-zcode.env")
for line in open(SSOT):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

PK, SK = os.environ["LANGFUSE_PK"], os.environ["LANGFUSE_SK"]
HOST = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24

now = datetime.datetime.now(datetime.timezone.utc)
frm = (now - datetime.timedelta(hours=HOURS)).isoformat().replace("+00:00", "Z")
to = now.isoformat().replace("+00:00", "Z")

obs: list[dict] = []
cursor = None
while True:
    p = {"fromStartTime": frm, "toStartTime": to, "limit": 500}
    if cursor:
        p["cursor"] = cursor
    r = requests.get(f"{HOST}/api/public/v2/observations", auth=(PK, SK), params=p, timeout=60)
    r.raise_for_status()
    d = r.json()
    obs += d.get("data", [])
    cursor = d.get("meta", {}).get("cursor")
    if not cursor or not d.get("data"):
        break

print(f"窗口: 近 {HOURS}h | 观测总数: {len(obs)}")

# Dify workflow trace（根 span 名 workflow）
wf = [o for o in obs if o.get("name") == "workflow" and not o.get("parentObservationId")]
print(f"Dify workflow run: {len(wf)} 个")

# serve 侧 trace（root 名是 url，带 serve- 特征由 metadata run_id 区分；这里按 span 名归）
serve_stages = [o for o in obs if (o.get("name") or "").startswith("stage:")]
serve_entries = [o for o in obs if (o.get("name") or "").startswith("entry ")]
if serve_stages:
    print(f"serve 阶段 span: {len(serve_stages)} 个 | 条目 span: {len(serve_entries)} 个")

# 错误分布
errs = [o for o in obs if o.get("level") == "ERROR"]
print(f"ERROR 节点: {len(errs)}")
groups = collections.Counter((o.get("name"), (o.get("statusMessage") or "")[:60]) for o in errs)
for (name, msg), n in groups.most_common(10):
    print(f"  {n:>3}x {name} | {msg}")

# LLM 代际
gens = [o for o in obs if o.get("type") == "GENERATION"]
if gens:
    print(f"LLM 调用: {len(gens)} 次（注：Dify unified_trace 的 usage 在 metadata，v2 API 不回显）")

# 耗时（serve 阶段）
lat = [o["latency"] for o in serve_stages if o.get("latency")]
if lat:
    lat.sort()
    n = len(lat)
    print(f"serve 阶段耗时: avg {statistics.mean(lat):.0f}s p50 {lat[n//2]:.0f}s max {lat[-1]:.0f}s")

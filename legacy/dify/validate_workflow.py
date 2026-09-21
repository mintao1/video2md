#!/usr/bin/env python3
"""校验 视频转文档-workflow.yml：yaml 解析、node/edge id 引用、变量选择器不悬空、loop 结构一致。

python3 dify/validate_workflow.py
"""
import re
import sys
from pathlib import Path

import yaml

F = Path(__file__).parent / "视频转文档-workflow.yml"
doc = yaml.safe_load(F.read_text())
graph = doc["workflow"]["graph"]
nodes = {n["id"]: n for n in graph["nodes"]}
edges = graph["edges"]

errors = []

# 1. 节点 id 唯一
if len(nodes) != len(graph["nodes"]):
    errors.append(f"节点 id 重复：{len(nodes)} 唯一 / {len(graph['nodes'])} 总数")

# 2. 边引用存在的节点
for e in edges:
    for side in ("source", "target"):
        nid = e[side]
        if nid not in nodes:
            errors.append(f"边 {e['id']} 引用不存在的节点 {nid}")

# 3. 边 id 唯一
eids = [e["id"] for e in edges]
if len(eids) != len(set(eids)):
    dup = {x for x in eids if eids.count(x) > 1}
    errors.append(f"边 id 重复：{dup}")

# 4. loop 结构：start_node_id 指向 loop-start；loop-start 的 parentId 指回 loop；内部节点 loop_id 一致
loop_nodes = {nid: n for nid, n in nodes.items() if n["data"].get("type") == "loop"}
for nid, n in loop_nodes.items():
    start_id = n["data"].get("start_node_id")
    if not start_id or start_id not in nodes or nodes[start_id]["data"].get("type") != "loop-start":
        errors.append(f"loop {nid} 的 start_node_id {start_id} 无效或不是 loop-start")
    else:
        if nodes[start_id].get("parentId") != nid:
            errors.append(f"loop-start {start_id} 的 parentId 不是 {nid}")
    inner = [e for e in edges if e.get("data", {}).get("isInLoop") and e.get("data", {}).get("loop_id") == nid]
    for e in inner:
        for side in ("source", "target"):
            t = nodes[e[side]]
            if t.get("parentId") != nid and t["data"].get("type") != "loop-start":
                errors.append(f"loop {nid} 内部边 {e['id']} 的 {side} {e[side]} 不属于该 loop")

# 5. 变量选择器引用不悬空：data.variables[].value_selector[0] / prompt/url/body 里的 {{#node.var#}}
sel_re = re.compile(r"\{\{#([\w-]+)\.([\w-]+)#\}\}")
all_vars = {}
for nid, n in nodes.items():
    for v in n["data"].get("variables") or []:
        vs = v.get("value_selector") or []
        if vs and vs[0] not in nodes:
            errors.append(f"节点 {nid} 变量 {v.get('variable')} 选择器指向不存在节点 {vs[0]}")
        all_vars.setdefault(vs[0] if vs else "", []).append(vs[1] if len(vs) > 1 else "")

def check_template(node_id, text, where):
    for m in sel_re.finditer(text or ""):
        src, var = m.group(1), m.group(2)
        if src not in nodes:
            errors.append(f"节点 {node_id} {where} 引用不存在节点 {src}")
            continue
        d = nodes[src]["data"]
        if d.get("type") == "llm":
            continue  # llm 节点隐式输出 text/finish_reason 等，DSL 里不列 outputs
        if d.get("type") == "loop":
            # loop 节点对外暴露的是 loop_variables（label），不是 outputs
            ok = var in {lv.get("label") for lv in (d.get("loop_variables") or [])}
        elif src == "start_node":
            ok = any(vv.get("variable") == var for vv in d["variables"])
        elif var == "body":  # http-request 的 body 字段
            ok = True
        else:
            ok = var in {k for k in (d.get("outputs") or {})}
        if not ok:
            errors.append(f"节点 {node_id} {where} 引用 {src}.{var}，但该节点无此输出")

for nid, n in nodes.items():
    d = n["data"]
    t = d.get("type")
    if t == "http-request":
        check_template(nid, d.get("url", ""), "url")
        check_template(nid, d.get("body", {}).get("data") if isinstance(d.get("body"), dict) else "", "body")
    elif t == "llm":
        for p in d.get("prompt_template") or []:
            check_template(nid, p.get("text", ""), f"prompt[{p.get('role')}]")
    elif t == "code":
        for v in d.get("variables") or []:
            vs = v.get("value_selector") or []
            if vs and vs[0] in nodes:
                pass  # 已在上方校验

# 6. loop break 条件必须读 loop 自有变量
#    （Dify checklist 的 invalidVariable 规则：拿节点引用的变量去该节点的 availableVars 里找，
#     loop 节点的作用域里没有 loop 内部节点 → 直接引 code_progress_x.done_flag 会被判非法；
#     正解 = assigner 每轮写 loop 变量，break 读 [loop_id, 变量名]）
for nid, n in loop_nodes.items():
    labels = {lv.get("label") for lv in (n["data"].get("loop_variables") or [])}
    for bc in n["data"].get("break_conditions") or []:
        vs = bc.get("variable_selector") or []
        if len(vs) < 2:
            errors.append(f"loop {nid} break 条件变量选择器为空/不完整：{vs}")
            continue
        if vs[0] != nid:
            errors.append(f"loop {nid} break 条件引用了 {vs[0]}（应为 loop 自身变量 {nid}.<变量名>）")
        elif vs[1] not in labels:
            errors.append(f"loop {nid} break 条件引用 loop 变量 {vs[1]}，但 loop_variables 里没定义")

# 7. loop_count 必须是 1..100 的整数（Dify LOOP_NODE_MAX_COUNT）
LOOP_NODE_MAX_COUNT = 100
for nid, n in loop_nodes.items():
    lc = n["data"].get("loop_count")
    if not isinstance(lc, int) or not (1 <= lc <= LOOP_NODE_MAX_COUNT):
        errors.append(f"loop {nid} 的 loop_count={lc} 不在 1..{LOOP_NODE_MAX_COUNT}")

# 8. end 节点输出变量名全局唯一（Dify checklist 硬性要求，跨 end 节点查重）
seen_out: dict[str, str] = {}
for nid, n in nodes.items():
    if n["data"].get("type") != "end":
        continue
    for o in n["data"].get("outputs") or []:
        name = o.get("variable")
        if name in seen_out:
            errors.append(f"end 节点输出变量重名：{name}（{seen_out[name]} 与 {nid}）")
        else:
            seen_out[name] = nid

# 9. 每个 loop 至少一个内边（有轮询体）
for nid, n in loop_nodes.items():
    inner = [e for e in edges if e.get("data", {}).get("loop_id") == nid]
    if not inner:
        errors.append(f"loop {nid} 无内部边")

if errors:
    print(f"❌ 校验失败（{len(errors)}）")
    for e in errors[:40]:
        print("  -", e)
    sys.exit(1)
print(f"✅ YAML 校验通过：{len(nodes)} 节点 / {len(edges)} 边 / {len(loop_nodes)} 个 loop")

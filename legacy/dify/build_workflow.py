#!/usr/bin/env python3
"""生成 视频转文档-workflow.yml（全阶段编排版）。

本地管线拆成 8 个阶段（download/transcribe/frames/ocr/select/clips/note/kb），
每个阶段 = 1 个 POST 节点 + 1 个轮询 loop（loopstart→wait→status→progress→assigner）。
选帧决策由工作流内 LLM 节点对照转录本内容做出；改写只准嵌入「已选中帧」清单。
「url 空」分支保留原兼容链（查最新任务 / 填 transcript 直接改写）。

改完重新生成：python3 dify/build_workflow.py
"""
from pathlib import Path

OUT = Path(__file__).parent / "视频转文档-workflow.yml"

HOST = "http://host.docker.internal:8790"
LOOP_COUNT = 100    # Dify 1.17 loop 硬限（兜底）；轮询窗自适应后正常不会触到
# 轮询窗自适应（2026-09-21）：间隔与退出都听服务端的——阶段快照带 wait_hint/budget_s/
# over_budget（serve._adaptive：预算 = 阶段基础开销 + Σ条目按时长折算；hint=budget/80，
# 100 轮覆盖 ≈1.25×预算 < 1.5×grace 熔断线，超预算一定先于轮次耗尽显式失败）。
# 取代旧的固定 STAGE_WAIT（55s×100≈95min 一刀切：短视频空等、长合集不够用）。
WAIT_INIT = 5       # 首轮间隔（job 还没建，快照未回，先小步快轮）

STAGES = ["download", "transcribe", "frames", "ocr", "select", "clips", "note", "kb"]

# 每阶段的 POST body（json 模板字符串；{} 表示空 body）
# note 不在其中：note 由「逐条目循环」单独驱动（每条目一次 POST /note，带 entry 指定目标）
STAGE_BODY = {
    "download": "{}",
    "transcribe": "{}",
    "frames": '{"max_frames": "{{#start_node.max_frames#}}"}',
    "ocr": "{}",
    "select": "{{#code_parse_select.payload#}}",
    "clips": "{}",
    "kb": "{}",
}

# 阶段 job 的轮询响应（POST /runs/{id}/{stage} 幂等去重后回带快照）：
# {job_id, status, progress, note, error, wait_hint, budget_s, elapsed_s, over_budget}
# v2 修复「异步入队」三漏洞：① done_flag 只在 status==done 置真（error 不再算完成）
# ② error/服务级错误快照 → fail_flag 显式带出（loop 看到即停 + loop 后 if 门拦下）
# ③ 响应解析失败按连续计数，5 连才 fail（单次网络抖动不误杀，但也不无限等）
# v3 轮询窗自适应：wait_hint 透传服务端建议间隔（写回 loop 变量供下一轮 /wait 用）；
# over_budget（执行时长 > 预算×grace，预算按条目数×时长折算）→ fail 显式终止，
# 取代「固定间隔×100 轮」的统一窗口。解析失败期 wait_hint 指数退避，不空转烧轮次。
CODE_PROGRESS = """import json

ICON = {"queued": "⏳", "running": "🔄", "done": "✅", "error": "❌", "none": "❔"}


def _hint(v, fallback):
    try:
        return max(1, min(60, int(str(v))))
    except Exception:
        return fallback


def main(resp: str, poll_fails: str, prev_hint: str) -> dict:
    try:
        n = int(str(poll_fails or "0"))
    except Exception:
        n = 0
    prev = _hint(prev_hint, 5)
    try:
        j = json.loads(resp or "{}")
    except Exception:
        j = None
    if isinstance(j, dict) and "error" in j and "status" not in j:
        # 服务级错误快照（404/409 PRECONDITION 等）：不是轮询抖动，立即判失败
        return {"progress_text": f"❌ {str(j.get('error'))[:180]}",
                "done_flag": "false", "fail_flag": "true", "poll_fails": "0",
                "wait_hint": str(prev)}
    if not isinstance(j, dict) or "status" not in j:
        n += 1
        return {"progress_text": f"⚠️ 阶段响应无法解析（连续 {n} 次，5 连判失败）",
                "done_flag": "false",
                "fail_flag": "true" if n >= 5 else "false",
                "poll_fails": str(n),
                "wait_hint": str(min(60, prev * 2))}
    hint = _hint(j.get("wait_hint"), prev)
    st = j.get("status", "none")
    if j.get("error"):
        st = "error"
    text = f"{ICON.get(st, st)} {j.get('progress') or j.get('stage') or '排队中'}"
    if j.get("budget_s") and st == "running":
        text += f"（已用 {j.get('elapsed_s', 0)}s / 自适应预算 {j['budget_s']}s）"
    if j.get("note"):
        text += f"\\n📝 {j['note']}"
    if j.get("selfcheck"):
        text += f"\\n🔎 检索自检 {j['selfcheck']}"
    if j.get("error"):
        text += f"\\n⚠️ {str(j['error'])[:150]}"
    if j.get("over_budget"):
        return {"progress_text": text + (
                    f"\\n⏰ 执行 {j.get('elapsed_s', 0)}s 超自适应预算 "
                    f"{j.get('budget_s', '?')}s×grace，判失败（修根因后重发工作流可续跑）"),
                "done_flag": "false", "fail_flag": "true", "poll_fails": "0",
                "wait_hint": str(hint)}
    return {"progress_text": text,
            "done_flag": "true" if st == "done" else "false",
            "fail_flag": "true" if st == "error" else "false",
            "poll_fails": "0",
            "wait_hint": str(hint)}
"""

# 预检（serve /preflight：ffmpeg/磁盘/vault/算力池）——开跑前把「全局必炸」暴露出来
CODE_PREFLIGHT = """import json


def main(resp: str) -> dict:
    try:
        j = json.loads(resp or "{}")
    except Exception:
        return {"ok": "false", "text": "⚠️ 预检无响应（serve 未起？先 uv run video2md.py serve）"}
    if j.get("ok"):
        names = "、".join(c.get("name", "?") for c in j.get("checks", []) if c.get("ok"))
        return {"ok": "true", "text": f"✅ 预检通过：{names}"}
    bad = [f"- {c.get('name')}: {c.get('detail', '')}" for c in j.get("checks", []) if not c.get("ok")]
    return {"ok": "false", "text": "❌ 预检未过：\\n" + "\\n".join(bad)}
"""

# 阶段失败终止器：以异常把工作流打成 failed（调用方看得见 status，而不是静默产出垃圾）
# __STAGE__ 在生成期替换成阶段名（code 节点入参只有变量选择器，常量直接烧进代码更省一个输入）
CODE_STAGE_FAIL = """def main(progress: str) -> dict:
    raise RuntimeError(
        "阶段 __STAGE__ 未成功，终止工作流（阶段 error / 超自适应预算 / 连续解析失败）。"
        "修根因后重新发起即可：serve 对进行中/已完成阶段幂等去重，会从断点续跑。"
        "最后状态：" + (progress or "")[-200:])
"""

CODE_CLEAN_URL = r"""import json, re

def main(url: str, part_items: str, force: str) -> dict:
    # 清洗链接：剥分享文本取 URL；抖音弹窗链接（modal_id）归一化为 /video/<id>
    # body 整体由 json.dumps 拼，杜绝链接里的引号/中文打碎 JSON
    u = (url or "").strip()
    m = re.search(r"https?://[^\s\u4e00-\u9fff\"'「」『』<>，。；]+", u)
    if m:
        u = m.group(0)
    m = re.search(r"douyin\.com/[^\s?#]*\?(?:[^#\s]*&)?modal_id=(\d+)", u)
    if m:
        u = f"https://www.douyin.com/video/{m.group(1)}"
    # force=on 时全量重跑（serve 端 create_run 不标 skipped，覆盖 state 幂等）
    f = (force or "").strip().lower() in ("on", "true", "1", "yes")
    body = json.dumps({"url": u, "part_items": (part_items or "").strip(), "force": f},
                      ensure_ascii=False)
    return {"url": u, "body": body}
"""

CODE_FMT_RUN = """import json


def main(resp: str) -> dict:
    try:
        j = json.loads(resp or "{}")
    except Exception:
        return {"run_id": "", "total": "0", "text": "⚠️ 建 run 服务响应异常：" + (resp or "空")[:200]}
    if j.get("error"):
        return {"run_id": "", "total": "0", "text": f"⚠️ {j['error']}"}
    if j.get("dedup"):
        return {"run_id": j.get("run_id", ""), "total": str(j.get("total") or 1),
                "text": f"♻️ 复用进行中的 run {j.get('run_id')}（{j.get('total')} 条目），继续推进阶段"}
    n_skip = sum(1 for e in j.get("entries") or [] if e.get("skipped"))
    txt = f"✅ run {j.get('run_id')}：{j.get('total')} 条"
    if n_skip:
        txt += f"（{n_skip} 条已在历史 state 完成，将跳过）"
    return {"run_id": j.get("run_id", ""), "total": str(j.get("total") or 1), "text": txt}
"""

CODE_ALLOWED_FRAMES = """import json
import re


def main(manifest: str) -> dict:
    # ① 只做「清单 → 每个条目的合法帧名表」，与 LLM 输出校验解耦（画布可见、可单独重跑）
    # http 节点的 body 是 _run_outputs 的整个 JSON（{"text": ...}）——先解包取纯文本，
    # 否则 \\S+\\.jpg 在 JSON 转义文本上会把 {"text":" 前缀吞进帧名，LLM 合法名全部对不上
    m = manifest or ""
    try:
        j = json.loads(m)
        if isinstance(j, dict) and isinstance(j.get("text"), str):
            m = j["text"]
    except Exception:
        pass
    # 按清单头「===== 条目 N/M：url =====」切分，取每个条目的合法帧名
    allowed = {}
    parts = re.split(r"===== 条目 (\\d+)/\\d+：", m)
    for i in range(1, len(parts), 2):
        try:
            allowed[str(int(parts[i]))] = re.findall(r"(\\S+\\.jpg)", parts[i + 1] or "")
        except Exception:
            continue
    if not allowed:  # 无分节头（单条目）→ 全清单都算 1 号条目
        allowed = {"1": re.findall(r"(\\S+\\.jpg)", m)}
    return {"allowed_json": json.dumps(allowed, ensure_ascii=False)}
"""

CODE_SELECT_BLOCKS = """import json
import re


def main(llm_out: str) -> dict:
    # ② 解析 LLM 输出（容忍 ```json 围栏）→ 保留原始「块」结构的候选帧
    # 保留块结构（而非拍平）是为了对齐旧语义：selections 模式下每个块各自去重
    try:
        raw = (llm_out or "").strip()
        raw = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", raw, flags=re.S).strip()
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    def fr(f):
        return {"name": (f.get("name") or "").strip(), "caption": (f.get("caption") or "")[:80]}

    sels = obj.get("selections") or []
    if sels:
        blocks = []
        for s in sels:
            try:
                idx = int(str(s.get("entry", "")).strip())
            except Exception:
                idx = 1
            blocks.append({"entry": idx, "frames": [fr(f) for f in (s.get("frames") or [])]})
        mode = "selections"
    else:
        blocks = [{"entry": 0, "frames": [fr(f) for f in (obj.get("frames") or [])]}]
        mode = "frames"
    return {"blocks_json": json.dumps(blocks, ensure_ascii=False), "mode": mode}
"""

CODE_SELECT_FILTER = """import json


def main(blocks_json: str, mode: str, allowed_json: str, total: str) -> dict:
    # ③ 按合法帧名表过滤（去重、限长图注）→ select 阶段的 POST payload
    try:
        blocks = json.loads(blocks_json or "[]")
    except Exception:
        blocks = []
    try:
        amap = json.loads(allowed_json or "{}")
    except Exception:
        amap = {}
    allowed = {}
    for k, v in (amap.items() if isinstance(amap, dict) else []):
        try:
            allowed[int(k)] = set(v)
        except Exception:
            continue
    total_n = int(total or "1")

    def filt(fr_list, pool):
        seen, fl = set(), []
        for f in fr_list or []:
            nm = (f.get("name") or "").strip()
            if nm and nm in pool and nm not in seen:
                seen.add(nm)
                fl.append({"name": nm, "caption": f.get("caption") or ""})
        return fl

    if mode == "selections":  # 多条目：每块独立过滤+去重（保留块结构对齐旧语义）
        out = [{"entry": b.get("entry", 1),
                "frames": filt(b.get("frames"), allowed.get(b.get("entry", 1), set()))}
               for b in blocks]
        out = [b for b in out if b["frames"]]
        payload = {"selections": out}
        n_sel = sum(len(x["frames"]) for x in out)
    else:  # 单条 frames 输出：池 = 条目1 的合法名；多条目场景取全体并集
        pool = allowed.get(1, set())
        if total_n > 1:
            pool = set().union(*allowed.values()) if allowed else set()
        fl = filt(blocks[0].get("frames") if blocks else [], pool)
        payload = {"selections": [{"entry": 1, "frames": fl}]} if total_n > 1 else {"frames": fl}
        n_sel = len(fl)
    note = "" if n_sel else "⚠️ LLM 未选出合法帧名，select 阶段将走本地启发式兜底"
    return {"payload": json.dumps(payload, ensure_ascii=False),
            "n_selected": str(n_sel), "note": note}
"""

CODE_SPLIT_NOTES = """import json
import re


def main(body: str, review_body: str) -> dict:
    # ① 选稿 + 分篇：校对稿有效（≥50 字）就用校对稿，否则用原稿；按 ===NOTE N=== 分隔行切篇
    src = (review_body or "").strip()
    if len(src) < 50:
        src = (body or "").strip()
    sections = []
    parts = re.split(r"^===NOTE\\s+(\\d+)\\s*===$", src, flags=re.M)
    for i in range(1, len(parts), 2):
        try:
            sections.append({"entry": int(parts[i]), "text": (parts[i + 1] or "").strip()})
        except Exception:
            continue
    if not sections:  # 单条目（无分隔行）→ 整篇算 1 号条目
        sections = [{"entry": 1, "text": src}]
    return {"sections_json": json.dumps(sections, ensure_ascii=False),
            "n_notes": str(len(sections))}
"""

CODE_PARSE_META = """import json
import re


def main(meta_raw: str) -> dict:
    # ② 元数据归一：单条目对象或 {"entries": [...]} → {条目号: {title,tags,summary}}（JSON 字符串）
    meta = {}
    try:
        cleaned = re.sub(r"```(json)?|```", "", (meta_raw or "{}").strip()).strip()
        m = json.loads(cleaned)
        if isinstance(m, dict):
            items = m["entries"] if isinstance(m.get("entries"), list) else [m]
            for em in items:
                if not isinstance(em, dict):
                    continue
                try:
                    idx = int(str(em.get("entry") or 1))
                except Exception:
                    idx = 1
                meta[str(idx)] = {"title": str(em.get("title", ""))[:100],
                                  "tags": [str(t).strip() for t in (em.get("tags") or []) if str(t).strip()][:8],
                                  "summary": str(em.get("summary", ""))[:300]}
    except Exception:
        pass
    return {"meta_json": json.dumps(meta, ensure_ascii=False)}
"""

CODE_AGG_BUILD = """import json


def main(sections_json: str, meta_json: str) -> dict:
    # ③ 组装：分篇正文 + 条目元数据 → notes（写盘用）+ 首篇 body/meta（旧链路出口）
    try:
        sections = json.loads(sections_json or "[]")
    except Exception:
        sections = []
    try:
        meta = json.loads(meta_json or "{}")
    except Exception:
        meta = {}
    notes = []
    for s in (sections if isinstance(sections, list) else []):
        if not isinstance(s, dict):
            continue
        try:
            idx = int(s.get("entry") or 1)
        except Exception:
            idx = 1
        md = dict((meta or {}).get(str(idx)) or {"title": "", "tags": [], "summary": ""})
        if not md.get("title"):
            md["title"] = f"视频笔记 {idx}"
        notes.append({"entry": idx, "body": s.get("text", "") or "", "meta": md})
    if not notes:
        notes = [{"entry": 1, "body": "", "meta": {"title": "", "tags": [], "summary": ""}}]
    first = notes[0]
    return {"notes_json": json.dumps(notes, ensure_ascii=False),
            "note_body": json.dumps({"notes": notes}, ensure_ascii=False),
            "body": first["body"], "meta": json.dumps(first["meta"], ensure_ascii=False),
            "n_notes": str(len(notes))}
"""

# ---------------------------------------------------------------- 逐条目写盘（A 方案）
CODE_ENTRY_NOTE = """import json
import re


def main(idx: str, organize: str, review: str, meta_raw: str) -> dict:
    # 单条目写盘 payload：正文取校对稿（≥50 字算有效），元数据归一成单条目对象
    body = (review or "").strip()
    if len(body) < 50:
        body = (organize or "").strip()
    meta = {}
    try:
        m = json.loads(re.sub(r"```(json)?|```", "", (meta_raw or "{}").strip()).strip())
        if isinstance(m, dict):
            meta = {"title": str(m.get("title") or "")[:100],
                    "tags": [str(t).strip() for t in (m.get("tags") or []) if str(t).strip()][:8],
                    "summary": str(m.get("summary") or "")[:300]}
    except Exception:
        meta = {}
    return {"body": body,
            "title": meta.get("title") or f"视频笔记 {idx}",
            "post_body": json.dumps({"entry": str(idx), "body": body, "meta": meta},
                                    ensure_ascii=False)}
"""

CODE_NOTE_NEXT = """def main(idx: str, total: str, resp: str) -> dict:
    # 下一轮条目号 + 退出标志（break 只认 loop 自有变量，故经 assigner 写回 done_flag）
    try:
        i = int(str(idx).strip() or "1")
    except Exception:
        i = 1
    try:
        n = int(str(total).strip() or "1")
    except Exception:
        n = 1
    wrote = '"status": "done"' in (resp or "") or '"status":"done"' in (resp or "")
    return {"next_idx": str(i + 1),
            "done_flag": "true" if i >= n else "false",
            "progress_text": f"note 阶段 · 条目 {i}/{n}" + ("" if wrote else "（跳过/未写）")}
"""

# 转录本端点回带 note_done（该条目已写盘）→ 幂等：已写盘条目跳过本条目全部 LLM（整理/校对/元数据）
# skipped（state 幂等命中）同样视同已写盘：本 run 的 artifacts 没有 note 记录，
# 但 state 里已完成——不视同跳过会把空转录本喂给 LLM 白烧 3 次调用/条（实测 17P 合集全跳过仍烧 25min）
CODE_ENTRY_STATE = """import json


def main(resp: str) -> dict:
    try:
        j = json.loads(resp or "{}")
    except Exception:
        j = {}
    return {"note_done": "true" if (j.get("note_done") or j.get("skipped")) else "false"}
"""


# ---------------------------------------------------------------- 节点构建
nodes, edges = [], []
nodes_by_id = {}

# —— 布局规范(Dify 1.17 实测,详见 video2md.py README「坑」)——
# 容器(loop)子节点的 position 是**相对容器左上角**的坐标,不是画布绝对坐标;
# 生成器全程用画布绝对坐标书写,落盘前由 normalize_layout() 统一换算并回填容器尺寸。
NODE_W = 244
NODE_H = {"start": 90, "end": 90, "http-request": 88, "code": 64, "llm": 98,
          "if-else": 126, "assigner": 64, "loop-start": 54}
LOOP_PAD = {"right": 16, "bottom": 20}   # 与 Dify LOOP_PADDING 同口径
LOOP_GAP = 60          # 容器之间的水平间距
STEP = 300             # 容器内子节点间距(Dify NODE_WIDTH+X_OFFSET 标准值)
STAGE_LOOP_W = 24 + 4 * STEP + NODE_W + LOOP_PAD["right"]   # 1484:loopstart+4 节点单行
NOTE_LOOP_X = 10668    # 6 个阶段 loop 之后(clips 右缘 10608 + 60)
NOTE_LOOP_W = 324 + 5 * STEP + NODE_W + LOOP_PAD["right"]   # 2084:note 两行,第二行 6 节点(324 起)


def normalize_layout(nodes):
    """把生成器的画布绝对坐标换算成 Dify 的导入语义:
    ① 容器子节点 position → 相对容器左上角(positionAbsolute 保留绝对坐标);
    ② 按子节点外包络回填容器 width/height(不让 Dify 导入时按相对偏移把框撑爆);
    ③ 普通节点补齐 width/height(仅布局提示,前端按 w-fit 自渲染,这里统一口径)。"""
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        n.setdefault("width", NODE_W)
        n.setdefault("height", NODE_H.get(n["data"].get("type"), 88))
        n.setdefault("positionAbsolute", dict(n["position"]))
    for n in nodes:
        p = by_id.get(n.get("parentId") or "")
        if not p:
            continue
        n["positionAbsolute"] = dict(n["position"])
        n["position"] = {"x": n["position"]["x"] - p["position"]["x"],
                         "y": n["position"]["y"] - p["position"]["y"]}
    for n in nodes:
        if n["data"].get("type") not in ("loop", "iteration"):
            continue
        children = [c for c in nodes if c.get("parentId") == n["id"]]
        if not children:
            continue
        n["width"] = max(c["position"]["x"] + c["width"] for c in children) + LOOP_PAD["right"]
        n["height"] = max(c["position"]["y"] + c["height"] for c in children) + LOOP_PAD["bottom"]


def node(nid, ntype, title, position, data_extra=None, parent=None, is_in_loop=False, loop_id=None):
    d = {"type": ntype, "title": title}
    if data_extra:
        d.update(data_extra)
    n = {"id": nid, "type": "custom", "position": {"x": position[0], "y": position[1]}, "data": d}
    if parent or (is_in_loop and loop_id):
        n["parentId"] = parent or loop_id
    if is_in_loop:
        n["draggable"] = False
        n["selectable"] = False
    nodes.append(n)
    nodes_by_id[nid] = n
    return nid


def edge(eid, src, sh, dst, th, loop_id=None):
    data = {"sourceType": nodes_by_id.get(src, {}).get("data", {}).get("type"),
            "targetType": nodes_by_id.get(dst, {}).get("data", {}).get("type")}
    if loop_id:
        data["isInLoop"] = True
        data["loop_id"] = loop_id
    e = {"id": eid, "source": src, "sourceHandle": sh, "target": dst,
         "targetHandle": th, "type": "custom", "data": data}
    if loop_id:
        e["zIndex"] = 1002
    edges.append(e)
    return eid


def http_node(nid, title, method, url, body_type="json", body_data="{}", headers="Content-Type:application/json",
              read=30, loop_id=None, x=0, y=0):
    d = {"type": "http-request", "title": title, "method": method, "url": url,
         "authorization": {"type": "no-auth", "config": None},
         "headers": headers if headers else "",
         "params": "",
         "body": {"type": body_type, "data": body_data},
         "timeout": {"connect": 10, "read": read, "write": 10},
         "ssl_verify": True}
    extra = {}
    if loop_id:
        extra = {"isInLoop": True, "loop_id": loop_id}
    return node(nid, "http-request", title, (x, y), {**d, **extra},
                is_in_loop=bool(loop_id), loop_id=loop_id)


def code_node(nid, title, code, outputs, variables, loop_id=None, x=0, y=0):
    d = {"type": "code", "title": title, "code_language": "python3", "code": code,
         "outputs": outputs, "variables": variables}
    extra = {}
    if loop_id:
        extra = {"isInLoop": True, "loop_id": loop_id}
    return node(nid, "code", title, (x, y), {**d, **extra}, is_in_loop=bool(loop_id), loop_id=loop_id)


def llm_node(nid, title, system, user, temperature=0.4, max_tokens=4096, x=0, y=0, loop_id=None):
    d = {"type": "llm", "title": title,
         "model": {"provider": "langgenius/openai_api_compatible/openai_api_compatible",
                   "name": "Kimi-K3", "mode": "chat",
                   "completion_params": {"temperature": temperature,
                                         **({"max_tokens": max_tokens} if max_tokens else {})}},
         "prompt_template": [{"role": "system", "text": system}, {"role": "user", "text": user}],
         "context": {"enabled": False, "variable_selector": []},
         "vision": {"enabled": False},
         # 上游中转偶发挂起（实测单次 600s 撞 PLUGIN_MAX_EXECUTION_TIMEOUT）：
         # 节点级重试 2 次、间隔 30s，把「一次挂起=整run失败」降级为「多等半分钟」
         "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 30000}}
    extra = {"isInLoop": True, "loop_id": loop_id} if loop_id else {}
    return node(nid, "llm", title, (x, y), {**d, **extra}, is_in_loop=bool(loop_id), loop_id=loop_id)


def loop_node(nid, title, x=0, y=0, idx_init: str | None = None):
    # break 条件必须读 loop 自有变量（assigner 每轮写入），不能直接引 loop 内节点输出：
    # checklist 校验时 loop 节点的 availableVars 里没有 loop 内部节点 → 判 Invalid variable
    # 退出二选一：done（status==done）或 fail（error/解析连炸）——error 绝不再当「完成」放行
    lvars = [{"id": f"var-{nid}-progress", "label": "progress",
              "value": "", "value_type": "constant", "var_type": "string"},
             {"id": f"var-{nid}-doneflag", "label": "done_flag",
              "value": "", "value_type": "constant", "var_type": "string"},
             {"id": f"var-{nid}-failflag", "label": "fail_flag",
              "value": "false", "value_type": "constant", "var_type": "string"},
             {"id": f"var-{nid}-pollfails", "label": "poll_fails",
              "value": "0", "value_type": "constant", "var_type": "string"},
             # 服务端建议的下一轮轮询间隔（自适应窗）；/wait?seconds= 直接消费
             {"id": f"var-{nid}-waithint", "label": "wait_hint",
              "value": str(WAIT_INIT), "value_type": "constant", "var_type": "string"}]
    if idx_init is not None:  # 逐条目循环：条目号从 1 起
        lvars.insert(0, {"id": f"var-{nid}-idx", "label": "idx",
                         "value": idx_init, "value_type": "constant", "var_type": "string"})
    d = {"type": "loop", "title": title, "start_node_id": nid + "start",
         "loop_count": LOOP_COUNT, "logical_operator": "or",
         "break_conditions": [{"id": f"cond-{nid}-done",
                               "variable_selector": [nid, "done_flag"],
                               "comparison_operator": "is", "value": "true",
                               "varType": "string"},
                              {"id": f"cond-{nid}-fail",
                               "variable_selector": [nid, "fail_flag"],
                               "comparison_operator": "is", "value": "true",
                               "varType": "string"}],
         "loop_variables": lvars}
    return node(nid, "loop", title, (x, y), d)


def loopstart_node(nid, loop_parent, x=0, y=0):
    n = {"id": nid + "start", "type": "custom-loop-start", "parentId": loop_parent,
         "draggable": False, "selectable": False,
         "position": {"x": x, "y": y}, "data": {"type": "loop-start", "title": "",
                                                "isInLoop": True}}
    nodes.append(n)
    return nid + "start"


# —— start ——
start_vars = [
    {"variable": "url", "label": "视频链接（B站分P/合集、YouTube 播放列表、抖音、小红书；填了就全阶段跑）",
     "type": "text-input", "required": False, "max_length": 500},
    {"variable": "part_hint", "label": "分段提示（长视频分块并行改写时由管线注入；单次调用留空）",
     "type": "text-input", "required": False, "max_length": 500},
    {"variable": "part_items", "label": "指定条目（分P/合集/播放列表，如 1,3-5；留空=全部）",
     "type": "text-input", "required": False, "max_length": 100},
    {"variable": "transcript", "label": "转录本（旧链路内部调用用；UI 入口留空，全阶段流程自动转写）",
     "type": "paragraph", "required": False, "max_length": 200000},
    {"variable": "video_filename", "label": "视频文件名", "type": "text-input",
     "required": False, "max_length": 300},
    {"variable": "duration", "label": "时长", "type": "text-input", "required": False, "max_length": 20},
    {"variable": "review", "label": "是否校对(on/off)", "type": "text-input",
     "required": False, "max_length": 8},
    {"variable": "frames_manifest", "label": "帧清单(文件名/时间戳/OCR；旧链路用)", "type": "paragraph",
     "required": False, "max_length": 100000},
    {"variable": "max_frames", "label": "目标帧数（默认 60；vision 本地按 ×3 池化，上限 120）",
     "type": "text-input", "required": False, "max_length": 8},
    {"variable": "force", "label": "强制重跑（on=忽略 state 幂等全量重来；留空=幂等跳过已处理条目）",
     "type": "text-input", "required": False, "max_length": 8},
]
node("start_node", "start", "开始", (80, 760), {"variables": start_vars})
node("if_url", "if-else", "填了链接？", (380, 760),
     {"logical_operator": "and",
      "cases": [{"case_id": "true", "logical_operator": "and",
                 "conditions": [{"id": "cond-url-notempty",
                                 "variable_selector": ["start_node", "url"],
                                 "comparison_operator": "not empty", "value": ""}]}]})
edge("edge-start-ifurl", "start_node", "source", "if_url", "target")

# —— 新链路：全阶段编排 ——
code_node("code_clean_url", "清洗链接（剥分享文本/抖音归一化）", CODE_CLEAN_URL,
          {"url": {"type": "string"}, "body": {"type": "string"}},
          [{"variable": "url", "value_selector": ["start_node", "url"]},
           {"variable": "part_items", "value_selector": ["start_node", "part_items"]},
           {"variable": "force", "value_selector": ["start_node", "force"]}], x=500, y=260)
node("http_post_runs", "http-request", "① 建 run（解析并展开条目）", (800, 260),
     {"method": "post",
      "url": f"{HOST}/runs",
      "authorization": {"type": "no-auth", "config": None},
      # X-Dify-Workflow-Run-Id：把 Dify 的 run id 下发给 serve，
      # serve 落进 run 记录 → Langfuse 里 Dify trace 与 serve trace 可互跳
      "headers": "Content-Type:application/json\nX-Dify-Workflow-Run-Id:{{#sys.workflow_run_id#}}",
      "params": "",
      # 整体模板：body 由清洗节点 json.dumps 拼，链接含引号/中文也不会打碎 JSON
      "body": {"type": "json", "data": "{{#code_clean_url.body#}}"},
      # /runs 同步做 URL 展开（抖音分享页抓取可能 70s+），读超时必须放宽
      "timeout": {"connect": 10, "read": 300, "write": 30}, "ssl_verify": True})
code_node("code_fmt_run", "② 格式化 run 回执", CODE_FMT_RUN,
          {"run_id": {"type": "string"}, "total": {"type": "string"}, "text": {"type": "string"}},
          [{"variable": "resp", "value_selector": ["http_post_runs", "body"]}], x=1100, y=260)
# 建 run 闸门：/runs 失败（解析失败/链接无效）时 run_id 为空——历史 bug 是空 run_id 继续走
# /runs//download 撞 404「not found」，真实错误被噪声盖掉；这里显式 fail-fast，报错即真相
node("if_run_ok", "if-else", "run 建好了？", (1300, 120),
     {"logical_operator": "and",
      "cases": [{"case_id": "true", "logical_operator": "and",
                 "conditions": [{"id": "cond-run-ok",
                                 "variable_selector": ["code_fmt_run", "run_id"],
                                 "comparison_operator": "not empty", "value": ""}]}]})
code_node("code_fail_run", "建 run 失败即终止",
          CODE_STAGE_FAIL.replace("__STAGE__", "建 run"),
          {"never": {"type": "string"}},
          [{"variable": "progress", "value_selector": ["code_fmt_run", "text"]}],
          x=1300, y=400)
edge("edge-fmt-ifrunok", "code_fmt_run", "source", "if_run_ok", "target")
edge("edge-ifrunok-fail", "if_run_ok", "false", "code_fail_run", "target")
# 预检链：serve /preflight 体检（ffmpeg/磁盘/vault/算力池），不过则立即终止——
# 不建 run、不烧条目（历史事故：token 过期+无代理，transcribe 每条目白烧 5min × 24 条）
http_node("http_preflight", "⓪ 预检（代理/token/磁盘）", "get", f"{HOST}/preflight",
          headers="", read=30, x=820, y=440)
code_node("code_preflight", "解析预检", CODE_PREFLIGHT,
          {"ok": {"type": "string"}, "text": {"type": "string"}},
          [{"variable": "resp", "value_selector": ["http_preflight", "body"]}], x=1120, y=440)
node("if_preflight", "if-else", "预检过了？", (1420, 440),
     {"logical_operator": "and",
      "cases": [{"case_id": "true", "logical_operator": "and",
                 "conditions": [{"id": "cond-preflight-ok",
                                 "variable_selector": ["code_preflight", "ok"],
                                 "comparison_operator": "is", "value": "true"}]}]})
code_node("code_fail_preflight", "预检失败即终止",
          CODE_STAGE_FAIL.replace("__STAGE__", "preflight"),
          {"never": {"type": "string"}},
          [{"variable": "progress", "value_selector": ["code_preflight", "text"]}],
          x=1720, y=440)
edge("edge-ifurl-clean", "if_url", "true", "code_clean_url", "target")
edge("edge-clean-preflight", "code_clean_url", "source", "http_preflight", "target")
edge("edge-preflight-parse", "http_preflight", "source", "code_preflight", "target")
edge("edge-parse-ifpreflight", "code_preflight", "source", "if_preflight", "target")
edge("edge-ifpreflight-runs", "if_preflight", "true", "http_post_runs", "target")
edge("edge-ifpreflight-fail", "if_preflight", "false", "code_fail_preflight", "target")
edge("edge-runs-fmt", "http_post_runs", "source", "code_fmt_run", "target")

# —— 每个阶段：1 个 loop（内含 POST+轮询+解析；POST 幂等去重并回带当前状态）——
# note 阶段不在这条通用循环里：它改成逐条目循环（合集一次写 N 篇 → 每次只写 1 篇，见 loop_note）
# 布局:loop 单行 5 节点(start→wait→post→解析→写入),容器宽 STAGE_LOOP_W,容器间 LOOP_GAP;
# kb 是最后一环,它前面要插 loop_note(见下),故 x 单独算(NOTE_LOOP_X+NOTE_LOOP_W+GAP)
_stage_x = {}
_x = 1404
for _s in [x for x in STAGES if x not in ("note", "kb")]:
    _stage_x[_s] = _x
    _x += STAGE_LOOP_W + LOOP_GAP
_stage_x["kb"] = NOTE_LOOP_X + NOTE_LOOP_W + LOOP_GAP
for s in [x for x in STAGES if x != "note"]:
    lid = f"loop_{s}"
    base_x = _stage_x[s]
    loop_node(lid, f"{s} 阶段（POST+轮询）", x=base_x, y=200)
    ls = loopstart_node(lid, lid, x=base_x + 24, y=268)
    # 轮询间隔 = 服务端自适应 hint（快照的 wait_hint 字段，经 loop 变量回灌）；
    # read 固定 75s：hint 上限 60（/wait 端点 clamp），read 必须 > 服务端 sleep 才不整流超时
    http_node(f"http_wait_{s}", "轮询间隔（自适应·服务端 hint）", "get",
              f"{HOST}/wait?seconds={{{{#loop_{s}.wait_hint#}}}}", headers="",
              read=75,
              x=base_x + 324, y=268, loop_id=lid)
    http_node(f"http_post_{s}", f"触发/轮询 {s} 阶段", "post",
              f"{HOST}/runs/{{{{#code_fmt_run.run_id#}}}}/{s}",
              body_data=STAGE_BODY[s], x=base_x + 624, y=268, loop_id=lid)
    code_node(f"code_progress_{s}", "解析阶段状态", CODE_PROGRESS,
              {"progress_text": {"type": "string"}, "done_flag": {"type": "string"},
               "fail_flag": {"type": "string"}, "poll_fails": {"type": "string"},
               "wait_hint": {"type": "string"}},
              [{"variable": "resp", "value_selector": [f"http_post_{s}", "body"]},
               {"variable": "poll_fails", "value_selector": [lid, "poll_fails"]},
               {"variable": "prev_hint", "value_selector": [lid, "wait_hint"]}],
              loop_id=lid, x=base_x + 924, y=268)
    node(f"assign_progress_{s}", "assigner", "写入进度", (base_x + 1224, 268),
         {"type": "assigner", "title": "写入进度", "version": "2",
          "isInLoop": True, "loop_id": lid,
          "items": [{"variable_selector": [lid, "progress"], "input_type": "variable",
                     "operation": "over-write",
                     "value": [f"code_progress_{s}", "progress_text"]},
                    # done_flag/fail_flag 落 loop 自有变量：break 条件据此退出（每轮 assigner 后检查）
                    {"variable_selector": [lid, "done_flag"], "input_type": "variable",
                     "operation": "over-write",
                     "value": [f"code_progress_{s}", "done_flag"]},
                    {"variable_selector": [lid, "fail_flag"], "input_type": "variable",
                     "operation": "over-write",
                     "value": [f"code_progress_{s}", "fail_flag"]},
                    {"variable_selector": [lid, "poll_fails"], "input_type": "variable",
                     "operation": "over-write",
                     "value": [f"code_progress_{s}", "poll_fails"]},
                    # 自适应轮询窗：服务端 hint 写回 loop 变量，下一轮 /wait 消费
                    {"variable_selector": [lid, "wait_hint"], "input_type": "variable",
                     "operation": "over-write",
                     "value": [f"code_progress_{s}", "wait_hint"]}]},
         is_in_loop=True, loop_id=lid)
    edge(f"edge-{ls}-{lid}wait", ls, "source", f"http_wait_{s}", "target", loop_id=lid)
    edge(f"edge-{lid}wait-{lid}post", f"http_wait_{s}", "source", f"http_post_{s}", "target", loop_id=lid)
    edge(f"edge-{lid}post-{lid}prog", f"http_post_{s}", "source", f"code_progress_{s}", "target", loop_id=lid)
    edge(f"edge-{lid}prog-{lid}assign", f"code_progress_{s}", "source", f"assign_progress_{s}", "target", loop_id=lid)
    # —— loop 后闸门：done 才放行；error/上限耗尽/解析连炸 → code_fail 抛错，工作流 failed ——
    # （修「异步入队」：历史上限耗尽会静默前进到下一阶段，拿空产物硬跑）
    node(f"if_ok_{s}", "if-else", f"{s} 成功？", (base_x + 300, 700),
         {"logical_operator": "and",
          "cases": [{"case_id": "true", "logical_operator": "and",
                     "conditions": [{"id": f"cond-ok-{s}",
                                     "variable_selector": [lid, "done_flag"],
                                     "comparison_operator": "is", "value": "true"}]}]})
    code_node(f"code_fail_{s}", f"{s} 失败即终止",
              CODE_STAGE_FAIL.replace("__STAGE__", s),
              {"never": {"type": "string"}},
              [{"variable": "progress", "value_selector": [lid, "progress"]}],
              x=base_x + 600, y=700)
    edge(f"edge-{lid}-ifok", lid, "source", f"if_ok_{s}", "target")
    edge(f"edge-ifok-fail-{s}", f"if_ok_{s}", "false", f"code_fail_{s}", "target")

# 阶段间取数（链上 pass-through）+ 内容选帧 LLM
# 布局:取数/选帧链放在阶段 loop 行下方(y=460/620),不挤容器之间的窄缝
node("http_get_transcript", "http-request", "取双通道转录本", (4340, 460),
     {"method": "get", "url": f"{HOST}/runs/{{{{#code_fmt_run.run_id#}}}}/stages/transcript",
      "authorization": {"type": "no-auth", "config": None}, "headers": "", "params": "",
      "body": {"type": "none", "data": []},
      "timeout": {"connect": 10, "read": 60, "write": 10}, "ssl_verify": True})
node("http_get_candidates", "http-request", "取候选帧清单", (6276, 460),
     {"method": "get", "url": f"{HOST}/runs/{{{{#code_fmt_run.run_id#}}}}/stages/candidates",
      "authorization": {"type": "no-auth", "config": None}, "headers": "", "params": "",
      "body": {"type": "none", "data": []},
      "timeout": {"connect": 10, "read": 60, "write": 10}, "ssl_verify": True})
# 注：旧链路曾有 http_get_selected / http_get_transcript2 两个「整段取数」节点，已随
# 逐条目改写（loop_note 内按 entry 单条取）一并摘除。

# —— 脊柱布线（显式）：阶段 loop 顺序串，GET 取数插在对应阶段 loop 之后 ——
# 每个 loop 出来必经 if_ok_<stage> 闸门：done_flag==true 才放行下一阶段；
# 否则进 code_fail_<stage> 抛错（工作流 failed）。杜绝「loop 退了=阶段成了」的异步入队错觉。
node("if_ok_note", "if-else", "note 成功？", (NOTE_LOOP_X + 300, 700),
     {"logical_operator": "and",
      "cases": [{"case_id": "true", "logical_operator": "and",
                 "conditions": [{"id": "cond-ok-note",
                                 "variable_selector": ["loop_note", "done_flag"],
                                 "comparison_operator": "is", "value": "true"}]}]})
code_node("code_fail_note", "note 失败即终止",
          CODE_STAGE_FAIL.replace("__STAGE__", "note"),
          {"never": {"type": "string"}},
          [{"variable": "progress", "value_selector": ["loop_note", "progress"]}],
          x=NOTE_LOOP_X + 600, y=700)
edge("edge-loopnote-ifok", "loop_note", "source", "if_ok_note", "target")
edge("edge-ifoknote-fail", "if_ok_note", "false", "code_fail_note", "target")
edge("edge-runs-loopdl", "if_run_ok", "true", "loop_download", "target")
edge("edge-okdl-looptr", "if_ok_download", "true", "loop_transcribe", "target")
edge("edge-oktr-gettr", "if_ok_transcribe", "true", "http_get_transcript", "target")
edge("edge-gettr-loopfr", "http_get_transcript", "source", "loop_frames", "target")
edge("edge-okfr-loopocr", "if_ok_frames", "true", "loop_ocr", "target")
edge("edge-okocr-getcands", "if_ok_ocr", "true", "http_get_candidates", "target")
edge("edge-getcands-selectllm", "http_get_candidates", "source", "llm_select_frames", "target")
edge("edge-getcands-allowed", "http_get_candidates", "source", "code_manifest_allowed", "target")
edge("edge-selectllm-blocks", "llm_select_frames", "source", "code_select_blocks", "target")
edge("edge-allowed-blocks", "code_manifest_allowed", "source", "code_select_blocks", "target")
edge("edge-blocks-parsesel", "code_select_blocks", "source", "code_parse_select", "target")
edge("edge-parsesel-loopsel", "code_parse_select", "source", "loop_select", "target")
edge("edge-oksel-loopclips", "if_ok_select", "true", "loop_clips", "target")
edge("edge-okclips-loopnote", "if_ok_clips", "true", "loop_note", "target")
edge("edge-oknote-loopkb", "if_ok_note", "true", "loop_kb", "target")

SEL_SYSTEM = ("你是视频配帧选帧员。任务：对照「转录本」的口播/画面内容，从「候选帧清单」里选出这篇视频笔记"
              "要嵌入的帧（截图/图表/公式/幻灯片），让「讲什么就配什么图」。\n\n"
              "规则：\n"
              "1. 只能使用候选帧清单中出现的文件名，禁止编造清单外的名字。\n"
              "2. 选帧要跟转录本内容相关：优先选画面文字（OCR）与对应段落语音话题一致的帧；"
              "没有画面文字的候选帧在内容确实需要时可选（纯图/动画关键帧）。\n"
              "3. 每个话题/章节 1–2 张，总量不要超过候选清单行数的 60%，单条目最多 12 张。\n"
              "4. 输出 JSON，不要任何解释、不要代码块：\n"
              "   单条目：{\"frames\": [{\"name\": \"帧文件名\", \"caption\": \"一句话图注\"}]}\n"
              "   多条目（候选清单含 ==== 条目 N/M：url ==== 分节头）："
              "{\"selections\": [{\"entry\": N, \"frames\": [{\"name\": \"...\", \"caption\": \"...\"}]}]}\n"
              "   caption 用于笔记图注，一句话描述该帧画面内容及其与讲稿的关联。")
SEL_USER = ("转录本（[mm:ss] 时间戳；多条目以 ==== 条目 N/M：url ==== 分节）：\n\n"
            "{{#http_get_transcript.body#}}\n\n"
            "候选帧清单（文件名 [mm:ss] OCR: 画面文字；OCR 为 — 表示无画面文字）：\n\n"
            "{{#http_get_candidates.body#}}")
llm_node("llm_select_frames", "内容选帧（对照转录本选图）", SEL_SYSTEM, SEL_USER,
         temperature=0.2, max_tokens=4000, x=6576, y=460)
code_node("code_manifest_allowed", "解包清单→每条目合法帧名表", CODE_ALLOWED_FRAMES,
          {"allowed_json": {"type": "string"}},
          [{"variable": "manifest", "value_selector": ["http_get_candidates", "body"]}],
          x=6576, y=620)
code_node("code_select_blocks", "解析 LLM 选帧输出（保留块结构）", CODE_SELECT_BLOCKS,
          {"blocks_json": {"type": "string"}, "mode": {"type": "string"}},
          [{"variable": "llm_out", "value_selector": ["llm_select_frames", "text"]}],
          x=6876, y=460)
code_node("code_parse_select", "过滤 LLM 选帧（滤非法名→payload）", CODE_SELECT_FILTER,
          {"payload": {"type": "string"}, "n_selected": {"type": "string"},
           "note": {"type": "string"}},
          [{"variable": "blocks_json", "value_selector": ["code_select_blocks", "blocks_json"]},
           {"variable": "mode", "value_selector": ["code_select_blocks", "mode"]},
           {"variable": "allowed_json", "value_selector": ["code_manifest_allowed", "allowed_json"]},
           {"variable": "total", "value_selector": ["code_fmt_run", "total"]}],
          x=7176, y=460)

# —— 改写链（只准嵌已选中帧）——
ORG_SYSTEM = ("你是资深的技术文章撰稿人。任务：把带时间戳的双通道视频转录本，改写成一篇完整、流畅、"
              "忠实原意的中文文章（就像 3Blue1Brown 官方文稿那样——是\"文稿改写\"，不是要点摘抄）。\n\n"
              "输入：【语音】为口播内容；【画面】为 OCR 提取的画面文字（幻灯片标题、字幕、代码、图表文字）。\n\n"
              "改写规则：\n"
              "1. 忠实完整：按视频原叙事顺序展开，保留全部实质内容与论证过程，去掉口水词、重复、口误；"
              "允许把口语改成通顺书面语，但不得引入转录本之外的事实。禁止凭空补充外部知识。\n"
              "2. 篇幅：正文约为转录本信息量的 60%–100%，宁可长而完整，不要压缩成提纲。\n"
              "3. 章节：用 `##` 分章（按内容自然分段，3–9 章），复杂章内可用 `###` 小节。每个 `##` 标题末尾附时间戳，格式：`## 章节名（mm:ss）`。\n"
              "4. 关键句加粗：每章至少 1 处 `**加粗**` 标出核心论断。\n"
              "5. 插图：在讲对应内容的位置嵌入可用帧，格式两行：\n"
              "   ![[attachments/帧文件名]]\n"
              "   *斜体图注（一句话，描述该帧画面内容）*\n"
              "   只能使用「帧清单」中列出的文件名，一个文件名最多嵌入一次，禁止编造清单外的文件名；"
              "帧数量多时优先选画面文字与当章内容相关的。\n"
              "6. 脚注：对术语、背景、与主线无关的补充，用 `[^n]` 内联标记，并在文末逐条给出定义（`[^n]: 说明文字`）。\n"
              "7. callout：视频中有演示、互动、思考、注意事项的地方，用 `> [!example] 思考题` 或 `> [!warning] 注意` 引用块呈现；不要滥用。\n"
              "8. 【画面】文字与相邻【语音】融合理解；直接引用画面文字时在句末标注（画面）。带 ⚠ 的低置信段谨慎采用、语义不明时略过。\n"
              "9. 专有名词、数字、代码、命令保持原样；简体中文输出。\n"
              "10. 多条目：转录本以 `===== 条目 N/M：url =====` 分节时，每个条目各写一篇独立文章；"
              "文章之间用单独一行 `===NOTE N===`（N 为条目序号，对应分节头）分隔；"
              "单条目时直接输出正文，不要加分隔行。\n"
              "输出格式：Markdown 正文（不要一级标题、不要代码块包裹全文、不要 TL;DR、不要元数据 JSON——正文直接从引言段开始）。\n")
META_SYSTEM = ('从视频笔记正文中提取元数据。只输出一个 JSON 对象，不要任何解释、不要代码块：\n'
               '{"title": "笔记标题，20 字以内，不加书名号或结尾标点", '
               '"tags": ["3-8 个标签，含主题与领域词"], "summary": "60 字以内一句话摘要"}')
REV_SYSTEM = ('你是严格的事实校对员。对照转录本逐段检查这篇文稿改写：事实错误、无中生有的内容、错误的时间戳、'
              '遗漏的关键内容、与转录本矛盾的表述。\n\n'
              '保持文章的章节结构、加粗、`![[attachments/...]]` 图片嵌入、图注、脚注、callout '
              '原样不动，只修正文字内容。若无需修改，原样输出全文。不要输出任何解释。\n')

# —— 逐条目改写链（A 方案）——
# 旧做法：一次 LLM 调用写完整 run 的 N 篇（靠 LLM 吐 `===NOTE N===` 分篇）。
# 两个致命处：① N 篇塞进 max_tokens=16000 必然被压缩/截断（合集笔记「很薄」的根因）；
# ② 分篇编号靠 LLM 自觉，错位就出现「条目 A 拿到条目 B 的正文」的静默错配。
# 现在：loop_note 每个条目单独走 取单条目 → 整理正文 → 校对 → 元数据 → 组装 → 写盘（带 entry 定点）。
ORG_SYSTEM_ONE = ("你是资深的技术文章撰稿人。任务：把带时间戳的一段视频转录本改写成一篇完整、流畅、"
                  "忠实原意的中文文章（就像 3Blue1Brown 官方文稿那样——是\"文稿改写\"，不是要点摘抄）。\n\n"
                  "输入：【语音】为口播内容；【画面】为 OCR 提取的画面文字（幻灯片标题、字幕、代码、图表文字）。\n\n"
                  "改写规则：\n"
                  "1. 忠实完整：按原叙事顺序展开，保留全部实质内容与论证过程，去掉口水词、重复、口误；"
                  "允许把口语改成通顺书面语，但不得引入转录本之外的事实。禁止凭空补充外部知识。\n"
                  "2. 篇幅：正文约为转录本信息量的 60%–100%，宁可长而完整，不要压缩成提纲。\n"
                  "3. 章节：用 `##` 分章（按内容自然分段，3–9 章），复杂章内可用 `###` 小节。每个 `##` 标题末尾附时间戳，格式：`## 章节名（mm:ss）`。\n"
                  "4. 关键句加粗：每章至少 1 处 `**加粗**` 标出核心论断。\n"
                  "5. 插图：在讲对应内容的位置嵌入可用帧，格式两行：\n"
                  "   ![[attachments/帧文件名]]\n"
                  "   *斜体图注（一句话，描述该帧画面内容）*\n"
                  "   只能使用「可用帧清单」中列出的文件名，一个文件名最多嵌入一次，禁止编造；"
                  "帧数量多时优先选画面文字与当章内容相关的。\n"
                  "6. 脚注：对术语、背景、与主线无关的补充，用 `[^n]` 内联标记，并在文末逐条给出定义（`[^n]: 说明文字`）。\n"
                  "7. callout：视频中有演示、互动、思考、注意事项的地方，用 `> [!example] 思考题` 或 `> [!warning] 注意` 引用块呈现；不要滥用。\n"
                  "8. 【画面】文字与相邻【语音】融合理解；直接引用画面文字时在句末标注（画面）。带 ⚠ 的低置信段谨慎采用、语义不明时略过。\n"
                  "9. 专有名词、数字、代码、命令保持原样；简体中文输出。\n"
                  "输出格式：Markdown 正文（不要一级标题、不要代码块包裹全文、不要 TL;DR、不要元数据 JSON——正文直接从引言段开始）。\n")
ORG_USER_ONE = ("本条目：第 {{#loop_note.idx#}}/{{#code_fmt_run.total#}} 条"
                "（合集里的一集，只写这一条，不要写其它条目、不要输出分隔行）\n"
                "视频：{{#start_node.video_filename#}}（时长 {{#start_node.duration#}}）\n\n"
                "可用帧清单（文件名 [mm:ss] OCR内容；空=该条目无可用帧）：\n\n"
                "{{#http_get_entry_frames.body#}}\n\n"
                "转录本（[mm:ss] 时间戳）：\n\n{{#http_get_entry_transcript.body#}}\n")
META_USER_ONE = ("视频：{{#start_node.video_filename#}}（第 {{#loop_note.idx#}} 条）\n\n"
                 "笔记正文：\n\n{{#llm_organize_entry.text#}}\n")
REV_USER_ONE = ("转录本：\n\n{{#http_get_entry_transcript.body#}}\n\n"
                "—— 笔记正文（待校对）——\n\n{{#llm_organize_entry.text#}}\n")

loop_node("loop_note", "note 阶段（逐条目：整理→校对→元数据→写盘）", x=NOTE_LOOP_X, y=200, idx_init="1")
ls_note = loopstart_node("loop_note", "loop_note", x=NOTE_LOOP_X + 24, y=268)
http_node("http_get_entry_transcript", "取本条目转录本", "get",
          f"{HOST}/runs/{{{{#code_fmt_run.run_id#}}}}/stages/transcript?entry={{{{#loop_note.idx#}}}}",
          headers="", x=NOTE_LOOP_X + 324, y=268, read=60, loop_id="loop_note")
code_node("code_entry_state", "解析条目状态（幂等位）", CODE_ENTRY_STATE,
          {"note_done": {"type": "string"}},
          [{"variable": "resp", "value_selector": ["http_get_entry_transcript", "body"]}],
          loop_id="loop_note", x=NOTE_LOOP_X + 24, y=588)
node("if_entry_done", "if-else", "本条目已写盘？", (NOTE_LOOP_X + 324, 588),
     {"logical_operator": "and",
      "conditions": [{"id": "cond-entry-done",
                      "variable_selector": ["code_entry_state", "note_done"],
                      "comparison_operator": "is", "value": "true"}]},
     is_in_loop=True, loop_id="loop_note")
node("if_entry_text", "if-else", "本条目有转录本？", (NOTE_LOOP_X + 624, 268),
     {"logical_operator": "and",
      "conditions": [{"id": "cond-entry-text",
                      "variable_selector": ["http_get_entry_transcript", "body"],
                      "comparison_operator": "not empty", "value": ""}]},
     is_in_loop=True, loop_id="loop_note")
http_node("http_get_entry_frames", "取本条目已选中帧", "get",
          f"{HOST}/runs/{{{{#code_fmt_run.run_id#}}}}/stages/selected?entry={{{{#loop_note.idx#}}}}",
          headers="", x=NOTE_LOOP_X + 924, y=268, read=60, loop_id="loop_note")
llm_node("llm_organize_entry", "整理正文（单条目）", ORG_SYSTEM_ONE, ORG_USER_ONE,
         temperature=0.4, max_tokens=16000, x=NOTE_LOOP_X + 1224, y=268, loop_id="loop_note")
node("if_review_entry", "if-else", "是否校对", (NOTE_LOOP_X + 1524, 268),
     {"logical_operator": "and",
      "conditions": [{"id": "cond-review-entry-on",
                      "variable_selector": ["start_node", "review"],
                      "comparison_operator": "is", "value": "on", "varType": "text-input"}]},
     is_in_loop=True, loop_id="loop_note")
# 第二行(y=428):校对/元数据/组装/写盘/推进——与第一行流向呼应(见内边布线)
llm_node("llm_review_entry", "校对（单条目）", REV_SYSTEM, REV_USER_ONE,
         temperature=0.2, max_tokens=16000, x=NOTE_LOOP_X + 324, y=428, loop_id="loop_note")
llm_node("llm_meta_entry", "提取元数据（单条目）", META_SYSTEM, META_USER_ONE,
         temperature=0.1, max_tokens=2000, x=NOTE_LOOP_X + 624, y=428, loop_id="loop_note")
code_node("code_entry_note", "组装本条目写盘 payload", CODE_ENTRY_NOTE,
          {"body": {"type": "string"}, "title": {"type": "string"},
           "post_body": {"type": "string"}},
          [{"variable": "idx", "value_selector": ["loop_note", "idx"]},
           {"variable": "organize", "value_selector": ["llm_organize_entry", "text"]},
           {"variable": "review", "value_selector": ["llm_review_entry", "text"]},
           {"variable": "meta_raw", "value_selector": ["llm_meta_entry", "text"]}],
          loop_id="loop_note", x=NOTE_LOOP_X + 924, y=428)
http_node("http_post_note_entry", "写盘本条目（POST /note + entry）", "post",
          f"{HOST}/runs/{{{{#code_fmt_run.run_id#}}}}/note",
          body_data="{{#code_entry_note.post_body#}}", x=NOTE_LOOP_X + 1224, y=428,
          read=300, loop_id="loop_note")
code_node("code_note_next", "下一轮条目号 / 退出标志", CODE_NOTE_NEXT,
          {"next_idx": {"type": "string"}, "done_flag": {"type": "string"},
           "progress_text": {"type": "string"}},
          [{"variable": "idx", "value_selector": ["loop_note", "idx"]},
           {"variable": "total", "value_selector": ["code_fmt_run", "total"]},
           {"variable": "resp", "value_selector": ["http_post_note_entry", "body"]}],
          loop_id="loop_note", x=NOTE_LOOP_X + 1524, y=428)
node("assign_note_vars", "assigner", "写回循环变量", (NOTE_LOOP_X + 1824, 428),
     {"type": "assigner", "title": "写回循环变量", "version": "2",
      "isInLoop": True, "loop_id": "loop_note",
      "items": [{"variable_selector": ["loop_note", "idx"], "input_type": "variable",
                 "operation": "over-write", "value": ["code_note_next", "next_idx"]},
                {"variable_selector": ["loop_note", "done_flag"], "input_type": "variable",
                 "operation": "over-write", "value": ["code_note_next", "done_flag"]},
                {"variable_selector": ["loop_note", "progress"], "input_type": "variable",
                 "operation": "over-write", "value": ["code_note_next", "progress_text"]}]},
     is_in_loop=True, loop_id="loop_note")

# 旧链路（兼容分支）沿用 ORG_SYSTEM / META_SYSTEM / REV_SYSTEM
# 逐条目循环的内边（全部带 loop_id，Dify 按它收集本轮子图）
# 注意：loop **内部**节点不能挂 loop 外的上游边——Dify loop 引擎要求本轮全部上游完成才执行，
# 外部节点不在本轮 → 节点永不执行 → break_conditions 求值时报 Variable not found 致命错误。
edge("edge-note-start-gettr", ls_note, "source", "http_get_entry_transcript", "target", loop_id="loop_note")
# 幂等旁路：已写盘条目直接推进下一条（不烧 LLM），未写盘才走 转录本检查→LLM 链
edge("edge-gettr-state", "http_get_entry_transcript", "source", "code_entry_state", "target", loop_id="loop_note")
edge("edge-state-ifdone", "code_entry_state", "source", "if_entry_done", "target", loop_id="loop_note")
edge("edge-ifdone-next", "if_entry_done", "true", "code_note_next", "target", loop_id="loop_note")
edge("edge-ifdone-iftext", "if_entry_done", "false", "if_entry_text", "target", loop_id="loop_note")
edge("edge-iftext-frames", "if_entry_text", "true", "http_get_entry_frames", "target", loop_id="loop_note")
edge("edge-frames-organize", "http_get_entry_frames", "source", "llm_organize_entry", "target", loop_id="loop_note")
edge("edge-organize-ifrev", "llm_organize_entry", "source", "if_review_entry", "target", loop_id="loop_note")
edge("edge-ifrev-review", "if_review_entry", "true", "llm_review_entry", "target", loop_id="loop_note")
edge("edge-organize-metaone", "llm_organize_entry", "source", "llm_meta_entry", "target", loop_id="loop_note")
edge("edge-review-aggone", "llm_review_entry", "source", "code_entry_note", "target", loop_id="loop_note")
edge("edge-ifrev-aggone", "if_review_entry", "false", "code_entry_note", "target", loop_id="loop_note")
edge("edge-meta-aggone", "llm_meta_entry", "source", "code_entry_note", "target", loop_id="loop_note")
edge("edge-aggone-post", "code_entry_note", "source", "http_post_note_entry", "target", loop_id="loop_note")
edge("edge-post-next", "http_post_note_entry", "source", "code_note_next", "target", loop_id="loop_note")
edge("edge-iftext-next", "if_entry_text", "false", "code_note_next", "target", loop_id="loop_note")
edge("edge-next-assign", "code_note_next", "source", "assign_note_vars", "target", loop_id="loop_note")

node("end_final", "end", "结束（全阶段）", (_stage_x["kb"] + STAGE_LOOP_W + LOOP_GAP, 260),
     {"outputs": [
         {"variable": "result", "value_selector": ["loop_kb", "progress"]},
         {"variable": "n_entries", "value_selector": ["code_fmt_run", "total"]},
         {"variable": "note_progress", "value_selector": ["loop_note", "progress"]},
         # 批量调用方凭 run_id 回查 serve 的条目 done map，甄别「succeeded 但条目实际未写盘」
         {"variable": "run_id", "value_selector": ["code_fmt_run", "run_id"]},
     ]})
edge("edge-okkb-end", "if_ok_kb", "true", "end_final", "target")

# —— 兼容分支（url 空）：查最新任务 / 填 transcript 直改 ——
node("if_status", "if-else", "空跑查状态？", (620, 900),
     {"logical_operator": "and",
      "cases": [{"case_id": "true", "logical_operator": "and",
                 "conditions": [{"id": "cond-transcript-empty",
                                 "variable_selector": ["start_node", "transcript"],
                                 "comparison_operator": "empty", "value": ""}]}]})
node("http_get", "http-request", "查询最新任务", (900, 940),
     {"method": "get", "url": f"{HOST}/jobs/latest",
      "authorization": {"type": "no-auth", "config": None}, "headers": "", "params": "",
      "body": {"type": "none", "data": []},
      "timeout": {"connect": 10, "read": 60, "write": 10}, "ssl_verify": True})
code_node("code_fmt_status", "格式化进度", """import json

ICON = {"queued": "⏳ 排队中", "running": "🔄 处理中", "done": "✅ 完成", "error": "❌ 失败"}


def main(resp: str) -> dict:
    try:
        j = json.loads(resp or "{}")
    except Exception:
        return {"text": "⚠️ 本地作业服务未响应（video2md serve 未运行？）"}
    st = j.get("status", "none")
    if st == "none":
        return {"text": "还没有处理过任何视频。在 url 字段粘贴 B站/YouTube 链接即可入队。"}
    lines = [ICON.get(st, st) + f"（任务 {j.get('id', '?')}，发起 {j.get('created', '?')}）",
             f"来源：{j.get('url', '')}"]
    if j.get("note"):
        lines.append(f"产物：{j['note']}")
    if j.get("selfcheck"):
        lines.append(f"检索自检：{j['selfcheck']}")
    if j.get("error"):
        lines.append(f"错误：{j['error']}")
    tail = j.get("log") or []
    if tail and st in ("running", "queued"):
        lines.append("最近日志：")
        lines += ["· " + t[:100] for t in tail[-3:]]
    return {"text": "\\n".join(lines)}
""", {"text": {"type": "string"}},
          [{"variable": "resp", "value_selector": ["http_get", "body"]}], x=1180, y=940)
node("end_status", "end", "状态回执", (1460, 940),
     {"outputs": [{"variable": "status", "value_selector": ["code_fmt_status", "text"]}]})  # 跨 end 节点输出名唯一
# 兼容分支从 start_node 读转录本/帧清单（无阶段节点可用）
ORG_USER_COMPAT = ("{{#start_node.part_hint#}}\n\n"
                   "视频：{{#start_node.video_filename#}}（时长 {{#start_node.duration#}}）\n\n"
                   "可用帧清单（文件名 [mm:ss] OCR内容；OCR 为空则无画面文字）：\n\n"
                   "{{#start_node.frames_manifest#}}\n\n"
                   "转录本（[mm:ss] 时间戳）：\n\n"
                   "{{#start_node.transcript#}}\n")
META_USER_COMPAT = ("视频：{{#start_node.video_filename#}}\n\n"
                    "笔记正文：\n\n{{#llm_organize.text#}}\n")
REV_USER_COMPAT = ("转录本：\n\n{{#start_node.transcript#}}\n\n"
                   "—— 笔记正文（待校对）——\n\n{{#llm_organize.text#}}\n")
llm_node("llm_organize", "整理正文（旧链路）", ORG_SYSTEM, ORG_USER_COMPAT,
         temperature=0.4, max_tokens=16000, x=900, y=1180)
llm_node("llm_meta", "提取元数据", META_SYSTEM, META_USER_COMPAT,
         temperature=0.1, max_tokens=2000, x=1180, y=1300)
node("if_review", "if-else", "是否校对", (1460, 1180),
     {"logical_operator": "and",
      "conditions": [{"id": "cond-review-on", "variable_selector": ["start_node", "review"],
                      "comparison_operator": "is", "value": "on", "varType": "text-input"}]})
llm_node("llm_review", "校对", REV_SYSTEM, REV_USER_COMPAT,
         temperature=0.2, max_tokens=16000, x=1740, y=1360)
code_node("code_split_notes_c", "拆篇（旧链路·校对稿优先）", CODE_SPLIT_NOTES,
          {"sections_json": {"type": "string"}, "n_notes": {"type": "string"}},
          [{"variable": "body", "value_selector": ["llm_organize", "text"]},
           {"variable": "review_body", "value_selector": ["llm_review", "text"]}],
          x=2020, y=1300)
code_node("code_parse_meta_c", "解析元数据 JSON（旧链路）", CODE_PARSE_META,
          {"meta_json": {"type": "string"}},
          [{"variable": "meta_raw", "value_selector": ["llm_meta", "text"]}],
          x=2020, y=1440)
code_node("code_agg", "组装输出（旧链路）", CODE_AGG_BUILD,
          {"notes_json": {"type": "string"}, "note_body": {"type": "string"},
           "body": {"type": "string"},
           "meta": {"type": "string"}, "n_notes": {"type": "string"}},
          [{"variable": "sections_json", "value_selector": ["code_split_notes_c", "sections_json"]},
           {"variable": "meta_json", "value_selector": ["code_parse_meta_c", "meta_json"]}],
          x=2300, y=1180)
node("end_node", "end", "结束", (2580, 1180),
     {"outputs": [
         {"variable": "body", "value_selector": ["code_agg", "body"]},
         {"variable": "meta", "value_selector": ["code_agg", "meta"]},
     ]})
edge("edge-ifurl-ifstatus", "if_url", "false", "if_status", "target")
edge("edge-ifstatus-get", "if_status", "true", "http_get", "target")
edge("edge-get-fmt", "http_get", "source", "code_fmt_status", "target")
edge("edge-fmtstatus-end", "code_fmt_status", "source", "end_status", "target")
edge("edge-ifstatus-organize", "if_status", "false", "llm_organize", "target")
edge("edge-organize-meta2", "llm_organize", "source", "llm_meta", "target")
edge("edge-meta-ifelse", "llm_meta", "source", "if_review", "target")
edge("edge-ifelse-review", "if_review", "true", "llm_review", "target")
edge("edge-organize2-split", "llm_organize", "source", "code_split_notes_c", "target")
edge("edge-ifelse-split", "if_review", "false", "code_split_notes_c", "target")
edge("edge-review-split-c", "llm_review", "source", "code_split_notes_c", "target")
edge("edge-meta2-parsemeta", "llm_meta", "source", "code_parse_meta_c", "target")
edge("edge-splitc-agg", "code_split_notes_c", "source", "code_agg", "target")
edge("edge-parsemetac-agg", "code_parse_meta_c", "source", "code_agg", "target")
edge("edge-code-end", "code_agg", "source", "end_node", "target")

# —— 落盘 ——
normalize_layout(nodes)   # 绝对坐标 → 容器相对坐标 + 回填容器 width/height(见函数 docstring)
doc = {
    "app": {
        "description": ("视频 → Obsidian 笔记 + Dify RAG（全阶段编排：下载/转写/抽帧/OCR/内容选帧/剪节段/写盘/入库"
                        "各自独立节点，画布可单步重跑；选帧由 LLM 对照转录本内容决策，帧与节段吸附所在语音段）"),
        "icon": "🎬", "icon_background": "#E0F2FE", "mode": "workflow",
        "name": "视频转文档", "use_icon_as_answer_icon": False, "kind": "app", "version": "0.4.0",
    },
    "workflow": {
        "conversation_variables": [], "environment_variables": [], "features": {},
        "graph": {"edges": edges, "nodes": nodes},
    },
    "viewport": {"x": 0, "y": 0, "zoom": 0.55},
}

import yaml  # noqa: E402

# 用空值占位 sourceType/targetType 会序列化成 null，Dify 可接受；这里显式补全方便人读
out = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
OUT.write_text(out)
print(f"✅ 生成 {OUT}（{len(nodes)} 节点 / {len(edges)} 边）")

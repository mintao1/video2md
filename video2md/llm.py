"""LLM 客户端与提示词：httpx 直连 openai-compatible 端点，模板在 prompts/。

Dify 抽离（2026-09-21）后，画布上 4 个 LLM 节点收回代码内：
  内容选帧 select_frames（temp 0.2 / 4000）——对照转录本从候选清单选帧
  整理正文 organize     （temp 0.4 / 16000）——逐条目改写
  校对     review       （temp 0.2 / 16000）——对照转录本校对（review=on 时）
  提取元数据 metadata    （temp 0.1 / 2000） ——title/tags/summary JSON

模板占位语法 {{var}}：不用 str.format——prompt 里 JSON 示例的字面花括号会被
format 当占位符炸掉；{{var}} 双花括号替换顺承 Dify 的 {{#node.var#}} 书写习惯。
选帧解析/过滤与写盘 payload 组装移植自 DSL 的 code 节点（build_workflow.py
CODE_ALLOWED_FRAMES / CODE_SELECT_BLOCKS / CODE_SELECT_FILTER / CODE_ENTRY_NOTE）。
"""
from __future__ import annotations

import functools
import json
import re
import time
from pathlib import Path

import httpx

from .config import ENV
from .util import current_log, redact

_PROMPT_DIR = Path(__file__).parent / "prompts"

LLM_BASE = (ENV.get("LLM_BASE_URL") or "").rstrip("/")
LLM_KEY = ENV.get("LLM_API_KEY") or ""
LLM_MODEL = ENV.get("LLM_MODEL") or "Kimi-K3"

# 与 Dify 画布节点参数一致（retry_config：2 次、间隔 30s——上游中转偶发挂起，
# 节点级重试把「一次挂起=整 run 失败」降级为「多等半分钟」）
_TASK_PARAMS = {
    "select_frames": {"temperature": 0.2, "max_tokens": 4000},
    "organize": {"temperature": 0.4, "max_tokens": 16000},
    "review": {"temperature": 0.2, "max_tokens": 16000},
    "metadata": {"temperature": 0.1, "max_tokens": 2000},
}

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_TRANSPORT = None  # 测试注入 httpx.MockTransport；None = 真实网络

_sleep = time.sleep  # 测试替换掉，免得重试间隔真等


# 假 LLM 模式（V2MD_FAKE_LLM=1）：e2e / Docker 冒烟不烧真实 token 的确定性出口。
# 只替代「文本生成」，解析/过滤/门禁全走真代码——测的正是后者。
_FAKE_LLM = (ENV.get("V2MD_FAKE_LLM") or "").lower() in ("1", "true", "on")

_FAKE_META = '{"title": "测试笔记（假LLM）", "tags": ["e2e", "测试"], "summary": "离线冒烟摘要"}'


def _fake_organize(entry_frames: str) -> str:
    """确定性正文：嵌入选中帧（entry_frames = select 产物的可用帧清单），贴近真实改写格式。"""
    frames = re.findall(r"(\S+\.jpe?g)", entry_frames or "")[:5]
    parts = ["## 引言（00:00）", "",
             "这是基于转录本整理的**测试正文**（假 LLM 占位，e2e/冒烟专用）。" + "展开。" * 20]
    for i, f in enumerate(frames, 1):
        parts += [f"## 第 {i} 节（00:{i:02d}）", "", f"第 {i} 节内容。" + "补充细节。" * 10,
                  f"![[attachments/{f}]]", f"*第 {i} 帧图注*", ""]
    parts.append("## 小结（00:15）\n\n" + "收尾。" * 40)
    return "\n".join(parts)



def render(template: str, **vars) -> str:
    """{{var}} 占位符替换；未知占位符保留原样（宁漏不炸——prompt 里可能有别的双花括号文本）。"""
    def sub(m: re.Match) -> str:
        return str(vars[m.group(1)]) if m.group(1) in vars else m.group(0)
    return _PLACEHOLDER.sub(sub, template)



@functools.lru_cache(maxsize=None)
def _load(task: str, role: str) -> str:
    return (_PROMPT_DIR / f"{task}.{role}.md").read_text(encoding="utf-8").strip()



def chat(system: str, user: str, *, task: str, temperature: float, max_tokens: int) -> str:
    """openai-compatible chat completion；网络错误/429/5xx 重试 2 次（对齐 Dify 节点），
    4xx 立即失败（鉴权/参数错重试无意义）。失败即 RuntimeError，不静默降级。"""
    if not LLM_BASE or not LLM_KEY:
        raise SystemExit("LLM_BASE_URL / LLM_API_KEY 未配置：写入 .env 或环境变量")
    payload = {"model": LLM_MODEL, "temperature": temperature, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    last_err = ""
    for attempt in range(3):
        try:
            with httpx.Client(transport=_TRANSPORT) as client:
                r = client.post(f"{LLM_BASE}/chat/completions", json=payload,
                                headers={"Authorization": f"Bearer {LLM_KEY}"},
                                timeout=httpx.Timeout(30.0, read=600.0))
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            # 429/5xx 可重试；其余 4xx（401/403/参数错）重试无意义，响亮失败
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError(f"LLM 请求失败 HTTP {r.status_code}: {r.text[:300]}")
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as exc:
            last_err = redact(str(exc))[:300]
        if attempt < 2:
            print(f"  ⚠ LLM {task} 第 {attempt + 1} 次调用失败（{last_err[:80]}），30s 后重试")
            _sleep(30)
    raise RuntimeError(f"LLM {task} 连续 3 次失败：{last_err}")



def llm_task(task: str, **vars) -> str:
    """按任务名取模板渲染并调用。vars 即 user 模板的 {{var}} 占位符实参。"""
    if _FAKE_LLM:
        return _fake_organize(vars.get("entry_frames", "")) if task in ("organize", "review") \
            else _FAKE_META
    p = _TASK_PARAMS[task]
    log = current_log()
    if log:
        with log.span(f"llm:{task}"):
            return chat(render(_load(task, "system"), **vars),
                        render(_load(task, "user"), **vars), task=task, **p)
    return chat(render(_load(task, "system"), **vars),
                render(_load(task, "user"), **vars), task=task, **p)



# ---------------------------------------------------------------- 选帧输出解析
# 移植自 DSL code 节点：CODE_ALLOWED_FRAMES（清单→合法帧名表）/
# CODE_SELECT_BLOCKS（解析 LLM JSON，保留块结构）/ CODE_SELECT_FILTER（滤非法名→payload）。

def allowed_frames_of(manifest: str) -> dict[int, list[str]]:
    """候选帧清单 → 每个条目的合法帧名表。无「===== 条目 N/M：」分节头（单条目）→ 全算 1 号条目。"""
    m = manifest or ""
    allowed: dict[int, list[str]] = {}
    parts = re.split(r"===== 条目 (\d+)/\d+：", m)
    for i in range(1, len(parts), 2):
        try:
            allowed[int(parts[i])] = re.findall(r"(\S+\.jpg)", parts[i + 1] or "")
        except Exception:
            continue
    if not allowed:
        allowed = {1: re.findall(r"(\S+\.jpg)", m)}
    return allowed



def parse_select_output(llm_out: str) -> tuple[list[dict], str]:
    """解析 LLM 选帧输出（容忍 ```json 围栏）→ (blocks, mode)。

    保留块结构（而非拍平）对齐旧语义：selections 模式下每个块各自去重。
    mode: "selections"（多条目分块）| "frames"（单条目平铺）。
    """
    try:
        raw = (llm_out or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}

    def fr(f: dict) -> dict:
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
        return blocks, "selections"
    return [{"entry": 0, "frames": [fr(f) for f in (obj.get("frames") or [])]}], "frames"



def filter_select(blocks: list[dict], mode: str, allowed: dict[int, list[str]],
                  total: int) -> tuple[dict, int, str]:
    """按合法帧名表过滤（去重、非法名丢弃）→ select 阶段的 args payload。

    返回 (payload, n_selected, note)：payload 形如 {"frames": [...]}（单条目）
    或 {"selections": [...]}（多条目），直接作 _stage_select 的 args。
    """
    allowed_sets = {int(k): set(v) for k, v in allowed.items()}

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
                "frames": filt(b.get("frames"), allowed_sets.get(b.get("entry", 1), set()))}
               for b in blocks]
        out = [b for b in out if b["frames"]]
        payload, n_sel = {"selections": out}, sum(len(x["frames"]) for x in out)
    else:  # 单条 frames 输出：池 = 条目1 的合法名；多条目场景取全体并集
        pool = allowed_sets.get(1, set())
        if total > 1:
            pool = set().union(*allowed_sets.values()) if allowed_sets else set()
        fl = filt(blocks[0].get("frames") if blocks else [], pool)
        payload = {"selections": [{"entry": 1, "frames": fl}]} if total > 1 else {"frames": fl}
        n_sel = len(fl)
    note = "" if n_sel else "⚠️ LLM 未选出合法帧名，select 阶段将走本地启发式兜底"
    return payload, n_sel, note



def select_frames(transcript: str, candidates: str, total: int) -> tuple[dict, int, str]:
    """内容选帧一步到位：LLM 调用 + 解析 + 合法名过滤 → select 阶段 payload。"""
    if _FAKE_LLM:
        # 假模式确定性选帧：每条目取清单前 2 个合法帧名
        allowed = allowed_frames_of(candidates)
        if total > 1:
            sels = [{"entry": k, "frames": [{"name": n, "caption": "e2e"} for n in v[:2]]}
                    for k, v in sorted(allowed.items())]
            return {"selections": sels}, sum(len(s["frames"]) for s in sels), ""
        frames = [{"name": n, "caption": "e2e"} for n in allowed.get(1, [])[:2]]
        return {"frames": frames}, len(frames), ""
    out = llm_task("select_frames", transcript=transcript, candidates=candidates)
    blocks, mode = parse_select_output(out)
    return filter_select(blocks, mode, allowed_frames_of(candidates), total)



# ---------------------------------------------------------------- 写盘 payload 组装
# 移植自 CODE_ENTRY_NOTE：正文取校对稿（≥50 字算有效）否则原稿；meta 归一成单条目对象。

def assemble_entry_note(organize: str, review: str, meta_raw: str) -> dict:
    """→ {"body": str, "meta": {"title","tags","summary"}}，直接作 _stage_note 的 args。"""
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
    return {"body": body, "meta": meta}

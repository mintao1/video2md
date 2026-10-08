"""评测旁路：DeepEval/RAGAS 给视频笔记打分，分数回写 Langfuse（scores）。

定位（2026-09-22 定稿）：离线批量为主（scripts/eval_notes.py），在线节点已接线
（V2MD_EVAL=1 时 graph 在 note_entry 后插 eval_entry，旁路不致命）。
不引入 Braintrust/Confident AI 平台——它们的能力 = 本地开源库打分 + 看板，看板由
Langfuse scores 承担；judge 模型走自有 openai-compatible 端点（LLM_BASE_URL），
token 成本与管线同源可见。本模块的 judge 适配层可整体拷给其它项目复用。

指标集：
- DeepEval G-Eval note_quality：结构完整 / 忠实原文 / 可读性 rubric（对照转录本）
- DeepEval Faithfulness：笔记论述 vs 转录本上下文
- DeepEval Summarization：摘要覆盖度（input=转录本，output=笔记）
- RAGAS Faithfulness（纯 LLM 指标；需 embedding 的 RAGAS 指标默认跳过——端点未必有 embeddings）
单指标失败不拖垮整批：跳过并记 warn（评测是旁路，不是门禁）。

judge 健壮性（2026-09-22，实测中转回包 JSON 不规范曾致 Faithfulness 整批被跳过）：
_Judge.generate(prompt, schema) 接管 deepeval 的 schema 解析——括号配平强提取 +
尾逗号修复 + 一次「只输出 JSON」重问，仍烂才抛错走跳过兜底。指标间线程级并发
（单篇耗时 ≈ 最慢的 Faithfulness，不再是全链总和）。judge 模型可用
V2MD_JUDGE_MODEL 单独换（缺省 LLM_MODEL）。

scores 上报：Langfuse SDK（trace_id = sha256("video2md-"+run_id)[:32] 确定性回挂原 trace；
2026-09-22 前为 serve- 前缀，历史 trace 在 Langfuse 仍用旧前缀）。
新 Cloud 组织（≥2026-09-16）legacy 读写 API 大面积 410：POST /api/public/scores 实测可用，
验证只能走 GET /api/public/v3/scores?traceId=（实测 2026-09-22）。SDK 发完首条 score
用该端点验证落库，不落库则进程内降级为 OTLP eval-span 属性（经 collector，trace 上仍可见）。
离线批量走 verify=False + verify_pending：整批只核验一次，免每条 score 20s sleep。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

import httpx

from .config import ENV

# judge 上下文预算：转录本可长达 6 万字符（MAX_TRANSCRIPT_CHARS），喂 judge 截到 2 万
_JUDGE_CTX_CHARS = 20000

# judge 重试放宽到 5 次：Faithfulness 一条链 4 次串行子调用，单次失败概率被放大
# （实测 2026-09-22 中转风暴日：空回包率 ~50%，3 次重试下单链成功率 ~60%，5 次 ~80%+）
_JUDGE_ATTEMPTS = 5

_SCORE_TRANSPORT = ""  # "" 未判定 | "sdk" | "otlp" | "off"


def trace_id_of(run_id: str) -> str:
    """与 observability._RunTrace 同一约定：Langfuse trace id = sha256 前 32 hex。"""
    return hashlib.sha256(f"video2md-{run_id}".encode()).hexdigest()[:32]


def _env(key: str) -> str:
    """os.environ 显式设置（含空串=显式关闭）优先；config.ENV（secrets 合并）兜底。"""
    if key in os.environ:
        return os.environ[key]
    return ENV.get(key, "")


# ---------------------------------------------------------------- judge 模型适配层

# JSON-only 前置：所有 judge 调用共用，先把「守规矩」压在 system 里
_JUDGE_SYSTEM = ("你是严谨的评测员。所有回答必须是且仅是合法 JSON："
                 "不要 markdown 围栏，不要解释，不要前言后语。")


def _judge_model() -> str:
    """judge 专用模型（V2MD_JUDGE_MODEL），缺省回退主管线模型。"""
    from .llm import LLM_MODEL
    return _env("V2MD_JUDGE_MODEL") or LLM_MODEL


def _scan_json_obj(text: str, pos: int = 0) -> tuple[int, int]:
    """从 pos 起找第一个括号配平的 {...}（跳过字符串字面量与 \\ 转义内的括号）。
    返回 [start, end)；找不到/不配平（输出被截断）抛 ValueError。"""
    start = text.find("{", pos)
    if start < 0:
        raise ValueError("输出里没有 JSON 对象")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise ValueError("JSON 对象括号不配平（输出被截断？）")


def _load_json_lenient(raw: str) -> dict:
    """中转回包容错解析，比 deepeval 的 trimAndLoadJson 多两层：括号配平扫描
    （容忍前言后语/围栏/串内括号）+ 逐候选尾逗号修复；全失败抛 ValueError。

    强于 deepeval 的「首 { 到尾 } 截取」：前言里的 {示例} 不会顶掉真回包，
    截断/多个对象时逐候选尝试，第一个能解析成 dict 的胜出。
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.S).strip()
    if not text:
        raise ValueError("judge 空输出")
    pos, err = 0, None
    while pos < len(text):
        try:
            s, e = _scan_json_obj(text, pos)
        except ValueError:
            break
        for cand in (text[s:e], re.sub(r",\s*([\]}])", r"\1", text[s:e])):  # 原样 → 去尾逗号
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError as exc:
                err = exc
        pos = max(e, s + 1)
    raise ValueError(f"judge 输出无法解析为 JSON 对象：{err or '无完整括号'}")


def _judge_deepeval():
    """DeepEval judge：复用 llm.chat（自有端点、流式、重试纪律），温度压 0 求稳。

    generate(prompt, schema)：deepeval 的 generate_with_schema 优先把 schema 传进来
    （base_model.generate_with_schema → self.generate(prompt, schema=...)），接住即
    接管解析——_load_json_lenient 强提取，失败重问一次「只输出 JSON」；仍烂才抛错，
    由 score_note 按既有口径跳过该指标（旁路不炸批）。schema=None 回原文。
    """
    from deepeval.models import DeepEvalBaseLLM

    from .llm import chat

    class _Judge(DeepEvalBaseLLM):
        def load_model(self):
            return None

        def generate(self, prompt: str, schema=None):
            raw = chat(_JUDGE_SYSTEM, prompt, task="eval", temperature=0.0,
                       max_tokens=4000, model=_judge_model(), attempts=_JUDGE_ATTEMPTS)
            if schema is None:
                return raw
            try:
                return schema(**_load_json_lenient(raw))
            except Exception:
                pass  # 解析/校验失败 → 重问一次
            repair = (prompt + "\n\n【修正】你的上次输出不是合法 JSON。这次只输出合法 JSON："
                      "不要围栏、不要解释、不要前言。\n上次输出（截断）：\n" + raw[:2000])
            raw2 = chat(_JUDGE_SYSTEM, repair, task="eval", temperature=0.0,
                        max_tokens=4000, model=_judge_model(), attempts=_JUDGE_ATTEMPTS)
            return schema(**_load_json_lenient(raw2))

        async def a_generate(self, prompt: str, schema=None):
            return self.generate(prompt, schema=schema)

        def get_model_name(self) -> str:
            return f"{_judge_model()} (video2md judge)"

    return _Judge()


def _judge_ragas():
    """RAGAS judge：老式 BaseRagasLLM 鸭子类型——async generate(prompt_value, n, ...)
    → LLMResult（langchain_core），结构化解析由 ragas 的 RagasOutputParser 负责
    （自带 3 次重试）。生成走 llm.chat：强制流式（防中转边缘 524）+ 显式
    max_tokens=4000（llm_factory 默认 InstructorModelArgs.max_tokens=1024，实测把
    判定 JSON 截断成 IncompleteOutput → 分数 NaN）+ 空回包重试。

    注：ragas.metrics.Faithfulness 已标记 deprecated（v1.0 移除，迁
    ragas.metrics.collections + instructor 接口），届时本适配层换成
    agenerate(prompt, response_model) 形态即可（同 _Judge 的 schema 路径）。
    """
    from .llm import chat

    class _RagasJudge:
        run_config = None  # evaluate() 若 set_run_config，接住即可

        def set_run_config(self, rc):
            self.run_config = rc

        async def generate(self, prompt, n=1, temperature=None, stop=None,
                           callbacks=None):
            from langchain_core.outputs import Generation, LLMResult

            text = prompt.to_string() if hasattr(prompt, "to_string") else str(prompt)
            gens = [Generation(text=chat(_JUDGE_SYSTEM, text, task="eval",
                                         temperature=temperature or 0.0,
                                         max_tokens=4000, model=_judge_model(), attempts=_JUDGE_ATTEMPTS))
                    for _ in range(max(1, n))]
            return LLMResult(generations=[gens])

        def is_finished(self, _response) -> bool:
            return True  # llm.chat 空回包已自重试/炸出，到这里的都是完整回包

    return _RagasJudge()


# ---------------------------------------------------------------- 打分


def score_note(note_md: str, transcript: str, *, with_ragas: bool = True) -> dict[str, float]:
    """对一篇笔记跑指标集 → {metric: 0~1 分}。单指标失败跳过（warn），不炸批次。

    指标级并发：四条 judge 链互相独立（metric 内部子调用有依赖、仍串行），
    线程池并行后单篇耗时 ≈ 最慢指标（Faithfulness 的 4 次子调用），不再是全链总和。
    judge 调用是阻塞 httpx，llm.chat 无共享可变状态，线程并发安全。
    """
    ctx = (transcript or "")[:_JUDGE_CTX_CHARS]
    scores: dict[str, float] = {}
    if not note_md.strip() or not ctx.strip():
        return scores
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from deepeval.metrics import FaithfulnessMetric, GEval, SummarizationMetric
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    judge = _judge_deepeval()
    tc = LLMTestCase(input=ctx, actual_output=note_md, retrieval_context=[ctx])
    metrics = [
        ("note_quality", GEval(
            name="note_quality",
            criteria="评估这篇视频笔记：1) 结构完整（标题/小节/时间戳/配图引用）；"
                     "2) 忠实原文（不编造转录本没有的内容）；3) 可读性（通顺、无拒答/空壳）。",
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.INPUT],
            model=judge)),
        ("faithfulness", FaithfulnessMetric(model=judge)),
        ("summarization", SummarizationMetric(model=judge)),
    ]

    def _deepeval(m) -> float:
        m.measure(tc)
        return round(float(m.score), 4)

    def _ragas() -> float:
        from ragas import EvaluationDataset, SingleTurnSample, evaluate
        from ragas.metrics import Faithfulness

        sample = SingleTurnSample(user_input="整理视频笔记", response=note_md,
                                  retrieved_contexts=[ctx])
        res = evaluate(EvaluationDataset([sample]), metrics=[Faithfulness()],
                       llm=_judge_ragas(), show_progress=False)
        val = res["faithfulness"]  # type: ignore[index]  # ragas 0.4 重载：单样本 Dataset 返回可下标结果
        if isinstance(val, (list, tuple)):
            val = val[0]  # ragas 0.4 evaluate() 按样本返回列表，单样本取首元素
        val = float(val)
        if val != val:  # NaN：judge 未产出有效判定，按失败跳过，不上报脏分
            raise ValueError("ragas faithfulness 为 NaN（judge 未产出有效判定）")
        return round(val, 4)

    def _run(name: str, fn) -> tuple[str, float] | None:
        try:
            return name, fn()
        except Exception as exc:
            print(f"  ⚠ 指标 {name} 失败（跳过）：{str(exc)[:120]}", file=sys.stderr)
            return None

    jobs: list[tuple[str, object]] = [(n, (lambda m=m: _deepeval(m))) for n, m in metrics]
    if with_ragas:
        jobs.append(("ragas_faithfulness", _ragas))
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        for fut in as_completed([ex.submit(_run, n, f) for n, f in jobs]):
            res = fut.result()
            if res:
                scores[res[0]] = res[1]
    return scores


# ---------------------------------------------------------------- scores 回写（SDK 优先，黑洞降级 OTLP）


def _verify_landed(trace_id: str, score_name: str) -> bool:
    """v3 scores 查询验证落库（新组织 legacy traces/scores GET 全 410，只认 v3）。

    实测（2026-09-22，本组织）：POST /api/public/scores 可用；GET traces/{id} 410；
    GET v2/scores 410；GET v3/scores?traceId= 可用（落库有分钟级延迟，调用方先 sleep）。
    """
    pk, sk = _env("LANGFUSE_PK"), _env("LANGFUSE_SK")
    host = (_env("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")
    try:
        r = httpx.get(f"{host}/api/public/v3/scores", auth=(pk, sk), timeout=8,
                      params={"traceId": trace_id, "limit": 50})
        return r.status_code == 200 and \
            any(s.get("name") == score_name for s in r.json().get("data", []))
    except Exception:
        return False


def _report_via_sdk(trace_id: str, scores: dict, comment: str) -> None:
    from langfuse import Langfuse

    lf = Langfuse(public_key=_env("LANGFUSE_PK"), secret_key=_env("LANGFUSE_SK"),
                  host=_env("LANGFUSE_HOST") or "https://cloud.langfuse.com")
    for name, value in scores.items():
        score_fn = getattr(lf, "score", None) or getattr(lf, "create_score")
        score_fn(trace_id=trace_id, name=name, value=value, comment=comment)
    lf.flush()


def _report_via_otlp(run_id: str, scores: dict, comment: str) -> None:
    """降级：分数作为 eval info 事件挂原 trace（经 collector；Langfuse 里 span 属性可见）。"""
    from . import observability
    from .util import RunLog

    log = RunLog("eval", run_id=run_id, trace_name=f"eval:{run_id}")
    log.info("eval", comment=comment, **{k: f"{v:.4f}" for k, v in scores.items()})
    observability.close_run(run_id)
    observability.flush()


def report_scores(run_id: str, scores: dict[str, float], *, comment: str = "",
                  verify: bool = True) -> str:
    """→ "sdk" | "otlp" | "off"。未配凭证且无 collector 端点 = off。

    verify=False（离线批量专用）：SDK 发完乐观返回 "sdk"，不逐条 sleep 核验；
    黑洞风险由调用方整批结束后交 verify_pending 一次核验兜底（免每篇 4×20s）。
    """
    global _SCORE_TRANSPORT
    if not scores:
        return "off"
    trace_id = trace_id_of(run_id)
    if _SCORE_TRANSPORT != "otlp" and _env("LANGFUSE_PK") and _env("LANGFUSE_SK"):
        try:
            _report_via_sdk(trace_id, scores, comment)
            if _SCORE_TRANSPORT == "sdk":
                return "sdk"
            if not verify:
                return "sdk"  # 未核验；疑似黑洞由 verify_pending 整批发现并 OTLP 重报
            time.sleep(20)  # 非 OTLP 路径有分钟级可见延迟（官方文档原话），宁可多等也不错判降级
            first = next(iter(scores))
            if _verify_landed(trace_id, first):
                _SCORE_TRANSPORT = "sdk"
                return "sdk"
            print("⚠️ Langfuse scores 疑似黑洞（201 但未落库），本进程降级 OTLP eval-span",
                  file=sys.stderr)
        except Exception as exc:
            print(f"⚠️ Langfuse SDK 上报失败（{str(exc)[:100]}），降级 OTLP eval-span",
                  file=sys.stderr)
        _SCORE_TRANSPORT = "otlp"  # 进程内永久降级，不再每条都试错
    if _SCORE_TRANSPORT == "otlp" or _env("OTEL_EXPORTER_OTLP_ENDPOINT"):
        _SCORE_TRANSPORT = "otlp"
        _report_via_otlp(run_id, scores, comment)
        return "otlp"
    _SCORE_TRANSPORT = "off"
    return "off"


def verify_pending(pending: dict[str, tuple[dict, str]]) -> str:
    """report_scores(verify=False) 的整批核验兜底：一次 sleep 替代逐条 20s。

    pending = {run_id: (scores, comment)}。已确认 sdk 可用直接返回；进程已降级 otlp
    则把未核验的全量 OTLP 重报；否则 sleep(20) 后逐 run 查 v3 scores，未落库的
    OTLP eval-span 重报并进程粘滞降级。→ "sdk" | "otlp" | "off"。
    """
    global _SCORE_TRANSPORT
    if not pending:
        return "off"
    if _SCORE_TRANSPORT == "sdk":
        return "sdk"
    if _SCORE_TRANSPORT == "otlp":  # 批内 SDK 已炸过：未核验的全部 OTLP 重报，不再查
        for rid, (sc, cm) in pending.items():
            _report_via_otlp(rid, sc, cm)
        return "otlp"
    time.sleep(20)  # 落库有分钟级延迟，整批只等这一次
    missing = [(rid, sc, cm) for rid, (sc, cm) in pending.items()
               if not _verify_landed(trace_id_of(rid), next(iter(sc)))]
    if not missing:
        _SCORE_TRANSPORT = "sdk"
        return "sdk"
    print(f"⚠️ {len(missing)}/{len(pending)} 条 scores 疑似黑洞（201 但未落库），"
          f"降级 OTLP eval-span 重报", file=sys.stderr)
    _SCORE_TRANSPORT = "otlp"
    for rid, sc, cm in missing:
        _report_via_otlp(rid, sc, cm)
    return "otlp"


# ---------------------------------------------------------------- 离线批量 CLI（python -m video2md.evals）
# 放包内而非 scripts/：.dockerignore 排除 scripts/，容器内评测只能靠模块入口。

_REGRESSION_DELTA = 0.05


def _iter_targets(state: dict, run_id: str = "", day: str = "", limit: int = 0):
    """state.json → 有 note+transcript 且文件俱在的已完成条目。"""
    from pathlib import Path
    n = 0
    for fp, rec in state.items():
        if rec.get("status") != "completed" or rec.get("dry_run"):
            continue
        if run_id and rec.get("run_id") != run_id:
            continue
        if day and rec.get("date") != day:
            continue
        note_p, tr_p = Path(rec.get("note", "")), Path(rec.get("transcript", ""))
        if not (note_p.exists() and tr_p.exists()):
            print(f"  ⏭ {rec.get('title', fp)}：note/transcript 文件不在本机，跳过", file=sys.stderr)
            continue
        yield fp, rec, note_p, tr_p
        n += 1
        if limit and n >= limit:
            return


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    from datetime import date
    from pathlib import Path

    from .util import load_state

    ap = argparse.ArgumentParser(
        description="video2md 笔记质量离线评测（DeepEval/RAGAS → Langfuse scores）")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--date", default="", help="只评某天完成的条目（YYYY-MM-DD）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--no-ragas", action="store_true")
    ap.add_argument("--baseline", default="",
                    help="基线写出路径（默认 docs/evals/baseline-<今天>.json；容器内请显式给 "
                         "/app/data/evals/… 落到持久卷）")
    ap.add_argument("--compare", default="", help="与旧基线对比，出回归报告")
    args = ap.parse_args(argv)

    rows: dict[str, dict] = {}
    pending: dict[str, tuple[dict, str]] = {}  # run_id → (scores, comment)，SDK 未核验
    for fp, rec, note_p, tr_p in _iter_targets(load_state(), args.run_id, args.date, args.limit):
        title = rec.get("title") or fp
        print(f"· 评测 {title[:50]}（run {rec.get('run_id', '?')}）")
        scores = score_note(note_p.read_text(encoding="utf-8"),
                            tr_p.read_text(encoding="utf-8"),
                            with_ragas=not args.no_ragas)
        if not scores:
            print("  ⚠ 无指标产出（全跳过）", file=sys.stderr)
            continue
        print("  " + "  ".join(f"{k}={v:.3f}" for k, v in scores.items()))
        if not args.no_upload and rec.get("run_id"):
            cm = f"{title[:80]} fp={fp}"
            via = report_scores(rec["run_id"], scores, comment=cm, verify=False)
            if via == "sdk" and _SCORE_TRANSPORT != "sdk":
                pending[rec["run_id"]] = (scores, cm)  # 未核验，批后 verify_pending 兜底
            print(f"  → Langfuse（{via}）")
        rows[fp] = {"title": title, "run_id": rec.get("run_id", ""), "scores": scores}

    if pending:
        print(f"scores 批量核验：{verify_pending(pending)}（{len(pending)} 条 trace）")

    if not rows:
        print("没有可评条目（state.json 空/文件不在/过滤太严）")
        return 0
    means = {k: round(sum(r["scores"][k] for r in rows.values() if k in r["scores"])
                      / max(1, sum(1 for r in rows.values() if k in r["scores"])), 4)
             for k in {k for r in rows.values() for k in r["scores"]}}
    baseline = {"date": date.today().isoformat(), "n": len(rows), "means": means, "rows": rows}
    out = Path(args.baseline or f"docs/evals/baseline-{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(baseline, ensure_ascii=False, indent=1))
    print(f"\n基线已写 {out}（{len(rows)} 篇）：" +
          "  ".join(f"{k}={v:.3f}" for k, v in means.items()))

    if args.compare:
        old = json.loads(Path(args.compare).read_text())
        print(f"\n对比 {args.compare}（{old.get('date', '?')}，{old.get('n', '?')} 篇）：")
        regressed = False
        for k, v in means.items():
            old_v = (old.get("means") or {}).get(k)
            if old_v is None:
                continue
            delta = v - old_v
            mark = "⚠️ 回归" if delta < -_REGRESSION_DELTA else ("✅" if delta > _REGRESSION_DELTA else "·")
            print(f"  {mark} {k}: {old_v:.3f} → {v:.3f}（{delta:+.3f}）")
            regressed = regressed or delta < -_REGRESSION_DELTA
        if regressed:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""test_evals.py — evals 旁路离线单测。

覆盖：traceId 确定性回挂 / score_note 指标编排（含单指标失败不炸批）/
report_scores 三运输路径（sdk 直挂 → 黑洞降级 otlp → 无凭证 off）/ RAGAS 开关。

运行：python3 test_evals.py（sys.modules 注入假 deepeval/ragas/langfuse，无网络无 token）。
"""
import hashlib
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）

os.environ["V2PDF_LOG_DIR"] = tempfile.mkdtemp(prefix="v2pdf-test-logs-")
for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "LANGFUSE_PK", "LANGFUSE_SK", "LANGFUSE_HOST"):
    os.environ[k] = ""

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from video2pdf import evals, observability


# ---- 假 deepeval / ragas / langfuse（sys.modules 注入，先于被测调用）----------
class _FakeMetric:
    def __init__(self, score=0.88, **kw):
        self.score = score

    def measure(self, _tc):
        pass


class _BoomMetric(_FakeMetric):
    def measure(self, _tc):
        raise RuntimeError("judge 炸了")


deepeval_mod = types.ModuleType("deepeval")
deepeval_metrics = types.ModuleType("deepeval.metrics")
deepeval_metrics.GEval = lambda **kw: _FakeMetric(0.91, **kw)
deepeval_metrics.FaithfulnessMetric = lambda **kw: _FakeMetric(0.82, **kw)
deepeval_metrics.SummarizationMetric = lambda **kw: _FakeMetric(0.73, **kw)
deepeval_testcase = types.ModuleType("deepeval.test_case")
deepeval_testcase.LLMTestCase = lambda **kw: kw
deepeval_testcase.LLMTestCaseParams = types.SimpleNamespace(ACTUAL_OUTPUT="out", INPUT="in")
deepeval_models = types.ModuleType("deepeval.models")


class _FakeBaseLLM:
    pass


deepeval_models.DeepEvalBaseLLM = _FakeBaseLLM
sys.modules.update({"deepeval": deepeval_mod, "deepeval.metrics": deepeval_metrics,
                    "deepeval.test_case": deepeval_testcase,
                    "deepeval.models": deepeval_models})

ragas_mod = types.ModuleType("ragas")
ragas_metrics = types.ModuleType("ragas.metrics")
ragas_metrics.Faithfulness = lambda: object()
ragas_llms = types.ModuleType("ragas.llms")
ragas_llms.llm_factory = lambda *a, **kw: object()
ragas_mod.EvaluationDataset = list
ragas_mod.SingleTurnSample = lambda **kw: kw
ragas_mod.evaluate = lambda *a, **kw: {"faithfulness": 0.77}
sys.modules.update({"ragas": ragas_mod, "ragas.metrics": ragas_metrics,
                    "ragas.llms": ragas_llms})

sdk_calls = []


class _FakeLangfuse:
    def __init__(self, public_key=None, secret_key=None, host=None):
        self.auth = (public_key, secret_key, host)

    def score(self, **kw):
        sdk_calls.append(kw)

    def flush(self):
        pass


langfuse_mod = types.ModuleType("langfuse")
langfuse_mod.Langfuse = _FakeLangfuse
sys.modules["langfuse"] = langfuse_mod

# judge 适配层不真烧 token：generate 直接回 JSON（DeepEval 假 metric 根本不读）
_real_judge_deepeval = evals._judge_deepeval  # 留给下面 JSON 容错/重问用例
_real_judge_ragas = evals._judge_ragas
evals._judge_deepeval = lambda: object()
evals._judge_ragas = lambda: object()


# ---- traceId 确定性（与 observability 同约定，跨进程回挂的根基）----------------
assert evals.trace_id_of("r1") == hashlib.sha256(b"video2md-r1").hexdigest()[:32]
assert evals.trace_id_of("r1") != evals.trace_id_of("r2")


# ---- score_note 编排 -----------------------------------------------------------
scores = evals.score_note("# 笔记\n正文" * 100, "转录本" * 1000)
assert scores == {"note_quality": 0.91, "faithfulness": 0.82, "summarization": 0.73,
                  "ragas_faithfulness": 0.77}, scores

# 单指标失败不炸批
deepeval_metrics.FaithfulnessMetric = lambda **kw: _BoomMetric(**kw)
scores2 = evals.score_note("笔记" * 50, "转录" * 500)
assert "faithfulness" not in scores2 and scores2["note_quality"] == 0.91
deepeval_metrics.FaithfulnessMetric = lambda **kw: _FakeMetric(0.82, **kw)  # 复原

# RAGAS 关闭 / 空输入
scores3 = evals.score_note("笔记" * 50, "转录" * 500, with_ragas=False)
assert "ragas_faithfulness" not in scores3
assert evals.score_note("", "转录") == {}

# ragas 0.4 evaluate() 按样本返回列表 → 单样本拆包（真实容器实测 2026-09-22）
ragas_mod.evaluate = lambda *a, **kw: {"faithfulness": [0.66]}
scores4 = evals.score_note("笔记" * 50, "转录" * 500)
assert scores4.get("ragas_faithfulness") == 0.66, scores4
# NaN = judge 未产出有效判定 → 按失败跳过，不进 scores（不上报脏分）
ragas_mod.evaluate = lambda *a, **kw: {"faithfulness": [float("nan")]}
assert "ragas_faithfulness" not in evals.score_note("笔记" * 50, "转录" * 500)
ragas_mod.evaluate = lambda *a, **kw: {"faithfulness": 0.77}  # 复原


# ---- report_scores：sdk 直挂（验证落库通过）------------------------------------
envmap = {"LANGFUSE_PK": "pk-t", "LANGFUSE_SK": "sk-t", "LANGFUSE_HOST": "https://lf.test"}
evals._env = lambda k: envmap.get(k, "")
evals._SCORE_TRANSPORT = ""
evals._verify_landed = lambda tid, name: True

via = evals.report_scores("r1", {"note_quality": 0.91}, comment="测试")
assert via == "sdk"
assert sdk_calls[0]["trace_id"] == evals.trace_id_of("r1")  # 自定义 trace_id 回挂
assert sdk_calls[0]["name"] == "note_quality" and sdk_calls[0]["value"] == 0.91

# 黑洞：发完不落库 → 降级 OTLP（本进程内粘滞）
observability._reset_for_tests()
spans = InMemorySpanExporter()
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

observability.init(span_exporter=spans, metric_reader=InMemoryMetricReader())
evals._SCORE_TRANSPORT = ""
evals._verify_landed = lambda tid, name: False
sdk_calls.clear()
via = evals.report_scores("r2", {"note_quality": 0.5}, comment="黑洞")
assert via == "otlp", via
ev = [s for s in spans.get_finished_spans() if s.name == "eval"]
assert ev and ev[0].attributes["note_quality"] == "0.5000"
# 粘滞：下一条不再走 SDK 试错
sdk_calls.clear()
assert evals.report_scores("r2", {"note_quality": 0.6}) == "otlp"
assert sdk_calls == []

# 无凭证且无 collector 端点 = off
envmap.clear()
evals._SCORE_TRANSPORT = ""
assert evals.report_scores("r3", {"note_quality": 0.5}) == "off"
assert evals.report_scores("r3", {}) == "off"


# ---- _load_json_lenient：中转回包各形态 ---------------------------------------
J = evals._load_json_lenient
assert J('{"a": 1}') == {"a": 1}
assert J('```json\n{"a": 1}\n```') == {"a": 1}                 # 围栏
assert J('好的，结果如下：{"a": 1} 以上供参考') == {"a": 1}        # 前言后语
assert J('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}     # 尾逗号
assert J('前缀 {示例} 干扰 {"a": 1}') == {"a": 1}                # 前言里的假对象被跳过
assert J('{"reason": "用 {x} 表示\\"} 结束"}') == {"reason": '用 {x} 表示"} 结束'}  # 串内括号+转义
for bad in ("没有任何括号", '{"a": 1', "", "   "):
    try:
        J(bad)
        raise AssertionError(f"应抛 ValueError: {bad!r}")
    except ValueError:
        pass
print("✅ _load_json_lenient：围栏/前言后语/假对象/尾逗号/串内括号/垃圾拒收")


# ---- _Judge.generate(prompt, schema)：强解析 + 重问修复 + 模型透传 -------------
from video2pdf import llm as _llm_mod

_real_chat = _llm_mod.chat


class _FakeSchema:
    def __init__(self, verdict):
        self.verdict = verdict


# 首次烂 JSON → 重问一次（附上次输出截断）→ 修复成功
_calls = []
_llm_mod.chat = lambda system, user, **kw: _calls.append(user) or (
    "好的我认为 verdict 是 yes（不好意思没按格式）" if len(_calls) == 1 else '{"verdict": "yes"}')
judge = _real_judge_deepeval()
out = judge.generate("给个结论", schema=_FakeSchema)
assert out.verdict == "yes" and len(_calls) == 2, _calls
assert "上次输出" in _calls[1]

# 重问还烂 → 抛 ValueError（score_note 按既有口径跳过该指标，不炸批）
_llm_mod.chat = lambda *a, **kw: "还是没学好"
judge = _real_judge_deepeval()
try:
    judge.generate("x", schema=_FakeSchema)
    raise AssertionError("应抛 ValueError")
except ValueError:
    pass

# schema=None（deepeval 直接 generate 的调用方）原样回原文
_llm_mod.chat = lambda *a, **kw: "随便什么文本"
judge = _real_judge_deepeval()
assert judge.generate("x") == "随便什么文本"

# V2PDF_JUDGE_MODEL 透传到 llm.chat；缺省回退 LLM_MODEL
_seen = {}
_llm_mod.chat = lambda system, user, **kw: _seen.update(kw) or '{"verdict": "yes"}'
judge = _real_judge_deepeval()
envmap["V2PDF_JUDGE_MODEL"] = "Fast-Judge"
judge.generate("x", schema=_FakeSchema)
assert _seen.get("model") == "Fast-Judge", _seen
envmap.pop("V2PDF_JUDGE_MODEL")
_seen.clear()
judge.generate("x", schema=_FakeSchema)
assert _seen.get("model") == _llm_mod.LLM_MODEL, _seen
_llm_mod.chat = _real_chat
print("✅ _Judge：schema 强解析/重问修复/双烂抛出/schema=None 原文/V2PDF_JUDGE_MODEL 透传")


# ---- _RagasJudge：老式 generate → LLMResult，走 llm.chat ------------------------
import asyncio

_calls2 = []
_llm_mod.chat = lambda system, user, **kw: _calls2.append(user) or '{"verdict": "ok"}'
judge_r = _real_judge_ragas()
res = asyncio.run(judge_r.generate("给个判定", n=1))
assert res.generations[0][0].text == '{"verdict": "ok"}' and len(_calls2) == 1


class _PV:  # PromptValue 形态：to_string()
    def to_string(self):
        return "对象形态 prompt"


res2 = asyncio.run(judge_r.generate(_PV(), n=2))
assert len(res2.generations[0]) == 2 and _calls2[-1] == "对象形态 prompt"
judge_r.set_run_config(object())  # evaluate() 的 run_config 注入被接住
assert judge_r.is_finished(res) is True
_llm_mod.chat = _real_chat
print("✅ _RagasJudge：generate→LLMResult/PromptValue 兼容/n 多采样/run_config 注入")


# ---- report_scores verify=False + verify_pending：整批一次核验 -----------------
envmap.update(LANGFUSE_PK="pk-t", LANGFUSE_SK="sk-t", LANGFUSE_HOST="https://lf.test")
evals._SCORE_TRANSPORT = ""
_sleeps = []
_real_sleep = evals.time.sleep
evals.time.sleep = _sleeps.append  # 记录而非真等（time 是全局模块，用后必须复原）
evals._verify_landed = lambda tid, name: True
sdk_calls.clear()
via = evals.report_scores("rb1", {"note_quality": 0.9}, comment="c", verify=False)
assert via == "sdk" and _sleeps == [] and evals._SCORE_TRANSPORT == ""  # 发完即走未定性
assert evals.verify_pending({"rb1": ({"note_quality": 0.9}, "c")}) == "sdk"
assert _sleeps == [20] and evals._SCORE_TRANSPORT == "sdk"              # 整批只等一次

# 黑洞批次：verify_pending 发现未落库 → OTLP 重报 + 进程粘滞降级
observability._reset_for_tests()
spans2 = InMemorySpanExporter()
observability.init(span_exporter=spans2, metric_reader=InMemoryMetricReader())
evals._SCORE_TRANSPORT = ""
evals._verify_landed = lambda tid, name: False
_sleeps.clear()
sdk_calls.clear()
evals.report_scores("rb2", {"note_quality": 0.5}, comment="x", verify=False)
assert evals.verify_pending({"rb2": ({"note_quality": 0.5}, "x")}) == "otlp"
assert evals._SCORE_TRANSPORT == "otlp" and _sleeps == [20]
ev2 = [s for s in spans2.get_finished_spans() if s.name == "eval"]
assert ev2 and ev2[0].attributes["note_quality"] == "0.5000"
evals.time.sleep = _real_sleep
print("✅ report_scores verify=False + verify_pending：批量核验/黑洞 OTLP 重报")

observability._reset_for_tests()
print("✅ test_evals.py 全部通过")

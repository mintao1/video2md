"""LangGraph 图编排层自动插桩：节点生命周期 / state 增量 / 路由决策（边）/ 子图就绪。

机制（langgraph 1.2.12 实证，2026-09-22 spike）：graph.invoke(config={"callbacks": [handler]})
后无需逐节点改代码——每个节点（RunnableCallable, trace=True）发 on_chain_start(inputs) /
on_chain_end(节点返回的 state 增量) / on_chain_error；条件路由函数（_route_ok/_route_note）
本身也发事件，其 on_chain_end(outputs) 就是路由决策串——「走哪条边」直接可见。事件
metadata（仅 START 带，END 不带，靠 run_id 映射表配对）恒有 langgraph_step / langgraph_node /
langgraph_checkpoint_ns（子图时以 | 分层）——将来 add_node(编译子图) 自动覆盖，无需改本模块。

设计纪律（对齐 observability.py 既有约定）：
- handler 鸭子类型（只补 ignore_chain/raise_error 两个类属性），零 import langchain_core：
  langchain-core 是 langgraph 的硬依赖（内存里本来就有），运行时代码不直接引用它；
  handle_event 吞掉 handler 全部异常，本模块再 try/except——插桩不可能弄炸主管线。
- 事件走 RunLog.info → jsonl 先落盘 → observability.record：与 stage/llm span 同一条
  确定性 trace（sha256("video2md-"+run_id)），Langfuse 侧为零时长点事件
  （circuit_breaker/partial_failure 同款）；不碰 _run_stage 每阶段 close_run 语义
  （跨阶段边界的真 span 会被强制闭合，OTel 评审存档定调零改动）。
- state 只发标量（entry_idx/status/errors 计数/键名）：run 大 dict 一律不进事件，
  字符串走 RunLog 统一脱敏。
- 每节点开始把 graph RunLog 注册进 _STAGE_LOGS（阶段节点随后覆盖、自弹；这里收自己的
  尾巴）→ select_frames_llm 等非阶段节点内的 llm_task 从此有活跃 RunLog，llm:select_frames
  span 补齐（llm.py 零改动）。
"""
from __future__ import annotations

import time

from .util import _STAGE_LOGS, RunLog, redact


class GraphObsHandler:
    """run_graph 的回调 handler（config["callbacks"]）。只实现同步链路（本项目仅 invoke）。

    事件分类（1.2.12 字段实证）：
    - START 无 langgraph_node metadata → 顶层图开始；name="LangGraph" 且带 metadata →
      嵌套子图边界（跳过，内层节点自带 | 分层 ns）
    - START name == metadata.langgraph_node → 节点；name != 它 → 该节点出边的路由函数
    - END 无 name 参数，全部靠 run_id 在 _t0/_router 映射表里配对
    """

    ignore_chain = False  # langchain_core.handle_event 需要的类属性（鸭子类型零 import）
    raise_error = False

    def __init__(self, run_id: str, trace_name: str = ""):
        self.log = RunLog("graph", run_id=run_id, trace_name=trace_name)
        self._graph_rid = ""  # 顶层图级事件的 run_id（graph:end 配对用）
        self._t0: dict[str, tuple[str, int, float]] = {}  # 事件 run_id → (节点, step, t0)
        self._router: dict[str, tuple[str, str]] = {}     # 事件 run_id → (路由函数名, 源节点)

    # ---------------------------------------------------------------- 回调入口
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       tags=None, metadata=None, **kw) -> None:
        try:
            self._start(str(run_id), str(kw.get("name") or ""), metadata or {}, inputs)
        except Exception:
            pass  # 观测永不破坏主管线（handle_event 吞错之外的双保险）

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None,
                     tags=None, metadata=None, **kw) -> None:
        try:
            self._end(str(run_id), outputs)
        except Exception:
            pass
        finally:
            self._unregister()  # 事件写炸不连累注册表清洁（幂等：只弹自己的）

    def on_chain_error(self, error, *, run_id, parent_run_id=None,
                       tags=None, metadata=None, **kw) -> None:
        try:
            rid = str(run_id)
            router = self._router.pop(rid, None)
            ent = self._t0.pop(rid, None)
            who = (router or ent or ("",))[0] if (router or ent) else ""
            if who:
                self.log.info("graph:error", node=who, error=redact(str(error))[:300])
        except Exception:
            pass
        finally:
            self._unregister()

    # ---------------------------------------------------------------- 内部
    def _start(self, rid: str, name: str, meta: dict, inputs) -> None:
        node = meta.get("langgraph_node")
        if node is None:
            if not self._graph_rid:  # 顶层图 START：root span 已由 deadman 开立，只留初始标量
                self._graph_rid = rid
                st = inputs if isinstance(inputs, dict) else {}
                self.log.info("graph:start", url=str(st.get("url") or ""))
            return
        if name == "LangGraph":
            return  # 嵌套子图边界：外层节点事件已覆盖，内层节点另发
        step = int(meta.get("langgraph_step") or 0)
        if name == node:  # 节点开始
            self._t0[rid] = (node, step, time.monotonic())
            # graph log 注册进 _STAGE_LOGS：非阶段节点内 llm_task 据此挂 span（select_frames
            # 补缺）。先注册后写事件——事件写炸不许连累注册（阶段节点随后覆盖、自弹）
            _STAGE_LOGS.setdefault(self.log.run_id, self.log)
            fields = {"node": node, "step": step}
            ns = str(meta.get("langgraph_checkpoint_ns") or "")
            if "|" in ns:  # 子图深度（当前无子图；有则自动带上）
                fields["ns"] = ns
            self.log.info("graph:node", **fields)
        else:  # 路由函数（挂在 <node> 的出边上）
            self._router[rid] = (name, str(node))

    def _end(self, rid: str, outputs) -> None:
        router = self._router.pop(rid, None)
        if router is not None:  # 路由决策 = 走哪条边（ok/fail/loop/done…）
            dec = outputs if isinstance(outputs, (str, int, float, bool)) else str(outputs)[:80]
            self.log.info("graph:route", router=router[0], frm=router[1], decision=dec)
            return
        ent = self._t0.pop(rid, None)
        if ent is None:
            if rid == self._graph_rid:  # 图级 END：终态留痕
                st = outputs if isinstance(outputs, dict) else {}
                self.log.info("graph:end", status=str(st.get("status") or ""))
            return
        node, step, t0 = ent
        out = outputs if isinstance(outputs, dict) else {}
        fields = {"node": node, "step": step, "durMs": round((time.monotonic() - t0) * 1000),
                  "keys": ",".join(out.keys())}
        if out.get("entry_idx") is not None:
            fields["entry_idx"] = out["entry_idx"]  # note 回环游标
        if out.get("status"):
            fields["status"] = out["status"]
        if isinstance(out.get("errors"), list):
            fields["errors_n"] = len(out["errors"])
        self.log.info("graph:state", **fields)

    def _unregister(self) -> None:
        """收自己的尾巴：阶段节点自己注册/弹出，只在本 log 仍注册时弹出（幂等）。"""
        if _STAGE_LOGS.get(self.log.run_id) is self.log:
            _STAGE_LOGS.pop(self.log.run_id, None)

"""LangGraph 图状态（RunState）。

Dify 抽离后，run 记录（runs.py 的 dict：entries/artifacts/stages）直接作为图状态
在节点间流动——全部 JSON 可序列化，SqliteSaver 才能落 checkpoint。
恢复语义不变：进程崩溃后重发同 URL 即新 run，state.json 幂等跳过已完成条目
（旧系统就是这个语义）；checkpoint 服务观测与逐条推进，不做 mid-node 续跑。
"""
from __future__ import annotations

from typing import TypedDict



class RunState(TypedDict, total=False):
    run_id: str          # 图 thread_id == run_id：checkpoint 与 runs.json 同键
    url: str             # 原始输入（可能带分享文本，resolve 节点负责剥）
    part_items: str      # 指定条目（1,3-5；空=全部）
    force: bool          # True=忽略 state 幂等全量重跑
    review: bool         # True=整理后过一遍校对 LLM
    max_frames: int      # 目标帧数（select 启发式兜底也用）

    run: dict            # runs.py 的 run 记录（entries/artifacts/stages/status/tmp）
    select_payload: dict  # 选帧 LLM 过滤后的 _stage_select args（{"frames"|"selections"}）
    select_note: str     # 选帧兜底提示（LLM 空选时带一句话进 run 记录）
    entry_idx: int       # note 逐条目循环游标（1 起）

    errors: list[str]    # 失败留痕（阶段熔断/LLM 三连败/条目级拒绝写盘）
    status: str          # running / done / error / partial

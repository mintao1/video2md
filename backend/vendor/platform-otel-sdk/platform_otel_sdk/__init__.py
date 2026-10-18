"""platform-otel-sdk: 统一 OTel SDK 初始化 + RunLog → traces/metrics 桥。

用法：
    import platform_otel_sdk as otel

    otel.init_otel(
        service_name="video2md-serve",
        service_namespace="video2md",
        hash_prefix="video2md-",
        trace_tags=["video2md"],
    )

    # 在 RunLog._write 回调中
    otel.record(rec, run_id=run_id, label=label, trace_name=trace_name)

    # 进程退出时
    otel.flush()

设计约束：
    - 不注册全局 provider（模块内自引用，测试可反复 init/reset）
    - 端点优先级：OTEL_EXPORTER_OTLP_ENDPOINT > LANGFUSE_PK/SK > no-op
    - atexit 自动 flush
    - drain() 确保 BatchSpanProcessor 根 span 送达
"""
from __future__ import annotations

from ._otel import (
    _RUNS,
    _RunTrace,
    close_run,
    drain,
    flush,
    get_meter,
    get_tracer,
    init_otel,
    open_run_ids,
    record,
    register_runtime_gauges,
    reset_for_tests,
)

__all__ = [
    "init_otel",
    "get_tracer",
    "get_meter",
    "record",
    "close_run",
    "flush",
    "drain",
    "open_run_ids",
    "register_runtime_gauges",
    "reset_for_tests",
    # 供产品自定义 record() 使用
    "_RunTrace",
    "_RUNS",
]
# 设计评审回应：OTel 批判逐条核对（为什么 traceId 用 sha256 约定而非 SDK 派生）

> 评审日期：2026-09-22 · 对象：一份外部架构批判（「拥抱 OTel SDK 而非绕开它」五点修正案）
> 背景：2026-09-22 观测栈定稿（`video2md/observability.py` + `docker-compose.obs.yml`）后的复核
> 上篇：[[2026-09-21-script-observability]]（观测设计的调研依据）

## TL;DR

1. **批判的核心诉求已在定稿栈全部实现**——OTel SDK 化（无手写 metrics）、collector
   分流、memory_limiter 置首、Prometheus 抓 `:8889`。它批判的是更早的设计迭代，
   定稿栈基本就是这轮批判的产物。
2. **三处硬伤不采纳**：①「OTel 下应用无法自定 traceId」不成立（SpanContext 构造是
   合法 SDK 机制，当日实测穿透到 Langfuse）；②`create_trace_id(seed)` 对齐方案内部
   不一致（私有派生 + 打破依赖隔离）；③「`/fail` 不告警」说反了（那是 healthchecks.io
   的标准告警路径）。
3. **两个可落地 delta 按决策落定**：RAGAS 保留（跨项目复用）；
   `resource_to_telemetry_conversion` 不配（单服务暂无收益）。
4. **结论：零代码改动**，架构维持 sha256 确定性 traceId + OTel SpanContext 锚。

## 一、逐条判定

| 批判建议 | 定稿栈现状 | 判定 |
|---|---|---|
| 拥抱 OTel SDK，metrics 用 Counter/Histogram | `observability.py:134-155` 全部 OTel instrument（counter×5 / histogram×2 / gauge×2）；不存在手写 metrics.py，api 从无 `/metrics` 端点 | ✅ 已实现 |
| Prometheus 抓 collector:8889 | `deploy/prometheus.yml` 只抓 `127.0.0.1:8889` | ✅ 已实现 |
| collector 补 memory_limiter 且放最前 | `deploy/otel-collector.yml`：memory_limiter(256MiB) → batch(2s/512) | ✅ 已实现 |
| 死人开关语义需澄清 | `deadman.py` start/ok/fail 三态 | ⚠️ 批判对 healthchecks.io 语义描述有误 |
| Trace ID 改用 `create_trace_id(seed=)` | sha256 约定 + SpanContext 锚 | ❌ 核心论据不成立（见二.1） |
| RAGAS 无 embedding 就不引入 | ragas 在 evals extra，缺 embedding 默认跳过 | 💡 有效观点，按决策保留（见四） |

## 二、三处硬伤的论证

### 1. 「OTel 协议下应用无法强制指定 trace ID」——不成立

SDK 默认随机生成，但**经 context 传入是合法 API**：跨服务传播本就依赖 remote parent
SpanContext 携带 trace_id、SDK 照单全收。`observability.py:177-181` 用伪造 remote
锚点（`SpanContext(trace_id=sha256("serve-"+run_id)[:32], is_remote=True)`）钉死
traceId，**2026-09-22 实测端到端成立**：trace `0x5f3c19a3…`（=
`sha256("serve-da76c99b")[:32]`）原样穿透 app→OTLP→collector→otlphttp→Langfuse
Cloud，UI 可查。同日 233 个历史 run 的回放（`backend/scripts/otel_replay.py`）复用同一机制
批量验证通过。

### 2. `create_trace_id(seed=...)` 方案内部不一致

`create_trace_id` 是 Langfuse SDK 的私有派生（非 sha256）。若 evals 用它、在线链路走
OTel（SDK 自生成或自定 id），两边**对不上**，分数回挂失效——除非 app 也 import
langfuse SDK 建 trace，这恰好打破了批判自己赞许的「依赖隔离在 evals 路径」。sha256
约定的真正价值：**四个互不依赖的组件各自独立推导出同一 trace id**——
app（observability）、`__stage` 子进程、evals（`evals.trace_id_of`）、replay 脚本，
谁都不用拉 Langfuse SDK。

### 3. healthchecks.io 的 `/fail` 语义说反了

`/fail` 会把 check 标记为 down 并**默认触发告警**（与「ping 缺失」并列的第二条告警
路径）。当前 start/ok/fail 三态即其教科书用法：`/start` 计运行时长、ok 重置、
`/fail` 显式报障。deadman 的设计意图（头注释）：开关只回答「跑没跑完」，
done/partial 都算跑完——trace/metrics 留痕失败细节，开关管死活。

## 三、决策记录

- **RAGAS 保留**：批判的「无 embedding 就移除」逻辑成立，但 ragas 被**其它项目复用**，
  保留安装（缺 embedding 端点时指标默认跳过的行为不变）。
- **`resource_to_telemetry_conversion: enabled: true` 不配**：metrics 会丢
  `service.name` 标签，但单服务场景无区分需求；将来多服务再加。
- **遗留待办修正（2026-09-22 复核）**：`evals.py` 的核验**已经**走
  `GET /api/public/v3/scores?traceId=`（`_verify_landed`，含同日实测注释：
  legacy 读写 API 410、v2 410、v3 可用）——本存档早先草稿所记「verify_pending
  走 legacy API 待修」不实，实际无需修改。本日独立复测结论一致：traces 侧
  legacy 404、v2 observations 读路径不可靠；scores 侧 v3 是唯一可用读端点。

## 四、当日实测证据链

| 验证项 | 结果 |
|---|---|
| OTLP 端点连通（直连，不经 17898 代理） | HTTP 200，ingestion job 入队 |
| 确定性 traceId 穿透 Langfuse | `0x5f3c19a3…` UI 可查（= sha256 约定值） |
| 服务器 6h 生产 trace 落库 | UI 确认 ✅ |
| 233 runs / 5165 事件回放 | UI 确认 ✅（`replayed=true` 可过滤） |
| legacy 读 API | 新组织 404（LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION） |
| v2 observations 读路径 | 不可靠：数据短暂可见后查空（摄入本身正常） |
| 17898 代理出口对 cloud.langfuse.com | TLS 掐断（SSL_ERROR_SYSCALL）——遥测出墙走直连 |

# 08 · 容量规划与 SLO

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：② 性能与容量（🔴 H：全维度空白）
- **本文是框架 + 测量方法；`⏳` 占位由 ROADMAP P1-6（容量实测）清偿，SLO 草案由 P1-5 评审固化。**

## SLO 草案（P1-5 评审前的工作假设）

| SLI | 定义 | 目标（草案） | 测量 |
|---|---|---|---|
| run 成功率 | 7 日内终态为 done/partial 的 run ÷ 全部终态 run（剔除输入错误 4xx 口径） | ≥ 99% | `v2md_runs` counter 按终态标签 |
| 提交接纳可用性 | POST /runs 非 5xx 比例（429 配额拒绝不计入失败） | ≥ 99.9%（30d） | 请求审计 jsonl / 反代日志 |
| SSE 首事件延迟 | connected → 首个业务事件 | p95 < 3s | ⏳ 需加直方图（P1-5 一并落） |
| bundle 投递成功率 | bundle 200 ÷ (200+410 非过期口径) | ≥ 99% | `api-requests.jsonl` |

**错误预算**：run 成功率月预算 = 1% × 月 run 数；预算消耗过半 → 冻结非修复性发布（纪律入部署清单）。

## 容量模型（框架）

### 已知常量与默认值

| 参数 | 值 | 出处 |
|---|---|---|
| 执行并发 | `V2MD_MAX_CONCURRENT_RUNS` 默认 1（单 worker 语义） | `config.py:133` |
| 总队列上限 | `V2MD_QUEUE_MAX` 默认 100 | `config.py:134` |
| 每用户在途 | `V2MD_USER_QUEUE_MAX` 默认 5 | `config.py:135` |
| 提交滑窗 | `V2MD_USER_SUBMIT_PER_MIN` 默认 30/分 | `config.py:136` |
| 上传单文件/配额/TTL | 2GB / 5GB每用户 / 24h | `config.py:141-143` |
| outbox TTL | 24h | `config.py:119` |
| 内存任务表 | `_TASKS_KEEP=200` 裁最老完成项 | `api.py:236` |
| 转录上限 | 6 万字符 ≈ 3h 密集语音 | `config.py:10` |
| 服务器基线 | ≥2C4G，无 GPU | README 部署章 |

### 待实测项（P1-6 负载测试产出后回填）

| 问题 | 测量方法 | 实测值 |
|---|---|---|
| 单 worker 吞吐 | mockpool 假算力 + `V2MD_FAKE_LLM`，N=50 连续 run 计完成速率 | ⏳ |
| 单 run 资源画像 | 1h 视频的 tmp 峰值/内存峰值/耗时分解（各阶段 `v2md_stage_seconds`） | ⏳ |
| 队列积压拐点 | 并发提交斜坡（1→50 并发），记 429 起点与队列消化曲线 | ⏳ |
| 磁盘增长 | runs.json/jsonb 每千 run 字节数；uploads/outbox 稳态驻留（配额×活跃用户） | ⏳ |
| PG 池压力 | 并发>1 + PG 档，观测 `max_size=5` 是否饱和（池等待耗时） | ⏳ |
| SSE 扇出上限 | 单 run N 个并发 SSE 订阅，队列丢弃起点 | ⏳ |

### 扩容经验法则（实测前的保守口径）

- 并发 >1 前：checkpointer 切 postgres（sqlite 单连接弱）；vault 自托管保持 1。
- 并发 N 的内存预算：每在跑 run ≈ ⏳（P1-6 回填；tmp 目录 1h 视频数百 MB 量级，2GB 上传上限×并发数是磁盘预算下限）。
- 多副本（P1-1 闭环后）：队列读侧已跨副本一致；水平扩容的下一个瓶颈是 outbox 本地卷（→ P2-4 对象存储化）。

## 成本观测口径（现状）

Langfuse 按 uid 归因 token/成本（`observability.py:203-215`）；gpu_router 侧冷却/用量记账在 `gpu-router-logs` 卷。**无预算闸**（P2-2）。

## 已知限制

本篇全部实测数字缺位（批判②维度 3 定为 H 的原因）——在 P1-6 清偿前，任何「能接多少用户」的回答都是猜测。

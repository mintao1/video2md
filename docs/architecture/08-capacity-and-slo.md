# 08 · 容量规划与 SLO

- 最后核对：2026-09-25 ｜ 基准：HEAD≈4438ef4（容量实测 P1-6 落地）
- 批判参照：② 性能与容量（🔴 H：全维度空白）
- **本文框架 + 实测基线（P1-6 ✅ mock 算力口径）+ SLO 成文（P1-5 ✅ 待评审）。**

## SLO（P1-5 成文，2026-09-25；评审状态见 ROADMAP 标注）

每个 SLI 给**精确口径**（分子/分母/剔除）、测量管道现状与燃烧告警参数——教科书纪律：
**没有仪表的 SLO 是许愿**，管道缺位的项如实标注依赖（P0-4/后续仪表），不虚报可测。

| SLI | 精确口径 | 目标 | 测量管道 | 燃烧告警 |
|---|---|---|---|---|
| **run 成功率** | 7d 窗口：终态 error ÷ 全部终态（done\|partial\|error）。输入错误在 resolve 期 4xx、**不产生 run 记录**故天然剔除；**cancelled 无法与管线 error 区分，计入失败侧（保守：宁紧勿松）** | ≥ 99%（预算 1% = 1.68h/7d） | ✅ `v2md_runs_total{status}`（Prometheus） | ✅ RunSLOBudgetFastBurn 14.4×/1h+5m 双窗；RunSLOBudgetSlowBurn 6×/6h+30m 双窗（多窗多燃率：长短窗同燃才报） |
| **SSE 首事件延迟** | connected 快照发出 → 首个业务事件转发（同连接） | p95 < 3s | ✅ `v2md_sse_first_event_seconds` 直方图（P1-5 本批落，桶 0.1–10s 含 3s 判读点） | 暂无（直方图先攒分布，告警阈值待基线） |
| 提交接纳可用性 | POST /runs 非 5xx ÷ 全部（429 配额拒绝**不计失败**——配额闸按设计工作） | ≥ 99.9%（30d） | ⚠️ 请求级计数器缺位：现状只能事后审计 `api-requests.jsonl`（非实时）；Prometheus 化待请求计数仪表（下批）或反代 access log（P0-4） | 待管道 |
| bundle 投递成功率 | bundle 200 ÷ (200+410 非过期口径) | ≥ 99% | ⚠️ 同上 jsonl 事后审计 | 待管道 |

**错误预算纪律**：run 成功率 7d 预算 1.68h；**预算消耗过半 → 冻结非修复性发布**（纪律入部署清单）。
**个人规模流量注（诚实口径）**：小样本下比率 SLI 呈 0/1 跳变，燃烧告警语义弱化为「窗口内全错」——
RunFailed/RunPartial 事件告警仍是主力，燃烧告警是 SLO 问责口径的成文锚点（规则内同注）。
**合法空闲语义**：无终态流量时比率无定义 → 不告警（「无指标≠故障」，与 gpu_router 仓 §10 同口径）。

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

| 问题 | 测量方法 | 实测值（2026-09-25，**mock 算力口径**——管线开销上限，非生产吞吐；真实 GPU 延迟归 gpu_router EMA 账本域） |
|---|---|---|
| 单 worker 吞吐 | `scripts/loadtest.py --runs N`（mockpool+假LLM，串行闸=1，8s 夹具） | json 档 **0.1–0.3 s/run**（两轮方差=冷热缓存）≈ 1.2万–2.5万 run/h 上限；PG 档全链 **0.8 s/run** ≈ 4800 run/h |
| 单 run 资源画像 | RSS 增量 + tmp 构成（转录/OCR 在远端不驻留本地） | api RSS 增量 ≈ **1 MB/run 量级**（10 run +12MB）；tmp 以视频轨主导：8s 夹具 2.8MB → 1h 外推 **≈1.2 GB**（×450 线性）+ 帧上限 60 + 节段 20s×N；阶段耗时 mock 口径无意义不填 |
| 队列积压拐点 | 同用户并发斜坡 1→50（默认限流档） | 429 首现 **c=10（RATE_LIMITED 30/min）**；c=6 并发无 USER_QUEUE_FULL——**admit TOCTOU 窗口**（补遗②）；50 并发无 5xx |
| 磁盘增长 | runs.json 字节/N；`pg_total_relation_size('v2md_runs')`/N | runs.json **≈1 KB/run**；PG v2md_runs **≈32 KB/run**（含索引；10 行小样本——表/索引固定页未摊薄，大样本会显著低于此值）；uploads 稳态=单文件≤2GB×TTL 24h 内活跃数，outbox 同式 |
| PG 池压力 | PG 档全链 N run 观测 PoolTimeout | 10 run **零 PoolTimeout**（P1-8 预算 10s）；代价=每 run ~0.7s 落库开销（0.8 vs 0.1–0.3s，如实计价）；并发>1 的饱和观测待真实多副本形态 |
| SSE 扇出上限 | 单 run N 并发 SSE 订阅收终态 | **N=20：20/20 无丢失**；队列上限 500 事件/订阅=理论界（短 run ~60 事件未触及），慢消费者超 500 积压才开始丢新事件 |

复跑：`uv run python scripts/loadtest.py --runs 10 --sse 20 [--pg]`（需 `V2MD_TEST_PG_DSN`）。

### 扩容经验法则（实测前的保守口径）

- 并发 >1 前：checkpointer 切 postgres（sqlite 单连接弱）；vault 自托管保持 1。
- 并发 N 的内存预算：每在跑 run ≈ ⏳（P1-6 回填；tmp 目录 1h 视频数百 MB 量级，2GB 上传上限×并发数是磁盘预算下限）。
- 多副本（P1-1 闭环后）：队列读侧已跨副本一致；水平扩容的下一个瓶颈是 outbox 本地卷（→ P2-4 对象存储化）。

## 成本观测口径（现状）

Langfuse 按 uid 归因 token/成本（`observability.py:203-215`）；gpu_router 侧冷却/用量记账在 `gpu-router-logs` 卷。**无预算闸**（P2-2）。

## 已知限制

本篇全部实测数字缺位（批判②维度 3 定为 H 的原因）——在 P1-6 清偿前，任何「能接多少用户」的回答都是猜测。

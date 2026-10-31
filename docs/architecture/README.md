# video2md 架构文档

本目录是 video2md 架构的**单一真相源（SSOT）**。README.md 只留产品定位与快速上手，架构/运维口径全部在这里。

## 阅读地图

| 篇 | 内容 | 读者 |
|---|---|---|
| [01-overview.md](01-overview.md) | 系统总览：上下文、核心数据流、设计哲学 | 所有人 |
| [02-runtime-topology.md](02-runtime-topology.md) | 运行拓扑与部署形态：进程/容器/网络视图、安全组口径 | 部署/运维 |
| [03-api-contract.md](03-api-contract.md) | HTTP 契约 + 鉴权账目：端点、错误信封、匿名面清单 | 前端/集成方 |
| [04-data-and-consistency.md](04-data-and-consistency.md) | 数据与一致性：存储分派、PG 表、幂等、备份、迁移纪律 | 后端 |
| [05-security.md](05-security.md) | 安全：威胁模型、鉴权、匿名面、密钥管理、上线检查清单 | 安全评审/部署 |
| [06-observability.md](06-observability.md) | 可观测性：三层模型、指标清单、告警链、watchdog | 运维/SRE |
| [07-gpu-pool-contract.md](07-gpu-pool-contract.md) | 算力池契约：gpu_plugins 协议、四种 provider、异步 job | 算力侧开发 |
| [08-capacity-and-slo.md](08-capacity-and-slo.md) | 容量规划与 SLO（含待实测占位） | SRE/容量规划 |
| [09-disaster-recovery.md](09-disaster-recovery.md) | 灾备 runbook：RTO/RPO、备份恢复、演练制度 | 运维/on-call |
| [10-rename-video2md.md](10-rename-video2md.md) | 项目命名与兼容策略 | 所有人 |
| [critique/](critique/) | 批判存档（有日期的快照，不更新）：十层对照 + 质量属性 | 评审/参考 |

## 文档纪律

1. **代码与文档冲突时，以代码为准**——并视为文档缺陷，发现即修（或登记批判）。
2. 每篇头部标「最后核对日期 + 核对基准 commit」；改代码动架构行为时同步改对应篇。
3. **形容词必须可指证**：「公平」「调度」「真相源」「高可用」这类词出现时，必须能在同段给出代码位置。
4. 已知限制不藏：每篇末「已知限制」节链接批判条目与 ROADMAP 编号，禁止用「未来支持」含糊带过。
5. 批判是**某时点的快照**（批判的是当时的代码与文档），批判条目被清偿后不删批判、更新 ROADMAP 状态。
6. 配置键以 `.env.example` 为准绳清单；文档引用键名必须与其一致。

## 十层布局（批判修订版，自 README「终局布局」迁入）

单公网入口、账目按 uid 隔离、算力池化、云端不储存、异地容灾。每层链到对应正篇；定级见 [批判①](critique/2026-09-24-ten-layer-review.md)。

| 层 | 布局（修订后口径） | 正篇 |
|---|---|---|
| 入口 | 域名 + 反代 TLS 是唯一公网入口；安全组只放 80/443，api/算力池/观测栈全回环。`/docs` 关闭当前是人工纪律（P0-4 机制化） | [02](02-runtime-topology.md) |
| 鉴权与账目 | acctkit 登录（Google id_token / 设备流 / 微信扫码）；身份表 `(provider, external_id) → uid`；run 按 uid 归属（越权 403、dedup 按 owner 隔离） | [03](03-api-contract.md) |
| 执行 | 单进程信号量并发闸门（无调度器，批判① E1）+ 配额三闸；任务表 **PG 读侧真相源**（`/queue`·配额·dedup·watchdog 大盘·启动认领走 DB）；进度事件 PG LISTEN/NOTIFY 扇出 | [04](04-data-and-consistency.md) |
| 状态 | Postgres 承载 users/runs/state + checkpointer（可选档）；json 单文件属开发机。⚠ 无 schema 版本化（P0-5） | [04](04-data-and-consistency.md) |
| 产物 | deliver「云端不储存」：outbox 在架 → bundle zip → ACK/TTL 即焚（物理删）；上传面 + 路径白名单 | [03](03-api-contract.md) |
| 限流配额 | 应用内提交三闸（429 信封）+ 磁盘配额 + 队列上限；**匿名面限流待 P0-2**、反代层限流待 P0-4 | [05](05-security.md) |
| 算力 | gpu_router 算力池独立伸缩（Colab/Kaggle/Modal/ZeroGPU/国内云），api 零 GPU | [07](07-gpu-pool-contract.md) |
| 数据安全 | 本地备份（daily/weekly + manifest）+ rclone 异地；RTO/RPO 与演练制度见 09 篇 | [09](09-disaster-recovery.md) |
| 发布 | CI → GHCR 不可变 tag → `deploy.sh`（目标机构建）+ 体检 → tag 回滚；GHCR 为手动灾备存档（E6 口径） | [02](02-runtime-topology.md) |
| 观测告警 | OTel → collector → Langfuse + Prometheus/Grafana；Alertmanager → 飞书；死人开关 + 卡死看门狗 | [06](06-observability.md) |

## 落地状态追踪（原 README「落地状态」迁入）

| 阶段 | 落地内容 | 状态 |
|---|---|---|
| P1/P2（e320ee5） | 并发闸门 `V2MD_MAX_CONCURRENT_RUNS`、配额限流三闸（429 统一信封）、`POST /uploads` 上传面 + 路径白名单、GHCR 发布流水线（tag 驱动 + `deploy.sh`）、`backup.sh` rclone 异地钩子 | ✅ |
| P3 | 存储分派 `json｜postgres`：`V2MD_RUNS/STATE/USERS_STORE` 三存储 + checkpointer 同池、`V2MD_EVENTS=pg`（LISTEN/NOTIFY 跨进程 SSE 扇出）、存量导入 `backend/scripts/migrate_json_to_pg.py` | ✅ |
| P4b（3f7bb1a） | 任务表 **PG 读侧真相源**（`V2MD_TASKS_STORE=postgres`）：`/queue`·配额·dedup·watchdog 大盘巡检·启动认领恢复走 `v2pdf_runs`；events `v2md_cmds` 心跳广播；多副本 tmp 保护 | ✅（读侧；写路径仍进程内，见 [04 篇](04-data-and-consistency.md#任务表两档口径)） |

**距「完整多副本生产」尚差四件**（原 README 出界项，全部已入 ROADMAP）：

| # | 缺口 | ROADMAP |
|---|---|---|
| 1 | `claimed_by` 租约认领（queued 态心跳 → 跨副本工作窃取） | P1-1 |
| 2 | 跨进程 cancel 端点（cmd 通道转发） | P1-2 |
| 3 | rescue/migrate 工具 PG 档适配 | P1-3 ✅（env 自动分派；E2E test_rescue_pg.py） |
| 4 | 多副本真双进程冒烟进 CI（现为单进程双实例模拟 + 一次性脚本） | P1-4 |

> 口径勘误（2026-09-24 批判① E4）：提交 3f7bb1a/f6a4a0e 所称「双进程冒烟全绿」实为**真库 + 单进程双实例模拟**（`test_pg_stores.py:159-211`，`_simulate_other_proc` 手工 NOTIFY 注入）。跨进程语义的端到端验证闭环待 P1-4。

# 批判②：ISO/IEC 25010 质量属性横切评估

- **批判对象**：video2md 系统整体（代码 + 部署资产 + 运维口径），对照「企业级云端生产」基线
- **方法**：ISO/IEC 25010 质量模型裁剪为八维；每维给 **现状证据 → 风险评级 → 与企业基线的差距 → ROADMAP 指向**
- **风险评级**：🔴H = 上公网前必须清偿；🟡M = 上量前必须；🟢L = 规模化阶段增强
- **与批判①的关系**：① 按「层」做叙述-现实对照（改错）；本篇按「属性」做横切定价（补缺）。同一缺口在两篇中互相引用（如 `①E3` = 批判①硬错误 3 号）
- 核对日期 2026-09-24，HEAD=f6a4a0e

---

## 计分卡总览

| 维度 | 评级 | 一句话结论 | ROADMAP |
|---|---|---|---|
| 1. 安全性 | 🔴 H | 认证面精致、匿名面裸奔；危险配置无防呆 | P0-1/2/3/4/6 |
| 2. 可演进性 | 🔴 H | 无 schema 版本化 = 在数据地基上计息 | P0-5, P1-9 |
| 3. 性能与容量 | 🔴 H | 全维度空白：无容量模型、无负载测试、无 SLO | P1-5/6 |
| 4. 可靠性 | 🟡 M | 幂等设计扎实；PG 池裸奔、验证闭环差最后一公里 | P1-1~4, P1-8 |
| 5. 可运维性 | 🟡 M | 脚本资产好；无 runbook、演练非制度化、部署有停机窗 | P1-7, P2-3 |
| 6. 可用性 | 🟡 M | 单副本 SPOF + 单 PG；恢复半自动 | P1-1, P2-1 |
| 7. 可观测性 | 🟢 L | 最强维度；缺 SLO 化告警与日志分级 | P1-5, P2-5 |
| 8. 成本 | 🟢 L | 有归因观测、无预算闸 | P2-2 |

---

## 1. 安全性 — 🔴 H

**现状证据**

做对的：owner 隔离 403（`api.py:283-296`）有契约测试；统一错误信封不泄栈（`api.py:634-661`）；上传流式 + 后缀白名单 + 配额 + TTL（`api.py:666-702`）；本地路径白名单三态闸（`api.py:353-376`）；密钥 SSOT 0600（`config.py:49-52`），git 历史无密钥泄漏（已扫）。

**缺口清单**（按严重度）：

| # | 缺口 | 证据 | 风险 |
|---|---|---|---|
| S1 | `V2MD_ENV=production` 与 `V2MD_AUTH=off` 可共存，启动不抗议 | `config.py` 无此校验；compose 已配 `V2MD_ENV=production`（`docker-compose.yml:56`） | 一次漏配 = 全量用户数据互通（共享 dev uid）。config.py 已有 fail-fast 先例（L113-115）却未覆盖最危险的组合 |
| S2 | `/auth/device/code`、`/auth/device/token` 匿名 + 零频控 | `api.py:708-739` | 可无限刷 Google device/code，耗 OAuth 配额、触 Google 风控（可能封 client_id） |
| S3 | `/preflight` 匿名返回内部路径、磁盘余量、算力池 provider 清单 | `api.py:93-141, 1035-1037` | 侦察面：攻击者据此画像基础设施 |
| S4 | `/docs`、`/openapi.json` 无条件开放 | `api.py:620-621` | 全量 API 契约公开（含错误码语义）；README 自述靠网关关，但网关配置不在仓（① 入口层） |
| S5 | 上传无 magic number 校验 | `api.py:350, 673` | 伪装后缀的任意内容可进入 yt-dlp/ffmpeg 解析路径（历史 CVE 密集区） |
| S6 | 脱敏正则只盖 `sk-`/`app-`/`dataset-` 三种前缀 | `util.py:112-118` | Google token、微信 secret、PG DSN 口令等形态进日志 jsonl 无遮盖 |
| S7 | compose PG 默认口令 `v2md-loopback-only` | `docker-compose.yml:98` | 回环绑定时可接受；DSN 指外部库/绑定误开即沦陷——默认值命名是唯一防线 |
| S8 | gpu_router `:9700` 无认证；colab quick tunnel 无鉴权公网 URL | `docker-compose.yml:25-26` 注释自述 | 「网络位置即信任」模型被 tunnel 打出公网缺口 |

**与企业基线的差距**：企业基线要求「危险配置启动期爆炸」（S1）、「匿名面先于认证面设防」（S2/S3/S4）、「上传内容嗅探」（S5）。当前防护重心恰好倒挂。

## 2. 可演进性 — 🔴 H

**现状证据**

做对的：存储分派缝（json|postgres 各三存储）、acctkit 零宿主可抽走、provider 注册缝、测试密度高（15 个测试文件、CI 三批门禁）、mypy/ruff 把关。

**核心缺口**：**无 schema 版本化迁移机制**。DDL 散在 store 构造函数（`pgstore.py:40-51, 88-93, 122-133`；`auth/pg_store.py:32-44`），`claimed_by` 靠 `ALTER TABLE IF NOT EXISTS` 就地打补丁（`pgstore.py:127`）。没有 `schema_version` 表、没有 migrations 目录、没有启动期漂移校验、没有回滚概念。

**为什么定为 H**：这是**以复利计息的技术债**。当前绿字段增量 DDL 对单库无害，但：(a) 第一个破坏性变更（拆 jsonb 提取列、改约束、清历史数据）到来时，代码与库结构的对齐全靠运气；(b) 多副本滚动部署时「新代码 + 旧库」窗口期无任何保护；(c) 迁移脚本 `migrate_json_to_pg.py` 是一次性工具，不是持续纪律。企业基线：schema 演进必须是版本化、可审计、可回滚（或有明确不回滚声明）的一等公民。

**次级缺口**：配置在 import 期加载为模块级单例（`config.py:65`），手写 `int()/float()` 散落（L123-156），无集中 schema——新加配置键无类型校验、无文档联动、测试靠快照机制绕行。

## 3. 性能与容量 — 🔴 H

**现状**：全维度空白。

- 无容量模型：`V2MD_MAX_CONCURRENT_RUNS` 默认 1，README 无「调到 N 需要什么资源」的任何指引（2C4G 起步是唯二数字之一）
- 无负载测试：CI 全是功能测试与单链冒烟，无并发压力/队列积压/磁盘增长的任何基准
- 无增长估算：uploads 配额 5GB/用户 × N 用户 = ？outbox TTL 24h 的峰值驻留 = ？runs.json/jsonb 记录随时间的膨胀率 = ？全部无答案
- 无 SLO：「服务多好算好」从未定义（详见可观测性维度）

**为什么定为 H**：不是「现在慢」，而是**无法回答任何容量问题**——「能接多少用户？」「磁盘多久撑爆？」「队列多深该扩容？」全都无从回答。对企业生产，「不知道自己不知道」比「知道但不够好」危险一个量级。⏳ 测量方法已写入 `08-capacity-and-slo.md`，P1-6 清偿。

## 4. 可靠性 — 🟡 M

**现状证据**

做对的（这是本系统的设计亮点）：幂等贯穿全栈（state.json 指纹幂等、dedup 判活、merge 行级原子、`ON CONFLICT` upsert）；恢复语义诚实（checkpoint 裁剪 + state 幂等重跑，不装「断点续传」）；`FOR UPDATE SKIP LOCKED` 认领（`pgstore.py:214-238`）与 steal 持锁复核（`watchdog.py:259-274`）的并发纪律教科书级；备份脚本有防重入锁与 manifest 校验。

**缺口**：

| # | 缺口 | 证据 |
|---|---|---|
| R1 | PG 池裸奔：无 connect_timeout/池获取超时/语句超时 | `pg.py:33-36` 只配 min/max/autocommit/prepare_threshold。PG 挂起 → 池获取无限阻塞 → API 全挂（故障放大器） |
| R2 | 跨进程语义从未经真双进程验证 | ①E4：`_simulate_other_proc` 手工 NOTIFY（`test_pg_stores.py:182-190`） |
| R3 | 出界项四件：queued 态无租约（队列里的 run 归谁管未定）、跨进程 cancel 不可能、rescue/migrate 工具只认 json 档 | README L282-287 自列 |
| R4 | 备份一致性接受瞬时口径（诚实但未量化影响） | README L182-183 |

## 5. 可运维性 — 🟡 M

**现状证据**

做对的：`deploy.sh`（体检 + 非零退出）、`backup.sh`/`restore.sh`（有测试）、smoke 脚本三件套、CI 覆盖 lint/类型/契约/真库/告警配置校验。

**缺口**：无 runbook（告警响了之后干什么——`deploy/prometheus-rules.yml` 8 条规则无一配处置手册链接）；恢复演练是「建议」非制度（README L175）；部署 = `compose up -d` 同容器替换的停机窗口（`deploy.sh:50`）；watchdog `exit` 自愈依赖 compose restart（`watchdog.py:299-309`），systemd/裸跑形态无资产。

## 6. 可用性 — 🟡 M

**现状**：单 api 副本（SPOF）+ 单 PG（SPOF）+ 单宿主。缓解项：lifespan 启动恢复重投 running run（`api.py:558-595`）、watchdog exit 自愈、容器 restart 策略。

**评级理由**：对「云服务器·多用户」定位，单副本 + 自动恢复是**可接受的起点**（run 是长任务，分钟级中断可由重投覆盖）；定为 M 而非 H 是因为故障半径 = 「在跑 run 延迟 + 重启窗口」，不丢数据。但注意与 R2 联动：多副本可用性故事的验证闭环还没合上。

## 7. 可观测性 — 🟢 L

**现状**：最强维度。三层模型（jsonl 本地真相 / OTel→Langfuse trace / OTel→Prometheus 指标）、9 指标 + 双 gauge、8 条告警规则进 CI 校验、飞书转译、死人开关、watchdog 三信号、traceId 确定性约定有评审存档。

**缺口**（皆为增强非缺陷）：告警事件式非 SLO 式（因 SLO 未定义，与维度 3 联动）；指标内存态重启清零（自述诚实）；日志 print 双轨无级别；trace 全量无采样（上量后 Langfuse 成本与噪音双升）。

## 8. 成本 — 🟢 L

**现状**：Langfuse 按 uid 归因成本（观测口径，`observability.py:203-215`）；gpu_router 有 provider 冷却/用量记账卷。

**缺口**：无每 run 单位成本估算（LLM token × 单价 + GPU 分钟 × 折算）、无每用户预算闸、超成本run 无熔断。对当前规模可接受；商业化前必须。

---

## 交叉引用索引

| 批判①条目 | 本篇维度 | ROADMAP |
|---|---|---|
| ①入口层半档 | 安全 S3/S4 | P0-3, P0-4 |
| ①鉴权层半档 | 安全 S1 | P0-1 |
| ①执行层 E1/E2/E3/E4 | 可靠性 R2/R3 | P0-7, P1-1~4 |
| ①状态层半档 | 可演进性（H）+ 可靠性 R1 | P0-5, P1-8, P1-9 |
| ①产物层残余瑕疵 | 安全 S5 | P0-6 |
| ①限流层缺失 | 安全 S2 | P0-2 |
| ①算力层半档 | 安全 S8 | P0-4（反代收口后评估） |
| ①数据安全层半档 | 可运维性 | P1-7 |
| ①发布层 E6 | 可运维性 | P0-7（口径修订）, P2-3 |
| ①观测层残余瑕疵 | 可观测性（L） | P1-5, P2-5 |

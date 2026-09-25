# 04 · 数据与一致性

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：① 状态层/数据安全层；② 可靠性 R1/R3、可演进性 H

## 数据资产清单

| 数据 | 缺省落点 | 生产落点 | 真相/性质 |
|---|---|---|---|
| runs（run 记录） | `runs.json`（项目根） | PG `v2md_runs` 或 data 卷 json | 状态真相源 |
| state（幂等记录） | `state.json` | PG `v2md_state` 或 data 卷 | 幂等 SSOT（键只增不删） |
| users/identities | `users.json` | PG `v2md_users`/`v2md_identities` | 账目 |
| checkpoints（图断点） | `data/checkpoints.sqlite` | PG `checkpoints*`（langgraph 自建） | **易失**：done 即删，可丢弃 |
| logs（run 遥测） | `logs/run-YYYY-MM-DD.jsonl` | 同（data 卷） | 本地真相，append-only |
| 产物 | vault 直写 | outbox 在架（TTL/ACK 即焚） | 多用户形态不留存 |
| 上传源文件 | `data/uploads/<uid>/` | 同 | TTL 24h 物理删 |
| run 临时目录 | `/tmp/video2md-run-*` | 同（容器 tmp） | 跑完必删；清扫双兜底 |

## 存储分派（P3）

每个存储独立 `json`（缺省，行为零变化）｜ `postgres`：

| 开关 | 适配器 | 语义映射 |
|---|---|---|
| `V2MD_RUNS_STORE` | `PostgresRunsStore`（`pgstore.py:35-77`） | json「锁内读改写」→ 行级 upsert（`ON CONFLICT`），**禁用全表替换**（多实例覆盖风险） |
| `V2MD_STATE_STORE` | `PostgresStateStore`（`pgstore.py:80-107`） | 全量替换 → 逐 fp upsert（键只增不删，语义等价） |
| `V2MD_USERS_STORE` | `PostgresIdentityStore`（`auth/pg_store.py`） | acctkit `IdentityStore` 同协议适配器 |
| `V2MD_CHECKPOINTER` | langgraph `PostgresSaver` | sqlite → PG（并发>1 建议切） |
| `V2MD_EVENTS` | local ｜ pg（LISTEN/NOTIFY） | 见下「事件总线」 |
| `V2MD_TASKS_STORE` | `PostgresTaskStore`（`pgstore.py:110-305`） | 见下「任务表两档口径」 |

json 档并发纪律：线程锁 + `fcntl` flock + per-pid tmp rename 原子写（`runs.py:108-140`）。PG 档四消费者（三存储 + checkpointer + events）共用一个进程级池（`pg.py:33-46`，min1/max5、autocommit、prepare_threshold=0）。

### PG 表结构

| 表 | 定义处 | 要点 |
|---|---|---|
| `v2md_runs` | `migrations/0001_init.sql` | run_id PK + owner/status/created 提取列 + `record jsonb`（**唯一真相**，提取列是其可查镜像）+ claimed_by（P4b 租约） |
| `v2md_state` | `migrations/0001_init.sql` | fp PK + record jsonb |
| `v2md_users` / `v2md_identities` | `migrations/0001_init.sql` | uid PK；(provider, external_id) PK → uid |
| `v2md_submit_log` | `migrations/0001_init.sql` | 60s 滑窗限流计数（单语句 upsert 原子判定） |
| `v2md_schema_version` | `pgmigrate.py` 建表 | 迁移记账（version PK + 文件名 + 应用时间） |

**schema 迁移纪律（P0-5 ✅）**：`video2md/migrations/`（包内目录，随镜像走）是 DDL 唯一真相，
store 构造函数只接线不建表；`v2md_schema_version` 逐迁移记账；`pg.shared_pool()` 建池即校验，
落后/超前一律拒起并提示 `python -m video2md migrate`（deploy.sh 在 up -d 后自动跑该步，
json 档跳过）。全部迁移幂等（IF NOT EXISTS）——存量库重跑 no-op 直接补记版本行（基线盖戳）。
边界：langgraph `PostgresSaver.setup()` 自管的 `checkpoints*` 表族同库共存，不收编。
加新迁移 = 落一个 `NNNN_*.sql` + `pgmigrate.MIGRATIONS` 注册表加一行。

切换路径：`scripts/migrate_json_to_pg.py` 幂等导入存量（反向迁移明确不提供，导入前自动应用迁移基线）。

## 任务表两档口径（批判① E2 修订后）

| | json 档（缺省） | postgres 档（`V2MD_TASKS_STORE=postgres`） |
|---|---|---|
| 写路径 | `_TASKS` 进程内 dict | **进程内实时视图 + 关键态落库**（P1-1）：`_enqueue` 即 `mark_queued` 翻 queued+钉名 claimed_by；`_drive` 起跑 `still_mine` 复核，被划走静默让位 |
| 读侧 | 进程内视图 | **PG 真相源**：`/queue` 计数、配额三闸、dedup 判活、watchdog 大盘巡检（running 卡死 2h 判定标 error + queued 短租约 3×period 接管重投）、启动恢复（属主甄别：`recovery_scan`+`claim_dead`+phb 进程心跳——B 重启不抢 A 活 run，死属主窗后认领）全走 `v2md_runs` |
| 跨副本 | 各自为战 | 各副本看到同一队列（读侧含排队项）；`GET /runs/{id}` 的 `task` 段仍只读本进程（`api.py:852-856`） |
| 滑窗限流 | 进程内 deque | `v2md_submit_log` 单语句 upsert，跨进程一致 |

**不变量**：`record jsonb` 是状态唯一真相；`status` 提取列由 `merge()` 整行覆盖同步，两态天然一致。

## 事件总线（local ｜ pg）

- `local`（缺省）：进程内 `dict[run_id → list[Queue]]`，工作线程 `call_soon_threadsafe` 投递，慢客户端队列满丢新事件（不反压）。
- `pg`：publish 在本地扇出外补一条 `NOTIFY v2md_events`；专用 LISTEN 连接的后台泵线程把他进程通知投给本进程订阅者（payload 带 `src` 进程号去重回声）。`bind_loop` 有界等 `_PUMP_READY`（5s）——NOTIFY 只投给已在 LISTEN 的会话，不回补（`events.py:51-60`，CI 竞态修复 f6a4a0e）。
- 指令通道 `v2md_cmds`：同泵多 LISTEN 一个 channel，当前只有 `hb`（watchdog 心跳跨进程广播 → `_LOCAL_HB` 缓存）。
- 上限：NOTIFY payload ≤7900B，超长截断为骨架（`events.py:111-125`）。

## 一致性口径（显式承诺）

1. **run 幂等**：state.json 指纹 + dedup 判活；重发同 URL 跳过（deliver 下需同 owner 且源产物在架，否则老实重跑）。
2. **恢复语义**：重投 = 图内幂等重入（resolve 复用、阶段按产物字段跳过）；不靠 checkpoint 续跑。
3. **备份瞬时口径**：runs.json/state.json 原子 rename 写，tar 视角恒完整；checkpoints 可丢弃接受瞬时不一致；严格一致 = 先 stop 再备（README 备份章）。
4. **deliver 即焚是物理口径**：`_outbox_live` 惰性焚 + sweeper 周期物理删 + 启动兜底扫（`api.py:201-226, 438-455`）。

## 并发原语清单

| 场景 | 原语 | 位置 |
|---|---|---|
| 执行并发闸门 | `asyncio.Semaphore` | `api.py:231` |
| json 档写 | 线程锁 + fcntl + tmp rename | `runs.py:108-140` |
| PG 行写 | 行级 upsert（无调用方锁） | `pgstore.py:61-77` |
| 启动恢复认领 | `SELECT … FOR UPDATE SKIP LOCKED` | `pgstore.py:214-238` |
| 崩溃残档接管 | 条件 UPDATE（租约）+ 持锁复核 | `pgstore.py:197-210`、`watchdog.py:259-274` |
| 滑窗限流 | 单语句 INSERT…ON CONFLICT | `pgstore.py:291-305` |
| 事件跨进程 | LISTEN/NOTIFY + src 去重 | `events.py` |

## 已知限制

1. ~~无 schema 版本化迁移~~（P0-5 ✅ 已闭环：migrations/ + v2md_schema_version + 建池校验拒起）。
2. **PG 池无超时参数化**（P1-8）：PG 挂起时池获取无限阻塞，故障放大为 API 全挂。
3. rescue/migrate 工具只认 json 档（P1-3）。
4. `runs.json` 无界增长（完成 run 永不删记录，只有 tmp 清理）——长期膨胀未治理（随 P1-6 容量实测定价）。

## 演进方向

P0-5 迁移纪律 → P1-1 租约认领（写路径 PG 化的前提）→ P1-3 工具适配 → P2-4 产物面对象存储化。

# ROADMAP：企业级生产就绪路线

- **来源**：`critique/2026-09-24-ten-layer-review.md`（十层对照）+ `critique/2026-09-24-quality-attributes.md`（质量属性定价）
- **分级口径**：
  - **P0 上公网前必须** — 未清偿即暴露公网 = 接受已知风险裸奔
  - **P1 上量前必须** — 单副本低频可用；多用户/多副本/商业化前必须
  - **P2 规模化增强** — 不缺也能跑，缺了限制天花板
- **每条字段**：动机（链批判证据）/ 内容 / 验收标准（可测试）/ 工作量（S<1天，M=1-3天，L>3天）/ 阻塞边
- **纪律**：完成项划掉并标注 commit；新增缺口先登记批判再入此表

---

## P0：上公网前必须（7 项）

> **进度**：✅ 7/7 全清（P0-1 dc0184d · P0-2 见本批 · P0-3 63bfe84 · P0-4 见本批 · P0-5 a3056e4 · P0-6 7ae5b7b · P0-7 9e834a2）——「可上公网」里程碑达成（生产接线仅剩：域名 DNS + TLS 证书放 deploy/certs/ + 安全组放 80/443）。

### P0-1 AUTH=off 生产防呆 🔒
- **动机**：批判② S1——`V2MD_ENV=production` + `V2MD_AUTH=off` 共存即裸奔；config.py 已有 fail-fast 先例（`config.py:113-115`）
- **内容**：`config.py` 启动校验：`V2MD_ENV=production` 时 `V2MD_AUTH` 不得为 `off`/空，违例 `RuntimeError` 明示修法；CI 加一条配置组合测试
- **验收**：compose 注释掉 AUTH 起栈 → 进程非零退出 + stderr 一行指明缺哪个键；测试断言该组合 raise
- **工作量**：S ｜ **阻塞**：无 ｜ **状态**：✅ dc0184d（DEPLOY_ENV 升格一等配置；组合测试子进程隔离 ×4）

### P0-2 匿名端点限流 🔒 ✅
- **动机**：批判② S2 + 批判①限流层——`/auth/device/code|token` 匿名零频控可刷 OAuth 配额
- **内容**：设备流两端点加 IP 滑窗限流（如 10 次/分/IP，进程内 dict 起步；PG 档可复用 `v2md_submit_log` 模式按 IP 记账）；429 走统一信封
- **验收**：测试连续打 N 次 → 第 N+1 次 429 `RATE_LIMITED`；README/03 篇匿名面清单更新
- **工作量**：M ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。落地口径：进程内滑窗（多副本由 nginx
  limit_req 兜底跨副本语义）；两端点**共享桶**（code+token 是同一次登录的两半）；**缺省 30/min
  而非示例的 10**——token 端点按 Google 建议间隔（~5s）轮询，合法单用户登录就要 12 次/min，
  10 会误伤正常轮询（批判修正）。XFF 信任锚=回环拓扑（api 绑 127.0.0.1 唯一入口=仓内 nginx，
  XFF 尾位=真实客户端；`V2MD_TRUST_XFF` 缺省 off，compose 生产档置 on）——P0-4 落地后依赖消解

### P0-3 /preflight 信息收敛 🔒
- **动机**：批判② S3——匿名暴露内部路径、磁盘余量、算力池 provider 清单
- **内容**：默认响应脱敏（只回 `ok` + 检查项名 + 布尔）；完整 detail 需认证 owner 或仅当绑定回环地址
- **验收**：公网形态请求 `/preflight` 无绝对路径/provider 名；`docker-compose` healthcheck 不受影响
- **工作量**：S ｜ **阻塞**：无 ｜ **状态**：✅ 63bfe84（off 档非回环亦脱敏——严于原方案；smoke grep 与 deploy.sh 回环体检不受影响）

### P0-4 反代配置实体化 🔒 ✅
- **动机**：批判①入口层 + E5——「网关侧关闭 /docs」无资产承载
- **内容**：`deploy/nginx.conf`（或 `Caddyfile`）入仓：SSE 关缓冲、`/docs` `/openapi.json` deny、`limit_req` zone（兜底 P0-2 的通用 HTTP 限流）、`client_max_body_size`；`deploy.sh` 起栈后自动验证 `/docs` 返回 403/404
- **验收**：`smoke_compose.sh` 或新增 smoke 走反代路径断言 `/docs` 被拦；README 部署章改指仓内配置
- **工作量**：M ｜ **阻塞**：无（P2-3 蓝绿依赖此项）｜ **状态**：✅（见 commit）。落地口径：
  `deploy/nginx.conf`（选 nginx 弃 Caddy：limit_req 原生 + api 的 X-Accel-Buffering 头本就指向
  nginx）+ compose `nginx` 服务（host 网络，挂载仓内配置与 `deploy/certs/`）+ `deploy.sh` 起栈后
  断言 `/docs`→403、healthz 经反代→200 + `scripts/smoke_proxy.sh`（sed 只换 upstream 验证同源
  配置：deny×3 / 透传 / SSE 即时性 / 200 连发 136 个 429 的 limit_req 实证）进 CI test job。
  TLS 证书=部署期人工接线（conf 尾模板 + certs/ gitignore）——域名/TLS 决策不影响资产与验证，
  原判定的「外部依赖阻塞」只作用于生产接线一步

### P0-5 schema 迁移纪律 🔧 ✅
- **动机**：批判②可演进性 H——DDL 散在构造函数，`claimed_by` 靠 ALTER 补丁（`pgstore.py:127`）
- **内容**：`migrations/` 目录（`0001_init.sql` 起，幂等）+ `schema_version` 表 + store 构造前启动校验（版本不符拒绝启动并提示跑迁移）；存量 `CREATE/ALTER IF NOT EXISTS` 收编为 0001 基线
- **验收**：手工 `UPDATE schema_version` 改低 → api 启动拒起；迁移脚本连跑两次幂等；CI 真库 job 跑迁移路径
- **工作量**：M ｜ **阻塞**：无（P1-1 依赖此项）｜ **状态**：✅ a3056e4。落地修正两处口径：
  ① migrations/ 放包内 `video2md/migrations/`（Dockerfile 只 COPY video2md/，仓根目录进不了镜像）；
  ② 版本表名 `v2md_schema_version`（同库共存 langgraph checkpoints*，通用名撞名风险）。
  校验收口 `pg.shared_pool()` 建池即校验（全消费者必经），超前（回滚旧代码）亦拒起；deploy.sh
  up -d 后自动 migrate，json 档跳过

### P0-6 上传 magic number 校验 🔒
- **动机**：批判② S5——后缀白名单可绕过，任意内容进 ffmpeg 解析路径
- **内容**：`POST /uploads` 流式写盘后读首 4KB 比对魔数（ftyp/RIFF/EBML/OggS 等白名单映射），不符即删 + 422
- **验收**：文本文件改名 `.mp4` 上传 → 422 `UNSUPPORTED_TYPE`；真实 mp4/m4a 不受影响
- **工作量**：S ｜ **阻塞**：无 ｜ **状态**：✅ 7ae5b7b（七后缀映射；配额闸先于魔数闸，既有 413 两测逐字节不变）

### P0-7 README 硬错误修订 📄
- **动机**：批判① E1–E7
- **内容**：按批判①「硬错误清单」逐条修订（执行层措辞、双进程口径、GHCR 灾备口径、/docs 人工纪律标注）；本项随架构文档体系落地（2026-09-24 批次）一并完成
- **验收**：批判① E1–E7 各行对照修订后文字
- **工作量**：S ｜ **阻塞**：无 ｜ **状态**：✅ 9e834a2（文档体系基线入库，E1–E7 锚定）

---

## P1：上量前必须（9 项）

> **进度**：✅ 9/9 全清（P1-1..P1-9，见各条 commit 锚点）——「可上量」里程碑达成。

### P1-1 claimed_by 租约认领（出界项①）✅
- **动机**：批判② R3——queued 态 run 无属主，多副本下「谁驱动队列里的活」未定
- **内容**：queued 态也心跳 + 租约字段；watchdog 大盘巡检覆盖 queued 超龄（不只 running）；steal 语义扩到 queued
- **验收**：杀副本 A（持 queued 项）→ 副本 B 租约到期接管，run 不丢不重
- **工作量**：L ｜ **阻塞**：P0-5（schema 演进需迁移纪律先行）｜ **状态**：✅ 机制级全绿（见 commit）。
  实施发现比记载更深（批判补遗 2026-09-25-p1-1-findings.md）：queued 此前**从不落库**（DB 停在
  created），配额闸/跨副本判活/gauge/重启恢复全看不见排队项。落地口径：`_enqueue` PG 档
  `mark_queued` 落库+钉名；lifespan `_queue_hb_loop` 排队心跳；**双租约**（排队项短租约
  3×WATCHDOG_PERIOD_S 接管重投不标 error，运行项维持 2h 卡死判定）；`_drive` 拿信号量先
  `still_mine` 复核——被划走静默让位（「不重」半边）。零 DDL（claimed_by/updated 即租约字段，
  P0-5 先行的兑现）。真双进程杀副本验收归 P1-4（阻塞边解除）

### P1-2 跨进程 cancel 端点（出界项②）✅
- **动机**：批判② R3——A 副本驱动的 run，B 副本收到 cancel 无法转发
- **内容**：`DELETE /runs/{id}`（或 POST cancel）：本进程在跑直接撤；非本进程走 events `v2md_cmds` 通道发 cancel 指令，目标进程收到后中断（协作式：阶段循环头查标志）
- **验收**：双副本冒烟中向 B 发 cancel 停掉 A 的 run；终态 error(cancelled) 留痕
- **工作量**：M ｜ **阻塞**：P1-1（✅）｜ **状态**：✅ 机制级全绿（见 commit）。落地口径：
  **`POST /runs/{id}/cancel`** 而非 DELETE——记录留痕不删除，与 DELETE /artifacts 的真删除语义
  区分（AIP-135）。本进程 queued 立即翻 error(cancelled)；running 协作式（`_run_stage_inner`
  条目循环头 SystemExit 走既有熔断通道）；他副本驱动 `publish_cmd("cancel")` 广播，收端只对
  本地驱动 run 落标志（防外部泄漏），回 cancelling。实施抓出并修复**复活竞态**：端点清标志
  先于挂起 _drive 醒来 → 已撤销 run 被复活驱动——标志生命周期改归 _drive finally。长单阶段
  内无取消点为已知限制（按处方「阶段循环头」）。真双进程 cancel 验收随 P1-4 冒烟

### P1-3 rescue/migrate 工具 PG 档适配（出界项③）✅
- **动机**：批判② R3——`scripts/rescue_partial.py`、`migrate_json_to_pg.py` 只认 json 档
- **内容**：两工具加 `--store postgres`（或按 env 自动分派），走 pgstore 适配器
- **验收**：PG 档环境下 rescue 能捞出 partial run；迁移脚本可重复跑幂等
- **工作量**：M ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。落地口径：**env 自动分派**（非
  --store 旗标——脚本全程走 runs._load_runs/_runs_merge 分派层，本就 90% 存储无关）。
  rescue 去除 PG 禁令（批判②「重发同 URL 幂等即可」的口径漏了本工具核心价值：**旧 tmp
  已下载视频复用**——17 集熔断在 15 集时省 15 次下载；重发同 URL 只跳 state 已完成条目，
  其余全部重新下载）；tmp_guard 改逐行 merge（PG 档 _save_runs 禁用）；healthz 防护读
  V2MD_PORT。顺带修两处脚本级实锘：`_part_selected` 单值分支与 range 分支口径不一致
  （None 严格 == 永假 → 单视频 part_items="0" 恒空）；rescue 完成判定改按条目 URL 匹配
  （按 playlist_index 的 None==0 永假会 4h 空等）。migrate 半边：P0-5 已接迁移基线 + 幂等
  upsert（本项内闭环）。验收 E2E：test_rescue_pg.py（真 PG + mockpool + 熔断残局种子 →
  复用视频 → done + 笔记），CI test job 已挂

### P1-4 多副本冒烟进 CI（出界项④）✅
- **动机**：批判① E4——跨进程语义只经单进程双实例模拟验证
- **内容**：`docker-compose.smoke.yml` 加 `video2md` 副本 2（或 scale）+ 真 PG，断言：A 提交 B 可见 /queue、A 死 B 接管（running 与 queued 两态）、SSE 跨副本关流；进 CI 或夜间 job。
  **含批判补遗②（2026-09-25）**：`recover_candidates` 不看 claimed_by——稳态副本 B 重启会把健康 A 的 running 行整批抢走重投（双驱动）；冒烟须断言「B 重启不抢 A 活 run」，修复需进程级存活信号（cmd 通道 proc 心跳），与本项同窗落地
- **验收**：CI 真双进程冒烟绿（含 B 重启不抢活）；批判① E4 口径随之修订为已闭环
- **工作量**：M ｜ **阻塞**：P1-1（✅）｜ **状态**：✅ 机制+冒烟全绿（见 commit）。落地口径与修订：
  ①**进程级而非 compose 级**冒烟（`scripts/smoke_replicas.sh`，仿 smoke_ci——CI 复用 test job 的
  PG service，零新基建；快租约档 period=5s/timeout=15s 让接管在时间预算内可观测）；②补遗②修复
  = `recovery_scan`+`claim_dead`+phb 进程心跳甄别（`_recover_interrupted` 后台任务，甄别窗不拖
  boot），五断言：可见性/甄别不抢/跨副本 SSE/跨副本取消/杀 A 接管（queued 认领+running 残档重投）；
  ③落地另修两个实锤：**时钟域 bug**（naive 阶段 ts 经 PG UTC cast 落未来 → running 残档检测在
  容器化部署静默失效；DB 侧只滤 updated、阶段 ts 应用侧解析）与 **P4b steal 复核自刷**（steal
  刷 updated 而复核含 updated → 接管空转）；④跨副本可见性补齐：阶段失败 errors 落 run 记录
  （record jsonb 唯一真相教义）+ `RunStatusOut.errors` 暴露。批判① E4 口径已闭环

### P1-5 SLO 定义 + 告警 SLO 化
- **动机**：批判②维度 3/7——告警事件式非 SLO 式
- **内容**：`08-capacity-and-slo.md` 填入 SLO 草案（建议：run 成功率 7d≥99%（非输入错误口径）、SSE 首事件 p95<3s、/queue 可用性 30d≥99.9%）；prometheus-rules 增错误预算燃烧率告警
- **验收**：SLO 文档评审通过；至少 1 条燃烧率告警进 `deploy/prometheus-rules.yml` 并过 promtool
- **工作量**：S-M ｜ **阻塞**：无

### P1-6 容量规划实测 ✅
- **动机**：批判②维度 3 H——无容量模型
- **内容**：负载测试脚本（N 并发 POST /runs + mockpool 假算力）；产出基线：单 worker 吞吐、runs.json/jsonb 膨胀率、uploads/outbox 磁盘增长曲线；数字回填 `08-capacity-and-slo.md`（`⏳ P1 待测` 占位处）
- **验收**：08 篇所有占位符替换为实测值 + 测试方法可复跑
- **工作量**：M ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。`scripts/loadtest.py` 四场景自旋栈
  （复跑一条命令，含 PG 对照组）；08 篇六个 ⏳ 全部回填实测值。**口径纪律**：mock 算力测的是
  管线开销上限（json 0.1–0.3s/run、PG 全链 0.8s/run、SSE 20/20 无丢、runs.json ~1KB/run、
  PG ~32KB/run 小样本含索引固定页、1h tmp 外推 ~1.2GB），真实 GPU 延迟归 gpu_router EMA 账本
  域——不冒充生产吞吐。**过程挖出三个实锤**（补遗 2026-09-25-loadtest-findings.md）：
  ①state.json 历史异构值（{"k2":49}）打穿 POST 全路 → create_run 加 isinstance 设防（已修）；
  ②配额闸 admit TOCTOU——并发突发互不可见在途计数，滑窗才是突发兜底（登记在册未修，单用户
  语义无恙）；③测量工具沙箱纪律（首轮 A 节点漏钉三件套读写真实数据——已修 + 起前清场 +
  崩溃安全清理）

### P1-7 灾备演练制度化 ✅
- **动机**：批判①数据安全层——演练是建议非制度；secrets 在备份机制外
- **内容**：`09-disaster-recovery.md` 定 RTO/RPO 目标；季度演练日历化（cron 提醒或 CI 定时 job 干跑 restore 到临时卷）；演练记录模板；secrets 备份方案二选一落实（密码管理器登记清单 / 加密后进备份卷）
- **验收**：第一次演练记录归档；RTO 实测值 ≤ 目标值
- **工作量**：S ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。落地口径：`scripts/drill_restore.sh`
  自含干跑（合成种子→backup 同口径打包→manifest 自算自比→恢复 diff→PG dump 往返→进程级
  起栈验证 3 历史run），**不触碰生产卷/栈**；月度 CI 干跑（`.github/workflows/drill.yml`，
  schedule+dispatch）+ 季度人工整演练 `--record --secrets-checked` 归档
  `docs/architecture/drills/`。secrets 二选一**裁定清单核对制**（弃加密归档：备份密钥鸡生蛋）。
  首次实测 RTO 4s（本质路径口径，整机 2h 预算保留），记录已归档 2026-09-25-Q3-01

### P1-8 PG 池超时/重试参数化 ✅
- **动机**：批判② R1——池获取无超时 = 故障放大器
- **内容**：`pg.py` 增 `connect_timeout`、池获取 `timeout`、`options` 语句超时（env 可调，如 `V2MD_PG_TIMEOUT_S`）；池耗尽时请求快速 503 而非挂死
- **验收**：测试模拟 PG 挂起 → API 在超时预算内 503，不无限阻塞
- **工作量**：S ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。单一预算键 `V2MD_PG_TIMEOUT_S`
  （缺省 10s，钳 ≥1s）三处同值：connect_timeout / 池获取 / statement_timeout（锁等待也
  被打断——池耗尽从永久变瞬态）。PG 不可达/超预算 → 集中 503 `PG_UNAVAILABLE` 信封
  （逐类精确注册 PoolTimeout/OperationalError/QueryCanceled；刻意不捕 DatabaseError 基类，
  编程错误仍 500 带栈）。实证修正预设：psycopg_pool open=True 并不阻塞建池（后台重连），
  预算在首次 connection() 生效——启动 fail-fast 由 shared_pool 的 validate_schema 首连
  兜住，同样有界。遗留（登记）：check_cluster 在事件循环上同步打 PG，池耗尽时单轮巡检
  仍可能冻结循环至预算上限（10s）——异步化留 P2 观测/巡检收口

### P1-9 config 集中 schema 校验收口 ✅
- **动机**：批判②可演进性次级缺口——手写 int() 散落、import 期加载
- **内容**：集中配置表（键/类型/默认/校验器/文档串），启动期统一解析 + 非法值一次性汇总报错；替换散落的手写转换
- **验收**：新加配置键只需在表中登记；非法值启动报全部错误（非第一个）
- **工作量**：M ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。落地口径：config._SPEC 单表
  （path/str/int/float/enum/csv 六型 + gt/min 约束），import 期全量解析、**错误攒齐一次报**
  （「共 N 处」清单）；消费面导出名与语义逐字节不变（MAX_CONCURRENT_RUNS 的 max(1,) 钳
  制改 min≥1 校验、`or 0` 兜底语义收编为「空串/未设=默认」——边缘配置行为变化已注记）；
  P0-1 生产防呆闸门并入同一汇总。**根修实证过的隔离缺口**：load_env 不再丢弃空串——真实
  环境变量空串=显式清空回落默认（旧「空串被丢→.env 渗漏」：V2MD_REMOTE="" 压不住 .env
  的 9700，套件红过一次）。import 期加载**保留为特性**（fail-fast 启动闸正是要的语义，
  批判点在散落与静默不在时机）。PG 域键（V2MD_PG_DSN/TIMEOUT_S）仍调用期读取（测试运行
  时可变），收编入表随下一批存储键迁移。验收测试：三非法键一次报全 + 空串清空 + 显式覆盖

---

## P2：规模化增强（5 项）

> **平台相位注（2026-09-26 建册）**：P2-1（多 worker）/ P2-3（蓝绿）/ P2-4（水平
> 扩容前提）与平台级 k8s 解耦路线天然接力——`../../../PLATFORM-ROADMAP.md`
> **Phase 4**（video2md 入 k3s；gate=平台 Phase 2 sing-train 验收过 + 本 P2 需求
> 真实）。届时 P2-1 的三选一评估应把「k3s replicas」纳入选项；P2-3 的反代双
> upstream 权重切换可由 `kubectl rollout` 原生替代；PG（profile pg 回环 5432）
> 状态面去留按平台册 §2.2 裁决逻辑单判。
>
> **模块简并注（2026-09-26 M 轴）**：本仓是平台运维资产的谱系源头（acctkit/
> 运维三件套/observability 定稿 → bookserve/gpu_router 移植复刻，头注链在册）——
> acctkit 内核共享归 **M2**（gate=bookserve 或本仓下次触碰 acctkit 时）、run 引擎
> 底层五件（util/events/watchdog/deadman/observability）归 **M3**、本仓
> FastAPI 版 feishu_webhook 与 obs 配置族归 **M1**（并入 Phase 1.1 运行时合并，
> 过渡首步=bookserve 的 httpcheck/health_check 增强回流本仓）。同上平台册 M 轴。

### P2-1 多 worker 形态声明与资产
- **动机**：批判②可用性 M——单副本 SPOF；watchdog exit 依赖 compose
- **内容**：评估三选一：gunicorn 多 worker（需先 P1-1 闭环跨副本语义）/ compose `deploy.replicas` + 反代负载 / 显式声明「单副本 + 快速自愈」为正式形态并补 systemd unit；产出写入 `02-runtime-topology.md`
- **验收**：形态决策记录（ADR）；若选多副本，P1-4 冒烟覆盖
- **工作量**：M ｜ **阻塞**：P1-1, P1-4

### P2-2 成本归因 → 预算闸 ✅
- **动机**：批判②维度 8——有观测无预算
- **内容**：每 run 成本估算（LLM token×单价 + GPU 分钟折算）落 runs 记录；每用户月度预算 env 闸，超限 429；Langfuse 侧成本看板例行化
- **验收**：预算超限用户收到 429 + 明确提示；成本报表可按 uid 导出
- **工作量**：M ｜ **阻塞**：无 ｜ **状态**：✅（见 commit）。落地口径：usage（原始凭据）+
  cost_usd（结算价）随 run 落盘，归因缝=ContextVar（llm usage 现成观测口 + 三处远程调用墙钟
  计时）；结算收口在 _drive finally（全终态路径单点、幂等不双计）；预算闸挂 _admit_submission
  第四道（json 档全表扫 / PG 档 SQL 求和）；GET /cost 本人视图 + `video2md cost --uid` 导出。
  单价/预算全 0=惰性缺省（不定价先记账，历史可离线重估——不编造市场价）

### P2-3 蓝绿/金丝雀发布
- **动机**：批判①发布层——`compose up -d` 停机窗口
- **内容**：反代双 upstream 权重切换（依赖 P0-4 的反代实体）；deploy.sh 增 `--canary` 模式
- **验收**：发布期间 /healthz 连续探测零失败
- **工作量**：L ｜ **阻塞**：P0-4

### P2-4 产物面对象存储化
- **动机**：outbox 在本地卷 = 多副本/多机的水平扩容前提不满足
- **内容**：outbox 后端抽象（本地 | S3/B2），bundle 从对象存储出；生命周期规则替代 sweeper
- **验收**：deliver 全链在 S3 后端下通过既有契约测试
- **工作量**：L ｜ **阻塞**：P1-1

### P2-5 tracing 采样 + 日志结构化
- **动机**：批判②可观测性残余——全量 trace 上量后成本/噪音双升；print 无级别
- **内容**：OTel tail/head 采样策略（错误 run 全采、成功抽样）；print 双轨收编为 logging 分级（jsonl 输出不变）
- **验收**：采样率 env 可调；日志级别 env 可调且 jsonl 字段不变（测试锁定）
- **工作量**：S-M ｜ **阻塞**：无

---

## 里程碑视图

```
现在 ──► P0 全清（约 1 周）──► 可上公网（单副本 · 低频 · 多用户）
         │
         ▼ P1 全清（约 3-4 周）──► 可上量（多副本就绪 · SLO 可问责 · 灾备可演练）
         │
         ▼ P2 按需（季度节奏）──► 规模化（蓝绿 · 预算闸 · 对象存储）
```

**依赖关键路径**：P0-5 → P1-1 → P1-2/P1-4 → P2-1/P2-4；P0-4 → P2-3。其余条目可并行。

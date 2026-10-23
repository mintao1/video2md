# 03 · HTTP 契约与鉴权账目

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：① 鉴权层/限流层；② 安全 S1–S5
- 契约硬化在 `video2md/models.py`（Pydantic）；契约测试 `test_api_contract.py`（TestClient 离线）


## Web 队列与阅读器（2026-10-18）

- `GET /runs` 返回真实 `status` 和 `queue_pos`；运行中已接收取消请求时显示
  `cancelling`，刷新后不会回到 running。未优先处理的任务按入队时间保持 FIFO。
- `POST /runs/{id}/resume`：仅 error/partial 可重试，返回 queued；按 owner 做原子状态认领，
  同一任务的并发请求只有一个成功，其余 409。已完成笔记和仍在架的 deliver 媒体保留。
- `POST /runs/{id}/promote`：仅 queued 可设为优先，持久化队列优先级，不抢占正在执行的工作。
- `DELETE /runs/{id}`：仅 done/partial/error 可删除任务记录；进行中 409，越权 403。
  任务逻辑删除并保留计费用量，不能通过删除记录重置月预算；
  删除 deliver 记录会焚毁其在架产物，不删除用户 vault 的笔记。
- `GET /runs/{id}/media/{name}`：只提供本人笔记明确引用的图片/视频；验证平铺文件名、扩展名、
  实际路径边界，拒绝未引用文件及外指符号链接。deliver 已焚或过期遵循 410。
  响应 `Cache-Control: no-store`；前端使用 Bearer 拉取 Blob，不把 token 放进 URL。
- `GET /prefs`、`PUT /prefs/{id}`：收藏和 0–100 阅读进度，按 uid 隔离、部分更新；
  JSON 首次请求会创建目录，容器默认 `/app/data/prefs.json`；PG 使用 0003 迁移。


## 端点总览

| 方法+路径 | 鉴权 | 说明 | 位置 |
|---|---|---|---|
| POST /uploads | owner | multipart 流式上传 → `{path,size,name}`，path 直接喂 /runs | `api.py:666` |
| POST /runs | owner | 建 run + 后台驱动；dedup 按 owner+URL 判活 | `api.py:791` |
| GET /runs | owner | 本人名下列表（created 倒序，deliver 附在架态） | `api.py:763` |
| GET /runs/{id} | owner | 终态 + 逐条目 done map + partial_failures；`task` 段=本进程实时视图 | `api.py:849` |
| GET /runs/{id}/note | owner | 产出 markdown（deliver 下从在架文件现读，已焚 410） | `api.py:883` |
| GET /runs/{id}/bundle | owner+deliver | 产物整包 zip，现打包发完即删 | `api.py:924` |
| DELETE /runs/{id}/artifacts | owner+deliver | ACK 即焚（rmtree 物理销毁），再删 410 | `api.py:940` |
| POST /runs/{id}/cancel | owner | 撤销进行中 run（P1-2）：排队立即 error(cancelled) 留痕；运行中协作式（条目循环头 SystemExit，长单阶段内无取消点）；他副本驱动走 cmd 广播回 cancelling；终态 409 `NOT_CANCELLABLE` | `api.py` cancel_run |
| GET /runs/{id}/events | owner | SSE：connected 快照 → 事件流 → 终态关流；15s `: ping` 保活 | `api.py:966` |
| POST /auth/device/code | **匿名+IP 限流** | Google 设备流第一步（P0-2：30/min/IP 滑窗，429 RATE_LIMITED；与 token 共享桶） | `api.py` |
| POST /auth/device/token | **匿名+IP 限流** | device_code → id_token（轮询与 code 共享限流桶——同一次登录的两半） | `api.py` |
| GET /cost | owner | 本人本月成本归因（P2-2）：spend/usage/单价回显/预算余量；超预算 POST /runs → 429 `BUDGET_EXCEEDED`；明细导出 `python -m video2md cost --uid X` |
| GET /queue | **匿名** | `{queue_len, running, limits}`（gpu_router 看门狗回读） | `api.py:1008` |
| GET /healthz | **匿名** | 存活 + version（Docker healthcheck） | `api.py:1029` |
| GET /preflight | **匿名** | 环境体检（ffmpeg/磁盘/vault|outbox/算力池） | `api.py:1035` |
| GET /docs, /openapi.json | **匿名**（直连回环） | OpenAPI——公网经仓内 nginx 一律 403（P0-4 ✅，smoke_proxy.sh 钉住） | `api.py` |

### POST /runs 请求字段

| 字段 | 作用 |
|---|---|
| `url` | 视频链接（合集自动展开）或本服务 uploads 路径（鉴权档只认本人目录内） |
| `part_items` | 只跑指定条目，如 `1,3-5` |
| `force` | 忽略 state 幂等全量重跑 |
| `review` | 启用校对 LLM（更准更慢） |
| `max_frames` | 候选帧池上限（默认 60） |
| `resolve_only` | 只解析建 run 不驱动（批量预飞；后续正式 POST dedup 命中并驱动） |

### 错误信封与限流码

非 2xx 统一 `{"error": {"code", "message", "details"}}`（`api.py:636-639`）。业务符号码走 `X-Error` 头 → `details`：

| 码 | 场景 |
|---|---|
| `QUEUE_FULL` / `USER_QUEUE_FULL` / `RATE_LIMITED` | 429 提交闸三态（总队列/每用户在途/60s 滑窗） |
| `LOCAL_PATH_FORBIDDEN` | 422 裸本地路径（多用户档） |
| `UNSUPPORTED_TYPE` / `FILE_TOO_LARGE` / `QUOTA_EXCEEDED` | 422/413/413 上传面 |
| `AUTH_REQUIRED` / `FORBIDDEN` / `NOT_FOUND` | 401/403/404 鉴权归属 |
| 410 Gone | deliver 产物已焚（ACK/TTL/盘上丢失）——重发 URL 生成新 run |
| 409 | bundle/artifacts 在非 deliver 模式调用 |

## 鉴权（acctkit）

登录工程本体在顶层 `acctkit/`（零框架依赖，可整体拷走）；宿主接线 `video2md/auth/`（约 20 行）。引擎按序尝试 provider（`NoCredentials` 换下一家、`AuthError` 直抛），验出外部身份落库为稳定内部 uid（首登开户、email/name 回填）。

| Provider | 触发头 | 机制 |
|---|---|---|
| `google` | `Authorization: Bearer <id_token>` | JWKS 验签（iss/exp/aud=GOOGLE_CLIENT_ID），后端不持 secret |
| `google_device` | （设备流） | RFC 8628：/auth/device/* 桥 + TV/Limited-Input 型 client_secret |
| `wechat` | `Authorization: Wechat <code>` | 服务端 code→openid/unionid（urllib 强制直连） |
| `off`（默认） | 无/任意 | Fake+Dev 兜底：**所有无 token 请求共享同一 dev uid** |

身份表 `(provider, external_id) → uid`；微信 unionid 优先归并到 Google uid（账号联动，双 provider 进同一账号）。CORS：`V2MD_CORS_ORIGINS` 逗号白名单（空=不挂中间件）。

**acctkit 复用接口**（自 README 迁入）：对外只有一个方法 `identity = await engine.authenticate(request.headers)`——provider 按序尝试、落库稳定 uid、同步存储 I/O 丢线程全藏在后面。两个可替换缝（各有真适配器）：

| 缝 | 已落地适配器 | 挂法 |
|---|---|---|
| `AuthProvider` | `GoogleProvider(client_id)` / `WeChatProvider(app_id, app_secret)` / `DevProvider` / `FakeProvider` | 实现 `authenticate(headers)` 后 `register_provider(name, 工厂)` 即挂 |
| `IdentityStore` | `JsonFileStore(path)`（fcntl 锁 + tmp rename 原子替换）/ `PostgresIdentityStore` / `MemoryStore`（测试） | 同协议再写适配器 |

`build_from_env(env, prefix)` 认 `{prefix}AUTH` / `{prefix}AUTH_PROVIDERS` / `{prefix}USERS_FILE` / `GOOGLE_CLIENT_ID`（工厂注册表另有 `WECHAT_APP_ID/SECRET`），换前缀即复用。⚠ 宿主接线的 FastAPI 依赖注解必须真 `import Request`——`TYPE_CHECKING` 字符串注解会被 FastAPI 当 query 参数（`video2md/auth/__init__.py:10` 注释）。

## 产物形态双模式（`V2MD_ARTIFACT_MODE`，自 README 迁入）

| | `vault`（开发机默认） | `deliver`（多用户「云端不储存」） |
|---|---|---|
| 落点 | 直写 Obsidian vault（自托管自行同步） | per-run outbox → finalize 搬到在架位 `V2MD_OUTBOX_DIR/<run_id>/` |
| runs.json | 养 `md` 全文（`/note` 原语义） | **剥正文**——`/note` 从在架文件现读 |
| 生命周期 | 永久（vault 归用户） | TTL（默认 24h）到期惰性焚 + 周期 sweep + 启动兜底；**ACK 即焚不等 TTL** |
| 幂等跳过 | 同 URL 处理过即跳 | 仅当**同 owner 且源产物还在架**才跳（否则老实重烧——产物在别人手里，跳过=给个拿不到东西的空条目）；命中时响应条目带 `from_run` |

## 用户 LLM 配置（`/settings/llm`，自前端 README 迁入）

鉴权的 `GET/PUT/DELETE /settings/llm`。JSON 档写入 `V2MD_PREFS_FILE` 同目录的
`llm-settings.json`（0600，可用 `V2MD_LLM_SETTINGS_FILE` 覆盖）；`V2MD_PREFS_STORE=postgres`
时写入独立的 `v2md_llm_settings` 表。升级 PG 部署前运行
`uv run --project backend python -m video2md migrate`，然后重启后端。
凭据为服务端可读取的配置数据，应按服务端机密文件/数据库管理，不包含在 run 记录与图 checkpoint 中。

## 匿名面清单（安全敏感，与 05 篇联动）

| 端点 | 暴露内容 | 现状防线 | ROADMAP |
|---|---|---|---|
| `/auth/device/code` `/auth/device/token` | 可消耗 Google OAuth 配额 | **无频控** | P0-2 |
| `/queue` | 队列深度/并发档位 | 无（探针口径） | 反代限源（P0-4 后可选） |
| `/healthz` | 版本号 | 无（探针口径） | 可接受 |
| `/preflight` | 内部路径/磁盘余量/算力池 provider | **无脱敏** | P0-3 |
| `/docs` `/openapi.json` | 全量契约 | 依赖反代 deny（人工纪律） | P0-4 |

## 进度双通道

轮询 `GET /runs/{id}` 与 SSE `GET /runs/{id}/events` 并存。SSE：连接先发 `{"event":"connected","status","url"}`，之后逐条 `data:` 为 `logs/run-*.jsonl` 同形事件（含 `graph:*`）；见 `graph:end` 或终态即关流。`V2MD_EVENTS=pg` 时跨副本扇出（驱动进程与 SSE 连接可不同侧，见 [04 篇](04-data-and-consistency.md#事件总线)）。

## 已知限制

1. **`GET /runs/{id}` 的 `task` 段只读本进程内存**（`api.py:852-856`）：多副本下，副本 B 看不到副本 A 驱动的 run 的实时 task 字段（终态与 stages 仍准——走 runs 表）。SSE 与轮询 `status` 不受影响。
2. 匿名面四项无频控（P0-2 前）——上公网前必须清偿。
3. 「多副本同一队列」= 读侧口径（`/queue` 计数、配额、dedup 判活走 PG）；写路径进程内（批判① E2）。

## 演进方向

P0-2 匿名限流、P0-3 preflight 脱敏、P0-4 反代 deny 机制化、P1-2 跨进程 cancel 端点（新增 `DELETE /runs/{id}` 或等价物）。

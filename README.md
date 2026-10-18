# video2md

视频 → Obsidian 文稿式笔记。**LangGraph 进程内编排**（2026-09-21 起 Dify 已抽离）：
下载/转写/抽帧/OCR/选帧/剪节段/写盘各是图节点，LLM（选帧/改写/校对/元数据）由
`video2md/llm.py` httpx 直连 openai-compatible 端点（默认 Kimi-K3）。
GPU 重活（转写/OCR/剪辑）默认由官方 [Google Colab CLI](https://github.com/googlecolab/google-colab-cli) 直连 T4 执行；同一 run 的 GPU 阶段复用会话，剪辑结束后立即关机，异常路径也触发释放。HTTP 算力服务保留为显式兼容配置。

**生产形态 = 云服务器 · 多用户 API**：登录鉴权（Google id_token/设备流 + 微信扫码）→
run 按 uid 归属隔离 → deliver「云端不储存」投递（bundle 拉取 + ACK/TTL 即焚），
公网经反代 TLS 暴露。架构/部署/运维的单一真相源在
**[docs/architecture/](docs/architecture/)**，
部署落地见[部署与架构文档](#部署与架构文档云服务器--多用户)。

```
POST /runs（FastAPI 内嵌图，并发闸门 V2MD_MAX_CONCURRENT_RUNS，默认 1=串行）
  resolve（剥分享文本/展开合集/state 幂等标记）
    → download → transcribe → frames → ocr          # 阶段节点 = runs.py 的 _stage_*（熔断/条目容错保留）
    → select_frames_llm（对照转录本选帧；失败/空选 → 本地启发式兜底，run 不炸）
    → select（发布选中帧入 vault attachments/）→ clips（±10s∪语音段 节段）
    → note_entry 逐条目回环：整理正文 →（review=on 时校对）→ 元数据 → 质量门禁 → 写盘
      · 幂等旁路：skipped/已写盘条目不烧 LLM；state.json 逐条落盘
    → finalize（run 状态落盘、清 tmp、裁剪 checkpoint）
  任一阶段熔断/全灭 → fail 节点显式终止（runs.json 留痕，重发同 URL 幂等续跑）
```

- 图状态 = runs.json 的 run 记录（JSON 可序列化），`thread_id == run_id`，
  checkpoint 落 `data/checkpoints.sqlite`（完成的 run 即 `delete_thread` 裁剪——
  恢复语义靠 state.json 幂等 + 重发 run，不靠 checkpoint 续跑）
- 笔记形态与质量门禁与 Dify 时代完全一致（见下），**产物对 Obsidian 无感**

## 项目目录

```text
frontend/                   前端
  web/                      Vue Web 应用（源码、测试、构建配置）
backend/                    后端
  video2md/                 API、任务管线、存储与数据库迁移
  acctkit/                  用户与鉴权
  gpu_plugins/              GPU 服务适配与参考实现
  scripts/                  批处理、数据迁移、评测、排障工具
  tests/                    后端测试与夹具
  vendor/                   随仓依赖
  pyproject.toml / uv.lock   Python 依赖配置
scripts/                    部署、备份、恢复、冒烟与仓库门禁
deploy/                     Kubernetes、Nginx 与部署夹具
docs/                       架构及运维文档
```

前端开发：`cd frontend/web && npm ci && npm run dev`。

后端开发：`cd backend && uv sync --frozen && uv run python -m video2md api`。
从仓根运行后端工具：`uv run --project backend python backend/scripts/<脚本名>.py`。
根目录的 Dockerfile、环境变量模板和 CI 配置供部署共用；镜像构建上下文仍为仓根。
已有根目录 `data/`、`logs/` 和 JSON 记录保留原位，避免覆盖 `backend/` 下的现有运行数据。

## 笔记形态（对齐「资料库/3Blue1Brown-神经网络」参考样式）

```
资料库/视频笔记/
  <标题>.md                        正文 = 转录本的完整文稿改写（非要点摘抄）
  transcripts/<标题>-transcript.txt  转录本 SSOT，正文 [!info] callout 内 wikilink
  attachments/<slug>-fNN-<mmss>.jpg  嵌入帧，LLM 对照转录本内容选图（本地评分兜底）
  attachments/<slug>-cNN-<mmss>.mp4  视频节段：帧 ±10s ∪ 所在语音段的可播片段
  索引.md                          追加行登记
```

正文结构：富 frontmatter（`lang/date/duration/transcribe_*/frames_*` + tags）
→ `> [!info] 视频信息`（来源链接/UP主、字幕文稿 wikilink、插图统计）
→ `> [!tip]- 章节导航`（折叠，从 `## 章节（mm:ss）` 标题机械提取）
→ 文稿正文：`##` 章节带时间戳锚点、关键句加粗、`![[attachments/…jpg]]` +
`![[attachments/…mp4]]` 配斜体图注、`[^n]` 脚注、callout。
嵌入文件名经本地校验（清单外引用自动剥离），防 LLM 幻觉图链。

**不缓存策略**：中间产物全在系统临时目录，跑完必删；持久落盘 = vault 笔记 +
`state.json`（幂等记录）+ `runs.json`（run 记录）+ `data/checkpoints.sqlite`（图 checkpoint）+
`logs/`（run 级 jsonl 遥测真相）。

## Colab CLI GPU

`cd backend && uv sync --frozen` 安装固定版本 CLI。首次在执行机器运行 `colab sessions`
完成 OAuth，然后在 `backend/.env` 配置（模板见仓根 `.env.example`）：

```dotenv
V2MD_REMOTE=colab-cli
V2MD_OCR_REMOTE=colab-cli
V2MD_CLIPS_REMOTE=colab-cli
V2MD_COLAB_SESSION=v2md-whisper
```

网页端也可在「我的 → Colab 授权」打开 Google 授权页，再将 Google 返回的授权码填回页面。
该凭证由整台服务共用：启用登录的部署需设置 `V2MD_COLAB_ADMIN_UIDS`（逗号分隔的内部 uid），
只有这些管理员可以更换账号；关闭登录时仅允许回环地址的开发请求操作。授权码十分钟内有效，
凭证仅保存在后端 CLI 凭证目录。页面显示的是凭证存在状态，连接有效性仍由实际任务检查。

ASR 使用 faster-whisper/CUDA，OCR 使用 RapidOCR，剪辑在同一 Colab 会话执行。
会话按需创建，剪辑阶段完成后立即执行 `colab stop -s <session>`，释放远端运行时，
不等待后续正文 LLM。阶段失败、异常退出和独立调用转写/OCR/剪辑也会在 finally 中触发关机。
关闭命令失败或超时会重试；仍失败则明确报错，异常路径保留原错误并输出关机失败提示。
LangGraph 节点线程共享本次任务的会话使用标记，避免子线程用了 GPU、外层却跳过关机。
T4 不可用默认明确失败，只有显式
`V2MD_COLAB_CPU_FALLBACK=on` 才启用 CPU 降级。跨进程共享同一会话时必须配置相同
`V2MD_COLAB_LOCK`（部署默认为 `/app/data/colab.lock`）。

容器把独立的 Colab 凭证目录挂到 `/home/appuser/.config/colab-cli`。K8s 清单使用
`/home/ubuntu/stack/video2md/colab-auth`，需预先在服务器完成 CLI 授权，将 token.json
复制到该目录，并让 UID/GID 10001 可写（CLI 需要刷新凭证和写 sessions.json）。
不要直接改宿主用户的凭证目录权限；不要将 token.json 提交到仓库。

真实 GPU 验收（不调用 LLM，测试结束释放专用会话）：

```bash
backend/.venv/bin/python backend/scripts/smoke_colab_cli.py --session v2md-smoke
# Linux：追加 --video /path/to/short-spoken.mp4；macOS 可用 say 合成测试语音
```

部署依赖中的 `platform-otel-sdk` 已随本仓 vendor，单仓 checkout 和镜像构建不再依赖兄弟目录。

## 运行拓扑

| | 本地开发（Mac/任意开发机） | 云服务器（生产 · 多用户） |
|---|---|---|
| 入口 | `cd backend && uv run python -m video2md api`（或 `graph-run` CLI） | Kubernetes API + Colab CLI + 反代 TLS 域名 |
| 鉴权 | `V2MD_AUTH=off`（无 token 归同一 dev uid） | `V2MD_AUTH=google`（`Authorization: Bearer <id_token>`；设备流/微信可并列） |
| OCR | macOS Vision（`uv sync --extra mac`，在 backend/ 下）或 Colab CLI | Colab CLI RapidOCR（Linux 无 Vision） |
| 转写/剪辑 | 本地 whisper/ffmpeg 或 Colab CLI | Colab CLI T4（镜像不含 whisper/torch） |
| 出网 | Clash 等本地出口（可选） | `V2MD_PROXY` 出网代理；国内站需大陆住宅类出口（风控，见「出网与风控」） |
| 笔记落地 | vault 直写本地 Obsidian | deliver 模式：outbox 在架 → 客户端拉 bundle → ACK/TTL 即焚 |

## 部署与架构文档（云服务器 · 多用户）

生产形态 = 云服务器 · 多用户 API。架构/部署/运维的单一真相源（SSOT）在
**[docs/architecture/](docs/architecture/README.md)**——部署前提与步骤、反代 TLS、
备份恢复、安全清单、容量与 SLO、灾备 runbook 全部在册：

| 文档 | 内容 |
|---|---|
| [架构索引](docs/architecture/README.md) | 阅读地图 + 十层布局（批判修订版）+ 落地状态追踪 |
| [01 系统总览](docs/architecture/01-overview.md) | 上下文图、核心数据流、设计哲学 |
| [02 运行拓扑与部署](docs/architecture/02-runtime-topology.md) | 部署前提/步骤、网络口径、发布回滚、恢复语义 |
| [03 HTTP 契约与鉴权](docs/architecture/03-api-contract.md) | 端点、错误信封、acctkit、产物双模式、匿名面清单 |
| [04 数据与一致性](docs/architecture/04-data-and-consistency.md) | 存储分派（json\|postgres）、PG 表、幂等口径、并发原语 |
| [05 安全](docs/architecture/05-security.md) | 威胁模型、已知缺口、公网上线检查清单 |
| [06 可观测性](docs/architecture/06-observability.md) | 三层模型、指标清单、告警链、卡死看门狗 |
| [07 算力池契约](docs/architecture/07-gpu-pool-contract.md) | gpu_plugins 协议、四种 provider、异步 job |
| [08 容量与 SLO](docs/architecture/08-capacity-and-slo.md) | 容量模型框架 + SLO 草案（P1 实测回填） |
| [09 灾备 runbook](docs/architecture/09-disaster-recovery.md) | RTO/RPO、备份恢复流程、演练制度 |
| [批判存档](docs/architecture/critique/) | 十层「叙述 vs 现实」对照 + ISO 25010 质量属性评估（2026-09-24） |

> ⚠ **上公网前必读**：[05 安全](docs/architecture/05-security.md) 的检查清单。
> （原 ROADMAP P0 七项清单已随该文件 2026-10-10 删除，原文见 git 历史。）

## 用法

```bash
# 发起（同步解析建 run → 后台跑图；合集自动展开，part_items 分窗，force 全量重跑，review 开校对）
curl -X POST localhost:8790/runs -H 'Content-Type: application/json' \
  -d '{"url": "https://www.bilibili.com/video/BV…", "part_items": "1,3-5", "force": false, "review": false}'

# 生产（云服务器 · 多用户）：公网域名 + 登录态（id_token 从前端登录侧拿）
curl -X POST https://api.example.com/runs -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <google_id_token>' \
  -d '{"url": "https://www.youtube.com/watch?v=…"}'

# 本地文件走上传面（多用户）：上传 → path 直接喂 /runs
up=$(curl -s -X POST https://api.example.com/uploads \
  -H 'Authorization: Bearer <google_id_token>' -F file=@demo.mp4 | jq -r .path)
curl -X POST https://api.example.com/runs -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <google_id_token>' -d "{\"url\": \"$up\"}"

# 状态 / 产物
curl localhost:8790/runs/<run_id>          # 终态 + 逐条目 done map + partial_failures
curl localhost:8790/runs/<run_id>/note     # 产出 markdown（标题/路径/正文）
curl localhost:8790/preflight              # 开跑前体检（ffmpeg/磁盘/vault/算力池）
curl localhost:8790/queue                  # {queue_len, running}（gpu_router 看门狗回读）

# 本地调试（不经 HTTP）
cd backend && uv run python -m video2md graph-run <url> [--force] [--review] [--max-frames 60]
cd backend && uv run python -m video2md runs [run_id]    # run 记录排障（只读）

# 批量
python3 backend/scripts/batch_csv.py [csv路径] [--limit N] [--force] [--api http://127.0.0.1:8790]
python3 backend/scripts/rerun_failed.py [url...]
```

| POST /runs 字段 | 作用 |
|---|---|
| `url` | B站/YouTube/抖音/小红书链接（合集自动展开）或本地文件路径（容器内路径） |
| `part_items` | 只跑指定条目，如 `1,3-5` |
| `force` | 忽略 state 幂等全量重跑 |
| `review` | 启用校对 LLM（更准、更慢） |
| `max_frames` | 候选帧池上限（默认 60） |
| `resolve_only` | 只解析建 run 不驱动（批量脚本分窗预飞用；后续正式 POST 会 dedup 命中并驱动） |

## HTTP 后端契约（多用户产品 · Phase 1/2）

GUI 前端把本服务当正式后端：契约硬化在 `backend/video2md/models.py`（Pydantic），契约测试
`backend/tests/test_api_contract.py`。**端点清单、错误信封、鉴权 provider、产物双模式（vault/deliver）、
匿名面清单全部迁入 [docs/architecture/03-api-contract.md](docs/architecture/03-api-contract.md)**。

## acctkit：可复用账号登录模块（顶层包）

登录工程本体在 **`acctkit/`**——零 Web 框架/零宿主依赖（标准库 + 可选 google-auth），
整个目录拷进别的项目即用。对外接口（`authenticate(headers)`）、两个可替换缝
（AuthProvider/IdentityStore）、`build_from_env` 配置语义详见
[docs/architecture/03-api-contract.md](docs/architecture/03-api-contract.md)。

## 阶段与质量门禁

| 环节 | 机制 |
|------|------|
| 转写 | `condition_on_previous_text=False` 抑制幻觉循环；丢弃 no_speech_prob>0.6 / compression_ratio>2.4 的段；段置信度 `exp(avg_logprob)`，<0.5 标 ⚠ |
| 抽帧 | 均匀网格 2/3 ∪ 场景切变 1/3，极近帧去重 0.4s，模糊门禁保底最清晰 5 张 |
| 选帧 | LLM 对照转录本选帧（只准候选清单内文件名，代码过滤非法名）；本地启发式兜底 = OCR 文字主信号 + 语音段分桶配额 + MAE 去重；LLM 失败自动降级兜底不炸 run |
| 节段 | 剪辑窗口吸附帧所在 whisper 语音段（±10s ∪ 段 ±1s，clamp ±20s）；帧↔节段一一配对 |
| OCR | Vision 置信度 <0.5 丢弃；远程池 RapidOCR；图文帖由 OCR 合成转录本 |
| LLM | 4 组 prompt 在 `video2md/prompts/*.md`（`{{var}}` 占位）；调用 3 次重试（429/5xx/网络），4xx 不重试；上游中转挂起容忍 30s×2 |
| 写盘门禁 | **拒答/空壳绝不落盘**（`looks_like_refusal`）；转录本 <200 字（`V2MD_MIN_TRANSCRIPT_CHARS`）跳过写盘；缺转录本拒绝写盘；嵌入 sanitize 防幻觉图链 |
| 多条目 | 每条目独立子目录（`tmp/eNN`），单条失败不阻断；部分失败记 `partial_failures`，终态 `partial` |
| 熔断 | 阶段内同错误签名连错：全局型（认证/网络/风控）2 连即熔，其余 `V2MD_CB_THRESHOLD`（默认 3）连熔 |
| 幂等 | `state.json` 按视频指纹/归一化 URL 记完成态；写盘成功逐条落盘；重发同 URL 跳过 |
| 人工 | frontmatter `reviewed: false`，Obsidian 筛查后手改 `true` |

## 可观测性

三层模型（本地 jsonl 真相 / OTel→Langfuse trace / OTel→Prometheus 指标）+ 告警链
（Prometheus 规则 → Alertmanager → 飞书）+ 死人开关 + run 卡死看门狗（mark/exit 双档）。
**全部口径迁入 [docs/architecture/06-observability.md](docs/architecture/06-observability.md)**。

## 测试与 CI 门禁

[![CI](https://github.com/mintao1/video2md/actions/workflows/ci.yml/badge.svg)](https://github.com/mintao1/video2md/actions/workflows/ci.yml)

push/PR 自动跑三个 job（`.github/workflows/ci.yml`）：**test**（pytest 契约 + 11 个脚本式测试（含运维三件套）+ `scripts/smoke_ci.sh` 进程级 mockpool 全链冒烟 + 告警配置 promtool/amtool 校验，全离线零 token）、
**lint**（ruff + mypy）、**docker-build**（生产镜像构建路径）。CI 跑什么，本地就能跑什么：

布局口径：pyproject 在 `backend/`——Python 侧 uv/pytest/脚本式测试在 `backend/` 下跑；
冒烟/e2e 脚本在仓根或 backend/ 直接执行（脚本自己定位路径）。

```bash
cd backend
uv run ruff check video2md acctkit scripts   # lint（配置在 pyproject.toml [tool.ruff]）
uv run mypy video2md acctkit                     # 类型检查（[tool.mypy]，check_untyped_defs）
uv run pytest tests/test_api_contract.py tests/test_acctkit.py -q
cd ..
bash scripts/smoke_ci.sh                         # 进程级 mockpool 全链冒烟（跨平台，CI 同款）
```

离线测试清单（与 CI 同款，均在 `backend/` 下 `uv run`）：

```bash
uv run python3 tests/test_llm.py             # LLM 客户端：模板渲染/重试纪律/流式收集/usage 捕获/选帧解析（MockTransport 离线）
uv run python3 tests/test_graph.py           # 图编排：节点序/失败路由/幂等短路/兜底/熔断/partial/checkpoint 裁剪
                                             # + checkpointer env 分派 + deliver artsink 缝/搬家剥正文（全打桩离线）
uv run python3 tests/test_resolve.py         # URL 解析/条目展开/幂等键（离线）
uv run python3 tests/test_remote_clients.py  # 算力池薄客户端 HTTP 契约（MockTransport 离线）
uv run pytest tests/test_api_contract.py     # HTTP 后端契约：鉴权/owner 403/422 错误形状/SSE 关流/OpenAPI
                                             # + deliver 面（bundle zip/ACK 410/TTL 惰性焚/sweep/owner 收紧幂等）（TestClient 离线）
uv run pytest tests/test_colab_cli.py        # Colab CLI 三负载/跨进程传输契约/异常释放/GPU 不可用不静默降级（离线假 CLI）
uv run pytest tests/test_acctkit.py          # acctkit 模块自身接口：provider 次序/Google+微信打桩/开户回填归并/前缀工厂（零宿主零网络）
uv run python3 tests/test_observability.py   # OTel 桥：span 树/gen_ai 属性/traceId 确定性/指标仪表/deadman（InMemory 离线）
uv run python3 tests/test_watchdog.py        # run 卡死看门狗：三层心跳判定/mark 翻转/exit 自愈/异常静默（离线）
uv run python3 tests/test_backup_scripts.py  # 备份脚本：dry-run 拼装/防重入锁/retention/manifest 校验（stub docker 离线）
uv run python3 tests/test_otel_replay.py     # 历史回放：jsonl→span 树重建/悬空闭合/时间戳还原（InMemory 离线）
uv run python3 tests/test_evals.py           # evals 旁路：指标编排/scores 三运输路径/回归对比（假 deepeval/ragas/langfuse）
./tests/test_e2e.sh                   # 端到端：合成视频 → 自起 api（假 LLM）→ 全断言（本地 whisper，不烧 token）
MODE=deliver ./tests/test_e2e.sh      #   deliver 形态分支：bundle 拉取解包跑同套断言 + vault 零写入 + ACK 即焚 410 链
./scripts/smoke_api_vault.sh    # 解耦 API 路子冒烟（开发机）：POST+SSE+轮询+/note 纯 HTTP，产物进本机 vault（真 LLM 少量 token）
./scripts/smoke_api_deliver.sh  # deliver 形态真 LLM 冒烟：在架→bundle 投递→/note 一致性→ACK 即焚 410，产物不进开发机 vault
scripts/smoke_compose.sh        # compose 全链路冒烟：mockpool 假算力 + 假 LLM（零 GPU 零 token）
REAL_LLM=1 ./tests/test_e2e.sh        # 烧真实 LLM 的端到端（用 ~/.config/video2md.env）
# gpu_router 本体测试在 ../gpu_router/tests/
```

## 本地验收入口与测试身份

当前没有指定的线上验收地址或真实测试账号。默认本地入口：前端
`http://localhost:5173`、后端 `http://127.0.0.1:8790`；启动命令见上文。
本地跨域联调需设置 `V2MD_CORS_ORIGINS=http://localhost:5173`。

- 本地开发身份：`V2MD_AUTH=off` 使用固定 `dev` 身份，不需要手机号或密码；所有请求共享该身份。
  前端可通过 `http://localhost:5173/login#id_token=local-acceptance-only`
  进入本地联调。这里的字符串只是开发占位符，不能用于生产登录。
- 自动化测试身份：`backend/tests/fixtures/web_integration_api.py` 提供 Alice/Bob，
  分别使用 `Authorization: Bearer integration-alice` / `Bearer integration-bob`。
  这两个凭据仅由测试夹具识别；`npm run test:integration` 会自行启动和清理夹具服务。
- 真实登录：使用 Google 授权码流和自己授权的账号。短信、密码、微信按钮目前仍接 mock，
  返回的 `mock-id-token` 不能作为生产测试账号。

测试进程应把 runs/state/users/prefs、上传、日志、vault 和 outbox 指向临时目录，
PostgreSQL 测试使用专门的可清空数据库。不要把现有业务库交给 `test_pg_stores.py`。

### 2026-10-18 实际验收记录

对象为当天的**未提交工作区**，不代表线上部署或某个已发布版本。浏览器验收使用独立
回环端口与合成教材；GPU 验收使用专用 Colab 会话和合成语音，不上传用户视频。
截图见 [本轮测试证据](docs/testing/2026-10-18/)。
结构化结果见 [results.json](docs/testing/2026-10-18/results.json)。

| 检查 | 实际结果 |
|---|---|
| 后端 API / acctkit / Colab CLI 契约 | `93 passed` |
| 前端生产构建、真实 HTTP 集成 | 构建成功；`21 passed` |
| mypy | 36 个源文件通过 |
| 十个脚本式测试 | graph / llm / observability / otel_replay / evals / graph_obs / resolve / remote_clients / watchdog / backup_scripts 均退出 0 |
| 进程级完整冒烟（mock 算力、mock LLM） | done；SSE、笔记、帧、视频节段、转录本通过 |
| 真实 Colab T4 | ASR 置信度 0.921；OCR 识别测试文字；剪辑经 ffprobe 校验；会话释放 |
| 同一任务真实 GPU + 真实 LLM | **失败**：run `af031a9d` 完成 download / transcribe / frames / ocr / select / clips，LLM 选帧与正文请求返回 HTTP 402 `Insufficient account balance`；选帧可降级，正文失败使终态为 error |
| HTTP deliver 成功链（mock 算力、mock LLM） | run `1e905153` done；ZIP 完整性、帧/剪辑、与 `/note` 正文一致、vault 零写入、ACK 物理销毁及 bundle/note 再取 410 均通过 |
| PG 偏好与控制面并发 / rescue | `3 passed`；rescue E2E 通过 |
| 双 API 进程 + PG | 跨副本可见性、不抢活任务、SSE、取消、杀进程接管全部通过（算力与 LLM 为 mock） |
| 浏览器 + 真实 HTTP | 教材加载、收藏刷新持久化、阅读器正文与图片、阅读进度写回通过；教材为合成夹具 |
| 本地读请求基线 | 100 次 `/runs`、并发 10，全部 200；p50 8.7ms / p95 60.2ms；仅小数据本地基线 |

本轮发现，尚未修改业务代码：

1. **真实 LLM 余额不足，阻断核心转换**：配置中的上游返回 HTTP 402，正文无法生成；需恢复上游可用性后重跑真实全链路。此轮真实任务未能验证 bundle / ACK 成功链。
2. **导出未实现**：教材库选择 Markdown 后仅提示打包，不生成文件；`LibraryView.vue` 的
   `onExport` 仅显示 toast。PDF / EPUB / LaTeX 也共用该处理器。
3. **同步误报成功**：点击“立即同步”直接显示成功，没有执行刷新/同步；“上次同步于 2 分钟前”是静态文案。
4. **登录仍有原型入口**：短信、密码、微信返回模拟 token，与真实鉴权后端不兼容。
5. **CI lint 失败**：`backend/scripts/otel_replay.py:49` 和
   `backend/scripts/smoke_colab_cli.py:42` 的导入顺序触发 Ruff I001。
6. **PG 测试模式未恢复**：`test_pg_stores.py` 在事件测试后设置 `V2MD_EVENTS=local`，
   取消广播测试未恢复 pg，导致断言失败。仅在内存执行版本恢复 pg 后全脚本通过；
   仓库脚本保持原样。真实双进程取消测试通过，不能把此断言失败视为已确认的生产取消故障。
7. **终态后新连 SSE 不结束**：对一个已 done 的 run 执行
   `curl -sN --max-time 3 http://127.0.0.1:<测试端口>/runs/<run_id>/events`，
   收到 `connected/status=done` 后仍等到客户端超时（退出 28）。`run_events` 仅在收到
   后续业务事件后检查终态，没有在初始快照后结束；不影响本轮已通过的运行中订阅场景。

生产鉴权进程实测：`/runs`、`/prefs` 的匿名请求及两个夹具 token 均被拒绝（401），
`/me` 匿名请求返回 401；随后 `/me` 携带模拟 token 的一次请求超过 10s 客户端预算，
此项记为未通过的鉴权响应时延观察，尚未确认根因，不能宣称所有坏 token 都能及时拒绝。

本机没有 Docker，未验证生产镜像构建、容器 Nginx 反代；没有线上域名及真实 Google
测试账号，未验证公网 TLS、真实 OAuth 成功登录、站点下载风控及生产规模容量。
这些未测项和上述功能缺口关闭前，本轮不作为完整生产上线验收通过。

## 旧版目录

本地旧版目录归入 `backend/legacy/`。当前仓库不含受版本控制的 Dify 旧版源码，新链路不依赖此目录。

## FAQ

- **长视频**：转录本上限 6 万字符（约 3 小时密集语音），超出请先切段分批跑
- **whisper 首跑**（Mac 本地模式）：下载模型（turbo ≈1.6GB / small ≈460MB）到 `~/.cache/whisper`；
  云上容器不含 whisper——默认通过 Colab CLI 在远端 GPU 转写
- **个人 LLM**：Web「我的 → LLM API 配置」显式填写 OpenAI 兼容 Base URL、API Key 和可选模型；
  保存后用于该用户后续启动的任务，Key 不回显，留空保留已存 Key，“恢复服务默认”可清除个人配置。
  JSON 档凭据文件为 prefs 同目录的 `llm-settings.json`（0600）；PG 档升级先执行
  `uv run --project backend python -m video2md migrate`（新增 0004 迁移），再重启后端。
  服务默认 LLM 仍通过 `~/.config/video2md.env` 的 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` 配置。
- **从 Dify 迁来**：Dify 旧版 DSL 不再维护；Dify 基座（`~/dify/docker`）可整体停用
- **隐私**：deliver 模式云端不留产物（bundle 拉取 + ACK/TTL 即焚，tmp 跑完必删）；
  转录文本经 LLM 中转（`LLM_BASE_URL` 指向的服务）

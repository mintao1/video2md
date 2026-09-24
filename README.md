# video2md

视频 → Obsidian 文稿式笔记。**LangGraph 进程内编排**（2026-09-21 起 Dify 已抽离）：
下载/转写/抽帧/OCR/选帧/剪节段/写盘各是图节点，LLM（选帧/改写/校对/元数据）由
`video2md/llm.py` httpx 直连 openai-compatible 端点（默认 Kimi-K3）。
GPU 重活（转写/OCR/剪辑）外包给 [gpu_router](https://github.com/mintao1/gpu-router) 算力池（Colab/Kaggle/Modal/ZeroGPU/国内云动态选举）。

**生产形态 = 云服务器 · 多用户 API**：登录鉴权（Google id_token/设备流 + 微信扫码）→
run 按 uid 归属隔离 → deliver「云端不储存」投递（bundle 拉取 + ACK/TTL 即焚），
公网经反代 TLS 暴露。架构/部署/运维的单一真相源在
**[docs/architecture/](docs/architecture/)**（含[生产就绪 ROADMAP](docs/architecture/ROADMAP.md)），
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

## 运行拓扑

| | 本地开发（Mac/任意开发机） | 云服务器（生产 · 多用户） |
|---|---|---|
| 入口 | `uv run python -m video2md api`（或 `graph-run` CLI） | docker compose（api + gpu_router [+ 观测栈]）+ 反代 TLS 域名 |
| 鉴权 | `V2MD_AUTH=off`（无 token 归同一 dev uid） | `V2MD_AUTH=google`（`Authorization: Bearer <id_token>`；设备流/微信可并列） |
| OCR | macOS Vision（`uv sync --extra mac`）或远程池 | 远程池（Linux 无 Vision） |
| 转写/剪辑 | 本地 whisper/ffmpeg 或远程池 | 远程池（镜像不含 whisper/torch） |
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
| [ROADMAP](docs/architecture/ROADMAP.md) | P0 上公网前 / P1 上量前 / P2 规模化（带验收标准） |
| [批判存档](docs/architecture/critique/) | 十层「叙述 vs 现实」对照 + ISO 25010 质量属性评估（2026-09-24） |

> ⚠ **上公网前必读**：[05 安全](docs/architecture/05-security.md) 的检查清单 +
> [ROADMAP P0](docs/architecture/ROADMAP.md)（AUTH 防呆、匿名端点限流、preflight 脱敏、
> 反代配置实体化、schema 迁移纪律、上传内容嗅探等七项）。

## Obsidian 插件（Video2MD Inbox · 产物回传主路）

把「服务器产物 → 开发侧 vault」解耦成装在 Obsidian 里的独立插件：轮询本人名下 done run →
拉 `GET /runs/{id}/bundle` zip → 解包写进 `资料库/视频笔记/`（索引按 wikilink 去重 append）→
`DELETE artifacts` ACK 即焚。附带命令面板提交链接与 Google 设备流登录。工程在
`obsidian-plugin/`（TypeScript + esbuild，CI 有独立 plugin job）。

**前置**：服务器切 deliver 模式 + google_device 鉴权（生产档进 `secrets/video2md.env`：
`V2MD_ARTIFACT_MODE=deliver`、`V2MD_AUTH_PROVIDERS=google,google_device`，
另配 TV/Limited-Input 型 `GOOGLE_CLIENT_ID/SECRET`）；存量 run 收编用
`python scripts/migrate_run_owner.py --to <uid> [--status done] [--dry-run]`。

**API 访问**——云服务器形态：按 [02 篇部署步骤](docs/architecture/02-runtime-topology.md)起反代 TLS 后，插件设置页填
`https://api.example.com`，「登录」走 Google 设备流（浏览器输码授权）即可，无需任何
隧道。自托管单机形态 API 只绑回环时，可 `ssh -L 8790:127.0.0.1:8790 <server>` 隧道
访问 `http://127.0.0.1:8790`（常驻可做成系统服务）。

**构建与安装**：

```bash
cd obsidian-plugin && npm ci && npm run build
mkdir -p "$HOME/Documents/Obsidian Vault/.obsidian/plugins/video2md-obsidian"
cp main.js manifest.json "$HOME/Documents/Obsidian Vault/.obsidian/plugins/video2md-obsidian/"
# Obsidian → 设置 → 第三方插件 → 启用 Video2MD Inbox → 设置页点「登录」（浏览器输码授权）
```

登录后状态栏显示同步节拍；`立即同步收件箱` 命令手动触发；设置页可调轮询间隔、目标
文件夹与 autoAck。首次使用建议先点「连接测试」确认隧道与凭据两档都通。

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
uv run python -m video2md graph-run <url> [--force] [--review] [--max-frames 60]
uv run python -m video2md runs [run_id]    # run 记录排障（只读）

# 批量
python3 scripts/batch_csv.py [csv路径] [--limit N] [--force] [--api http://127.0.0.1:8790]
python3 scripts/rerun_failed.py [url...]
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

GUI 前端把本服务当正式后端：契约硬化在 `video2md/models.py`（Pydantic），契约测试
`test_api_contract.py`。**端点清单、错误信封、鉴权 provider、产物双模式（vault/deliver）、
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

```bash
uv run ruff check video2md acctkit scripts   # lint（配置在 pyproject.toml [tool.ruff]）
uv run mypy video2md acctkit                 # 类型检查（[tool.mypy]，check_untyped_defs）
uv run pytest test_api_contract.py test_acctkit.py -q
bash scripts/smoke_ci.sh                     # 进程级 mockpool 全链冒烟（跨平台，CI 同款）
```

离线测试清单（与 CI 同款）：

```bash
python3 test_llm.py             # LLM 客户端：模板渲染/重试纪律/流式收集/usage 捕获/选帧解析（MockTransport 离线）
python3 test_graph.py           # 图编排：节点序/失败路由/幂等短路/兜底/熔断/partial/checkpoint 裁剪
                                # + checkpointer env 分派 + deliver artsink 缝/搬家剥正文（全打桩离线）
python3 test_resolve.py         # URL 解析/条目展开/幂等键（离线）
python3 test_remote_clients.py  # 算力池薄客户端 HTTP 契约（MockTransport 离线）
python3 -m pytest test_api_contract.py  # HTTP 后端契约：鉴权/owner 403/422 错误形状/SSE 关流/OpenAPI
                                        # + deliver 面（bundle zip/ACK 410/TTL 惰性焚/sweep/owner 收紧幂等）（TestClient 离线）
python3 -m pytest test_acctkit.py       # acctkit 模块自身接口：provider 次序/Google+微信打桩/开户回填归并/前缀工厂（零宿主零网络）
python3 test_observability.py   # OTel 桥：span 树/gen_ai 属性/traceId 确定性/指标仪表/deadman（InMemory 离线）
python3 test_watchdog.py        # run 卡死看门狗：三层心跳判定/mark 翻转/exit 自愈/异常静默（离线）
python3 test_alert_rules.py     # 告警配置：规则结构/指标名对齐/AM 路由/飞书卡片拼装（YAML 离线）
python3 test_backup_scripts.py  # 备份脚本：dry-run 拼装/防重入锁/retention/manifest 校验（stub docker 离线）
python3 test_otel_replay.py     # 历史回放：jsonl→span 树重建/悬空闭合/时间戳还原（InMemory 离线）
python3 test_evals.py           # evals 旁路：指标编排/scores 三运输路径/回归对比（假 deepeval/ragas/langfuse）
./test_e2e.sh                   # 端到端：合成视频 → 自起 api（假 LLM）→ 全断言（本地 whisper，不烧 token）
MODE=deliver ./test_e2e.sh      #   deliver 形态分支：bundle 拉取解包跑同套断言 + vault 零写入 + ACK 即焚 410 链
./scripts/smoke_api_vault.sh    # 解耦 API 路子冒烟（开发机）：POST+SSE+轮询+/note 纯 HTTP，产物进本机 vault（真 LLM 少量 token）
./scripts/smoke_api_deliver.sh  # deliver 形态真 LLM 冒烟：在架→bundle 投递→/note 一致性→ACK 即焚 410，产物不进开发机 vault
scripts/smoke_compose.sh        # compose 全链路冒烟：mockpool 假算力 + 假 LLM（零 GPU 零 token）
REAL_LLM=1 ./test_e2e.sh        # 烧真实 LLM 的端到端（用 ~/.config/video2md.env）
# gpu_router 本体测试在 ../gpu_router/tests/
```

## legacy/（Dify 时代归档，只读备查）

`legacy/` 保留 Dify 编排时代的资产：DSL 生成器三件套（`dify/`）、stdlib 版 serve.py、
kb.py（Dify dataset 入库）、对应测试与 dify 专属运维脚本。**新链路不依赖其中任何文件**。

## FAQ

- **长视频**：转录本上限 6 万字符（约 3 小时密集语音），超出请先切段分批跑
- **whisper 首跑**（Mac 本地模式）：下载模型（turbo ≈1.6GB / small ≈460MB）到 `~/.cache/whisper`；
  云上容器不含 whisper——转写必须走算力池
- **换 LLM**：改 `~/.config/video2md.env` 的 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`，重启服务
- **从 Dify 迁来**：`legacy/` 里的 DSL 不再维护；Dify 基座（`~/dify/docker`）可整体停用
- **隐私**：deliver 模式云端不留产物（bundle 拉取 + ACK/TTL 即焚，tmp 跑完必删）；
  转录文本经 LLM 中转（`LLM_BASE_URL` 指向的服务）

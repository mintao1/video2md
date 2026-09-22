# video2md

视频 → Obsidian 文稿式笔记。**LangGraph 进程内编排**（2026-09-21 起 Dify 已抽离）：
下载/转写/抽帧/OCR/选帧/剪节段/写盘各是图节点，LLM（选帧/改写/校对/元数据）由
`video2md/llm.py` httpx 直连 openai-compatible 端点（默认 Kimi-K3）。
GPU 重活（转写/OCR/剪辑）外包给 [gpu_router](https://github.com/mintao1/gpu-router) 算力池（Colab/Kaggle/Modal/ZeroGPU/国内云动态选举）。

```
POST /runs（FastAPI 内嵌图，串行信号量=1）
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

| | Mac 本地（开发） | lcode-4c（生产，4C/7.5G） |
|---|---|---|
| 入口 | `uv run python -m video2md api`（或 `graph-run` CLI） | docker compose 双服务 |
| OCR | macOS Vision（`uv sync --extra mac`）或远程池 | 远程池（Linux 无 Vision） |
| 转写/剪辑 | 本地 whisper/ffmpeg 或远程池 | 远程池（镜像不含 whisper/torch） |
| 代理 | Clash 等本地出口 | `V2MD_PROXY=http://127.0.0.1:17897`（Mac→服务器 SSH 隧道转发，`com.singtrain.proxytunnel`） |
| 笔记落地 | 本地 Obsidian vault | `vault` docker 卷 → Syncthing 同步回 Mac |

> **本机无常驻服务（2026-09-22 起）**：原 launchd 8790 实例已停，Mac 回归纯开发机——
> 开发时手动 `uv run python -m video2md api`；恢复常驻 = plist 改回原名（现为
> `~/Library/LaunchAgents/com.linmintao.video2md-server.plist.disabled`）+ `launchctl
> bootstrap gui/$UID …/com.linmintao.video2md-server.plist`。本机运行时产物（logs/
> runs.json/state.json 等）已归档 `~/Archive/dify-video2md-local-2026-09-22*.tar.gz`。

## 部署 lcode-4c

前提：服务器 Docker + Compose；两仓库同级放 `~/stack/{video2md,gpu_router}`
（别处则设 `GPU_ROUTER_DIR`）。全部命令在 Mac 上执行：

```bash
# 1. 同步代码（secrets 被排除，单独放）
rsync -az --delete --exclude '.venv' --exclude 'logs' --exclude '__pycache__' \
  --exclude '.env' --exclude 'secrets' --exclude 'state.json*' --exclude 'runs.json*' \
  --exclude 'data' --exclude '.DS_Store' \
  ./ lcode-4c:~/stack/video2md/
rsync -az --delete --exclude '.venv' --exclude 'logs' --exclude '__pycache__' \
  --exclude 'gpu_router.json' --exclude '.DS_Store' \
  ~/Desktop/开发项目/gpu_router/ lcode-4c:~/stack/gpu_router/

# 2. 凭证（0600；只放服务器，不进 git）
#    secrets/video2md.env   ← LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（本机 SSOT 是 ~/.config/video2md.env）
#                            + V2MD_DEADMAN_PING_URL（可选，死人开关）
#    secrets/gpu_router.env ← HTTP_PROXY=http://127.0.0.1:17897 等（出墙代理，SSH 隧道转发）
#    secrets/obs.env        ← LANGFUSE_BASIC / HTTPS_PROXY / GF_SECURITY_ADMIN_PASSWORD（观测栈，可选）
#    secrets/colab-cli/token.json（可选；缺了 colab provider 自动冷却）
scp ~/.config/video2md.env lcode-4c:~/stack/video2md/secrets/video2md.env

# 3. 构建（x86 原生构建；PIP_INDEX_URL 走清华镜像，ghcr/debian 源已换国内镜像）
ssh lcode-4c 'cd ~/stack/video2md && \
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  docker compose -f docker-compose.yml -f docker-compose.obs.yml build'
#    ⚠ 实测 2026-09-22：依赖变更触发的全量 `uv sync` 在该机可能 0 CPU/0 socket 挂死
#    （三次复现；小范围 uv pip install 正常）。挂死就杀掉重试 `--progress=plain`；
#    纯代码改动可用补丁层秒级绕过：
#      printf 'FROM video2md:latest\nCOPY video2md/ /app/video2md/\nCOPY acctkit/ /app/acctkit/\n' > /tmp/patch.Dockerfile
#      docker build -t video2md:latest -f /tmp/patch.Dockerfile . && docker compose ... up -d
#    依赖小版本升级同样绕得动（2026-09-22 langgraph 1.2.12 实操）：补丁层里加
#      COPY pyproject.toml uv.lock /app/ 和
#      RUN PIP_INDEX_URL=… UV_INDEX_URL=… uv pip install --python /app/.venv/bin/python \
#          langgraph==<版本> langchain-core==<版本>
#    （uv pip 不走挂死的 sync 代码路径；版本与 uv.lock 保持一致）

# 4. 起栈（含观测栈：collector/prometheus/grafana）+ 体检
ssh lcode-4c 'cd ~/stack/video2md && \
  docker compose -f docker-compose.yml -f docker-compose.obs.yml up -d && \
  curl -s localhost:8790/healthz && curl -s localhost:8790/preflight && \
  curl -s "localhost:9700/status?refresh=true" && \
  docker logs video2md-otel-collector-1 2>&1 | tail -3'
#    观测栈验证：curl -s 127.0.0.1:9090/-/healthy（prometheus）· curl -s 127.0.0.1:3000/api/health（grafana）
#    真跑一条后：Langfuse Cloud 看 trace/generation；Grafana 走 ssh -L 3000:127.0.0.1:3000 lcode-4c
```

- **网络拓扑**：双服务均 `network_mode: host`——gpu_router 要直连宿主回环上的隧道代理
  （`127.0.0.1:17897`），video2md 与池子互访也走回环。api 绑 `127.0.0.1:8790`（无认证，
  公网经安全组拦截；外部调用走 SSH 隧道 `ssh -L 8790:127.0.0.1:8790 lcode-4c` 或加 nginx 反代）
- **Postgres checkpointer（按需，缺省 sqlite 零变化）**：`V2MD_CHECKPOINTER=postgres` +
  `V2MD_PG_DSN`（两行进 `secrets/video2md.env`）。compose 的 postgres:17-alpine 挂
  `profiles:["pg"]` 默认**不起**（外部托管 PG 只给 DSN 同入口）：
  ```bash
  echo 'V2MD_CHECKPOINTER=postgres' >> secrets/video2md.env
  echo 'V2MD_PG_DSN=postgresql://v2md:***@127.0.0.1:5432/v2md' >> secrets/video2md.env
  docker compose --profile pg up -d postgres   # PG 未就绪时 api fail-fast + restart 自拉起
  ```
  依赖变更走上面的 patch 层实操（uv sync 挂死教训）：
  ```dockerfile
  COPY pyproject.toml uv.lock /app/
  RUN PIP_INDEX_URL=… uv pip install --python /app/.venv/bin/python \
      langgraph-checkpoint-postgres==3.1.2 "psycopg[binary,pool]==3.3.6"
  ```
  checkpoint 是易失数据（done 即 `delete_thread` 裁剪），**无存量迁移**。
- **deliver 形态激活（按需，缺省 vault 零变化）**：`V2MD_ARTIFACT_MODE=deliver` +
  可选 `V2MD_OUTBOX_DIR`（容器指 data 卷）/`V2MD_OUTBOX_TTL_H`（默认 24）。
  产物不落 vault、runs.json 不养正文，bundle 拉取 + ACK/TTL 双路即焚（契约见
  [HTTP 后端契约](#http-后端契约多用户产品--phase-12)节）
- **国内站风控**：机房 IP 直连 B站 秒 412，故 `V2MD_PROXY + V2MD_PROXY_DOMESTIC=1`
  让 yt-dlp 子进程走隧道出口（住宅 IP）；进程内 httpx（LLM 等）不受影响仍直连
- **YouTube**：同一隧道出口即可（`V2MD_PROXY` 对非国内站同样生效）；隧道断则境外源不可用
- **无可用 provider 时**：池子 `/status` 全灭 → 转写失败。兜底选项：给
  `gpu_router.docker.json` 的 `aliyun` provider 填国内 GPU 容器地址，或给服务器配独立代理

### Syncthing（笔记同步回本地 Obsidian）

服务器上 vault 卷 ↔ Mac 的 Obsidian vault。Syncthing 免费 P2P、二进制友好（mp4 节段）：

```bash
# 服务器：docker 跑 syncthing（挂载 vault 卷），或 apt install syncthing 后 systemd 跑
# Mac：brew install syncthing（或 syncthing-macos）
# 两侧互加设备（服务器安全组开 22000/tcp 或走 relay），共享文件夹：
#   服务器端路径 = /var/lib/docker/volumes/video2md_vault/_data  →  Mac 端 = ~/Documents/Obsidian Vault/资料库/视频笔记
# 仅需双向同步该目录；冲突策略默认即可（笔记只增不改）
```

## 用法

```bash
# 发起（同步解析建 run → 后台跑图；合集自动展开，part_items 分窗，force 全量重跑，review 开校对）
curl -X POST localhost:8790/runs -H 'Content-Type: application/json' \
  -d '{"url": "https://www.bilibili.com/video/BV…", "part_items": "1,3-5", "force": false, "review": false}'

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

GUI 前端把本服务当正式后端：契约硬化在 `video2md/models.py`（Pydantic request/response），
交互式文档 **`/docs` + `/openapi.json` 已开**（生产要关由网关负责）。图核 `run_graph` 不感知鉴权。

**鉴权（可插拔 provider，工程在顶层 [`acctkit/`](#acctkit可复用账号登录模块顶层包)，`video2md/auth/` 只是接线）**：
- `V2MD_AUTH=off`（默认）：不校验，所有无 token 请求归同一个稳定 dev uid——
  本机开发、SSH 隧道、`test_e2e.sh`、`batch_csv.py` 全靠它，行为与 Phase 1 之前一致。
- `V2MD_AUTH=google`：前端 Google 登录拿 `id_token` → 头 `Authorization: Bearer <id_token>`
  → 后端 `google-auth` 验签（JWKS/iss/exp，`audience=GOOGLE_CLIENT_ID`）取 `sub`。
  **后端不持 client secret**，只需 OAuth「Web」客户端的 client_id。
- `V2MD_AUTH_PROVIDERS` 逗号列表按序尝试（`NoCredentials` 换下一家、`AuthError` 直抛）；
  **微信开放平台扫码登录是 Phase 2 落地的第二家 provider**：前端拿临时 `code`（单次有效
  5 分钟）→ 头 `Authorization: Wechat <code>` → 后端服务端换 `openid`/`unionid`
  （`WECHAT_APP_ID`/`WECHAT_APP_SECRET` 进凭证文件）。两家 scheme 各认各头，互不干扰；
  `current_identity` 与 run 隔离层零改。
- 身份表 `users.json`（`V2MD_USERS_FILE`，容器指 data 卷）：`(provider, external_id) → uid`
  首登自动开户；**run 按内部 uid 归属**（`run["owner"]`），他人 run 的
  `/runs/{id}`、`/note`、`/events` 一律 403，不存在才 404；dedup 也按 owner 隔离
  （不同用户同链接各建各的 run）。**Google+微信账号联动已落地**：微信 `external_id`
  取 unionid 优先（openid 兜底），`put_identity("wechat", <unionid>, <同一 uid>)` 把微信
  身份归并到已存在的 Google uid，两家登录进同一个账号（见 `test_acctkit` 双 provider 归并测）。
- CORS：`V2MD_CORS_ORIGINS` 逗号列表放行 GUI origin（空=不挂中间件）。
  生产绑公网务必 `V2MD_AUTH=google` + 反代 TLS（Google 回调需 https）。
- `/healthz` `/queue` `/preflight` **保持不鉴权**（docker healthcheck / gpu_router 看门狗在打）。

**进度双通道**：轮询 `GET /runs/{id}` 保留原形状；新增 SSE
`GET /runs/{run_id}/events`（text/event-stream，同样过 owner 校验）——
连上先推 `{"event":"connected","status","url"}` 快照，之后每条 `data:` 即
`logs/run-*.jsonl` 的事件记录（含 `graph:*` 图级事件），15s 无事件发 `: ping`，
`graph:end`/终态即关流。进程内总线扇出（`video2md/events.py`），多 worker 前的
Redis pub/sub 留 Phase 2。

**错误契约**：非 2xx 统一 `{"error": {"code", "message", "details"}}`
（`AUTH_REQUIRED`/`FORBIDDEN`/`NOT_FOUND`/`VALIDATION`…；422 带定位信息）。

**产物形态双模式（`V2MD_ARTIFACT_MODE`，缺省 vault=现状零变化）**：

| | `vault`（开发机默认） | `deliver`（多用户「云端不储存」） |
|---|---|---|
| 落点 | 直写 Obsidian vault（Syncthing 回本地） | per-run outbox → finalize 搬到在架位 `V2MD_OUTBOX_DIR/<run_id>/` |
| runs.json | 养 `md` 全文（`/note` 的原语义） | **剥正文**——`/note` 从在架文件现读 |
| 生命周期 | 永久（vault 归用户） | TTL（`V2MD_OUTBOX_TTL_H`，默认 24h）到期惰性焚 + 周期 sweep + 启动兜底；**ACK 即焚不等 TTL** |
| 幂等跳过 | 同 URL 处理过即跳（旧语义） | 仅当**同 owner 且源产物还在架**才跳（否则老实重烧——产物在别人手里，跳过=给个拿不到东西的空条目）；命中时响应条目带 `from_run` 指旧 run |

deliver 新增端点（都过 owner 闸：越权 403 / 无 run 404 / 非 deliver 模式 409）：
- `GET /runs/{id}/bundle` — 在架产物整包 zip（笔记+转录+帧+节段+`索引-append.md`，
  wikilink 相对结构原样，客户端解进本机 vault 即成品）；现打包发送、发完即删，云端不留副本。
  已焚/过期 → **410 Gone**（提示重发 URL 生成新 run）。
- `DELETE /runs/{id}/artifacts` — ACK 即焚：客户端确认收完，目录**物理销毁**（rmtree），
  返回 `{"run_id","status":"burned"}`；再删 → 410。
- `GET /runs/{id}` deliver 下附 `"artifacts": {"mode","present","expires"}` 拉取窗口；
  `/preflight` 体检项 vault→outbox 自动切换。

## acctkit：可复用账号登录模块（顶层包）

登录工程本体在 **`acctkit/`**——零 Web 框架依赖、零宿主依赖（标准库 + 可选 google-auth，
整个目录拷进别的项目即用）。对外接口只有一个：

```python
identity = await engine.authenticate(request.headers)   # Mapping[str,str] → Identity(uid,…)
```

藏在这一个方法后面的全部行为：provider 按序尝试（`NoCredentials` 换下一家 /
`AuthError` 直抛）、验出的外部身份落库成**稳定内部 uid**（首登自动开户、email/name 回填）、
同步存储 I/O 丢线程。没有 mode 标志——「不校验模式」就是列表排成 `[Fake, Dev]`，
与生产接线共用同一条按序语义。

两个可替换缝（各自两个真适配器，不是假想缝）：

| 缝 | 已落地适配器 | 挂法 |
|---|---|---|
| `AuthProvider` | `GoogleProvider(client_id)`（ID-Token 验签，不持 secret）/ `WeChatProvider(app_id, app_secret)`（扫码 code→服务端换 openid/unionid，stdlib urllib + 强制直连国内站，无新依赖）/ `DevProvider` / `FakeProvider` | 新登录方式实现 `authenticate(headers)` 后 `register_provider(name, 工厂)` 即挂，引擎/存储/隔离零改 |
| `IdentityStore` | `JsonFileStore(path)`（fcntl 跨进程锁 + tmp rename 原子替换）/ `MemoryStore`（测试） | 换 Postgres 用户表 = 同协议再写一个适配器 |

宿主接线（本项目 = `video2md/auth/`，仅二十来行）：

```python
engine = build_from_env(ENV, "V2MD_", store=JsonFileStore(USERS_FILE))  # 配置解析策略在工厂里
async def current_identity(request: Request) -> Identity:               # 唯一框架适配器
    return await engine.authenticate(request.headers)                   # 注解务必真 import Request，
                                                                        # TYPE_CHECKING 字符串注解会被 FastAPI 当 query 参数
```

`build_from_env(env, prefix)` 认 `{prefix}AUTH` / `{prefix}AUTH_PROVIDERS` /
`{prefix}USERS_FILE` / `GOOGLE_CLIENT_ID` 四个键（工厂注册表另有 `WECHAT_APP_ID` /
`WECHAT_APP_SECRET`），别的项目换前缀即复用同一套配置语义。
`AuthError.status` 只是建议码——HTTP 响应形状（错误包络、401/403 转译）永远是宿主的。

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

三层，各司其职（2026-09-22 定稿）：

```
video2md（OTel SDK：traces + metrics）
     │ OTLP HTTP :4318
     ▼
otel-collector（memory_limiter → batch）
     ├─ traces 管道 → otlphttp（HTTPS_PROXY 出墙）→ Langfuse Cloud
     └─ metrics 管道 → prometheus exporter :8889 → prometheus → grafana
evals 旁路：scripts/eval_notes.py → Langfuse scores（SDK 直挂，黑洞自动降级 OTLP）
死人开关：run 终态 → V2MD_DEADMAN_PING_URL（healthchecks.io 兼容）
```

- **追踪范围（2026-09-22 定）：Langfuse 只收生产**。生产遥测走服务器 collector；
  本机开发不配 OTEL/LANGFUSE 端点（`~/.config/video2md.env` 只留 LLM 三键），保持
  纯本地 jsonl——生产 trace/成本/评测不被开发流量稀释。本机时代的历史（233 个 run，
  含 Dify 时代的真实业务 run）已一次性回放进 Langfuse，root 属性 `replayed=true` 可
  过滤；按需再回放：`LANGFUSE_PK=… LANGFUSE_SK=… uv run python scripts/otel_replay.py
  [--run-id …]`（一次性操作，重复跑产生重复 span；离线测试 `test_otel_replay.py`）。
  ⚠️ 落库核验陷阱：2026-09-16 后新建的 Langfuse 组织 legacy 读 API（/api/public/traces）
  404、v2 observations 读路径不可靠（数据短暂可见后查空）——核验以网页 UI 为准，
  摄入本身正常（回放与生产 trace 均已在 UI 验证）。
- **本地真相**：`logs/run-YYYY-MM-DD.jsonl`——span started/completed 事件对（悬空的
  started 即崩溃点），日志层自动脱敏；`run → stage → entry → llm:*` 逐级 span 树。
  观测栈全挂也不影响主管线（jsonl 先落盘，export 异常静默）。
- **图编排层插桩（2026-09-22）**：`graph_obs.GraphObsHandler` 挂在 `run_graph` 的
  `config["callbacks"]` 上（LangGraph 官方可观测通道，节点/路由零逐点改码），
  `graph:*` 事件进同一条确定性 trace——`graph:node`（节点+superstep）/`graph:state`
  （state 增量键名+entry_idx 游标+status 标量，大 dict 不进事件）/`graph:route`
  （条件路由决策串：ok/fail/loop/done，即「走哪条边」）/`graph:start|end`。
  Langfuse 侧为零时长点事件（circuit_breaker 同款纪律）；handler 鸭子类型零 import
  langchain_core（它是 langgraph 硬依赖，但运行时代码不直接引用）；langgraph 1.2.12
  字段口径由 `test_graph_obs.py` 钉住，升级漂移先红。子图就绪：将来
  `add_node(编译子图)` 自动覆盖（checkpoint_ns 以 `|` 分层）。附带修复：每节点开始把
  graph RunLog 注册进 `_STAGE_LOGS`，select_frames_llm 等非阶段节点内的
  `llm:select_frames` span 从此不缺。`langgraph dev`（Studio）走 LangGraph Server
  事件流不经过 run_graph，与此互不干扰。
- **trace/LLM 成本（Langfuse Cloud）**：每 run 一条 trace，traceId =
  `sha256("video2md-"+run_id)`（确定性，evals 分数据此回挂；2026-09-22 前为
  `serve-` 前缀，Langfuse 里 233 条历史回放 trace 仍用旧前缀）；LLM 调用渲染为 generation
  （gen_ai semconv：model/usage/TTFT + **prompt/completion 全文**经 `_otel` 旁路上云，
  不落 jsonl/SSE；模型定价在 Langfuse 项目侧配）。trace 名常量 `video2md-run`
  （URL 进 root input——名字当 API 保低基数，按官方 best-practices）；`userId`=
  run 归属 uid（全 span 传播，可按用户过滤/归因成本）；tags=`video2md`/`mode:vault|deliver`；
  environment 取 `V2MD_ENV`（缺省 development，生产 compose 侧应设 production）。
  熔断与部分失败是 trace 上的即时事件（circuit_breaker/partial_failure），
  阶段产物计数（帧数/转录字数/OCR 命中/节段数/note 字节）挂在各条目 span 下。
  **开发/生产分项目**：Langfuse 里给开发机单开 project，密钥只进 `~/.config/video2md.env`，
  两边数据天然分家；新组织（2026-09-16 后建）legacy traces API 已禁用，程序查询走
  `GET /api/public/v2/observations?fromStartTime=…&fields=core,basic,io,model,usage,trace_context`。
  设计依据存档：`docs/research/2026-09-22-otel-critique-review.md`——traceId 为何用
  sha256 约定 + OTel SpanContext 锚（合法 SDK 机制，四组件免 Langfuse SDK 对齐）
  而非 `create_trace_id`；`/fail` 是 healthchecks.io 标准告警路径；RAGAS 保留决策。
- **指标（Grafana）**：起观测栈后 `ssh -L 3000:127.0.0.1:3000 lcode-4c` →
  http://127.0.0.1:3000 看预置看板：run 终态速率、阶段耗时、LLM token/调用量、
  熔断计数、队列深度。指标进程内存态，重启清零（运行态非账本）。
- **死人开关**：配 `V2MD_DEADMAN_PING_URL` 后 run 开始 ping `/start`、done/partial
  ping 原 URL、error ping `/fail`——「任务压根没跑/挂死」只有外部监控看得见。
- **evals 旁路**：`scripts/eval_notes.py` 对 state.json 已完成条目批量打分
  （DeepEval G-Eval/Faithfulness/Summarization + RAGAS faithfulness；judge 走自有
  LLM 端点，可用 `V2MD_JUDGE_MODEL` 单独换模型），分数回写 Langfuse 同 trace，
  本地落 `docs/evals/baseline-<date>.json` 回归基线（`--compare` 对比旧基线，
  均值跌 >0.05 退出码 2）。judge 输出走括号配平强解析 + 一次「只输出 JSON」重问
  （中转回包不规范不再拖掉 Faithfulness）；四指标线程级并发 + scores 整批一次核验
  （单篇约 3 分钟，不再 9 分钟+）。需 embedding 的 RAGAS 指标默认跳过（端点无
  embeddings 时）。**在线模式**：secrets 里开 `V2MD_EVAL=1`，graph 在 note_entry 后
  插 eval_entry 节点，逐条目写盘即评（失败只留痕不影响 run；缺省不接线）。
  服务器容器内跑（baseline 落到持久卷）：`docker exec video2md-video2md-1 \
  python -m video2md.evals --limit 5 --baseline /app/data/evals/baseline-$(date +%F).json`
- **起停观测栈**：`docker compose -f docker-compose.yml -f docker-compose.obs.yml up -d`
  （凭证 secrets/obs.env，见 docker-compose.obs.yml 头注释；未起栈时 app 回退
  LANGFUSE 直连，再退纯本地 jsonl）。
- 图调试：`langgraph dev`（本地开发，Studio UI 看 checkpoint/单步）

## 测试与 CI 门禁

[![CI](https://github.com/mintao1/video2md/actions/workflows/ci.yml/badge.svg)](https://github.com/mintao1/video2md/actions/workflows/ci.yml)

push/PR 自动跑三个 job（`.github/workflows/ci.yml`）：**test**（pytest 契约 + 8 个脚本式测试 + `scripts/smoke_ci.sh` 进程级 mockpool 全链冒烟，全离线零 token）、
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
- **隐私**：视频本体不出本机/本服务器；转录文本经 LLM 中转（`LLM_BASE_URL` 指向的服务）

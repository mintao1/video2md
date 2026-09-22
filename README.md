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

**不缓存策略**：中间产物全在系统临时目录，跑完必删；唯一持久落盘 = vault 笔记 +
`state.json`（幂等记录）+ `runs.json`（run 记录）+ `data/checkpoints.sqlite`（图 checkpoint）。

## 运行拓扑

| | Mac 本地（开发） | lcode-4c（生产，4C/7.5G） |
|---|---|---|
| 入口 | `uv run python -m video2md api`（或 `graph-run` CLI） | docker compose 双服务 |
| OCR | macOS Vision（`uv sync --extra mac`）或远程池 | 远程池（Linux 无 Vision） |
| 转写/剪辑 | 本地 whisper/ffmpeg 或远程池 | 远程池（镜像不含 whisper/torch） |
| 代理 | Clash 等本地出口 | `V2MD_PROXY=http://127.0.0.1:17897`（Mac→服务器 SSH 隧道转发，`com.singtrain.proxytunnel`） |
| 笔记落地 | 本地 Obsidian vault | `vault` docker 卷 → Syncthing 同步回 Mac |

## 部署 lcode-4c

前提：服务器 Docker + Compose；两仓库同级放 `~/stack/{dify-video2md,gpu_router}`
（别处则设 `GPU_ROUTER_DIR`）。全部命令在 Mac 上执行：

```bash
# 1. 同步代码（secrets 被排除，单独放）
rsync -az --delete --exclude '.venv' --exclude 'logs' --exclude '__pycache__' \
  --exclude '.env' --exclude 'secrets' --exclude 'state.json*' --exclude 'runs.json*' \
  --exclude 'jobs.json' --exclude 'data' --exclude '.DS_Store' \
  ./ lcode-4c:~/stack/dify-video2md/
rsync -az --delete --exclude '.venv' --exclude 'logs' --exclude '__pycache__' \
  --exclude 'gpu_router.json' --exclude '.DS_Store' \
  ~/Desktop/开发项目/gpu_router/ lcode-4c:~/stack/gpu_router/

# 2. 凭证（0600；只放服务器，不进 git）
#    secrets/video2md.env   ← LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（本机 SSOT 是 ~/.config/video2md.env）
#                            + V2MD_DEADMAN_PING_URL（可选，死人开关）
#    secrets/gpu_router.env ← HTTP_PROXY=http://127.0.0.1:17897 等（出墙代理，SSH 隧道转发）
#    secrets/obs.env        ← LANGFUSE_BASIC / HTTPS_PROXY / GF_SECURITY_ADMIN_PASSWORD（观测栈，可选）
#    secrets/colab-cli/token.json（可选；缺了 colab provider 自动冷却）
scp ~/.config/video2md.env lcode-4c:~/stack/dify-video2md/secrets/video2md.env

# 3. 构建（x86 原生构建；PIP_INDEX_URL 走清华镜像，ghcr/debian 源已换国内镜像）
ssh lcode-4c 'cd ~/stack/dify-video2md && \
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  docker compose -f docker-compose.yml -f docker-compose.obs.yml build'
#    ⚠ 实测 2026-09-22：依赖变更触发的全量 `uv sync` 在该机可能 0 CPU/0 socket 挂死
#    （两次复现；小范围 uv pip install 正常）。挂死就杀掉重试 `--progress=plain`；
#    纯代码改动可用补丁层秒级绕过：
#      printf 'FROM video2md:latest\nCOPY video2md/ /app/video2md/\n' > /tmp/patch.Dockerfile
#      docker build -t video2md:latest -f /tmp/patch.Dockerfile . && docker compose ... up -d

# 4. 起栈（含观测栈：collector/prometheus/grafana）+ 体检
ssh lcode-4c 'cd ~/stack/dify-video2md && \
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
curl localhost:8790/checkpoint             # 对账快照（算力池/临时目录残留）

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

- **本地真相**：`logs/run-YYYY-MM-DD.jsonl`——span started/completed 事件对（悬空的
  started 即崩溃点），日志层自动脱敏；`run → stage → entry → llm:*` 逐级 span 树。
  观测栈全挂也不影响主管线（jsonl 先落盘，export 异常静默）。
- **trace/LLM 成本（Langfuse Cloud）**：每 run 一条 trace，traceId =
  `sha256("serve-"+run_id)`（确定性，evals 分数据此回挂）；LLM 调用渲染为 generation
  （gen_ai semconv：model/input/output tokens/TTFT/重试次数；模型定价在 Langfuse
  项目侧配）。熔断与部分失败是 trace 上的即时事件（circuit_breaker/partial_failure），
  阶段产物计数（帧数/转录字数/OCR 命中/节段数/note 字节）挂在各条目 span 下。
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

## 测试

```bash
python3 test_llm.py             # LLM 客户端：模板渲染/重试纪律/流式收集/usage 捕获/选帧解析（MockTransport 离线）
python3 test_graph.py           # 图编排：节点序/失败路由/幂等短路/兜底/熔断/partial/checkpoint 裁剪（全打桩离线）
python3 test_resolve.py         # URL 解析/条目展开/幂等键（离线）
python3 test_remote_clients.py  # 算力池薄客户端 HTTP 契约（MockTransport 离线）
python3 test_observability.py   # OTel 桥：span 树/gen_ai 属性/traceId 确定性/指标仪表/deadman（InMemory 离线）
python3 test_evals.py           # evals 旁路：指标编排/scores 三运输路径/回归对比（假 deepeval/ragas/langfuse）
./test_e2e.sh                   # 端到端：合成视频 → 自起 api（假 LLM）→ 全断言（本地 whisper，不烧 token）
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

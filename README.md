# dify-video2md

视频 → Obsidian 文稿式笔记 + Dify 知识库 RAG。本地转写/抽帧 OCR，Dify 基座（本地 1.17.1 / Cloud 皆可）做 LLM 全文改写与问答。

**管线已阶段化**：下载/转录/抽帧/OCR/选帧/剪节段/写盘/入库各是独立阶段（serve（`python -m video2md serve`，8790）的独立端点 + Dify 工作流里的独立节点），画布可见、每阶段可单独重跑；**「哪帧配哪段文字」的选帧决策由工作流 LLM 对照转录本内容做出**，本地内容评分（OCR 文字 + 语音段对齐）作离线兜底。
**代码已包化**：`video2md/` 包按职责切 12 模块（config/util/resolve/download/transcribe/frames/clips/note/kb/runs/serve/__main__），只保留 workflow 可达路径（本地 CLI 直跑链路已于 2026-09-20 删除）。

```
Dify 工作流「视频转文档」（画布节点编排）         本地 serve（python -m video2md serve，端口 8790）
┌────────────────────────────────┐
│ POST /runs → loop 轮询          │──► 建 run：resolve URL → entries（分P/合集/播放列表展开）
│ POST download → loop            │──► 下载当前条目 + ffprobe
│ POST transcribe → loop          │──► whisper 段级时间戳
│ POST frames → loop              │──► 候选帧池（均匀网格 ∪ 场景切变；Vision 3× / Gemini 1×）
│ POST ocr → loop                 │──► 候选 OCR → 候选清单（帧名/时间戳/OCR 文本/置信）
│ GET stages/transcript+candidates│──► 双通道转录本 + 候选清单（喂工作流 LLM）
│ ★LLM 内容选帧节点               │     对照转录本内容 → JSON 选中帧（每章 1–2 张 + 图注）
│ POST select → loop              │──► 发布选中帧入 vault attachments/ + 精选清单
│ GET stages/selected             │
│ LLM 整理正文（只准嵌选中帧）     │
│ LLM 元数据 / 校对               │
│ POST note → loop                │──► 写盘：正文/转录本/索引（sanitize 防幻觉图链）
│ POST kb → loop                  │──► 知识库上传 + 检索自检
└────────────────────────────────┘
```

候选池默认本地；**转录/剪节段/OCR 三类算力负载都可外包给 GPU 算力池**（`V2MD_REMOTE`/`V2MD_CLIPS_REMOTE`/`V2MD_OCR_REMOTE` 指向 `http://127.0.0.1:9700`，池内 Colab/Kaggle/Modal/ZeroGPU 动态选举：源片上传/直连 CDN → T4 级 GPU 上 faster-whisper/ffmpeg-nvenc/RapidOCR → 结果拉回；整体失败**不**回退本地 CPU——实测 CPU 重活里 clips 占 97min/总时长最大头，见「阶段化管线」）；Dify 侧做编排与决策。

## 笔记形态（对齐「资料库/3Blue1Brown-神经网络」参考样式）

```
资料库/视频笔记/
  <标题>.md                        正文 = 转录本的完整文稿改写（非要点摘抄）
  transcripts/<标题>-transcript.txt  转录本 SSOT，正文 [!info] callout 内 wikilink
  attachments/<slug>-fNN-<mmss>.jpg  嵌入帧，工作流 LLM 对照转录本内容选图（本地评分兜底）
  attachments/<slug>-cNN-<mmss>.mp4  视频节段：帧 ±10s ∪ 所在语音段的可播片段
  索引.md                          追加行登记
```

正文结构：富 frontmatter（`lang/date/duration/transcribe_*/frames_*/llm_tokens` + tags）
→ `> [!info] 视频信息`（来源链接/UP主、字幕文稿 wikilink、插图统计）
→ `> [!tip]- 章节导航`（折叠，从 `## 章节（mm:ss）` 标题机械提取）
→ 文稿正文：按视频叙事顺序的连贯文章，`##` 章节（标题带时间戳锚点）、关键句加粗、
`![[attachments/…jpg]]` + `![[attachments/…mp4]]` 配斜体图注、`[^n]` 脚注、
`> [!example] 思考题` 等 callout。
嵌入文件名经本地校验（清单外引用自动剥离），防 LLM 幻觉图链；
节段从原视频机械剪取（±10s ∪ 所在语音段、H.264+aac、faststart），与帧文件名一一配对。

**不缓存策略**：中间产物全在系统临时目录，跑完必删；唯一持久落盘 = vault 笔记 + `state.json`（幂等记录）。

## 阶段化管线（serve + 工作流编排）

### 本地 serve 的阶段端点

启动：launchd `com.linmintao.video2md-server`（`uv run python -m video2md serve`，8790；Dify 容器内经 `host.docker.internal:8790` 访问）。每个阶段是一个幂等端点：同一 run 的同一阶段已有 queued/running/done 的 job 时复用（响应 `dedup: true` + 当前状态快照，多条匹配取最新），**失败回带 error 快照**（工作流 loop 看到即停），显式 `{"retry": true}` 才重跑。

**前置门控（409 PRECONDITION）**：阶段有硬依赖（transcribe/frames←download，ocr←frames，select←ocr+transcribe，clips/note←select，kb←note 已入队），前置未 done 拒绝建 job；`GET /stages/*` 同样门控（产出阶段未 done → 409，空清单不喂 LLM）。`{"force": true}` 显式绕过。恢复流程：修根因 → `POST …/stage {"retry": true}` 一次 → 重新发起工作流（dedup 取最新 job，旧 error 档不挡路）。

**预检 `GET /preflight`**：ffmpeg/磁盘/vault/算力池可达性（池内有无可用 provider）逐项体检，`ok:false` 即环境问题，工作流第 ⓪ 节点调它，不过即终止——不建 run、不烧条目。

**阶段熔断**：`_run_stage` 按错误签名（抹掉 URL/路径/随机串/数字）计数，全局型错误（认证/网络/风控）连错 2 条、其余连错 `V2MD_CB_THRESHOLD`（默认 3）条即终止整个阶段——同一根因不让每条目各白烧一遍。

| 端点 | 作用 | 产物字段 |
|---|---|---|
| `GET /preflight` | 环境体检（代理/token/磁盘/ffmpeg） | `{ok, checks}` |
| `POST /runs` `{url, part_items?}` | 建 run：resolve 展开条目（分P/合集/列表） | `run_id, total` |
| `POST /runs/{id}/download` | 下载当前条目 + ffprobe | `video` |
| `POST /runs/{id}/transcribe` `{model?}` | whisper 段级时间戳 | `segs` |
| `POST /runs/{id}/frames` `{max_frames?}` | 候选帧池（网格∪场景切变） | `candidates` |
| `POST /runs/{id}/ocr` `{ocr?}` | 候选 OCR → 候选清单 | `ocr_raw, cand_manifest` |
| `GET /runs/{id}/stages/transcript\|candidates\|selected` | 产物文本（喂工作流 LLM 节点） | `{text, entries}` |
| `POST /runs/{id}/select` `{frames:[{name,caption}]}` | 发布选中帧入 vault + 精选清单 | `published, manifest` |
| `POST /runs/{id}/clips` | 剪节段（吸附语音段） | `clips` |
| `POST /runs/{id}/note` `{body, meta}` | 写盘（sanitize + 索引） | `note` |
| `POST /runs/{id}/kb` `{dry_run?}` | 知识库上传 + 检索自检 | `doc_id` |
| `GET /runs/{id}` | run 总览：各阶段 done map | — |

阶段 job = `python -m video2md __stage <run_id> <stage> <args-json>` 子进程（worker 复用现有 job 队列，`/jobs/{id}` 可轮询）。`__stage` 内部对 run 内条目串行推进，关键阶段（download/transcribe/frames/ocr）全部条目失败即终止后续阶段。

### Dify 工作流节点图（`dify/视频转文档-workflow.yml`）

url 非空时走全阶段链（8 个阶段 loop，每个 loop = `POST 阶段 → 轮询 → code 解析 → assigner 写进度`，服务端对相同 run+stage 去重并回带状态）：

```
start(url, ocr?, max_frames?) → ⓪ GET /preflight（不过即终止，不建 run）→ code_fmt_run(POST /runs)
  → loop_download → if_ok 闸门 → loop_transcribe → if_ok 闸门 → GET stages/transcript
  → loop_frames → if_ok → loop_ocr → if_ok → GET stages/candidates
  → ★llm_select_frames（Kimi-K3：对照转录本 → JSON 选中帧，只准用清单内文件名）
  → code_parse_select（过滤非法帧名）→ loop_select → if_ok → loop_clips → if_ok
  → llm_organize（只准嵌精选清单帧）→ llm_meta → if_review → llm_review
  → code_agg_staged → loop_note → if_ok → loop_kb → if_ok → end
```

**防「异步入队」设计（2026-09-20 事故后加固）**：loop 只在 `status==done` 时退出为成功；
`error` / 服务级错误（404/409）/ 连续 5 次响应解析失败 → `fail_flag` 退出。每个 loop 后挂
`if_ok` 闸门：非 done 一律进 `code_fail` 抛错把工作流打成 failed——**loop 上限耗尽（Dify 硬限
100 轮）也是失败而非前进**。轮询间隔按阶段配（transcribe 55s×100≈95min、ocr 45s、clips 30s、
其余 20s；wait 节点 read 超时 = 间隔+15s，短于服务端 sleep 会整流超时）。

兼容分支保留：url 空 + transcript 空 → 查最新任务；填 transcript → 原 LLM 改写链（不重跑本地管线）。

> **布局坑（2026-09-19 修）**：loop 子节点的 `position` 是**相对容器左上角**的坐标（ReactFlow 语义），
> 不是画布绝对坐标。写绝对坐标时 Dify 导入会把它当相对偏移，容器被自动撑大到「绝对坐标+节点宽+padding」
> （实测 8 个 loop 全部撑成巨框、子节点溢出框外）。`build_workflow.py` 全程用画布绝对坐标书写，
> 落盘前由 `normalize_layout()` 统一换算（子节点改相对 + `positionAbsolute` 留绝对 + 按子节点外包络
> 回填容器 width/height）；容器内子节点间距用 Dify 标准 300（NODE_WIDTH 240 + X_OFFSET 60），
> 小于节点宽 244 会互相叠。普通（非容器）节点的框大小由前端 `w-fit` 决定，DSL 的 width/height 只是布局提示。

### 帧选择与节段对齐（「讲什么配什么图」怎么落）

- **候选池**：均匀网格 2/3 ∪ 场景切变 1/3（内容切换处增密），极近帧去重 `FRAME_DEDUP_EPS=0.4s`，模糊门禁保留最清晰 5 张；总量 ≤ `max_frames`（默认 60）。池大小：Vision = `min(120, max_frames×3)`，Gemini = `max_frames`（付费不增量）；env `V2MD_FRAME_POOL` 覆盖。
- **工作流选帧（主路径）**：LLM 节点输入双通道转录本 + 候选清单（每行 `帧名 [mm:ss] OCR: …`），输出 `{"frames":[{name, caption}]}`——只准用清单内文件名、每章 1–2 张、优先画面文字与语音话题一致的帧。code 节点过滤清单外名字（防幻觉帧名）。
- **本地选帧（CLI 兜底）** `select_content_frames`：OCR 文字（置信≥0.5）= 主信号，评分 = 有文字(3) + 文字量(≤1) + 语音段对齐(≤1) + 清晰度(≤0.5)；按 whisper 段分桶 → 每话题至少 1 帧；跨桶 MAE 去重（同一幻灯片跨段只留一张）；无文字清晰帧按评分补位。
- **节段吸附**：剪辑窗口 = ±10s ∪ 帧所在 whisper 语音段 ±1s（clamp 到 ±20s；<4s 回退 ±10s）——节段跟「这段话在讲什么」走，不是随机取窗。
- 帧命名 `<slug>-fNN-<mmss>.jpg`（时间戳可溯源），节段与帧一一配对（`-f`→`-c`、`.jpg`→`.mp4`）。

## 前置条件

- macOS（OCR 依赖 Vision 框架）、ffmpeg/ffprobe、whisper（均已安装）
- [uv](https://docs.astral.sh/uv/)

## 安装

```bash
cd ~/ZCodeProject/dify-video2md
uv sync
cp .env.example .env   # 稍后填 key
```

## Dify 配置（本地基座，约 10 分钟）

基座：`~/dify/docker`（Dify 1.17.1，nginx → `http://localhost:8090`），控制台浏览器打开即可。

### 1. 配模型供应商（右上角头像 → 设置 → 模型供应商）

- 安装 **OpenAI-API-compatible** → 添加模型：
  - API Key、API endpoint URL、模型名（模型类型选 LLM）
  - ⚠ 基座跑在 Docker 里，**容器内 127.0.0.1 不是宿主机**——本机中转要填 `http://host.docker.internal:端口/v1`（已验证容器内可解析）
  - 模型上下文长度填 `128000`（影响长转录本）
- 安装 **SiliconFlow** 插件（插件市场）→ 填 [siliconflow.cn](https://siliconflow.cn) 的 key（bge-m3 embedding 免费）
- 右上角 **系统模型设置** → Embedding 模型选 `BAAI/bge-m3`

### 2. 导入 Workflow

工作室 → 创建应用 → **导入 DSL 文件** → 选 `dify/视频转文档-workflow.yml`

> 导入后编排画布里 3 个 LLM 节点会标红（模型未找到）——逐个点开，把模型换成你第 1 步配置的模型。

右上角 **发布** → 左侧 **API 访问**（右上角）→ 创建 **API 密钥** → 得 `app-…`（`DIFY_API_KEY`）

### 3. 建知识库

知识库页 → **创建知识库** → 命名「视频知识库」（先不用上传文档，脚本会推）→ 创建后从 **URL 末段**复制 dataset id（`DIFY_DATASET_ID`）

知识库列表页右上角 **API** → **API 密钥** → 创建 → 得 `dataset-…`（`DIFY_DATASET_API_KEY`）

### 4. 凭证（dify_kit 基座 SSOT）

凭证首选写入 `~/.config/dify-zcode.env`（dify_kit 基座契约，环境变量可覆盖）：

```
DIFY_BASE_URL=http://localhost:8090/v1     # 本地基座；Dify Cloud 则 https://api.dify.ai/v1
DIFY_APP_API_KEY=app-…                     # Workflow 应用 Key
DIFY_DATASET_API_KEY=dataset-…             # 知识库 API Key（不上传可缺）
DIFY_DATASET_ID=<uuid>                     # 可选；缺省自动按名解析/创建「视频知识库」
```

项目 `.env` **只放本机非机密配置**（代理/V2MD_*）；key 一律 SSOT。`ask` 子命令已随 CLI 直跑链路一并删除（workflow 应用无 chat 端点）。

## 支持的链接与多条目展开

一条链接可以代表**多个视频**——管线先用 yt-dlp 元数据模式枚举、再逐个处理；每个条目独立笔记、转录、附件与 state 记录，单条失败不阻断其它条目。

| 链接形态 | 行为 |
|---|---|
| B站单视频 `/video/BV…` | 1 篇笔记 |
| B站**分P** `/video/BV…?p=3` | 只处理第 3 P；不带 `p=` 的多P默认展开全部 |
| B站**合集/系列/播放列表** `space.bilibili.com/<uid>/lists/<sid>`、medialist、收藏夹 | 展开全部条目；会员/私有集需要 Chrome cookies |
| YouTube 单视频 `watch?v=…` / `youtu.be/…` | 1 篇笔记 |
| YouTube **播放列表** `watch?list=…`、`/playlist?list=…`、频道 | 展开全部条目 |
| **抖音** `douyin.com/video/<id>` | 单视频；通常需要 Chrome 新鲜 cookies（`s_v_web_id`），失败会明确提示 |
| **抖音博主主页** `douyin.com/user/<sec_uid>` | 枚举全部作品（视频+**图文**）：CDP 驱动本机 Chrome（9222 调试端口，需已登录抖音）滚动拦截 post 接口，一次拿全量元数据；图文帖走图集管线（下载图集→OCR 合成转录本→照常出笔记） |
| **小红书** `xiaohongshu.com/explore/<id>`、`/discovery/item/<id>` | 短视频单条；图文笔记/反爬会明确报错 |

在 Dify「视频转文档」应用里：`url` 填链接（合集/播放列表自动展开全部条目）；
`part_items` 填 `1,3-5` 可只跑指定集；`force` 填 `on` 忽略 state 幂等全量重跑。

- 短链 `v.douyin.com`、`xhslink.com` 会先跟随重定向再解析
- 多条目附件名带 `p003-BV…` 等稳定前缀，不互相覆盖；state 按归一化条目 URL（含 `?p=N`）幂等，`force=on` 强跑
- Dify「视频转文档」应用的 `url` 字段走同一条异步入口；另填 `part_items`（如 `1,3-5`）可筛选
- 作业服务 `GET /jobs/{id}` 返回 `total/completed/failed/current` 与最近日志，支持服务重启后恢复排队任务

## GPU 算力池（统一出口，2026-09-21 起 Colab 直联已抽离）

本地 whisper/CPU 剪辑是最慢一环（15 分钟视频 CPU 转写十几分钟到一小时；clips 重剪 97min/条）。
所有 GPU 算力负载——**转录 / 剪节段 / OCR**——统一走独立项目
**[~/Desktop/开发项目/gpu_router](../..)**（统一算力池，`127.0.0.1:9700`）：
池内按「能力优先级 + 配额水位 + 延迟 EMA + 分类熔断冷却 + 手动开关」在 Colab / Modal /
Kaggle / HF ZeroGPU / 国内云之间动态选举，一家没卡自动换下一家，
**绝不静默回退本地 CPU**。本仓只保留薄客户端——与池子之间只有 HTTP 契约，
下载/截帧/选帧/入库仍在本机，池子碰不到 localhost 的 Dify 与 vault 之外的资产。

```bash
cd ~/Desktop/开发项目/gpu_router && .venv/bin/python -m gpu_router   # 起池子（配 gpu_router.json）
# 本仓 .env：
#   V2MD_REMOTE=http://127.0.0.1:9700         # 转录
#   V2MD_CLIPS_REMOTE=http://127.0.0.1:9700   # 剪节段
#   V2MD_OCR_REMOTE=http://127.0.0.1:9700     # OCR（不设则保持本地 Vision）
```

- **Colab 由池子全权接管**：会话编排（建/复用/用完即关）、token 体检、闲置看门狗都在池子进程内；
  池子配置里 `extra.lease_file`/`extra.queue_url` 指回本仓即恢复「队列保温 + serve 让路」语义
- **等卡哨兵**：`scripts/wait_pool.sh`——探池子 `/status` 任一 asr provider 可用即起批次
  （取代旧 colab_watch.sh 的 Colab 单点探测）
- 平台接入步骤、熔断时长依据（官方+论坛调研）、算法路由细节见池子项目的
  `docs/gpu-router.md` 与 README

### 并行与提速（实测）

| 阶段 | 算法 | 效果 |
|------|------|------|
| 转录 | 池子 T4 级 GPU + faster-whisper 批处理推理 | 16min 音频 ≈ 51s（本地 CPU 13min+） |
| 转录 ∥ 截帧+OCR | 两阶段互不依赖 → 线程池并发 | 省 ~30–40s/条 |
| 长文改写 | workflow loop_note **逐条目**改写（合集一次写 N 篇 → 每篇独立 LLM 调用） | 杜绝 N 篇塞一次调用的截断/错配 |
| 剪节段 | 池子 GPU ffmpeg（nvenc 实测探测）并行切 | 45 帧逐条 260s → ~65s |
| OCR | 帧级线程池并发（6 路）/ 池子 RapidOCR 批量 | 45 帧 ~30s |

其他可选算法（按收益/复杂度排）：远端 whisper 常驻 worker（再省每次模型加载 20–40s）、
yt-dlp `--concurrent-fragments` 并行分片下载、帧选择两遍法（先低价判有无文字再全量 OCR）、
合并后一次「缝合 pass」统一跨段文风。

## Dify UI 入口（就在「视频转文档」应用本体上）

不需要额外应用：**打开 Dify 工作室 → 视频转文档**（或 WebApp `http://localhost:8090/run/Ep0bSdJWNnGMlqKv`），按输入框分流：

| 怎么填 | 走什么 |
|--------|--------|
| 只填 `url`（B站/YouTube 链接） | 全阶段链：`POST /runs` → 每阶段一个 loop（`POST 阶段 → 轮询 → code 解析 → assigner 写进度`），中间插入 **LLM 内容选帧节点**（对照转录本从候选清单选帧）→ 写盘 → 入库；节点面板逐轮显示 `🔄 阶段 ocr · 已用 178s` / `📝 笔记路径` / `🔎 检索自检 PASS`，跑完返回最终状态。各阶段 loop 可单独中断重跑 |
| 全都不填直接运行 | 查最新任务状态（含日志尾 / 产物 / 自检结果） |
| 填 `transcript` 等（管线内部调用） | 原 LLM 改写链，输出 body + meta |

- **轮询怎么实现的**：循环节点里 `GET /wait?seconds=N`（延时跑在本地服务上，**不占 Dify sandbox**——sandbox code 节点里 sleep 会被 15s worker 上限杀掉）→ `POST /runs/{id}/{stage}`（幂等去重回带快照）→ code 解析 → assigner 写入 loop 变量 → 中断条件命中即结束
- 覆盖时长：每阶段 `loop_count 100 × 间隔`（transcribe 55s≈95min / ocr 45s / clips 30s / 其余 20s）；工作流单 run 步数上限已在服务端调到 `WORKFLOW_MAX_EXECUTION_STEPS=3000`（默认 500 会被长合集撞穿）
- 想看长任务的实时进度就保持这个 run 打开；也可以随时 `curl localhost:8790/checkpoint`
- 同链接已在排队/处理中会**去重复用任务**；已处理完的链接按归一化 URL 幂等直接跳过（`force` 填 `on` 重跑）——重复提交不会产生第二份笔记
- 管线**只有 workflow 一条入口**（本地 CLI 直跑已删）；排障用 `python -m video2md runs [run_id]`

## 可观测性

每次运行产生 `run_id`，span 采用 started/completed 事件对（借鉴 ZCode CLI 遥测：
日志中悬空的 started 即崩溃点）+ `spanId` 配对 + ISO 时间戳，落 `logs/run-YYYY-MM-DD.jsonl`
（`durationMs/status/attempt` 字段，日志层自动脱敏 key）；`state.json` 记录每视频的
spans 与状态；笔记 frontmatter 记 `llm_tokens`（LLM token 用量）。
Dify 调用失败按指数退避重试（2/4/8/16s，最多 5 次），4xx 不重试。

作业服务侧：

| 端点 | 用途 |
|------|------|
| `GET /checkpoint` | 一次快照：队列长度、各状态计数、最近任务、算力池快照、临时目录残留 |
| `GET /jobs/{id}` | 单任务：`status/total/completed/failed/current/stage/progress/note/selfcheck/partial_fail` + 日志尾 |
| `GET /queue` | 真实队列计数 `{queue_len, running}`——算力池 colab provider 据此判队列保温（`/jobs` 只回最近 10 条，队列深会漏看） |
| `GET /wait?seconds=N` | 给工作流循环用的服务端延时（≤60s），避免占用 sandbox |
| `GET /runs/{id}` | run 总览：条目、各阶段 done map（阶段化排障用） |
| `logs/serve-requests.jsonl` | 每次 `POST /jobs` 与阶段触发的审计（时间/来源/URL/job_id/阶段）——判断「链接到底有没有打到服务」 |

## 用法

管线由 Dify workflow「视频转文档」驱动（工作室画布 / WebApp / API 均可发起）：

| 输入 | 作用 |
|---|---|
| `url` | B站/YouTube/抖音/小红书链接（合集自动展开）或**本地文件路径** |
| `part_items` | 只跑指定条目，如 `1,3-5` |
| `force` | `on` = 忽略 state 幂等全量重跑 |
| `review` | `on` = 启用校对 LLM 节点（更准、更慢） |
| `max_frames` | 候选帧池上限（默认 60，Vision 池 ×3 上限 120） |

```bash
# API 发起（streaming 等 workflow_finished）
python3 scripts/rerun_failed.py        # 参考实现：批量串行 + 结果汇总

# 运维
python -m video2md runs [run_id]       # 查看 run 状态（只读排障）
curl localhost:8790/checkpoint         # serve 快照：队列/算力池/临时目录
./test_e2e.sh                          # 端到端自检（serve 阶段 API 驱动，不烧 LLM）
```

## 质量门禁

| 环节 | 机制 |
|------|------|
| 转写 | `condition_on_previous_text=False` 抑制幻觉循环；丢弃 no_speech_prob>0.6 或 compression_ratio>2.4 的段；段置信度 `exp(avg_logprob)`，加权均值落 frontmatter `transcribe_conf`，<0.5 的段标 ⚠ |
| 抽帧 | 均匀网格∪场景切变生成候选池，极近帧去重 `FRAME_DEDUP_EPS=0.4s`；中心区域边缘强度剔模糊（全剔时保底最清晰 5 张）；Vision 池大小按 `V2MD_FRAME_POOL`/`max_frames` 控制 |
| 选帧 | 工作流 LLM 对照转录本选帧（只准用候选清单内文件名，code 节点过滤非法名）；本地启发式兜底 = OCR 文字置信≥0.5 主信号 + 语音段分桶配额 + MAE 去重 |
| 节段 | 剪辑窗口吸附帧所在 whisper 语音段（±10s ∪ 段 ±1s，clamp ±20s）——节段与讲述内容对齐；帧↔节段文件名一一配对 |
| OCR | macOS Vision 置信度 <0.5 丢弃；重复画面文字去重（Gemini 路径 2026-09-20 已摘除，key 作废） |
| LLM | prompt 硬约束「只准用转录本、要点带时间戳」；输出 <100 字视为失败；元数据 JSON 解析失败自动降级为文件名标题；分块改写里**单段拒答/空输出 → 该段降级为时间线原文**（不拼进正文） |
| 写盘门禁 | **拒答/空壳绝不落盘**（`looks_like_refusal`：拒答套话，或 <60 字且无 `##` 章节）——实测有 LLM 的一句「抱歉，我无法提供相关信息」被当成正文写成笔记并入库；**转录本 <`V2MD_MIN_TRANSCRIPT_CHARS`(200) 字 → 疑似片头/片尾，跳过写盘**；缺转录本拒绝写盘 |
| 多条目 | 每条目独立子目录（`tmp/eNN`）——共享 tmp 时 `frames/`、`asr/` 的 `mkdir` 会撞 `FileExistsError`（实测 17P 合集只有条目 1 能出帧），且 `g_001.jpg` 类帧名会跨条目互相覆盖 |
| RAG | 上传后自动跑 3 条检索自检，全命中输出 `SELF-CHECK PASS` |
| 回归 | `test_e2e.sh`：say 合成已知文本 + 烧录字幕 → 断言转写/OCR/笔记/索引/临时目录清理全链路 |
| 人工 | frontmatter `reviewed: false`，Obsidian 筛查后手改 `true` |

## 测试

```bash
python3 test_jobs.py            # serve 作业编排（阶段 job/队列恢复/熔断）
python3 test_resolve.py         # URL 解析/条目展开/幂等键
python3 test_remote_clients.py  # 远端算力薄客户端（clips/ocr HTTP 契约，MockTransport 不联网）
./test_e2e.sh                   # 端到端：合成视频 → serve 阶段 API 逐阶段驱动 → 断言（本地 whisper，不烧 LLM、不入库）
# GPU 路由本体测试在 ~/Desktop/开发项目/gpu_router/tests/
```

## FAQ

- **长视频**：转录本上限 6 万字符（约 3 小时密集语音），超出请先切段分批跑
- **whisper 首跑**：会下载模型（turbo ≈1.6GB / small ≈460MB）到 `~/.cache/whisper`
- **换 LLM**：只动 Dify 控制台的供应商配置，本地脚本零改动
- **隐私**：视频本体永不离开本机；基座本地部署时转录文本也不出本机

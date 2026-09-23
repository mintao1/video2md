# video2md

视频 → Obsidian 文稿式笔记。**LangGraph 进程内编排**（2026-09-21 起 Dify 已抽离）：
下载/转写/抽帧/OCR/选帧/剪节段/写盘各是图节点，LLM（选帧/改写/校对/元数据）由
`video2md/llm.py` httpx 直连 openai-compatible 端点（默认 Kimi-K3）。
GPU 重活（转写/OCR/剪辑）外包给 [gpu_router](https://github.com/mintao1/gpu-router) 算力池（Colab/Kaggle/Modal/ZeroGPU/国内云动态选举）。

**生产形态 = 云服务器 · 多用户 API**：登录鉴权（Google id_token/设备流 + 微信扫码）→
run 按 uid 归属隔离 → deliver「云端不储存」投递（bundle 拉取 + ACK/TTL 即焚），
公网经反代 TLS 暴露。整体分层见[终局布局](#终局布局云服务器--多用户)，落地步骤见
[部署](#部署云服务器--多用户)章。

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

| | 本地开发（Mac/任意开发机） | 云服务器（生产 · 多用户） |
|---|---|---|
| 入口 | `uv run python -m video2md api`（或 `graph-run` CLI） | docker compose（api + gpu_router [+ 观测栈]）+ 反代 TLS 域名 |
| 鉴权 | `V2MD_AUTH=off`（无 token 归同一 dev uid） | `V2MD_AUTH=google`（`Authorization: Bearer <id_token>`；设备流/微信可并列） |
| OCR | macOS Vision（`uv sync --extra mac`）或远程池 | 远程池（Linux 无 Vision） |
| 转写/剪辑 | 本地 whisper/ffmpeg 或远程池 | 远程池（镜像不含 whisper/torch） |
| 出网 | Clash 等本地出口（可选） | `V2MD_PROXY` 出网代理；国内站需大陆住宅类出口（风控，见「出网与风控」） |
| 笔记落地 | vault 直写本地 Obsidian | deliver 模式：outbox 在架 → 客户端拉 bundle → ACK/TTL 即焚 |

## 部署（云服务器 · 多用户）

前提（厂商中立——任意云 VM / 自托管服务器皆可）：
- Docker + Compose（≥2C4G 起步；GPU 全在远端算力池，服务器本身不需 GPU）
- 一个域名 + TLS 证书（Google 登录回调要求 https；Caddy 可全自动签续）
- LLM 端点凭证（openai-compatible 三键）
- 至少一家 GPU provider 凭证（colab-cli token / kaggle.json / …，缺了对应 provider 自动冷却）
- Google OAuth「Web」客户端的 client_id（后端只验签不持 secret）；插件/CLI 无浏览器登录
  另备「TV 与 Limited Input」型 client（设备流，需 client_secret）
- 两仓库同级放 `~/stack/{video2md,gpu_router}`（别处则设 `GPU_ROUTER_DIR`）

**1. 代码与凭证**（secrets 0600，只放服务器，不进 git）：

```bash
# 服务器上拉两仓库（或从开发机 rsync——排除 .venv/logs/secrets/data 等本地产物）
git clone <本仓库地址> ~/stack/video2md
git clone <gpu_router 地址>  ~/stack/gpu_router

# secrets/ 目录：
#   secrets/video2md.env   ← LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#                            + 多用户生产档（见下）+ V2MD_DEADMAN_PING_URL（可选，死人开关）
#   secrets/gpu_router.env ← 出网代理 env（HTTP_PROXY 等，可选但强烈建议）
#   secrets/obs.env        ← LANGFUSE_BASIC / GF_SECURITY_ADMIN_PASSWORD（观测栈，可选）
#   secrets/colab-cli/token.json、secrets/kaggle/kaggle.json（可选）
```

`secrets/video2md.env` 多用户生产档（完整键义见 `.env.example`）：

```ini
V2MD_AUTH=google                            # 绝不 off 上公网（见下检查清单）
V2MD_AUTH_PROVIDERS=google,google_device    # 接微信扫码改 google,wechat
V2MD_ARTIFACT_MODE=deliver                  # 云端不储存：outbox + ACK/TTL 即焚
V2MD_CORS_ORIGINS=https://app.example.com   # GUI 前端 origin 白名单
V2MD_ENV=production                         # 遥测环境标签（compose 已默认）
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
# GOOGLE_CLIENT_SECRET=…     # 仅设备流需要（TV/Limited-Input 型 client）
# WECHAT_APP_ID / WECHAT_APP_SECRET=…        # 微信扫码（可选）
# V2MD_PG_DSN=postgresql://… # Postgres checkpointer（可选，缺省 sqlite）
```

**2. 构建 + 起栈 + 体检**（国内服务器可 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 加速）：

```bash
cd ~/stack/video2md
docker compose -f docker-compose.yml -f docker-compose.obs.yml build
docker compose -f docker-compose.yml -f docker-compose.obs.yml up -d
curl -s localhost:8790/healthz && curl -s localhost:8790/preflight && \
  curl -s "localhost:9700/status?refresh=true"
# 观测栈验证：curl -s 127.0.0.1:9090/-/healthy（prometheus）· curl -s 127.0.0.1:3000/api/health（grafana）
# 真跑一条后：Langfuse Cloud 看 trace/generation；Grafana 访问见「可观测性」节
```

**3. 反代 TLS（公网唯一入口）**——api 只绑服务器回环（`V2MD_HOST=127.0.0.1`），公网
流量全部经反代终结 TLS。nginx 示例（SSE 必须关缓冲，否则 `GET /runs/{id}/events`
事件流被攒包）：

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;         # SSE 必需
        proxy_read_timeout 3600s;    # bundle 大包下载 / SSE 长连接
        client_max_body_size 512m;   # 预留将来上传面
    }
}
```

Caddy 等价（自动 TLS，主体两行）：

```caddyfile
api.example.com {
    reverse_proxy 127.0.0.1:8790 { flush_interval -1 }   # flush_interval -1 = SSE 不缓冲
}
```

安全组口径：**只放行 80/443**；api(8790)、算力池(9700)、观测栈(3000/9090/4318…) 全部
只绑回环，公网不可达。

**4. 公网上线检查清单**：

| 项 | 口径 |
|---|---|
| `V2MD_AUTH` | 必须 `google`——`off` 档下测试头 `X-Test-Auth` 可假冒任意 uid，等于裸奔 |
| `V2MD_CORS_ORIGINS` | GUI 前端 origin 白名单（逗号分隔；空 = 不挂 CORS 中间件，仅同源/内网适用） |
| `V2MD_ARTIFACT_MODE` | `deliver`（多用户「云端不储存」；vault 直写是开发/自托管姿势） |
| `V2MD_ENV` | `production`（compose 已配；Langfuse 开发/生产分项目） |
| `/docs` `/openapi.json` | 生产由网关关闭（反代 deny 该路径） |
| `/healthz` `/queue` `/preflight` | 刻意不鉴权（docker healthcheck / 看门狗探针在打）；经反代后公网同样可达——介意信息暴露可在反代层限源 |

### 备份与恢复（2026-09-23）

`scripts/backup.sh`（服务器上跑，cron 每日）：打包 `v2md-data` 卷（state/runs/users/
checkpoints/logs）+ `vault` 卷（vault 模式产物——deliver 形态不用此卷）+ PG 在跑时
`pg_dump`；落盘 `backups/daily/`（默认留 7 份）+ `backups/weekly/`
（周一晋升，留 4 份）+ sha256 manifest。恢复走 `scripts/restore.sh <日期>`（先校验
manifest，覆盖前自动快照现有卷到 `backups/pre-restore-<日期>/`）。

```bash
# 服务器 crontab -e：每日 04:15 备份（logs 落 /var/log/v2md-backup.log）
15 4 * * * /home/<user>/stack/video2md/scripts/backup.sh >> /var/log/v2md-backup.log 2>&1

# 手动跑 / 恢复演练（建议每季度做一次：恢复到临时 dst 验证归档可用）
ssh <server> '~/stack/video2md/scripts/backup.sh --dst /mnt/backup/v2md'
ssh <server> '~/stack/video2md/scripts/restore.sh 2026-09-23 --only data'   # 交互确认
```

- **secrets/ 不在卷里**：`secrets/video2md.env`、`secrets/obs.env`、colab/kaggle token
  在宿主目录——手工纳入你的备份（或至少记入密码管理器）；丢了 = LLM 凭证/登录配置重配。
- **一致性口径**：runs.json/state.json 原子 rename 写，tar 视角恒完整；checkpoints.sqlite
  可丢弃（成功 run 会删 checkpoint），接受瞬时不一致；严格一致可先 `docker compose stop video2md` 再备。
- **异地容灾**：`V2MD_BACKUP_REMOTE`（rclone remote，如 `b2:v2md-backup`）配置后
  `backup.sh` 在本地备份 + manifest 校验完自动 `rclone copy` 推对象存储——只落本地盘的
  备份防不了整机损失，异地副本是数据安全面的一环（见[终局布局](#终局布局云服务器--多用户)）。

### 发布与回滚

镜像经 CI 发布 GHCR（不可变 tag），部署/回滚走 tag 驱动脚本：

```bash
# 发版：打 tag 推上去 → CI release job 构建 + push ghcr.io/mintao1/video2md
#   （tag = semver + sha-<短哈希>；镜像 LABEL 带 version/revision）
git tag v0.3.0 && git push origin v0.3.0

# 部署：目标机 checkout tag → compose build → up -d → healthz/preflight 体检
scripts/deploy.sh v0.3.0 --host user@server          # 或设 V2MD_DEPLOY_HOST

# 回滚 = 部署上一个可用 tag（体检失败时脚本非零退出并提示）
scripts/deploy.sh v0.2.0 --host user@server
```

- GHCR 镜像同时是**灾备存档**：git 不可用时可 `docker pull ghcr.io/mintao1/video2md:<tag>`
  手动顶上；`/healthz` 的 `version` 字段与镜像 LABEL 同源（pyproject 单源），核对线上
  版本不用进容器
- 日常跟进 main：`scripts/deploy.sh main --host user@server`（跟随分支，不推荐生产）

### 运行面要点

- **并发与配额**（执行层/限流配额的落地键，全进程内零外部依赖）：`V2MD_MAX_CONCURRENT_RUNS`
  （默认 1=单 worker 语义；调大前 checkpointer 建议切 postgres，vault 自托管保持 1）；
  `V2MD_QUEUE_MAX`（总队列，超 → 429 `QUEUE_FULL`）/`V2MD_USER_QUEUE_MAX`（每用户在途，
  429 `USER_QUEUE_FULL`）/`V2MD_USER_SUBMIT_PER_MIN`（60s 滑窗，429 `RATE_LIMITED`；
  三者 0=不设限）。`/queue` 响应带 `limits` 档位；429 走统一错误信封
- **上传面与路径白名单**（产物层）：`POST /uploads`（multipart，挂鉴权）流式落
  `data/uploads/<uid>/`，带单文件上限（`V2MD_UPLOAD_MAX_GB`）、每用户配额
  （`V2MD_UPLOAD_QUOTA_GB`）、类型白名单与 TTL 清扫（`V2MD_UPLOADS_TTL_H`）；返回的
  `path` 直接喂 `POST /runs`。非 http 输入按 `V2MD_LOCAL_PATHS`（默认 `auto`：
  AUTH=off 放行本地路径=开发/自托管姿势，鉴权档只认本人 uploads 白名单内路径；
  `allow`/`deny` 显式覆盖）——拒绝 422 `LOCAL_PATH_FORBIDDEN`
- **网络拓扑**：双服务均 `network_mode: host`——算力池/队列在宿主回环
  （`127.0.0.1:9700`），出网代理也常驻宿主回环（桥接网络够不到）。api 绑
  `127.0.0.1:8790`，公网唯一入口 = 反代 TLS（见部署章）；服务器本地调试可
  `ssh -L 8790:127.0.0.1:8790 <server>` 打隧道。
- **Postgres checkpointer（按需，缺省 sqlite）**：`V2MD_CHECKPOINTER=postgres` +
  `V2MD_PG_DSN`（两行进 `secrets/video2md.env`）。compose 的 postgres:17-alpine 挂
  `profiles:["pg"]` 默认**不起**（外部托管 PG 只给 DSN 同入口）：
  ```bash
  echo 'V2MD_CHECKPOINTER=postgres' >> secrets/video2md.env
  echo 'V2MD_PG_DSN=postgresql://v2md:***@127.0.0.1:5432/v2md' >> secrets/video2md.env
  docker compose --profile pg up -d postgres   # PG 未就绪时 api fail-fast + restart 自拉起
  ```
  checkpoint 是易失数据（done 即 `delete_thread` 裁剪），**无存量迁移**。
- **deliver 形态激活**：`V2MD_ARTIFACT_MODE=deliver` +
  可选 `V2MD_OUTBOX_DIR`（容器指 data 卷）/`V2MD_OUTBOX_TTL_H`（默认 24）。
  产物不落 vault、runs.json 不养正文，bundle 拉取 + ACK/TTL 双路即焚（契约见
  [HTTP 后端契约](#http-后端契约多用户产品--phase-12)节）
- **出网与风控（厂商中立口径）**：云服务器访问境外源（YouTube/Colab/Kaggle/HF/Langfuse）
  需出网代理时配 `V2MD_PROXY`——只作用于 yt-dlp 等子进程，进程内 httpx（LLM 等）不受
  影响仍直连。国内视频站（B站/抖音/小红书）对机房 IP 风控（B站对境外 IP 412 + 对
  非大陆 IP 地域锁，普通机场节点实测全灭）：需 `V2MD_PROXY_DOMESTIC=1` 且代理侧有
  **大陆住宅类出口**才能稳定抓取——这是选代理链路的硬约束，部署前先验证。
- **无可用 provider 时**：池子 `/status` 全灭 → 转写失败。兜底选项：给
  `gpu_router.docker.json` 的 `aliyun` provider 填国内 GPU 容器地址

## 终局布局（云服务器 · 多用户）

单公网入口、账目按 uid 隔离、算力池化、云端不储存、异地容灾——各层布局：

| 层 | 布局 |
|---|---|
| 入口 | 域名 + 反代 TLS 是唯一公网入口（SSE 不缓冲；`/docs` `/openapi.json` 网关侧关闭）；安全组只放 80/443，api/算力池/观测栈全回环 |
| 鉴权与账目 | acctkit 登录（Google id_token / 设备流 / 微信扫码，provider 可插拔）；身份表 `(provider, external_id) → uid` 首登开户、多 provider 归并同一 uid；run 按 uid 归属——越权 403、dedup 按 owner 隔离 |
| 执行 | 多 worker 并发：队列调度 + 每用户并发上限 + 公平排队，单用户长任务不阻塞他人；进度事件经 Redis pub/sub 扇出，SSE 在多 worker 下口径一致 |
| 状态 | Postgres 承载 users / runs / state（各存储走同协议适配器，如 `IdentityStore`）+ checkpointer；单文件 JSON 形态只属开发机 |
| 产物 | deliver「云端不储存」：per-run outbox 在架 → bundle zip 拉取 → ACK/TTL 即焚；上传走专用上传面 + 路径白名单（不收裸本地路径） |
| 限流配额 | per-user 限流（入口/反代层）+ 磁盘配额 + 队列长度上限 |
| 算力 | gpu_router 算力池独立伸缩（Colab/Kaggle/Modal/ZeroGPU/国内云动态选举），与 api 解耦——api 侧零 GPU，服务器只跑轻编排 |
| 数据安全 | 本地备份（daily/weekly + sha256 manifest + 季度恢复演练）+ rclone/restic 异地推对象存储（B2/S3/OSS） |
| 发布 | CI（lint/test/build）→ 镜像 push registry（不可变 tag 锚 git sha）→ 部署流水线上线 + healthz/preflight 体检 → 按 tag 一键回滚 |
| 观测告警 | OTel → collector → Langfuse（trace/LLM 成本按 uid 归因）+ Prometheus/Grafana（指标）；Alertmanager → 飞书卡片；死人开关 + run 卡死看门狗（exit 自愈） |

## Obsidian 插件（Video2MD Inbox · 产物回传主路）

把「服务器产物 → 开发侧 vault」解耦成装在 Obsidian 里的独立插件：轮询本人名下 done run →
拉 `GET /runs/{id}/bundle` zip → 解包写进 `资料库/视频笔记/`（索引按 wikilink 去重 append）→
`DELETE artifacts` ACK 即焚。附带命令面板提交链接与 Google 设备流登录。工程在
`obsidian-plugin/`（TypeScript + esbuild，CI 有独立 plugin job）。

**前置**：服务器切 deliver 模式 + google_device 鉴权（生产档进 `secrets/video2md.env`：
`V2MD_ARTIFACT_MODE=deliver`、`V2MD_AUTH_PROVIDERS=google,google_device`，
另配 TV/Limited-Input 型 `GOOGLE_CLIENT_ID/SECRET`）；存量 run 收编用
`python scripts/migrate_run_owner.py --to <uid> [--status done] [--dry-run]`。

**API 访问**——云服务器形态：按部署章起反代 TLS 后，插件设置页填
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
  生产绑公网务必 `V2MD_AUTH=google` + 反代 TLS（Google 回调需 https）——完整口径见
  部署章「公网上线检查清单」。
- `/healthz` `/queue` `/preflight` **保持不鉴权**（docker healthcheck / gpu_router 看门狗在打）。

**进度双通道**：轮询 `GET /runs/{id}` 保留原形状；新增 SSE
`GET /runs/{run_id}/events`（text/event-stream，同样过 owner 校验）——
连上先推 `{"event":"connected","status","url"}` 快照，之后每条 `data:` 即
`logs/run-*.jsonl` 的事件记录（含 `graph:*` 图级事件），15s 无事件发 `: ping`，
`graph:end`/终态即关流。事件总线扇出（`video2md/events.py`；多 worker 形态下经
Redis pub/sub，见终局布局·执行层）。

**错误契约**：非 2xx 统一 `{"error": {"code", "message", "details"}}`
（`AUTH_REQUIRED`/`FORBIDDEN`/`NOT_FOUND`/`VALIDATION`…；422 带定位信息）。

**产物形态双模式（`V2MD_ARTIFACT_MODE`）**：

| | `vault`（开发机默认） | `deliver`（多用户「云端不储存」） |
|---|---|---|
| 落点 | 直写 Obsidian vault（自托管，自行同步回本地） | per-run outbox → finalize 搬到在架位 `V2MD_OUTBOX_DIR/<run_id>/` |
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
- **指标（Grafana）**：Grafana 仅绑服务器回环——本地经
  `ssh -L 3000:127.0.0.1:3000 <server>` 隧道（或反代 + 自行加访问控制）打开
  dashboards 看预置看板：run 终态速率、阶段耗时、LLM token/调用量、熔断计数、队列深度。
  指标进程内存态，重启清零（运行态非账本）。已开匿名只读（GF_AUTH_ANONYMOUS_*，
  面板仅回环+隧道可达，写操作仍需 admin）。
- **死人开关**：配 `V2MD_DEADMAN_PING_URL` 后 run 开始 ping `/start`、done/partial
  ping 原 URL、error ping `/fail`——「任务压根没跑/挂死」只有外部监控看得见。
- **run 卡死看门狗（2026-09-23）**：`video2md/watchdog.py`——graph.invoke 无超时，
  ffmpeg/yt-dlp 挂死时工作线程永远占着串行信号量（队列饿死且死人开关不响：进程没死）。
  心跳三源取最小龄：RunLog 每条事件 / `_run_stage_inner` 每条目循环 / `started`+stages ts
  兜底；api 内周期任务（`V2MD_WATCHDOG_PERIOD_S`，默认 60s）扫内存任务表，最近活动超
  `V2MD_WATCHDOG_TIMEOUT_S`（默认 7200s）判卡死。动作 `V2MD_WATCHDOG_ACTION`：
  `mark`（默认，开发机）= run 标 error + 死人开关 /fail + 指标 `v2md_watchdog_trips_total`
  （卡死线程无法强杀，信号量仍占，需人工重启）；`exit`（生产 compose 已配）= 标记后
  `os._exit(70)`，容器 restart 自愈、lifespan 只重投 running 状态的 run（不会死循环）。
- **告警链（2026-09-23）**：Prometheus 规则（`deploy/prometheus-rules.yml`，8 条：
  指标流沉默/看门狗触发/run error/partial/熔断/队列积压/LLM 失败/collector 掉线）
  → Alertmanager（`deploy/alertmanager.yml`，critical 4h 重发、warning 12h，
  critical 抑制同源 warning）→ 飞书转译器（`deploy/feishu_webhook.py`，AM 无原生飞书
  receiver，卡片格式转译 + 持密钥）→ 飞书群机器人。密钥 `FEISHU_WEBHOOK_URL` 进
  `secrets/obs.env`；配置校验进 CI（promtool/amtool + `test_alert_rules.py`）。
  恢复（resolved）也通知（卡片绿色）。`absent_over_time` 沉默告警依赖 collector 的
  `metric_expiration: 5m`（deploy/otel-collector.yml）。
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

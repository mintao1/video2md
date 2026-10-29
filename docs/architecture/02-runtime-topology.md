# 02 · 运行拓扑与部署形态

> 当前 GPU 执行面（2026-10-18）已切换为 Colab CLI：K8s API 清单的三个 REMOTE
> 均为 `colab-cli`，不要求启动 gpu_router。部署前准备清单指定的独立 Colab 授权目录，
> 供 UID/GID 10001 写入 token 刷新和会话状态；详见 [README](../../README.md#colab-cli-gpu)。
> 下文涉及算力池进程的步骤只适用于显式使用 HTTP REMOTE 的兼容部署。


- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：① 入口层/发布层；② 可用性/可运维性

## 进程与容器视图

```
云服务器（≥2C4G，无 GPU）
├─ nginx（仓内 deploy/nginx.conf）:80(/443 模板) ── 唯一公网入口（P0-4 ✅）
│    └─→ 127.0.0.1:8790
├─ docker compose 栈（network_mode: host）
│    ├─ api 服务（deploy/docker-compose.prod.yml）  uvicorn 单进程，V2MD_HOST=0.0.0.0:8790
│    │    ├─ asyncio 信号量闸门（V2MD_MAX_CONCURRENT_RUNS，默认 1）
│    │    ├─ 工作线程跑同步 graph（asyncio.to_thread）
│    │    ├─ 常驻周期任务：watchdog 巡检 / outbox sweeper / uploads sweeper
│    │    └─ events PG 泵线程（V2PDF_EVENTS=pg 时，LISTEN v2pdf_events+v2pdf_cmds）
│    ├─ gpu_router 容器    :9700（无认证面，安全组兜底——见已知限制）
│    └─ postgres 服务（deploy/docker-compose.prod.yml）  postgres:16-alpine；127.0.0.1:5432
├─ 平台统一观测栈（~/stack/platform-obs，2026-09-26 obs 双栈合并——平台 Phase 1.1/M1；
│    本仓自有 docker-compose.obs.yml 退役为本地预览资产，服务器不再使用）
│    ├─ otel-collector :4318 → traces 按 service.name 路由各自 Langfuse 项目 / prometheus exporter :8889
│    ├─ prometheus :9090 → grafana :3000（匿名 Viewer，仅回环+隧道）
│    └─ alertmanager :9093 → feishu-webhook :9760（v2pdf 群）/9761（book 群）→ 飞书
└─ 卷：v2pdf-data（state/runs/users/checkpoints/logs/outbox/uploads）
       vault（vault 模式产物）、gpu-router-logs、v2pdf-pg
```

## 网络口径

| 端口 | 绑定 | 公网可达 | 说明 |
|---|---|---|---|
| 80/443 | 反代 | ✅ 唯一入口 | 仓内 nginx（P0-4 ✅）：SSE 关缓冲；`/docs`/`/openapi.json`/`/redoc` deny；匿名双层限流；body 上限 2g。TLS 证书部署期接 `deploy/certs/`（模板见 conf 尾） |
| 8790 | 127.0.0.1 | ❌ | api；`ssh -L 8790:127.0.0.1:8790` 隧道调试 |
| 9700 | 全接口 | ❌（安全组+iptables 不放行） | gpu_router **无认证**（兼容 HTTP REMOTE 部署的独立服务） |
| 3000/9090/4318/8889/9760 | 127.0.0.1 | ❌ | 平台统一观测栈（platform-obs） |
| 5432 | 127.0.0.1 | ❌ | PG（compose 内置时）；外部托管 PG 走 DSN |

安全组：只放行 80/443。⚠ 此口径当前是**文档纪律**——仓内无 IaC/防火墙脚本承载（批判①入口层）。

## 出网与风控（硬约束）

- 境外源（YouTube/Colab/Kaggle/HF/Langfuse）：`V2PDF_PROXY` 只作用于 yt-dlp 等子进程；进程内 httpx（LLM）直连。gpu_router 侧代理走 `secrets/gpu_router.env`。
- 国内站（B站/抖音/小红书）对机房 IP 风控：需 `V2PDF_PROXY_DOMESTIC=1` 且代理侧有**大陆住宅类出口**——部署前必须先验证，否则下载阶段必炸。

## 部署前提与步骤（自 README 部署章迁入）

前提（厂商中立——任意云 VM / 自托管皆可）：
- Docker + Compose（≥2C4G 起步；GPU 全在远端算力池，服务器本身不需 GPU）
- 一个域名 + TLS 证书（Google 登录回调要求 https；Caddy 可全自动签续）
- LLM 端点凭证（openai-compatible 三键）
- 至少一家 GPU provider 凭证（colab-cli token / kaggle.json / …，缺了对应 provider 自动冷却）
- Google OAuth「Web」客户端 client_id（后端只验签不持 secret）；CLI 无浏览器登录另备「TV 与 Limited Input」型 client（设备流，需 client_secret）
- 两仓库同级放 `~/stack/{video2pdf,gpu_router}`（别处则设 `GPU_ROUTER_DIR`）

**1. 代码与凭证**（secrets 0600，只放服务器，不进 git）：

```bash
git clone <本仓库地址> ~/stack/video2pdf
git clone <gpu_router 地址>  ~/stack/gpu_router
# secrets/ 目录：
#   secrets/video2pdf.env   ← LLM 三键 + 多用户生产档键 + V2PDF_DEADMAN_PING_URL（可选）
#   secrets/gpu_router.env ← 出网代理 env（HTTP_PROXY 等，可选但强烈建议）
#   secrets/obs.env        ← LANGFUSE_BASIC / GF_SECURITY_ADMIN_PASSWORD（观测栈，可选）
#   secrets/colab-cli/token.json、secrets/kaggle/kaggle.json（可选）
```

`secrets/video2pdf.env` 多用户生产档（完整键义见 `.env.example`）：

```ini
V2PDF_AUTH=google                            # 必须非 off（P0-1 后由启动防呆机制保证）
V2PDF_AUTH_PROVIDERS=google,google_device    # 接微信扫码改 google,wechat
V2PDF_ARTIFACT_MODE=deliver                  # 云端不储存：outbox + ACK/TTL 即焚
V2PDF_CORS_ORIGINS=https://app.example.com   # GUI 前端 origin 白名单
V2PDF_ENV=production                         # 遥测环境标签（compose 已默认）
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
# GOOGLE_CLIENT_SECRET=…     # 仅设备流需要（TV/Limited-Input 型 client）
# WECHAT_APP_ID / WECHAT_APP_SECRET=…        # 微信扫码（可选）
# V2PDF_PG_DSN=postgresql://… # Postgres 可选档：checkpointer + 存储三件
#                            # + V2PDF_EVENTS=pg + V2PDF_TASKS_STORE=postgres（04 篇）
```

**2. 构建 + 起栈 + 体检**（国内服务器可 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 加速）：

```bash
cd ~/stack/video2md
docker compose -f deploy/docker-compose.prod.yml build
docker compose -f deploy/docker-compose.prod.yml up -d
curl -s localhost:8790/healthz && curl -s localhost:8790/preflight && \
  curl -s "localhost:9700/status?refresh=true"
# 观测面归平台统一栈（~/stack/platform-obs）：curl -s 127.0.0.1:9090/-/healthy · curl -s 127.0.0.1:13133/health
# （2026-09-26 obs 双栈合并后本栈不再叠 docker-compose.obs.yml——叠了会与平台栈抢端口）
```

**3. 反代 TLS**——api 只绑回环，公网流量全部经反代终结 TLS。**配置实体已入仓（P0-4 ✅）**：
`deploy/nginx.conf`（compose 的 nginx 服务挂载，host 网络）——SSE 关缓冲、OpenAPI 面 deny、
`limit_req` 双 zone（通用 120r/m + 设备流 30r/m 对齐应用层）、`client_max_body_size 2g`。
`scripts/smoke_proxy.sh` 在 CI 逐面验证。TLS 证书部署期放 `deploy/certs/`（gitignore，模板见
conf 尾注释；lego/certbot DNS-01 或前置 Caddy 自动 ACME）。历史参考配置：

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
        proxy_read_timeout 3600s;    # bundle 大包 / SSE 长连接
        client_max_body_size 512m;
    }
    # P0-4 后标配：location = /docs { deny all; }  location = /openapi.json { deny all; }
}
```

Caddy 等价（自动 TLS）：`api.example.com { reverse_proxy 127.0.0.1:8790 { flush_interval -1 } }`（flush_interval -1 = SSE 不缓冲）。

**4. 上线检查清单**：见 [05-security.md](05-security.md#公网上线检查清单p0-清偿后逐项打勾)。

## 部署/发布/回滚

```bash
git tag v0.x.y && git push origin v0.x.y        # CI release job → GHCR 不可变 tag
scripts/deploy.sh v0.x.y --host user@server     # 目标机 checkout tag → compose build → up -d → 体检
scripts/deploy.sh <上一个 tag> --host …          # 回滚 = 部署旧 tag
```

**当前形态的真实口径**（批判① E6 修订后）：
- 部署 = **目标机现场构建**（`scripts/deploy.sh:49`），非 pull GHCR 镜像；CI 构建产物与部署产物不保证逐字节一致（uv.lock 收敛大部分漂移）。
- GHCR 镜像是**手动灾备存档**（`deploy.sh:12-13`：git 不可用时 `docker pull` 手动顶上），不在脚本主路径。
- `compose up -d` 同容器替换 = **有停机窗口**（秒级，在跑 run 靠 lifespan 恢复重投）。

## 恢复语义

进程重启 → lifespan（`api.py:558-595`）：runs 表里 `running` 的 run 重新入队（tmp 已清的翻 error 不重跑）；PG 档走 `FOR UPDATE SKIP LOCKED` 认领，并发起的副本只有一份拿到候选集（`pgstore.py:214-238`）。error 的 run 不自动重跑（避免双份产物）。

watchdog `V2PDF_WATCHDOG_ACTION=exit`（compose 生产已配）：判定卡死 → 标 error → `os._exit(70)` → compose `restart: unless-stopped` 拉起（`watchdog.py:299-309`）。⚠ 裸跑/systemd 形态无对应资产（ROADMAP P2-1）。

## 运行面开关速查（自 README「运行面要点」迁入）

| 开关 | 口径 |
|---|---|
| `V2MD_MAX_CONCURRENT_RUNS` | 执行并发闸门（默认 1=单 worker）；调大前 checkpointer 切 postgres；vault 自托管保持 1 |
| `V2MD_QUEUE_MAX` / `V2MD_USER_QUEUE_MAX` / `V2MD_USER_SUBMIT_PER_MIN` | 三道提交闸（0=不设限）；PG 档跨副本一致，json 档进程内 |
| `V2MD_UPLOAD_*` | 上传面：单文件 2GB / 每用户 5GB / TTL 24h（`data/uploads/<uid>/`） |
| `V2MD_RUNS/STATE/USERS_STORE` `V2MD_TASKS_STORE` `V2MD_EVENTS` | 存储分派与事件扇出档位（详见 [04 篇](04-data-and-consistency.md)）；切换前先 `backend/scripts/migrate_json_to_pg.py` 导存量 |
| `V2MD_CHECKPOINTER=postgres` + `V2MD_PG_DSN` | 两行进 `secrets/video2md.env`；生产 Compose 固定提供 `postgres` 服务，`api` 依赖健康检查后启动（PG 未就绪时 fail-fast，restart 自拉起）；checkpoint 易失无存量迁移 |
| `V2MD_ARTIFACT_MODE=deliver` + `V2MD_OUTBOX_DIR/TTL_H` | 产物不落 vault、bundle+ACK/TTL 即焚（03 篇） |

存量 run 收编（迁移到多用户归属）：`python backend/scripts/migrate_run_owner.py --to <uid> [--status done] [--dry-run]`。

## 已知限制

1. ~~反代配置不在仓~~（P0-4 ✅：deploy/nginx.conf + smoke_proxy.sh CI 验证 + deploy.sh 起栈后 /docs→403 断言；nginx 由主机/平台反代托管）。「安全组只放 80/443」由仓内拓扑承载。
2. gpu_router :9700 无认证，信任模型 = 网络位置；colab quick tunnel 是无鉴权公网 URL（批判② S8）。
3. 单副本 + 单 PG，均为 SPOF；多副本读侧就绪、写路径与验证闭环见 ROADMAP P1-1/P1-4。
4. 生产 Compose 的 PG 凭证必须通过 `.env`/`VIDEO2MD_ENV_FILE` 提供；DSN 指外部库时必须显式覆盖。

## 演进方向

P0-4 反代实体化 → P1-1/P1-4 多副本闭环 → P2-1 多 worker 形态声明 → P2-3 蓝绿发布。

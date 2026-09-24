# 05 · 安全

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：② 安全维度（🔴 H）；① 入口/鉴权/限流/算力层
- **结论先行：在 ROADMAP P0 全清之前，本系统不应暴露公网。**

## 威胁模型（STRIDE 简表）

| 威胁 | 资产/面 | 现状对策 | 残余风险 |
|---|---|---|---|
| 假冒（S） | 用户身份 | Google JWKS 验签 / 微信 code 换 unionid；设备流桥 | AUTH=off 无生产防呆（S1） |
| 篡改（T） | run 记录/产物 | owner 隔离 403；原子写；行级 upsert | 低 |
| 抵赖（R） | 操作痕迹 | `logs/api-requests.jsonl` 请求审计；run jsonl | 无签名/不可抵赖需求（当前规模可接受） |
| 信息泄露（I） | 路径/配额/基础设施画像 | — | `/preflight` 未脱敏（S3）；`/docs` 开放（S4） |
| 拒绝服务（DoS） | OAuth 配额/队列/磁盘 | 认证提交三闸 | **匿名端点零频控**（S2）；上传无内容嗅探（S5） |
| 提权（E） | uid 越权 | `_require_run_access` 403 | 低（有契约测试钉住） |

## 防线现状（已落地）

- **归属隔离**：run owner=内部 uid，越权 403/无 run 404（`api.py:283-296`）；dedup 按 owner 隔离；`GET /runs` owner 过滤。
- **错误信封**：统一 `{"error":{code,message,details}}`，不泄栈（`api.py:634-661`）。
- **上传面**：后缀白名单 + `sanitize_filename` + 1MB 分块流式 + 单文件上限 + 每用户配额 + TTL（`api.py:666-702`）。
- **路径闸**：`V2MD_LOCAL_PATHS` auto/allow/deny；鉴权档只认本人 uploads 目录内路径（`api.py:353-376`）。
- **密钥管理**：SSOT `~/.config/video2md.env`（0600）；服务器 `secrets/` 挂 ro；`.env`/`secrets/` 在 .gitignore；git 历史扫描无泄漏（2026-09-24 核对）。
- **出网风控口径**：国内站必须大陆住宅出口（硬约束，见 02 篇）。

## 已知缺口（批判② S1–S8 全表）

| # | 缺口 | 评级 | ROADMAP |
|---|---|---|---|
| S1 | production + AUTH=off 可共存，启动不抗议 | 🔴 | P0-1 |
| S2 | `/auth/device/*` 匿名零频控（可刷 OAuth 配额） | 🔴 | P0-2 |
| S3 | `/preflight` 匿名暴露路径/磁盘/provider 清单 | 🔴 | P0-3 |
| S4 | `/docs` `/openapi.json` 开放，关闭靠人工反代纪律 | 🔴 | P0-4 |
| S5 | 上传无 magic number 内容校验 | 🟡 | P0-6 |
| S6 | 日志脱敏只盖 `sk-`/`app-`/`dataset-` 三前缀（`util.py:112-118`） | 🟡 | 随 P2-5 日志收口一并治 |
| S7 | compose PG 默认口令仅回环兜底 | 🟢 | DSN 外指时必须覆盖（部署清单） |
| S8 | gpu_router :9700 无认证；colab quick tunnel 无鉴权公网 URL | 🟡 | P0-4 后评估（短期：tunnel 仅临时调试用） |

## 公网上线检查清单（P0 清偿后逐项打勾）

| 项 | 达标口径 |
|---|---|
| P0-1..P0-4, P0-6 | ROADMAP 五项全绿（启动防呆/匿名限流/preflight 脱敏/反代实体+`/docs` deny 验证/上传嗅探） |
| `V2MD_AUTH` | `google`（或含 google/wechat 的组合），**机制保证**（P0-1 fail-fast） |
| `V2MD_ARTIFACT_MODE` | `deliver`（多用户「云端不储存」） |
| `V2MD_CORS_ORIGINS` | 只列真实前端 origin |
| `V2MD_ENV` | `production`（Langfuse 分项目） |
| 反代 | 仓内配置（P0-4）：SSE 关缓冲、`/docs` `/openapi.json` deny、`limit_req` |
| 安全组 | 只放 80/443（02 篇网络口径逐项核对） |
| 密钥 | `secrets/` 0600 + 已入密码管理器/备份方案（09 篇） |
| 观测 | 告警链自测（触发一条 warning 看飞书卡片）；死人开关 URL 已配 |
| 备份 | `backup.sh` 已 cron + 异地 remote 已配 + 恢复演练跑过一次（09 篇） |

## 演进方向

P0 五项清偿 → P2-2 预算闸（成本维度安全）→ 按需引入 WAF/限源（反代层，P0-4 之后的增量）。

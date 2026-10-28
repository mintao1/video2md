# 2026-10-29 生产联调排查

目标站点：`https://ferosse.tech`。前半轮只读检查线上接口、K3s 状态和静态 bundle；随后使用已批准的 fixture 完成一次登录态线上任务复测。没有修改线上资源。

## 现场结论

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `/healthz` | 通过，但版本信息不足 | HTTP 200，`version=0.2.0`；没有 git SHA / image provenance |
| `/preflight` | **失败** | HTTP 200，业务 `ok=false`；`ffmpeg/disk/vault=true`，`gpu_pool=false` |
| API Pod | **旧发布** | `video2md-api-685c9d9865-z6wd`，image `video2md:ca87534304ab` |
| API 算力配置 | **失效路由** | `V2MD_REMOTE/OCR_REMOTE/CLIPS_REMOTE=http://127.0.0.1:9700` |
| GPU Router | **不可用** | Pod `0/1 ImagePullBackOff`；镜像拉取事件为镜像源 HTTP 403，9700 无监听 |
| OAuth 协议门禁 | **线上未生效** | `/auth/google/login` 在缺少或传 `terms_accepted=0` 时仍 302 到 Google 并创建旧 `v2pdf_oauth_state` |
| 前端 bundle | **线上未生效** | 线上仍引用旧 `index-BGJgc8WH.js` / `QueueView-Cx9ml1Tf.js`，包含本地上传 mock、草稿 mock 和 `u_mock` 用户兜底 |
| 匿名安全面 | 通过 | `/me=401`、`/runs=401`、`/docs=403`、`/openapi.json=403`、`/terms=200`、`/privacy=200` |

## 根因

1. GitHub `origin/main` 当前是 `ca87534304ab`，线上服务器 checkout 和镜像都来自该提交。当前工作区的协议门禁、真实上传、错误可见性、Colab 路由和相关评测仍是未提交改动，部署工作流不会读取它们。
2. 已成功的 Deploy run 只验证了 rollout 和 `/healthz=200`。它没有检查 `/preflight.ok=true`，也没有校验发布 SHA 或前端 bundle 指纹，因此 GPU Router 已失效仍会被标绿。
3. 线上采用旧 `127.0.0.1:9700` GPU Router 配置，但该路由 Pod 因镜像源 403 无法启动。当前工作区的目标配置已切到按租户 `colab-cli`，并固定 `/app/.venv/bin/colab` 与 18080 relay；这套配置尚未发布。

## 本地修复与验证

本地工作区已包含协议门禁、生产按钮去 mock、错误持久显示、媒体读取、Colab 租户和 `colab-cli` 部署清单等修复。本轮另外补了两处防回归：

- `scripts/deploy.sh`：`/preflight` 请求失败或返回 `ok=false` 时非零退出，不再放行未就绪部署。
- `.github/workflows/deploy.yml`：产品 checkout 固定到触发本次运行的 `${{ github.sha }}`，rollout 后读取 `/preflight`，失败即回滚。
- `ImportModal.vue`：网盘来源提供真实链接输入；切换来源时清理不可持久化的本地 File，避免把旧文件误提交；移除默认 B 站占位 URL。

验证结果：前端集成测试 `75 passed`，前端回归 `1 passed`，生产构建通过；后端生产浏览器/部署/计算/登录/跟进回归 `80 passed`；`git diff --check` 和 shell/YAML 语法检查通过。

## 发布后验收条件

发布必须使用同一条确定的 `video2pdf`/`video2md` 主线，不能把当前旧 `video2pdf` 主线与这份旧路径工作区混合部署。发布后按以下顺序复测：

1. `/healthz` 返回目标 release SHA（或等价不可变 image tag）。
2. `/preflight` 返回 `ok=true`，且算力检查为当前选定的 `colab_cli=true`；若继续使用 GPU Router，则必须先恢复可拉取镜像并验证 9700 `/status`。
3. `/auth/google/login` 缺少协议参数时返回品牌首页错误，不得创建 OAuth state；带 `terms_accepted=1` 才允许 302。
4. 首页、队列和个人中心静态 bundle 不再出现 `mock`、`u_mock`、`v2pdf_*` 兼容兜底；本地文件必须经过 `/uploads` 后再创建任务。
5. 用批准的 fixture 做一次最小任务，确认 run 持久化、SSE 阶段、教材正文、图片/视频真实可播放和 usage/cost 同一 run 对账。

## 本次发布与复测结果

- `acdbddd`：迁移协议门禁、真实上传、Colab 代理和 `colab-cli` 路由；`82a43e0`、`53c079e`：修复镜像导入后的节点磁盘闸门。最终发布 run：`40772999891`，构建、镜像导入、rollout、预检和 smoke 全部通过。
- 线上 Pod：`video2md-api-c949fb977-9vcvz`，镜像 `video2md:53c079e98369`；节点预检 `ok=true`，`colab_cli=true`，ffmpeg、disk、vault 均通过。
- 线上浏览器：协议未勾选时 Google 按钮为 disabled；勾选后按钮启用。匿名 HTTP 面保持 `/me=401`、`/runs=401`、`/docs=403`、`/openapi.json=403`、`/redoc=403`、`/terms=200`、`/privacy=200`。
- OAuth 入口：缺少或传 `terms_accepted=0` 时回品牌首页错误，不创建 Google state；传 `terms_accepted=1` 才跳转 Google。
- 新前端 bundle 不再出现 `u_mock`、本地上传 mock 或默认 B 站占位 URL；批准 fixture 的线上付费路径复测结果见下节。

## 批准 fixture 全链路复测

- 通过已登录线上 UI 提交唯一化 fixture：`billing-index-lesson.mp4?fixture=20261029-success-1`，run `a3edda79`。队列从“处理中”实时收敛到“历史完成”，无重试或人工干预。
- 线上运行事件日志记录了完整图链：`download` → `transcribe`（4 段，置信度 0.975）→ `frames`（80 候选）→ `ocr`（80/80 命中）→ `select`（8 帧）→ `clips`（8/8 入库）→ `llm:organize` → `llm:metadata` → `graph:end status=done`；最终教材写入 `/vault/资料库/视频笔记/数据库索引与查询优化入门-2.md`。这些 stage 事件是线上 SSE 总线的同一事件记录。
- 任务卡与个人中心费用流水对账一致：输入 `6485` tokens、输出 `1917` tokens、GPU `336.757` 秒、费用 `$0.001931`；本月合计显示同一成本 `$0.001931`，并包含该 run 的 `done` 明细。
- 已登录同源接口交叉核对：`GET /runs/a3edda79`、`GET /runs/a3edda79/note`、`GET /cost` 均返回 200；SSE `GET /runs/a3edda79/events` 返回 200，首帧为 `connected` 且状态为 `done`。图片媒体返回 `200 image/jpeg`（22080 bytes），视频媒体 Range 请求返回 `206 video/mp4`（1 KiB/209675 bytes）。
- 教材库显示 1 本、1/1 讲、100%；阅读器页面能打开正文和 `billing-index-lesson-f01-0000.jpg` 等真实图片引用，正文同时包含 `billing-index-lesson-c01-0000.mp4` 等视频引用。来源链接保留唯一化 fixture query，说明产物归属于本次 run。
- 对照坏例仍符合预期：`b299dba0` 在 `note` 质量门禁失败（0/0 tokens，费用 `$0.000000`）；`a383b9cf` 为幂等跳过（无教材、费用待结算）。

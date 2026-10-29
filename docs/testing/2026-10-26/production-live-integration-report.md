# 2026-10-26 生产联调记录

## 范围与证据边界

目标站点：`https://ferosse.tech`。本次完成真实浏览器登录、Google 回调、Colab 授权、会话刷新、教材库、个人中心和任务历史检查，并用只读 HTTP 探针核对 API、GPU Router 与模型渠道。随后以唯一 URL 变体强制提交两条真实生产任务，完成 GPU 转录、OCR、模型生成、插图/剪辑和教材落库；第二条任务额外用同一登录主体的 Bearer 令牌实时订阅 SSE，直至 `graph:end`。记录不保存邮箱、OAuth code/state/token、Cookie、API key、完整 URL 查询串或用户内容。

## 实际结果

| 检查点 | 结果 | 现场事实 |
| --- | --- | --- |
| 协议门禁 | 通过 | 未勾选协议时登录被阻止；确认协议后才进入 Google 授权。 |
| Google 登录与回调 | 通过 | Google 回调成功并进入已登录站点，会话刷新后仍保持登录。 |
| Colab 授权 | 通过 | 当前账号完成 Google 同意和一次性授权码提交；页面显示“已连接我的 Colab”和“你的 Colab 授权已保存”。 |
| 教材库与任务历史一致性 | 通过 | 新任务 `c2e5d741` 完成后，任务页显示 `全部任务(17)`、`历史完成(6)`、`转换失败(11)`；教材库新增《数据库索引原理与联合索引优化》，任务卡与教材正文均可打开。 |
| 个人中心与额度 | 有限 | 登录态可用；当前套餐仍显示免费版，额度为 `0 / 0 小时`、`0 / 0 GB`。 |
| API 健康与预检 | 通过 | `/healthz=200`；`/preflight` 返回 `ok=true`，ffmpeg、磁盘、vault、Colab CLI 均通过。 |
| GPU/模型计算通道 | 通过 | GPU Router `1/1 Running`，API 使用按租户 Colab CLI；生产 Secret 已切到可用的 OpenCode Go 路由和 `glm-5.3-flash`。本次两个模型调用均返回 HTTP 200，`span_log` 记录模型名和 usage。 |
| 新建转换与计费闭环 | 通过 | 真实任务 `c2e5d741`：转录 4 段、置信度 0.98；候选帧 80；OCR 命中 80/80；LLM 选帧 4；clips 4/4；note 非空并写入 vault。任务卡显示输入 6141、输出 1043、GPU 368.514 秒、费用 `$0.001443`。 |
| Bearer 认证 SSE 实时订阅 | 通过 | 新任务 `70f4a4d4` 的未认证订阅返回 `401`，带 Bearer 的实时订阅返回 `200`；连接快照为 `connected`，随后收到 `stage:download`、`stage:transcribe`、`stage:frames`、`stage:ocr`、`stage:select`、`stage:clips`，最终收到 `graph:end(status=done)`。 |
| 第二条真实转换与账单闭环 | 通过 | `70f4a4d4` 完成 1/1 条目；3 次 `glm-5.3-flash` 调用均成功，输入 6445、输出 8360、GPU 486.728 秒；教材《数据库索引与查询优化》非空写入 vault，结算 `$0.005147`。 |

## 生产部署证据

- API Deployment 当前镜像：`video2md:0.2.0-colab-tenant-20261026d`，Pod 已滚动到 `1/1 Running`。
- 生产配置已从失效的 `127.0.0.1:9700` 路由切换为 `colab-cli`，并固定 `/app/.venv/bin/colab`；clips 使用远端通道。
- GPU Router 当前 Pod 为 `1/1 Running`，滚动状态通过；镜像已导入 K3s containerd 的正确 socket，历史 ImagePullBackOff 未再复现。
- OAuth 修复显式把代理传入 token exchange 和 userinfo 请求，生产授权已实际验证成功。
- 生产模型通道使用模型列表 `https://opencode.ai/inference/go/v1/models`、聊天端点 `https://opencode.ai/inference/go/openai/v1/chat/completions` 与 `glm-5.3-flash`；模型列表和真实聊天调用均返回 200。此前对聊天基址拼接 `/models` 的 403/404 是错误探测路径，不代表渠道不可用。
- 成功任务教材产物：`/vault/资料库/视频笔记/数据库索引原理与联合索引优化.md`，frontmatter 标记 `frames_kept=4`、`frames_ocr=80`、`transcribe_conf=0.98`，正文和 4 个媒体引用可从教材阅读页访问。
- 本轮把已验证的 `18080` 代理、`NO_PROXY` 排除 OpenCode 和可用的 `hotfix-20261021l` 初始化镜像写回 Kustomize，并滚动校验到 `1/1/1`；滚动后健康、预检和模型列表/聊天复测仍为 200。

## 匿名 HTTP 探针

同一生产域名的只读探针：`/healthz=200`（版本 `0.2.0`）、`/terms=200`、`/privacy=200`、`/me=401`、`/runs=401`、`/docs=403`、`/openapi.json=403`、`/redoc=403`。`/preflight=200` 且业务结果为 `ok=true`。

## 本地验证与评测门禁

- 后端全量：`1839 passed, 7 skipped, 8 warnings`。
- 前端集成：`75 passed`；回归集：`1 passed`；生产构建：通过。
- OAuth/生产计算配置定向测试：`18 passed`；相关 Ruff 检查通过。
- 浏览器生产评测集：8 项通过；登录边界集：5 项通过；跟进/状态集：6 项通过；租户隔离集：通过；质量证据检查：通过。
- 教材库 loading 回归覆盖“加载中不显示 0 本”的场景；图测试已隔离全局状态，避免全量测试互相污染。
- 本次生产评测集补充了成功转换、模型渠道、教材产物、usage/cost 对账和 Bearer SSE follow-up 证据；`production-followup` 七项离线合同检查通过。

## Bearer SSE 与第二条真实任务证据

- 订阅脚本在任务进入 `running` 后立即打开 `/runs/70f4a4d4/events`，请求头为当前登录主体的 `Authorization: Bearer`；令牌只在本机进程内使用，没有写入仓库或报告。
- 生产响应对照：无认证 `401`，Bearer `200`；实时流共收到 61 条事件，阶段顺序包含 `download → transcribe → frames → ocr → select → clips`，并以 `graph:end`、`status=done` 结束。认证订阅不是已完成任务的历史回放。
- 任务状态：`done`，所有条目 `download/transcribe/frames/ocr/select/clips/note=true`；教材路径为 `/vault/资料库/视频笔记/数据库索引与查询优化.md`，正文 2857 字符、8 帧保留、80 帧 OCR 命中、转写置信度 0.98。

## 第二条任务 usage/cost 对账

- 单任务 usage：`llm_in_tok=6445`、`llm_out_tok=8360`、`llm_cached_in_tok=0`、`gpu_s=486.728`。
- 价格快照：输入 `$0.15/MTok`、输出 `$0.50/MTok`、缓存输入 `$0.03/MTok`、GPU 免费。
- 计算：`6445 × 0.15e-6 + 8360 × 0.50e-6 = 0.00514675`，结算四舍五入为 `$0.005147`，与生产 `/runs/70f4a4d4` 和 `/cost` 明细一致。
- 本月汇总更新为 18 条任务，输入 `333484`、输出 `97125`、GPU `10609.952` 秒、支出 `$0.096341`；与上一条成功任务的汇总增量逐项相等。

## 结论

教材库与任务历史不一致的问题已定位并修复为前端异步加载状态误导；GPU Router、Colab CLI、OAuth 代理链路、可用模型通道和生产部署均已恢复。真实任务 `c2e5d741` 与 `70f4a4d4` 均完成完整转换并产生可读教材，模型调用成功且 usage/cost 与月度汇总一致。第二条任务已用 Bearer 完成实时 SSE 订阅，从 `connected` 到阶段事件再到 `graph:end(done)` 的闭环证据已保存；未认证请求仍被 401 拒绝。

## 成功任务 usage/cost 对账

- 单任务 usage：`llm_in_tok=6141`、`llm_out_tok=1043`、`llm_cached_in_tok=0`、`gpu_s=368.514`。
- 价格快照：输入 `$0.15/MTok`、输出 `$0.50/MTok`、缓存输入 `$0.03/MTok`、GPU 免费。
- 计算：`6141 × 0.15e-6 + 1043 × 0.50e-6 = 0.00144265`，结算四舍五入为 `$0.001443`，与任务卡和 `usage_settlements.attribution_usd` 一致。
- 本月该租户汇总：17 条任务，输入 `327039`、输出 `88765`、GPU `10123.224` 秒、支出 `$0.091194`；成功任务已计入汇总。

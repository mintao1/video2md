# 生产模型计费测试结果（2026-10-21）

## 授权与范围

用户已明确授权本项目生产测试执行真实计费任务，并要求把持续授权固化进评测集。
本轮从 Google 登录开始，在前端显式输入视频链接并点击生产提交按钮。报告不记录登录
凭据、OAuth 参数、模型密钥或完整受版权保护内容。

## 结果

**阻塞在视频解析，未进入 GPU/LLM 阶段，实际新增计费为 0。**

| 阶段 | 结果 | 证据 |
|---|---|---|
| Google 登录 | PASS | 登录后进入 `/library`，会话有效 |
| YouTube 链接显式输入 | PASS | 前端输入 `aircAruvnKk` 对应公开视频并提交 |
| YouTube 解析 | FAIL | yt-dlp：`Sign in to confirm you’re not a bot` |
| Bilibili 链接显式输入 | PASS | 前端输入产品默认公开视频 `BV1WE411d7v4` 并提交 |
| Bilibili 解析 | FAIL | 生产容器经配置代理仍返回 HTTP 412 |
| 前端失败反馈 | FAIL | 两次提交均未显示错误；输入/弹窗状态变化造成静默失败 |
| run 持久化 | FAIL | 生产 `v2md_runs` 为 0 行，`v2md_submit_log` 为 0 行 |
| GPU/LLM 调用 | BLOCKED | 没有 run，管线未启动 |
| usage/cost 结算 | BLOCKED | 没有 run，未产生新增 usage/cost |
| 教材产物 | BLOCKED | 未进入转写、OCR、LLM 或写盘阶段 |

## 新增评测与防回退

- 新增 `production-model-billing-eval-set.jsonl`，覆盖真实计费授权、任务创建、usage/cost、
  幂等、产物闭环和失败可见性。
- 新增 `backend/tests/test_production_billing_eval_set.py` 静态门禁，3 项通过。
- 完整 API 契约与新门禁合跑：86 项通过。
- 前端集成回归：22 项通过。

## 生产缺陷

1. **P0：生产视频来源不可解析。** YouTube 需要登录/cookies，Bilibili 出口被 412
   风控拒绝；当前用户无法从两个主入口创建任务，因此模型计费主链不可达。
2. **P1：创建任务失败被前端静默吞掉。** `quickSubmit`/`startConvert` 没有错误处理，弹窗
   还会在异步提交完成前自行关闭。用户看不到后端 400，也无法判断是否扣费。
3. **P1：计费测试无法证明 usage/cost。** 这是 P0 的下游阻塞，不得将“零成本”误判为
   计费功能通过。

## 复测入口条件

- 为生产 yt-dlp 配置合法可用的 YouTube cookies，或修复 Bilibili 所需的住宅出口/cookies；
- 前端展示 `POST /runs` 的错误信封，并仅在创建成功后关闭弹窗；
- 之后重新执行同一评测集，必须拿到 run_id、终态、LLM/GPU usage、`cost_usd` 和教材产物。

## 复测二（生产热修复镜像 `0.2.0-hotfix-20261021h`）

本轮通过真实 Google account chooser 完成登录并回到 `/tasks`，随后在可见前端输入受控短视频
`https://ferosse.tech/test-assets/model-billing-smoke.mp4`。静态资源返回 `video/mp4`，因此该
样例不受 YouTube/Bilibili 反爬影响。

结果仍在创建 run 前失败：生产 API 进程的 `PATH` 不包含 `/app/.venv/bin`，但 `yt-dlp`
实际安装于 `/app/.venv/bin/yt-dlp`。API 的解析子进程无法使用镜像内已安装的解析器。

| 检查 | 结果 |
|---|---|
| 过期会话识别 | PASS；首次提交跳转 `/login?reason=expired` |
| Google 重新登录与回跳 | PASS；回到 `/tasks`，未记录 token/邮箱 |
| 前端显式输入受控视频链接 | PASS |
| `POST /runs` | FAIL；HTTP 400，链接解析失败 |
| `yt-dlp` 安装 | PASS；`/app/.venv/bin/yt-dlp`，版本 2026.08.19 |
| API 子进程 PATH | FAIL；缺少 `/app/.venv/bin` |
| 持久化 | `v2md_runs=0`；提交限流日志累计本轮 1 次 |
| GPU/LLM usage、cost、教材产物 | BLOCKED；任务未创建，新增模型费用为 0 |

复测前必须让 API 子进程通过绝对路径或正确的虚拟环境 PATH 调用 `yt-dlp`，并增加容器内
解析器可执行性启动门禁。修复后才可继续验证 Colab、LLM usage/cost、幂等计费和教材产物。

## 复测三（生产热修复镜像 `0.2.0-hotfix-20261021l`）

继续使用可见前端和同一受控短视频执行真实生产复测。任务 `9ad9ee9d` 成功创建；多次点击
“重试任务”始终复用该 run，数据库保持 1 个 run，未发生重复创建或重复扣费。

| 阶段 | 结果 | 生产证据 |
|---|---|---|
| Google 登录与回跳 | PASS | 经 `lcode-cn` OAuth 出口重新登录并返回 `/tasks` |
| 前端显式输入链接 | PASS | 输入框保留完整受控 MP4 URL |
| 下载与工作目录 | PASS | 修复代理继承、通用 MP4 format 和 hostPath 权限后完成下载 |
| 重试幂等 | PASS | 多次重试仅有 1 个 run / 1 个 distinct run |
| Colab ASR | PASS | 2 段，置信度 0.97；修复 PyAV/faster-whisper/CUDA 依赖 |
| Colab OCR | PASS | 处理 5 帧 |
| Colab clips | PASS | 1/1；优先使用系统 ffmpeg，外部下载设置 30 秒超时 |
| Colab 关机 | PASS | 终态后确认无活动 session |
| LLM 网络路由 | PASS | `api2.niubi.win` 已绕过错误代理，能够到达上游网关 |
| LLM 推理 | BLOCKED | 当前密钥下 `/models` 宣告的 8 个模型均返回 403“该渠道已被禁用” |
| usage/cost | FAIL | `gpu_s=0.038`、LLM token=0、cost=0；GPU 数值与实际长任务明显不符 |
| 教材产物 | BLOCKED | LLM 阶段失败，未形成最终教材 |

本轮证明视频下载与 Colab 三段 GPU 主链已恢复，但不能判定模型计费通过。剩余两个门禁是：

1. LLM 网关管理员为当前密钥启用至少一个可用渠道，或提供可工作的 base URL、模型和密钥；
2. 修正 GPU usage 归集，使 ASR/OCR/clips 的真实计时进入 run usage 与成本结算，而不是仅记录
   `0.038` 秒级请求开销。

在两项完成前，评测必须保持失败；不得因 cost 为 0 将本轮标记为通过。

## 复测四（可见前端新建 run `aef9643a`）

从退出登录开始重新走 Google account chooser，成功回到教材库；随后进入任务页，显式输入
`https://ferosse.tech/test-assets/model-billing-smoke.mp4` 并点击“开始解析”。这是一次新提交，
不是旧 run 重试。

| 检查 | 结果 |
|---|---|
| 登录、回跳、会话 | PASS |
| 显式输入并创建 run | PASS；`aef9643a` |
| 下载 | PASS；41 秒、640×360 的受控 MP4 |
| Colab ASR | PASS；约 176 秒，2 段，置信度 0.97 |
| Colab OCR | PASS；约 76 秒，5 帧 |
| clips | PASS；本地处理 1/1，不计 GPU 秒数 |
| GPU usage 归集 | PASS；`gpu_s=251.977`，与 ASR+OCR 墙钟时间吻合 |
| Colab 关机 | PASS；队列空闲后日志确认自动关闭 |
| LLM | FAIL；选帧与教材调用均返回 HTTP 403“该渠道已被禁用” |
| LLM usage | FAIL；输入/输出 token 均为 0 |
| 生产定价 | FAIL；GPU、LLM 输入、LLM 输出三个单价均配置为 `0.0` |
| cost | FAIL；`0.0`，不得判定计费成功 |
| 教材 | BLOCKED；LLM 失败，任务终态为 error |

本轮关闭了上一轮 `gpu_s=0.038` 的计时证据缺口，但暴露出独立的生产定价配置门禁：即使
LLM 渠道恢复，当前全零单价仍会使 cost 恒为零。计费验收必须同时要求可用 LLM 渠道与
非零、经审批的生产单价表。

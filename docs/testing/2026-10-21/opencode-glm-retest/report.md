# OpenCode Go / GLM-5.3-Flash 生产联调 · 2026-10-21

本轮接续上轮失败验收，用户指定 OpenCode Go 与 `glm-5.3-flash`，并确认接受登录条款。**指定模型的真实生成、阅读和任务计费验收 PASS；24 步完整生产门禁仍 FAIL。** 已完成生产前端显式输入、真实推理、账本复算、重复提交和预算拒绝复测，以下保留成功、失败和未满足的验收项。

后续 11 条逐项批注已落实为[独立评测与防回退矩阵](annotation-regression-matrix.md)：新增 75 项离线回放/反例检查和 2 项实际 HTTP 拒绝集成测试，接入 CI。该补充未重新生成或部署，也未改变本报告生产门禁结论。

## 接入与边界

- Base URL：`https://opencode.ai/inference/go/openai/v1`，模型 `glm-5.3-flash`。
- 官方 Go 归因单价（USD/百万 tokens）：输入 0.15、输出 0.50、缓存读取 0.03。
- 免费 Colab 成本 $0，保留远程调用墙钟秒数；这个数包含传输、冷启动和排队，是占用时长上界，不是硬件 GPU 忙碌采样。
- Go 是订阅额度；token 单价用于额度/成本归因，不代表每次请求新增等额现金支出。供应商最终账单未在本轮查询。
- [官方接口、价格与使用范围](https://opencode.ai/v2/docs/console/go/)说明 Go 面向编程代理流量。客户端使用真实 `video2md/0.2.0` 标识及按 run 稳定的会话编号，没有伪装成其他客户端。当前接口成功并不等于供应商承诺支持所有生产应用场景。
- 密钥写入本机 0600 配置及 Kubernetes Secret，不进入仓库、截图、报告或浏览器表单回显。增加了 `oc_sk_` 日志脱敏回归。

## 连通与登录

本机最小请求返回 200，生产容器初次流式探测因代理返回 TLS `WRONG_VERSION_NUMBER` 而失败。对照探测证明：使用现有代理失败，关闭环境代理进行同一请求返回 200。最终仅将 `opencode.ai` 加入保留原值的 NO_PROXY；证书验证保持开启。生产代码的流式调用随后成功返回非空文本和输入 23、输出 54 tokens。

这些独立连通性探测不绑定 run，所以不进入任务账本，详见 `connectivity.json`、`production-proxy-diagnosis.json`、`production-model-probe-after-proxy.json`。不能把任务账本差值声称为整个供应商账号的所有消耗。

真实浏览器在生产品牌页勾选已确认的条款，使用现有 Google 账号登录，进入教材库。初始真实账本：4 个任务、GPU 639.766 秒、LLM 输入/输出 0、成本 $0。截图 `production-login.png`、`production-billing-before.png`；运维补充记录 `production-before.json`。

## 第一次真实生成：edfa1b1a

在前端显式输入并提交 `https://ferosse.tech/test-assets/model-billing-smoke.mp4`，新建第五个任务。

| 阶段 | 实测 |
| --- | --- |
| 下载 | 406,492 字节，1.825481 秒 |
| 转写 | Colab，2 段，文本非空、时间戳有效；远程调用 137.746 秒 |
| 候选帧 | 5 张，文件非空、时间戳合法 |
| OCR | Colab，5/5 帧；远程调用 88.765 秒；OCR 命中 0/5 |
| 选帧 | 真实 LLM，选中 2 帧 |
| 剪辑 | 2/2 节段可用；其中已有节段复用；远程调用 104.717 秒 |
| LLM | 输入 2,047、输出 8,690 tokens（包含供应商报告的 reasoning tokens） |
| 结算 | GPU 331.228 秒，成本 $0.004652，单价快照保留 |
| 产物 | partial，未生成教材；转录本 193 字，小于既有 200 字门槛 |

复算：`round(2047 × 0.15 / 1e6 + 8690 × 0.50 / 1e6, 6) = 0.004652`。账本仍保留真实消耗，不回填成零或成功。

此轮暴露两个遗漏：质量门禁在 organize/metadata 调用后才拒绝短文本；partial 的原因只留在编排状态，未落到任务记录，界面还显示“立即阅读”。对应证据为 `production-result.json`、`pipeline-log.txt`、`production-partial.png`。

## 据此修复并建立回归

- 所有待处理文本不足门槛时，在选帧 LLM 前拒绝并走 fail 路由；单条短文本也在 organize/metadata 前拒绝。保留 200 字门槛。
- finalize 保存错误；历史 partial 可从保留的转录文本与失败阶段得出安全诊断。
- 无 note 的任务不进入教材库、完成计数和教材统计，不显示阅读按钮，显示“生成受阻”及原因。
- 重复提交已存在任务时，前端刷新权威记录，防止同一 run 出现重复卡片及丢失用量。
- 新增真实 HTTP/适配器/组件测试和编排路由测试。先复现失败，再修复：短文本零模型调用、阈值边界、partial 原因持久化、无产物不计教材、重复提交不重复显示。
- 修复旧 LLM 测试在模块收集期全局替换遥测 SDK 的顺序污染。

最终通过后端 466 项（8 项既有弃用警告）、前端 31 项集成加 1 项提交回归、编排脚本全部 9 组案例、所改后端文件 ruff 与前端生产构建。测试日志见 `v2md-final-regression.txt`、`v2md-graph-regression.txt`。离线测试不替代生产证据。

## 第二次受控输入

为正向验收准备 595 字数据库索引课程，使用本地 Tingting 语音与四张技术文字课件合成。视频容器时长 122.25 秒，源音频 106.158503 秒；口述内容与媒体参数保存于 `fixture-lesson.txt`、`fixture-media.json`。这是受控测试输入；模型、ASR、OCR 和生产计费均继续走真实服务。

生产 URL：`https://ferosse.tech/test-assets/billing-index-lesson.mp4`。

## 第二次真实生成：4df77d9c

前端输入并提交上述 URL，13:30:05 创建第六个任务，终态 `done`，记录最后更新时间为北京时间 13:44:41.867。产生《数据库索引原理与查询优化》，Markdown 2,910 字符，教材库由 0 增为 1。本轮经历标准 Google 登录以及会话过期后的同账号重新登录；退出测试引用同一持续验收的[上一轮明确退出记录](../billing-retest/report.md)，没有声称本模型轮次再次执行了退出。

| 阶段 | 实测 |
| --- | --- |
| 下载 | 1,054,426 字节，2.155531 秒 |
| ASR | Colab，4 段、时间戳有效、文本非空；远程调用 147.762 秒 |
| 候选帧 | 80 张，上限 120；文件非空、时间戳合法 |
| OCR | Colab，80/80；远程调用 253.416 秒 |
| 选帧 | LLM 模式，8 张，均在候选 manifest 中 |
| 剪辑 | GPU，8/8；远程调用 407.942 秒 |
| 模型 | `glm-5.3-flash`，输入 6,498、输出 6,919 tokens，未拒答 |
| 结算 | GPU 809.120 秒、GPU 费用 $0，LLM 成本归因 $0.004434 |
| 阅读 | 正文、5 张引用图片和 5 段引用视频可用；实际播放首段至约 19.48/20 秒 |

复算：`round(6498 × 0.15 / 1e6 + 6919 × 0.50 / 1e6, 6) = 0.004434`。输出 token 包含供应商报告的 reasoning tokens；缓存输入为 0。月度隔离窗口增量恰为该任务用量和费用：任务 +1、输入 +6,498、输出 +6,919、GPU +809.120 秒、成本 +$0.004434。

月度最终合计 6 个任务、输入 8,545、输出 15,609、GPU 1,780.114 秒、成本归因 $0.009086；其中 $0.004652 属于前一个未产教材但实际消耗过模型的任务。没有篡改历史失败任务或抹除真实费用。证据：`lesson-result.json`、`production-final-readonly.json`、`billing-reconciliation.json`、`production-completed.png`、`production-reader-media.png`、`playback-proof.json`、`production-library.png`、`production-billing-final.png`。

再次在生产前端提交同一 URL，返回原 run；任务、token、GPU 和费用增量全部为 0。临时将预算设为 $0.000001，在生产导入框提交带独立查询参数的受控 URL，返回 429；错误持续留在弹窗内，输入保留，无成功提示、新任务或模型费用。最后恢复原预算配置（有效值 0，关闭限制），当前活跃任务 0。证据：`reuse-reconciliation.json`、`production-budget-rejected.png`、`lesson-after-budget.json`、`budget-restored.json`。

## 防回退与尚未通过的完整门禁

评测集覆盖 24 步阶段契约，既有 78 个反例及本轮新增短文本、真实 SDK 请求头、凭据脱敏、重复提交、预算错误保留等回归进入 CI。真实生产证据单独保存，不将合成数据或历史失败改成通过。

- `billing-evidence.json` → `billing-gate-result.txt`：**PASS**，只表示指定模型的登录、显式提交、成功推理、可见用量费用和教材增量均满足该门禁。
- `stage-evidence.json` → `stage-gate-result.txt`：**21 项 PASS、1 项 FAIL、2 项 BLOCKED**，完整门禁保持红色。
- pps-008：Colab 实际于 13:46:55.380 自动关闭，但最后任务记录更新时间至关闭约 133.513 秒，超过既有 30 秒保温 +15 秒轮询门槛。缺少更精细的队列空闲事件，保守判失败，不能据配置存在就声称时限通过。
- pps-013：教材、阅读和媒体通过；当前 vault 模式未实现整包导出。库页导出函数仅提示、bundle API 仅 deliver 模式可用，组合交付验收阻塞。
- pps-016：URL 已真实解析并下载，但没有独立记录 resolver 耗时，不能用下载阶段耗时代替该时限证据。

本报告不声称项目全部验收通过，也未查询供应商账户独立账单；不同来源站、上传、长期稳定性和模型内容准确率仍不在本轮正向样例的证明范围内。

## 部署与诊断纠正

最终 API 镜像 `video2md:0.2.0-integration-20261021g`，manifest digest `sha256:6aa5c1814dea6f41a499a1927b29bfe1ac778d995475ff9f3b1cf74fe4d069f4`。最终 Pod Running、ready、重启 0，见 `deployment-status.json`；健康路由为 `/healthz`。GPU router 保留 `gpu-router:0.2.3-hotfix-20261021e`。

曾将延迟返回的工具输出误判为剪辑传输持续卡死。核对时间线后确认：worker 13:38:19 完成 8/8，原有归档下载在 13:43:47 完成，主任务随后已成功；此后约 13:58 安装的临时传输 helper 没有参与该成功任务。临时 CLI 修改已恢复为原文件并校验相同 SHA256、helper 已移除，相关源码、测试和部署实验也已撤回，见 `temporary-cli-rollback.json`。没有将临时实验描述为上线修复。`clip-*` 调试文件仅为这次放弃的诊断记录。

发布滚动更新期间曾出现旧阅读页 CSS 预加载失败；发布完成并刷新后正文和媒体加载通过。没有为这一瞬时现象添加未经证实的阅读器修复。未创建提交或 PR；仓库原有改动保留。

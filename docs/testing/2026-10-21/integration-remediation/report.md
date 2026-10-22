# 生产联调修复记录 · 2026-10-21

已部署 `video2md:0.2.0-integration-20261021c`。代码、合同测试及本地真实界面验证通过；完整生产模型计费验收仍阻塞，不能发布全链路通过结论。

用户确认的成本口径：Colab 免费 GPU 成本 $0，保留实际 GPU 秒数；LLM 按实际模型官方价格归因。费用归因不等同于供应商最终账单。

| 上轮问题 | 本轮处理 | 验证与当前状态 |
| --- | --- | --- |
| GPU 与 LLM 单价全零，无法解释零费用 | 显式增加 free/metered GPU 政策；增加缓存输入 token 和价格快照；LLM 零定价仍显示未配置 | 免费 GPU 政策已部署；生产 LLM 单价仍零，`pricing_configured=false`，保留红项 |
| 任务列表丢失用量和失败原因 | API 返回 owner 范围内的 usage/cost/安全诊断；前端展示编号、阶段、HTTP 状态及行动提示 | 合同测试、HTTP 适配器与组件渲染、本地真实 UI 通过；生产旧任务函数读取证据见 production-readonly.json |
| “明细流水”无法操作 | 接入 `/cost`，显示月度汇总、当前单价、逐任务及已删除账单；缺历史价格快照时明确提示 | 本地点击与真实 HTTP 数据通过；生产登录后 UI 待确认条款后补验 |
| 已完成任务再次提交可能重复生产计费 | 相同用户、规范化 URL、分集范围且产物仍有效时复用完成任务；force、跨用户、缺失产物不误复用 | API 测试通过；完整生产幂等复测待成功产物 |
| 每步缺少机器可验证证据 | 写入阶段耗时、文件/时间戳/分段/帧/剪辑数量等测量；缺少测量则门禁拒绝通过 | 24 个稳定阶段案例、78 个反例；合成证据默认拒绝；旧失败证据保持原样 |
| 模型渠道 403 | 查明生产旧网关禁用；现存项目 MiMo v2.5 已退役，v2.6-flash 最小请求返回 402 余额不足 | 外部阻塞，未复制不可用密钥、未对旧 Kimi 渠道套用 MiMo 单价、未重新消耗 GPU |

## 自动验证

- 后端 API 合同、生产阶段/计费/联调/来源/生命周期/运行时评测和 readiness：456 passed。
- 前端 HTTP 集成：28 passed；提交失败防回退：1 passed。
- `vue-tsc -b && vite build` 通过。
- 本轮先误用不存在的 readiness 测试文件，未运行任何测试；更正为 `test_production_readiness_gate.py` 后上述 456 项通过。
- 已清除临时调试输出。最终构建的 ProfileView JS SHA-256 为 `da6e92ff709491226ca977578c15ca1e45a20bacd3d3a68ac30f9706014d1825`，API 源文件为 `b62c32f9ebb59a8f4edd1f0a38c37333e342864f848004b0a3da48b994a61114`，均与远端部署源一致。

## 真实界面操作证据的边界

生产站点新品牌页正常展示；未勾选条款点击登录时明确拦截。Google 登录前的条款接受属于法律协议动作，已请求用户操作时确认，目前未代为勾选。截图 `brand-agreement-gate.png`。

在隔离的本地真实前端与 FastAPI 上，用测试身份 alice（非真实 Google 登录）、独立临时数据及禁用执行的队列完成：

1. 点击“明细流水”，显示每任务 token、GPU 秒数、费用及未定价提示；免费 GPU 政策明确显示“费用 $0，保留实际用量”。见 `local-free-gpu-billing.png`。
2. 失败卡片显示 `note`、HTTP 403、GPU 204.669 秒、token 0、费用 $0，未展示上游伪密钥。见 `local-failed-task.png`。
3. 在前端显式输入 `https://ferosse.tech/test-assets/model-billing-smoke.mp4`，点击开始解析，获得测试任务 `f115e8bd`，排队成功。见 `local-video-entered.png`、`local-video-submitted.png`。队列信号量为零，没有执行模型或 GPU。
4. 再输入本地路径，被 `V2MD_LOCAL_PATHS=deny` 拒绝；错误可见且输入保留。见 `local-submission-error.png`。这是顶部输入表单的人工 UI 证据，弹窗错误分支另由回归测试覆盖。

测试浏览器跨本地第二端口请求 `/cost` 曾返回 Failed to fetch，服务端 CORS/HTTP 检查正常；使用与生产相同的同源代理后通过。未为此更改产品权限或安全策略。重启本地 fixture 时旧端口暂不可用，换用测试端口并重启代理后恢复，未影响生产。

`production-readonly.json` 由生产容器内 API 函数和响应模型采集，明确不是已登录浏览器 HTTP 验证。`public-boundaries.json` 验证匿名鉴权边界及公开页面。两者不能替代生产登录及模型执行证据。

## 部署与回退

依赖未变，前端本地构建后在既有不可变基础镜像上覆盖经过测试的后端和静态资源。完整镜像构建曾因 npm 下载超时中断，未影响在线服务。

- 镜像：`video2md:0.2.0-integration-20261021c`。
- 基础镜像：`video2md:0.2.0-hotfix-20261021l`。
- 补丁源清单 SHA-256：`cc3901e11bba35ff5493a2c0b6fe90d5e186337d74feea71281dbfcd86046864`。
- 镜像清单摘要：`sha256:99a9cc32600ebeacaea223327838a81df03e7599146e373b2699fe88c829e543`。
- Kubernetes rollout 已完成，公开 `/healthz` 返回 200。
- 部署前完整快照保存在服务器 `/tmp/video2md-before-integration-20261021c.json`，权限 600；不纳入仓库，避免包含配置秘密。
- 部署只变更 API 镜像及免费 GPU 计价环境变量，没有更改模型凭据或抹平历史账单。

## 未完成的生产验收

需要用户恢复可用且有余额的模型通道，并确认生产登录页的条款接受。随后才能从真实 Google 登录开始，在生产前端显式输入视频链接，验证成功产物、正数 LLM token、对应模型官方单价、费用计算、月度差值、完成任务复用及预算拒绝。

MiMo 官方价格资料：[按量计费](https://mimo.mi.com/docs/en-US/price/pay-as-you-go)。当前探测模型 v2.6-flash 的海外美元输入/输出/缓存输入价格分别为 $0.14/$0.28/$0.0028 每百万 tokens；国内人民币计价不同，启用前需按实际账户币种核准。未将此价格写入仍使用旧网关的生产环境。

重新运行旧生产阶段证据的门禁仍返回 FAIL，符合历史事实。单元测试绿色并不代表生产全链路绿色。

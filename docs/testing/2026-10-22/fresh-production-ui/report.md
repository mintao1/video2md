# 2026-10-22 新一轮生产 UI 验收：部分通过

目标站点：https://ferosse.tech 。按用户要求从登录开始，显式填写真实 LLM URL/API Key、完成 Colab/Kaggle 登录、提交真实 B 站合集并核对模型用量与费用。历史报告只提供背景，不算本轮通过证据。

**本轮真实生成和模型用量/成本归集对账通过，整体验收未全部通过。** 项目 Colab 授权受管理员权限阻塞；新生成第一讲没有任何媒体引用；第二讲视频在本轮观察窗口内未就绪。本轮没有改账、修改正文或重新请求模型来掩盖这些结果。

## 本轮实际结果

| 检查 | 状态 | 直接观察 |
| --- | --- | --- |
| 生产健康接口 | 通过 | `GET /healthz` 返回 `ok=true`、version `0.2.0` |
| 退出旧的项目登录态 | 通过 | 点击教材库“退出登录”，回到品牌首页 |
| 未勾协议的登录校验 | 通过 | 点击 Google 登录后停留首页，显示“请先阅读并同意《用户服务协议》与《隐私保护政策》” |
| 用户服务协议、隐私政策页面 | 通过 | UI 可打开并读取；隐私政策说明基本身份信息、链接、视频及产物的处理和第三方服务用途 |
| 项目重新 Google 登录 | 通过 | 用户确认协议后勾选，通过现有 Google 账号重新登录，回到教材库，基线 3 本教材 |
| 前端显式填写真实 LLM 配置 | 通过 | 从约定凭证文件读取后在真实前端输入 URL、密码型 Key 和模型；保存成功，重新进入页面仍为已保存个人配置，Key 不回显。端点 `https://opencode.ai/inference/go/openai/v1`，模型 `glm-5.3-flash` |
| Colab 官方页面账号 | 已观察，不等于重新授权通过 | 官方页面显示现有 Google 账号；尚未执行项目内 Colab OAuth，不代表后端凭证或 GPU 配额有效 |
| Kaggle 真实重新登录 | 通过 | 官方 UI 退出旧会话 → Login → Sign in with Google → 首页显示账号欢迎信息和 Your Work |
| 真实 B 站合集可访问 | 通过 | `BV1LV411r77C` 页面标题“PPT短视频教程合集，持续更新中……”，显示 9 集，前两集 01:00 / 00:55 |
| 项目 Colab 授权 | 权限阻塞 | 已登录账号在 `/profile/colab` 显示“需要服务管理员权限”；未绕过或修改管理员名单 |
| 项目前端输入原合集 | 通过，命中去重 | 显式输入 `BV1LV411r77C` 并点击一次解析，仍为 10 个任务，账目不变；不能冒充新生成 |
| 内置示例样本 | 下载失败 | `BV1WE411d7v4` 创建 `1d13e1c9`；实际为单视频，不适合作为合集夹具，download 失败、未进入后续阶段，月度模型用量未新增 |
| 新合集创建与执行 | 通过 | 已核实分集目录的 `https://www.bilibili.com/video/BV1R7411u7GT/` 从前端创建 `628f38fc`，实际前两集 158 / 586 秒。下载、转写、抽帧、OCR、剪辑、正文和元数据生成全部完成，终态 done、无 partial_failures |
| 两讲正文与数量 | 通过 | 阅读器可切换两讲，Markdown 分别 6,299 / 11,332 字节；个人中心教材数量 3 → 4 |
| 模型真实调用与计费对账 | 通过（用量及成本归集） | OpenCode 官方 UI 最终导出 97 条、truncated=false，其中 `video2md-628f38fc` 关联 5 个请求，全部 HTTP 200；122,193 输入 / 6,707 输出 / 0 缓存 tokens 与应用完全相同 |
| 第一讲媒体 | 未通过 | 后端已有 6 段剪辑，但生成 Markdown 的附件引用数为 0；阅读器 image/video 元素均为 0。这是生成结果缺少引用，不能以等待加载解释 |
| 第二讲媒体 | 部分通过，播放未验证 | Markdown 含 11 张图片＋11 段视频。后端全部 22 个引用文件存在，11 个视频 ffprobe 均成功；前端 11 张图片全部解码。在初次媒体采样后的 156.544 秒仍为 11 个媒体加载提示、0 个 video 元素，因此未执行实播，不判播放通过 |

## 确认点与后续验收

用户已明确同意项目协议，继续登录测试，并回复“暂时不限调用额度”。本轮没有将其解释为购买新套餐、充值或打开额外用量；服务商 Go 页面显示付费覆盖至 2026-11-06 01:26:00（+08:00），Use credit 关闭、可用 credit $0。

账单基线：输入 99,365 / 输出 66,524 tokens，GPU 远程调用墙钟 7,065.982 秒，成本归因 $0.045923，10 个任务。最终前端显示输入 221,558 / 输出 73,231 tokens，GPU 8,171.942 秒，成本归因 $0.067605，12 个任务。月度增量与新成功任务的输入、输出、GPU 墙钟和费用逐项一致；另一个新增任务是内置示例下载失败，没有观测到月度模型增量。该失败记录自身没有完整的 usage/cost 字段，不将其冒称已结算的逐任务零费用。

新成功任务费用公式：`122193 × 0.15 / 1000000 + 6707 × 0.5 / 1000000 = $0.02168245`。服务商逐请求总和相同；应用按六位小数显示 `$0.021682`。免费 GPU 单价为 0，远程调用墙钟 1105.960 秒仍保留；不将墙钟解释为物理 GPU 忙时。应用四个正文/元数据 span 加一个选帧请求构成五次调用，选帧没有独立持久 llm span，不伪造第五条应用 span。

首次导出只有本轮 4 个请求，独立对账实际返回 FAIL（尚缺 2,357 输入 / 104 输出 tokens）；刷新服务商 UI 后取得最终 5 条完整记录，独立对账返回 PASS。两份私有原件分别保存，初次失败证据 `reconciliation-initial.json` 未覆盖；最终证据为 `reconciliation-final.json`。未把初次快照不齐判定成生产漏记，也未跳过差异检查。

服务商原始导出可能含请求头和身份字段，保存在仓库外的 0700 目录，文件 0600；公开证据仅含白名单会计字段及原件 SHA256。订阅覆盖期和 token 成本归集分开记录；本轮没有付款、充值或更改套餐，未证明新增逐请求现金扣款。

OCR 阶段耗时 620.640 秒，剪辑阶段 241.095 秒。原始 ASR 文本为 658 / 2470 字符，OCR 合并后时间线变为 52,167 / 28,000 字符；这解释了输入体量变化，但不构成教材内容准确性评测通过。媒体观察期间使用多个测试标签页，没有完整的前台连续采样，因此不声称已完成严格 60 秒媒体就绪 SLA 验收。

阅读器观察期间，另开个人中心一度持续加载；退出阅读页后，前端账单恢复显示最终用量与金额。这只记录先后观察，不据此断言网络、认证或浏览器调度的根因。返回教材库的列表仍有等待，随后 browser 控制在刷新时超时；不将库列表初始“0 本”当作真实数据丢失，也不将尚未完成的列表恢复算通过。4 本教材的数量依据来自已加载的个人中心。

## 证据

- `01-privacy-policy.png`：隐私政策。
- `02-login-consent-required.png`：当前未勾协议的登录页。
- `03-colab-account.png`：Colab 官方账号页面。
- `04-kaggle-login-start.png`：退出后 Kaggle 官方登录表单。
- `05-kaggle-login-success.png`：Google 登录返回后的 Kaggle 账号首页。
- `07-project-login-success.png`：项目 Google 登录返回教材库。
- `09-llm-saved.png`、`17-llm-persisted.png`：真实模型配置保存及持久化。
- `10-billing-before.png`、`billing-before-ui.txt`：月度账单基线。
- `11-colab-admin-required.png`：项目 Colab 授权权限阻塞。
- `12-real-collection-entered.png`：原合集显式输入（命中去重）。
- `15-verified-new-collection-entered.png`、`16-new-collection-running.png`：新合集显式提交和任务创建。
- `transcribe-frames-completed.json`：只读后端补证；不是 UI 替代验收。
- `kaggle-ocr-version-ui.txt`：本轮版本 23 Running，以及版本 21/22 已结束；未冒用历史 20 或更早版本。
- `19-supplier-go-status.png`：现有 Go 订阅状态。
- `20-real-config-entered-full-masked.png`：真实配置同时填写的完整页面，密钥仅有掩码。
- `21-task-completed.png`、`task-completed-ui.txt`：本轮任务 done 和已结算费用。
- `final-backend.json`、`book-delivery.json`：只读的最终任务、两讲正文摘要和媒体文件验证。
- `provider-run-accounting-redacted.json`、`reconciliation-final.json`、`monthly-reconciliation.json`：独立逐请求及月度增量对账。
- `reader-first-render-audit.json`、`reader-second-media-later.json`：第一讲无媒体、第二讲媒体等待的前端 DOM 实测。
- `23-billing-final.png`、`25-billing-result-visible.png`、`billing-summary.png`、`billing-final-ui.txt`：最终前端月账单。

执行工具为 lcode-auto 技能指定的 computer use。Chrome 扩展连接接口报请求头策略加载错误；Chrome 原生观察出现空白，改用同一 computer use 工具的应用内浏览器继续，未改用其他浏览器自动化后端。此工具异常未被记作产品失败。

用户后续要求的两个官方平台登录跳转按钮已加在本地 `LlmSettingsCard.vue` 配置区下方，构建、SSR 链接检查通过，尚未部署，不能算生产 UI 已有这两个按钮。本轮未改动部署配置、历史证据或生产账目；未提交代码、发布、付款或充值。

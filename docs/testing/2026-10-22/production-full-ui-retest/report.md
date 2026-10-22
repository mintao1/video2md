# 2026-10-22 真实生产前端全流程复测：部分通过

目标：https://ferosse.tech 。本轮独立证据；历史报告只用于定位环境。所有登录、输入和任务提交经 lcode-auto 指定的 computer use，服务器只读记录单列为补证。

用户在本轮明确同意网站服务协议和隐私政策，并对模型调用回复“不限制”。未充值、购买套餐或修改生产部署。

**新合集两讲生成及独立用量/成本归因对账通过；项目 Colab/Kaggle 前端连接和阅读器视频就绪未通过。不能宣称完整生产验收通过。**

| 验收项 | 本轮结果 | 证据 |
| --- | --- | --- |
| 未同意协议时登录 | 通过；显示需要先同意协议 | `01-login-consent-required.png/txt` |
| 协议与隐私公开页 | 可访问；隐私页仍声明运营者联系方式待补充 | `02-privacy-policy.png` |
| 未登录访问个人中心 | 重定向 `/?redirect=/profile` | 真实 UI 观察 |
| 项目真实重新登录 | 通过；退出旧会话、确认协议、选择现有 Google 账号后回到 4 本教材的教材库 | `03-project-login.png` |
| 显式填写真实 LLM 配置 | 通过；从本机既有凭证文件读取，在前端逐项输入 URL、密码型 Key、模型并保存，出现成功提示 | `04-real-llm-entered.png`、`05-llm-saved.png` |
| 项目前端 Colab 授权 | 阻塞；当前普通 Google 用户显示“需要服务管理员权限”，无可操作授权按钮 | `06-colab-admin-blocked.png/txt` |
| Colab 官方网页登录 | 观察到既有 Google 登录账号；没有重新完成项目 OAuth，不判项目连接通过 | `10-colab-official-account.png` |
| 项目前端 Kaggle 入口 | 未通过；生产配置区没有本地新版的“登录 Kaggle”按钮 | 生产个人中心 UI 与本地组件对照 |
| Kaggle 官方重新登录 | 通过；真实 Sign Out → Login → Sign in with Google → Welcome 账号首页 | `07-kaggle-login-form.png`、`08-kaggle-relogin-success.png` |
| 显式输入真实 B 站合集 | 通过；输入 `https://www.bilibili.com/video/BV1MS4y117Bv/`，点击一次转换按钮，创建新任务 `8c5d87f3` | `09-real-collection-entered.png`、`11-new-production-task.png/txt` |
| 真实 GPU 作业 | 通过；本轮 Kaggle #27–#32 六个作业最终全部 Successful，活动数归零，均标注 T4 x2；与本轮两集 ASR/OCR/剪辑成功阶段记录共同判定 | `12-kaggle-live-gpu.png/txt`、`18-kaggle-all-six-success.png/txt`、`progress-14.json` |
| 新教材与模型真实调用 | 通过；`8c5d87f3` 终态 done、无 partial_failures，两讲正文可切换，个人中心教材数 4 → 5 | `progress-14.json`、`19-production-completed.png/txt`、`book-delivery.json`、`25-billing-final.png/txt` |
| 服务商独立对账 | 通过；完整导出 97 条、未截断，关联本任务 6 个唯一请求，全部 HTTP 200；输入 99,340 / 输出 14,491 / 缓存输入 0 tokens，与应用一致 | `provider-run-accounting-redacted.json`、`provider-reconciliation.json` |
| 月度增量 | 通过；成本归因 $0.067605 → $0.089751，增量 $0.022146；任务数 12 → 13，用量增量与本任务一致 | `monthly-reconciliation.json`、`25-billing-final.png/txt` |
| 正文媒体文件与服务端解码 | 通过；两讲各 16 / 18 张图片和 16 / 18 段视频引用，共 68 个文件、40,896,472 字节；34 段视频 ffprobe 均成功 | `book-delivery.json` |
| 阅读器视频实播 | 未通过；第一讲已加载页面的前台观察 183.430 秒时，16 张图片解码，仍有 16 个加载提示、0 个 video；第二讲前台 89.115 秒时仅 1 张图片、35 个加载提示、0 个 video；因此没有执行实播，未将文件有效冒充播放通过 | `reader-first-visible-samples.json`、`reader-second-samples.json`、`22-reader-first-media-pending.png`、`24-reader-second-after-60s.png/txt` |
| 已完成合集重复提交 | 通过；再次从前端输入同一 URL 并提交，任务数仍为 13、原任务 `8c5d87f3` 保持，月度用量及成本完全不变，无新增活动任务 | `26-dedup-url-entered.png`、`27-dedup-completed.png/txt`、`dedup-reconciliation.json`、`final-after-dedup.json` |

配置端点为 `https://opencode.ai/inference/go/openai/v1`，模型 `glm-5.3-flash`。密钥未写入报告；填写截图使用密码掩码。

账单基线：12 个任务、输入 221,558 / 输出 73,231 tokens、GPU 调用墙钟 8,171.942 秒、成本归因 $0.067605，无活动任务。`baseline-server.json` 是只读服务器补证，不替代前端账单验收。GPU 墙钟不代表设备忙时，token 成本归因不等于新增现金扣款。

生产前端固定提交范围 `0,1,2`，本轮实际解析前两集，时长分别 1,154 / 1,261 秒。未声称处理整个合集。两集下载、转写、抽帧和 OCR 已完成；转写阶段 342.389 秒、抽帧 96.714 秒、OCR 463.527 秒。原始 ASR 文本 5,813 / 3,487 字符，OCR 合并后为 22,534 / 35,489 字符；这只是阶段证据，不代表内容准确率通过。

运行版本只读核对：API 为 `video2md:0.2.0-note-embeds-20261022f`，GPU 为 `gpu-router:0.2.3-ocr-layout-20261022g`，两服务 Ready/Updated 均为 1，见 `runtime-images.json`。Kaggle 官方 UI 当前版本 #30 的 OCR worker 成功，运行 203.7 秒、GPU T4 x2，见 `15-kaggle-version30-success.png`、`16-kaggle-ocr-runtime.png/txt`。

OpenCode 官方 Go 页面显示 Paid period ends 2026/11/6 01:26:00，Use credit 关闭、可用 credit $0，见 `14-supplier-subscription.png/txt`。通过官方 UI 导出的最近 24 小时基线共 91 条、truncated=false，本轮 session 请求数为 0；原件位于仓库外 0700 目录、文件 0600，仅将 SHA256 和数量写入 `supplier-before-summary.json`。

## 最终生成与计费

两讲 Markdown 为 14,537 / 15,295 字节，共 29,832 字节。后端实际保存的候选剪辑数为 46 / 51；正文只引用其中 16 / 18 段，不混淆候选数量与已发布引用数量。下载、ASR、抽帧、OCR、选帧、剪辑、正文与元数据阶段均有本轮成功记录；剪辑阶段总计 637.571 秒。

服务商 6 条请求合计成本归因 **$0.02214650**，应用按六位小数显示 **$0.022146**，差异 $0.00000050，在对账脚本的六位显示容差内。当前输入/输出价格计算为 `99340 × 0.15 / 1e6 + 14491 × 0.5 / 1e6`。免费 GPU 归因 $0，仍记录调用墙钟 1,348.769 秒；月度 GPU 增量相同。服务商两条请求各输出 4,000 tokens，这些用量也计入总额；独立导出没有回复正文，不据此断言它们是空回包。

本轮独立原件 `supplier-final.json` 保存在仓库外私密目录；公开材料仅包含白名单会计字段及 SHA256。独立对账命令实际返回 PASS，月度对账各字段均为 true。现有订阅覆盖与 token 成本归因分开记录：本轮没有产生或核验新增逐请求现金扣款，没有将美元归因金额说成新付款。

## 未通过项与边界

1. **P1：普通用户无法在项目中连接 Colab。** `/profile/colab` 仍要求服务管理员权限。本地个人租户改造尚未出现在本轮生产版本中；未修改管理员名单、绕过权限或替普通用户注入凭证。
2. **P1：项目内缺少 Kaggle 登录/连接入口。** 官方 Kaggle 重新登录成功，但不能替代“从项目配置区进入并连接个人 Kaggle”的验收，也不证明浏览器账号自动绑定到服务端 GPU 凭证。
3. **P1：阅读器视频长期未就绪。** 在记录的前台观察窗口内无法实播；后端文件存在和解码成功只排除了这些文件本身缺失/不可解码，未确定网络、鉴权、请求调度或前端加载逻辑的根因。未修改媒体或伪造播放状态。

两讲正文包含可见的 Obsidian callout 标记和字幕 wikilink 原文；本轮未做全文准确率、公式渲染、完整合集或浏览器矩阵验收。页面固定只跑前两集，不将本轮两集成功夸大为整个合集成功。

工具环境异常：Chrome 扩展请求头策略加载失败，使用同一 computer use 的应用内浏览器。B 站官方页面出现 ERR_CONNECTION_RESET，未绕过安全限制；项目自身解析和下载成功，区分浏览器访问失败与业务处理结果。部分后台辅助页签随后失效，保留已取得证据，不把工具失效归为产品故障。

阅读器观察后，返回教材库及直接导航曾未及时完成，最终出现一次浏览器 Page.navigate 超时。通过同一 computer use 的新页签打开个人中心，关闭旧的媒体观察页后，5 本教材及完整最终账单加载成功。该先后关系不足以确定根因，未将工具导航超时单独算作已定位的产品缺陷。

本轮仅测试及新增证据，未修改业务源码、部署、管理员名单、历史账目和历史验收文件。

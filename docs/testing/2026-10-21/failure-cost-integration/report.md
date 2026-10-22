# 上轮生产问题联调结果

本轮开始于 2026-10-21，结束于 2026-10-22（Asia/Shanghai）。

**结论：应用修复已部署，生产容器回放、真实模型探测和代码回归通过；完整生产验收仍未通过。**
GPU 配额、真实前端操作工具及服务商独立账单是目前的外部阻断。历史失败记录未关闭。

## 已部署结果

最终镜像 `video2md:0.2.0-failure-cost-20261021b`，原镜像为
`video2md:0.2.0-integration-20261021m`。中间版本 a 发布核心修复，b 追加两处类型修正。
最终 Deployment generation 54，Ready/Updated 副本均为 1，Pod 重启 0 次；五个运行文件
SHA256 均与本地清单一致。`/healthz=200`，匿名 `/me`、`/cost=401`。
证据：`release-manifest.json`、`deployment-final.json`、`pods-final.json`、
`production-runtime-verification.json`。

| 上轮发现 | 本轮处理 | 验证与未关闭部分 |
| --- | --- | --- |
| OCR/剪辑失败后仍调用模型 | 发布依赖前置检查；OCR 失败条目退出模型选帧，剪辑等依赖失败在正文/校对/元数据前拦截 | 使用生产代码与历史任务副本在独立进程回放，OCR、clips 两例模型调用均为 0；没有写回任务、调用真实模型或伪造前端提交。成功教材产出仍待可用 GPU 与前端验收 |
| 空内容重试漏记 usage | 发布每次 HTTP 请求最终 usage 的累计，保留逐次 usage、缓存及完整性信息；同流累计块不重复计数 | 原生产代码复现应记 300/100、实际 200/60；升级后同一复现完整记录 300/100。另有真实单次推理验证，真实空内容重试场景尚未在新任务中重新观测 |
| Google 换票间歇超时 | 停用与主服务同端口冲突、已累计重启约 2.4 万次的旧 `oauth-relay.service`；在用 `video2md-oauth-relay.service` 恢复仓库约定的回环监听；错误提示明确可重新登录 | 单一服务 active、旧服务 disabled/inactive、监听 127.0.0.1 已实查；修改后两组共 6 次无凭据 token 端点探测均正常返回 400，约 1.7–3.5 秒。没有证明重复服务是旧两次超时的唯一根因；三次真实 Google 登录仍待前端工具 |
| 服务商实际扣款未核验 | 修正门禁对 Go 订阅口径的误解，独立支持逐请求现金扣款与订阅票据核验 | 按量模式保留严格金额匹配；订阅模式要求独立已付款账单、账户/订阅/账期一致、完整用量导出与额度内请求，禁止把 token 归因当作现金。真实账单没有取得，继续未核验 |
| 前端只见通用 note 错误 | API 安全诊断定位 clips/OCR 等实际依赖，并提示补跑失败条目 | 新增 3 项 HTTP 契约回归；生产历史任务读取到 clips 阶段及可执行提示，原始路径/上游凭据不透出。浏览器渲染待真实 UI 验收 |
| 上轮全依赖类型检查 8 项错误 | B站公共 API 增加 dict/list 精确重载；下载超时参数显式传入，保持原有行为 | 全量 39 个后端源文件 mypy 通过；B站解析/下载及隔离回归通过，两处修改已进入最终镜像 |

## 真实服务调用

1. 在单独的 Colab 会话进行小型 JSON/tar 往返控制：14 字节 JSON、10,240 字节 tar
   的远端与下载 SHA256 一致，随后确认会话关闭。该对照只能反驳“所有 tar 下载必然失败”，
   不能证明历史 22 片归档丢失已修复。见 `gpu-transfer-control.json`。
2. 通过实际鉴权的 GPU 路由提交受控视频的两个 2 秒剪辑，任务 `4a244f52e4d5`。
   源文件 406,492 字节，约 10.1 秒进入 error，观察到 quota 类错误；该请求未产生 LLM
   调用。池内 Colab 进入配额冷却，该次请求也未由 Kaggle 后备完成。
   见 `gpu-clips-smoke.json`、`gpu-pool-availability.json`。
3. 在生产容器通过真实 `llm.chat` 执行最小模型探测：`glm-5.3-flash`，HTTP 200，
   1 次尝试，输入 30 / 输出 45 tokens，非空响应，`usage_complete=true`，约 3.51 秒。
   见 `production-model-smoke.json`。这是连通探测，不是前端创建的教材任务，不计入用户任务账本。

原失败任务的 OCR 会话消失、22/22 片剪完但归档未返回的最终根因仍未证实。
本轮先检查下载路径/接口、共享会话提前回收、旧完成状态三项候选；小型传输通过，
真实剪辑重现又被配额挡住。因此没有依据猜测重写或部署 GPU 会话机械，也没有切换 CPU
或清除配额冷却来制造成功结论。

## 账本与服务商口径

只读核对后用户任务仍为 8 个，月度输入 43,653 / 输出 54,945 tokens，GPU 4,517.134 秒，
成本归因 $0.032899，活跃用户任务 0。见 `billing-final.json`。

真实模型连通探测发生在用户任务之外，30/45 tokens 按当前归因单价约 $0.000027；
它不包含在上述任务账本中，因此不能声称本轮上游账户总消耗为零。
历史失败任务前两次空回包用量没有原始记录，未猜测或回填少记金额。

[OpenCode 官方说明](https://opencode.ai/v2/docs/console/go/)将 Go 定义为订阅额度。
应用 token 成本归因、账期订阅付款、额度内单请求新增现金是不同金额；新门禁区分它们，
并未据通道可调用推断“订阅已付款”。

新增订阅示例 `docs/evals/production-failure-cost-subscription-evidence.example.json`
明确为 synthetic。真实结算仍需服务商独立票据与脱敏用量导出。
`historical-gate.txt` 保留旧四项证据的 FAIL，未用本地或生产隔离回放替代真实业务验收。

## 自动验证

- 后端相关回归：**937 passed**，见 `backend-tests-final.txt`。
- 前端集成：**45 passed**，见 `frontend-tests-final.txt`；不是生产浏览器操作。
- CI 实际工作目录及口径的后端 lint：通过，见 `lint-ci.txt`。
- 全量后端类型检查：39 个源文件通过，见 `typecheck-full.txt`；新证据门禁自身类型检查通过。
- 原生产重试复现：修复前失败、修复后通过，见 `production-retry-before.txt` / `production-retry-after.txt`。
- 新诊断测试首次因测试身份未映射被 owner 闸门拒绝；修正夹具映射后，确认原诊断分支
  3 例失败再修复通过，真实红测保存在 `dependency-diagnostic-red.txt`。
- 新增 OAuth 单实例/回环/旧 unit 禁用反例与订阅票据正反例，均接入原有 CI 测试入口。
- 一次从仓库根目录显式指定 backend lint 配置的额外检查出现导入分类差异，保留在
  `lint-final.txt`；最终使用真实 CI 工作目录后通过，没有为错误工作目录改动其他模块导入。

## 生产验收阻断与继续条件

- **GPU 可用性**：须先恢复可用的 Colab 配额或可工作的既有 GPU 后备，再重新验证 OCR、
  剪辑归档、完整教材生成和阅读；当前不能关闭原归档故障。
- **真实 UI**：当前会话没有 computer use 工具。已请求恢复并检索相关连接，未发现可用连接；
  本轮没有真实 Google 登录、前端显式输入 B站链接、新增 B站任务、教材阅读或导出证据。
  按 [lcode-auto 技能](/Users/linmintao/.agents/skills/lcode-auto/SKILL.md) 的
  “切换其他 UI 自动化后端需用户明确指定”，未自行切换其他浏览器自动化后端。
- **服务商独立账单**：没有 OpenCode 已结算账单/用量导出访问能力，插件检索也未提供
  合适的账单连接。仅 API key 和 token 费用公式不足以核验现金支出；不要求用户发送密钥。

## 发布与回退

本轮按经过测试的运行文件制作补丁镜像；核心修复与后续类型修正的清单分别保存在
`release-manifest-a.json` 和 `release-manifest.json`。API 发布前确认无活跃任务，
原 Deployment 快照保留在服务器 `/tmp/v2md-failure-cost-20261021a/deployment-before.json`
且权限为 600，没有复制其配置秘密到本地报告。

需要回退时可将 API 镜像恢复为 `video2md:0.2.0-integration-20261021m`；该版本仍有原计费缺陷。
OAuth 主服务原配置备份在 lcode-cn 的 `/root/video2md-oauth-before-20261021/`，
旧同端口冲突服务应保持停用。未推送 Git 或创建 PR。

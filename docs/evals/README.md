# 全栈评测集

`bilibili-production-review-eval-set.jsonl` 为 B站联调的 **44 条用户批注逐项编号**
`bbr-001` 至 `044`：每项都有输入场景、测量断言、证据文件、应用回归入口和至少
3 个可执行失败反例，共 132 个基础反例。字段按
[Langfuse 数据集指南](https://langfuse.com/academy/datasets) 分离 `input`、
`expectedOutput`、`metadata`。这是仓库内的确定性评测，未上传或改写云端数据集。

```bash
# 完全离线；TLS/412/失效代理、8/100分P、隔离、运维脚本均使用受控边界
backend/.venv/bin/python -m pytest backend/tests/test_bilibili_review_eval_set.py backend/tests/test_bilibili_review_operations.py backend/tests/test_bilibili_test_isolation.py backend/tests/test_bilibili_public_fallback.py -q
python3 scripts/check_bilibili_review_regressions.py
cd frontend/web && npm run test:integration && npm run test:regression
```

常规 CI 已接入上述批注自测、B站真实解析/下载函数的 MockTransport 故障回放、
本地记录哨兵隔离、回环出口配置及安装器幂等检查；前端检查实际 `QueueView`
入口和 `getMedia → HTTP → FastAPI` 的鉴权分段读取，避免只测未接线的工具函数。
没有访问生产、调用模型或新增费用。CI 文件已接线不代表远端 CI 已运行、分支保护已设置，
也不代表所有手动发布入口已被平台强制拦截。

回放器重新计算 run 归属、真实推理记录、用量单价公式、月度差值、GPU 阶段耗时、
重复提交账本和发布清单哈希。布尔值、NaN、缺字段、未知/重复条目不能冒充有效测量。
ASR 的 510/442 字使用 OCR 前快照；OCR 后的 1222/2188 字不混作原始语音转录。
该命令的 PASS 仅表示保存事实与防回退判定通过，**不是一次新的生产验收**。

严格关闭检查当前应返回非零：

```bash
python3 scripts/check_bilibili_review_regressions.py --require-closed
```

保留 `bbr-002` 的 8集历史在线探针缺失、`bbr-022` 的会话过期原因未定位，
以及 `bbr-044` 的登录观察/本地误写前原值未全部确认恢复。离线8/100集正例不补造
在线证据，测试隔离通过也不表示历史数据已恢复。不能通过改 `status=PASS` 销项。

发布工具另有只读预检，实际部署前调用后再决策；它本身不部署：

```bash
python3 docs/testing/2026-10-21/bilibili-remediation/verify_release.py --preflight
```

预检在生产读取所有运行记录，任何非终态（含排队、运行、取消中）、未知状态、健康异常或匿名媒体可读均阻断；
发布后省略 `--preflight` 检查文件哈希、单个就绪API实例、镜像版本与零重启。
此处仅离线测试了该工具，没有重新连接集群。逐批注结果见
[44 项覆盖矩阵](../testing/2026-10-21/bilibili-remediation/annotation-regression-results.json)。

`glm-production-review-eval-set.jsonl` 将 GLM 生产联调的 11 条用户批注逐项固化，
每条使用 `input`、`expectedOutput`、`metadata` 分离场景、验收标准与证据/测试引用，
并附至少 3 个可执行反例。除了已有 24 步业务契约，补充陈旧进度不能证明当前卡死、
临时 CLI 回滚哈希、预算恢复、真实播放进度以及摘要通过不能掩盖完整门禁失败等检查。
历史证据按原始结论保留，未使用的分块回传实验不计为生产修复。

```bash
# 75 项新增证据回放/反例/完整性检查，不访问服务或消耗模型额度
backend/.venv/bin/python -m pytest backend/tests/test_glm_review_eval_set.py -q
python3 scripts/check_glm_review_regressions.py
```

这两个命令的 PASS 只表示保存的案例与判定逻辑未回退，**不是重新访问生产验收**。
业务行为仍由既有阶段反例、LLM MockTransport、API 和前端 HTTP/组件测试覆盖；
前端新增实际 422 与 429 的 HTTP 拒绝用例，检查任务和账本无增量、错误可渲染，
表单不关闭与输入不清空由提交源代码守卫及既有生产截图补充。
11 条批注对应关系及本次命令结果见
[批注防回退矩阵](../testing/2026-10-21/opencode-glm-retest/annotation-regression-matrix.md)。
完整生产阶段门禁仍保留 pps-008、pps-013、pps-016 三项失败/阻塞，不能因回放通过改绿。

`production-pipeline-stage-eval-set.jsonl` 逐步覆盖生产流程的 **24 个检查点**，沿用
`pps-001` 至 `014`，新增任务持久化、来源解析、抽帧、选帧、月账对账、费用明细、
失败诊断、重试幂等、预算拒绝和提交失败表单保留（`015` 至 `024`）。
每项都有输入、预期测量字段、失败场景和可执行反例。每次发现问题，补入对应步骤的反例；
新增步骤使用新编号，历史编号不得重用。方法参考 [Langfuse 的数据集指南](https://langfuse.com/academy/datasets)。

`check_production_pipeline_stages.py` 校验实际字段和跨阶段一致性，不接受仅把 `status`
改成 `pass`。每步须有观察摘要、证据引用和同一个 run_id；缺失、重复、未知步骤、
`blocked`、`not_tested` 或 `fail` 均阻断。GPU 秒数须与 ASR/OCR/GPU clips 耗时及账本
一致；CPU clips 不能算 GPU；输入和输出 token 都须为正；LLM 输入/输出单价须为经确认的正数，GPU 可采用用户确认的免费零价（须提供 `gpu_policy=free` 和 `gpu_free_reason`）；
费用按 usage × 单价复算，精度容差半个 0.000001 USD；同属主隔离窗口的月度差值须
逐项与 run 一致。Colab 30 秒保温加 15 秒巡检的边界不能通过扩大证据中的窗口规避。

两个门禁层次分别运行：

```bash
# 离线防回退：每步正例、反例、缺证据和错误 run 归属；不连接生产、不烧 token
backend/.venv/bin/python -m pytest backend/tests/test_production_pipeline_stage_eval_set.py backend/tests/test_production_billing_eval_set.py -q

# 联网验收结束后，对同一次 run 的脱敏实测证据判定；任一步未过即非零退出
python3 scripts/check_production_pipeline_stages.py docs/testing/2026-10-21/billing-retest/stage-evidence.json
```

常规 CI 已接入逐步门禁自测、解析器运行时、Colab 路由/生命周期与前端提交失败回归。
单独的 `production-pipeline-gate.yml` 可手动指定仓库中的真实证据路径，默认拒绝合成数据，
失败时该验收 job 失败。它仅核验证据，不会发起付费任务或部署；并未声明 GitHub 分支保护
或部署平台已强制要求这个 job。

`production-pipeline-stage-evidence.example.json` 是**合成正例**，其数字及单价不代表生产：
仅使用 `--allow-synthetic` 可以运行门禁自身的离线正例。真实采集须使用 `kind=production`，
不可把合成模板改名充当实测；每次复测另存证据，保留历史失败。证据核验依赖采集者如实记录，
不替代真实 UI、数据库、日志及外部账单核对。

本轮 `312e97ae` 的完整 [逐步证据](../testing/2026-10-21/billing-retest/stage-evidence.json)
与 [结果矩阵](../testing/2026-10-21/billing-retest/stage-regression-matrix.md) 保持失败。
旧 14 步日期快照继续保留，但缺少新字段的旧快照不能用来通过完整门禁。

`production-integration-regression-eval-set.jsonl` 将本轮每一项生产发现分别固化为一个
独立评测。日期证据文件在 LLM、教材、token/cost 或 GPU 计时归集未闭环时必须保持红灯；
`check_production_integration_regressions.py` 只有在全部 case 都有显式通过证据时才返回成功。

`fullstack-regression.json` 是 video2md 的确定性回归集，覆盖认证、用户隔离、队列状态、阅读器媒体、教材库、个人中心、登出和同源 SPA 路由等跨前后端契约。

`textbook-quality-set.jsonl` 是模型“转录 → 教材”质量集。每行一个样本，包含原始转录、金标准要点/顺序/安全不变量、禁止臆造项和权重。新增样本时保持字段稳定；`id` 不复用，原始转录不要包含用户凭据。

`backend-hygiene-regression.json` 是后端卫生回归集，专门覆盖本次发现的
“测试隔离/执行入口问题”：仓库根目录与 `backend/` 目录导入一致、旧版
`state.json` 标量条目兼容、run 上下文成功/异常清理、测试替身恢复以及
`acctkit` vendor 与 SSOT 同步。对应的快速可执行门禁为：

```bash
backend/.venv/bin/python -m pytest backend/tests/test_backend_hygiene.py -q
backend/.venv/bin/python -m pytest backend/tests/test_vendor_acctkit.py -q
```

[`tenant-colab-login-eval-set.jsonl`](./tenant-colab-login-eval-set.jsonl) 是当前登录与个人
Colab 连接评测集，按本轮用户批注拆为 **12 项、29 个失败反例**。输入、通过标准与
来源/回归入口分别放在 `input`、`expectedOutput`、`metadata`；每个反例都有拒绝理由和
实际测试入口。仅使用离线身份与无效凭证，未上传或改写云端数据集。

新集覆盖首次 Google 登录引导与原目标保留、已有连接复用、可跳过连接及状态故障、
普通用户无需管理员名单、凭证/流程隔离、同 Google 账号核对、PKCE/时效/重放/权限边界、
ASR/OCR/剪辑的租户执行上下文、未连接用户不借用服务凭证，以及 LLM URL/API Key 表单
下方的两个平台入口。Colab 进入 `/profile/colab`；Kaggle 仅打开官方登录页，不能据此
声称本项目已保存 Kaggle 凭证或接入其个人 GPU。首次 Colab 权限需显式同意，不是静默授权。

已删除两套旧的登录交互/生产品牌 UI 集合，合并后不再并行维护“回调后无条件直接进入
目标页”的旧流程。保留品牌首页、协议门禁、路由白名单、过期提示、安全回跳、回调错误、
法律页返回与无 mock API 的有效要求；网站登录 `/` 与个人 Colab 连接是两个明确步骤。

| 原编号 | 当前覆盖 |
| --- | --- |
| `login-001..010` | 新集 `001..003`、`012`，已有 API 会话/归属契约；已连接与未连接分支分别断言 |
| `prod-login-ui-001..008` | 新集 `001..003`、`012`，继续执行品牌、协议、路由与安全回跳测试 |
| `login-011..015` | 保留的 OAuth 基础设施、换票不可达、容器部署专项集；历史故障结果不改写为当前通过 |

```bash
# 只检查评测定义、反例与回归引用；不执行产品测试，也不代表生产验收
python3 scripts/check_tenant_colab_eval_set.py

# 再执行前后端真实应用边界测试：独立临时账本、禁用本机密钥与真实 LLM；需要已安装项目依赖
python3 scripts/check_tenant_colab_eval_set.py --run
```

不带 `--run` 只核对定义和测试引用是否存在；加上 `--run` 执行实际前后端测试，
不能用目录检查代替应用测试。离线通过不代表新代码已发布、真实 Google
同意页已完成、个人 GPU 配额可用或生产任务已跑通。历史登录观测保留在日期报告中。

`oauth-infra-eval-set.jsonl` 将该阻塞拆成 5 个基础设施评测：DNS、TCP/TLS、出网/代理、
换票超时和日志脱敏。探针不得提交真实授权码；生产环境只输出错误类别、耗时和 request path。

`oauth-token-exchange-unreachable-eval-set.jsonl` 专门复现和关闭“Google 授权完成但后端换票
不可达”的生产故障。前 6 项不使用真实授权码，依次定位 DNS、TCP/TLS、httpx 实际出口、
受控 HTTP 响应、失败分类和日志脱敏；只有第 7 项需要用户亲自完成 Google 账号/MFA，
最终以 `/me=200`、`/library=200` 且刷新后会话仍在作为恢复标准。仅看到授权页或回到登录页
不能判为通过。

`deployment-oauth-container-eval-set.jsonl` 是必须在生产 API 容器/集群内执行的部署评测集，
覆盖容器 DNS、TCP/TLS、代理继承、NetworkPolicy/安全组、运行镜像版本和换票诊断日志。
本机探针不能替代这些断言；无集群权限时应标记为 `blocked`，不得伪造通过。

`production-fullsite-remediation-eval-set.jsonl` 是 2026-10-21 生产联调的专项评测集，
覆盖真实 Google 回调、`/me`、教材库/任务/个人页、法律页、API 文档封锁、
匿名 `/queue` 封锁、hostNetwork 回环隧道、relay 最小权限和 HTTPX 证书传输。
确定性防回退门禁为 `backend/tests/test_production_readiness_gate.py`，已接入 CI。

`production-model-billing-eval-set.jsonl` 是登录后的真实生产计费评测集，覆盖显式输入
视频链接、任务创建可观测性、LLM/GPU usage 与月度成本归因、幂等防重复计费、教材产物
闭环，以及提交失败时不得静默关闭表单。`prod-billing-010` 至 `018` 进一步逐条固化
2026-10-21 UI 复测事实：退出后重新 OAuth、显式输入、任务计数只增一、处理中阶段、
成功终态、教材增量、run 级用量/费用可见、可用模型与非零单价预检、失败卡诊断信息。
用户已明确授权：本项目后续生产测试允许真实
计费，无需逐次重复确认；仍须使用批准的公开视频、限制测试规模、报告实际成本，并且不得
记录任何登录凭据或模型密钥。结构、正反例与数值断言的防回退测试为
`backend/tests/test_production_billing_eval_set.py`。

生产证据按 `production-model-billing-evidence.example.json` 的脱敏形状填写并执行：

```bash
python3 scripts/check_production_billing.py evidence.json
```

来源全部被风控阻断、前端静默失败、没有持久化 run/提交日志、终态为 `error`、教材数
没有增加、run 级用量/费用不可见、模型渠道不可用、LLM 单价为零或 GPU 零价缺少明确免费依据，或没有真实模型调用与
正数 usage 时，门禁必须失败；`cost_usd=0` 本身绝不能被解释为计费通过。
计费摘要门禁也读取真实数值单价、正数 LLM token、成功推理和费用复算；
`positive_prices_configured=true` 的手填标记不能覆盖全零单价。
该摘要不能代替上方 24 步完整验收，尤其不能替代幂等、预算拒绝、失败详情和真实产物检查。

`colab-source-routing-eval-set.jsonl` 固化海外视频的最快生产路线：`lcode-4c` 只做
控制面与计费，`lcode-cn` 只做 Google OAuth CONNECT 中继，源视频由 Colab 就地拉取；
同时覆盖下载与模型预热并行、流式音频、最小媒体回传、缓存/幂等计费和 warm session。
原先把主生产迁到 `lcode-cn`、再回滚 `lcode-4c` 的 migration/compose 评测已删除，不能
作为恢复方案重新引入。填写脱敏证据后运行：

```bash
python3 scripts/check_colab_source_routing.py --evidence evidence.json
```

任一证据缺失、未通过，或文本命中已否决的中转/迁移路线，门禁均失败。确定性结构与
拒绝逻辑由 `backend/tests/test_colab_source_routing_eval_set.py` 覆盖；真实速度和费用仍须
在生产 smoke 中采集，不允许用静态断言冒充实测。

`colab-warm-lifecycle-eval-set.jsonl` 约束 Colab CLI 的计时生命周期：空队列不预启动；
任务到达后下载与模型加载并行；任务结束仅保温 30 秒供相邻任务复用；巡检周期 15 秒，
因此正常空闲会话必须在 30–45 秒内确认关闭。在途、排队或新鲜 lease 任一存在都禁止
关机；首次 stop 失败必须在下一巡检周期重试，不能重新等待完整保温窗。运行门禁：

```bash
python3 scripts/check_colab_warm_lifecycle.py --evidence evidence.json
```

生产证据必须使用单调时钟记录 session 创建、下载/模型预热起止、lease 释放、stop 尝试和
确认关闭时间；不得记录 OAuth token。确定性生命周期决策由
`backend/tests/test_colab_warm_lifecycle_eval_set.py` 防回退。

`video-resolver-runtime-eval-set.jsonl` 防止“镜像安装了 `yt-dlp`，API 子进程却找不到”
再次阻断真实计费。它从容器启动可执行性、受控 MP4 解析、前端创建并持久化 run，到
usage/cost 必须绑定该 run 四层验收。证据门禁：

```bash
python3 scripts/check_video_resolver_runtime.py --evidence evidence.json
```

确定性防回退测试为 `backend/tests/test_video_resolver_runtime_eval_set.py`；生产证据不得包含
OAuth token、邮箱、模型密钥或完整数据库连接串。

`production-readiness-regression.json` 把 PostgreSQL/API 健康、schema 版本、发布版本、
Git 提交/镜像溯源、Pod Ready/重启/近期错误、Google 出网、旧代理清退、公网路由、
未登录 `/me=401`、配置权限和真实浏览器登录固化为发布防回退集。生产环境只需
填写脱敏证据（参考 `production-readiness-evidence.example.json`），不得记录密钥值：

OAuth relay 证据必须声明 `host-network-loopback` 拓扑：Pod 因 `hostNetwork: true`
可通过 `127.0.0.1:18080` 访问宿主机 SSH tunnel，同时必须证明 tunnel 服务为 active。
这与普通 bridge 容器的非 loopback 要求不同，不得混用。
最终浏览器门禁同时要求回调成功、`/me=200`、`/library=200` 和刷新后会话保持；
仅 token endpoint 探针返回 4xx 不能把完整登录链路判绿。

```bash
# 域名接入前：验证基础设施阶段；pending 的浏览器项不阻断
python3 scripts/check_production_readiness.py evidence.json

# 域名接入后：发布门禁，真实 Google 登录及认证后的 GET /me 也必须通过
python3 scripts/check_production_readiness.py --require-browser evidence.json
```

建议评分口径：先做规则校验（标题、要点覆盖、步骤顺序、禁止项），再将模型输出与原始转录交给 LLM judge 评估忠实性和可读性。总分按样本 `rubric` 加权，任何 `forbidden_claims` 命中时将 `non_hallucination` 记 0，并标记人工复核。

评测分三层：

- `api`：FastAPI 响应、鉴权和数据隔离；
- `frontend`：Vue 路由、页面状态和交互；
- `api+frontend`：前后端联调契约。

运行现有自动化门禁：

```bash
python backend/tests/fixtures/web_integration_api.py "$(mktemp -d)"
npm --prefix frontend/web run test:integration
npm --prefix frontend/web run build
```

外部 Google OAuth、GPU、真实 LLM 不属于这个确定性集；它们应使用独立的联网/容量评测，避免把环境波动混入回归结果。


2026-10-21 用户确认按实际成本归集：免费 Colab GPU 费用记 0，仍完整记录 GPU 秒数；
LLM 按官方单价，并对 `prompt_tokens_details.cached_tokens` 使用缓存命中价格。
不得为了使旧版“GPU 必须正价”门禁通过而虚构 GPU 成本。新结算保存 `cost_pricing` 单价快照，
旧账不追改。MiMo 当前价格来源见 [官方按量价格](https://mimo.mi.com/docs/en-US/price/pay-as-you-go)。

### 2026-10-21 真实 B 站计费失败的四项防回退

`production-failure-cost-eval-set.jsonl` 对应任务 `3c05faf8` 的四个发现，每项均有
行为测试、正反证据和生产证据校验：

| ID | 行为防线 | 生产关闭条件 |
| --- | --- | --- |
| failure-cost-001 | OCR 失败条目不进入模型选帧；剪辑等依赖失败在整理/校对/元数据前拦截；健康条目继续 | 同版本故障条目模型调用为 0，同时健康对照可生成 |
| failure-cost-002 | 每个 HTTP 尝试的最终 usage 累加，包括空回包、解析/流失败和随后 401；同一 SSE 累计块不重复记；缺 usage 标为不完整 | 同一 run 全部请求的输入、输出、缓存 token 与应用记录一致，含空回包及成功重试 |
| failure-cost-003 | ReadTimeout、ConnectTimeout、ProxyError 可见且脱敏；一次性 state 不可重放；重新登录恢复回跳；超时有上界 | 同版本至少连续 3 次真实 Google 登录、`/me=200`、刷新仍有效；出现超时不能关闭 |
| failure-cost-004 | 服务商独立账单核对；禁止公式或共享余额代替实际扣款；遗漏付费空重试也拒绝 | 已结算脱敏导出按账户、run、请求 ID、输入/输出/缓存逐一对应，按量请求现金差额不超 0.000001 USD；订阅模式验证独立已付款账单、账期和额度内请求 |

离线入口（不访问生产、不消耗模型 token）：

```bash
cd backend
uv run pytest tests/test_failure_cost_regressions.py tests/test_production_failure_cost_eval_set.py tests/test_api_contract.py -q
```

生产证据参考 `production-failure-cost-evidence.example.json` 的字段结构；示例明确标为
`synthetic`，不能验收。`attempts` 是同一 run **全部**请求，不允许只填最终成功调用；
同一流多个累计 usage 尾块先取最后快照，再在请求间求和。`supplier_rows.debit_usd`
必须来自服务商独立已结算记录，不能由应用单价复算生成；账户与请求 ID 应做稳定的
脱敏映射以便比对，不保存邮箱、密钥、OAuth 参数、cookie 或正文。服务商采用其他
币种或无法隔离到请求时，应保持未核验并明确差异，不能伪造美元扣款。

OpenCode Go 使用订阅模式；参考 `production-failure-cost-subscription-evidence.example.json`。
该模式要求 `billing_mode=subscription`、独立已付款账单的 ID/账户/订阅 ID/账期/金额、
`role=supplier_invoice` 的文件 SHA256 引用，以及在账期内、属于同一订阅且额度内现金扣款为 0
的逐请求服务商用量导出；所有请求用量仍须匹配，不能漏掉重试。订阅账期付款与 token 成本
归因是独立金额，不要求二者相等，也不能仅凭当前通道配置推断订阅已付款。

证据引用使用相对证据 JSON 的本地文件路径和 SHA256；CLI 校验文件存在和摘要一致。
每个 finding 的 run/release 必须与顶层一致，`--expected-release` 必须使用被测镜像或
revision 的原值。实际门禁：

```bash
python3 scripts/check_production_failure_cost.py evidence.json --expected-release "$TESTED_RELEASE"
```

CI 自动运行离线回归；`production-pipeline-gate.yml` 的手动生产验收必须同时提供原有
24 阶段证据、新增四项证据和被测 release。`check_production_billing.py` 的旧摘要只说明
应用归因校验完成，服务商扣款由新门禁单独校验。当前历史生产证据
`production-failure-cost-evidence-2026-10-21.json` 按真实情况保留四项失败/未核验；
本地修复通过测试不关闭历史失败，也不代表新版本已部署或已完成生产复测。

OAuth relay 防回退同时要求仅 `video2md-oauth-relay.service` 活跃，旧 `oauth-relay.service`
禁用，SSH tunnel 模式只监听 `127.0.0.1`。当前布置从仓库
`deploy/oauth-relay/oauth-relay.service` 安装到规范的 unit 名，避免两个同端口服务并存。

2026-10-22 续测证据为 `production-failure-cost-evidence-2026-10-22.json`：
同一生产版本连续三次真实 Google 登录、认证后及刷新后的 `/me=200`，故 003 通过。
真实前端 B站合集提交产生任务 `f1fff0b8`，两集转写被 Colab 配额/Kaggle 空凭证阻断，
模型用量与费用增量均为 0；它没有覆盖 OCR/clips 故障与健康对照、真实空回包重试、
服务商结算，因此 001/002/004 继续拒绝，完整门禁仍 FAIL。见
`docs/testing/2026-10-22/production-ui-followup/report.md`；不改写前一天的历史失败矩阵。


2026-10-22 Kaggle 凭证/手机验证续测见
`docs/testing/2026-10-22/kaggle-token-integration/`。新式 access_token 认证与真实
两张 T4 的 CUDA 探针通过；后续 GPU 作业排队不当作产出成功。前端已显式提交
新任务 `dd586072`（前两集）。本期服务商账单和近 24 小时未截断的请求导出已取得，
原件只保存在仓库外私密目录；导出含 requestHeaders，不得直接复制入库。

新增只读核验入口（不调用模型、不修改生产账目）：

```bash
python3 scripts/reconcile_opencode_usage.py "$PROVIDER_EXPORT" "$APP_EVIDENCE" \
  --run-id "$RUN_ID" --output reconciliation.json
python3 -m pytest backend/tests/test_opencode_usage_reconciliation.py -q
```

该入口拒绝截断、重复请求、缺失用量和非法计数，将导出中单列的缓存输入归入总输入，
输出只含白名单记账字段。退出码 0 仅表示请求用量和 token 成本归因匹配；现金结算仍需
独立账单。本次 `be188f78` 对平；`3c05faf8` 输入少记 18,694、输出少记 8,000、缓存少记
9,344 tokens（缓存是输入子集，不可相加计算总漏记）。未将差异伪装成已回填或已结算。
原四项门禁继续保留 001/002/004 未关闭，不用新探针替代同版本完整生产验收。


### Kaggle 与账目续测：逐项评测和强制防回退

`kaggle-billing-followup-eval-set.jsonl` 将 2026-10-22 续测结论拆为 12 个独立项，
每项有 input、expectedOutput、metadata、至少 4 个独立反例；共 12 个正例、56 个反例。
结构参考 [Langfuse 数据集指南](https://langfuse.com/academy/datasets)，本轮仅使用本地
脱敏资产，没有创建云端数据集或调用模型。

| ID | 独立评测项 | 拦截的回退 |
| --- | --- | --- |
| kfi-001 | 新式令牌配置和认证 | 空凭证、令牌不可读、缺用户名、秘密值入证据 |
| kfi-002 | T4/CUDA 真实分配 | CPU 冒充 GPU、零设备、旧唯一标识、镜像和运行源码不匹配 |
| kfi-003 | 登录后显式输入并单次提交 | 自动提交、提交其他 URL、重复创建、旧 run 借证据 |
| kfi-004 | 合集前两集逐条下载 | 漏下载、重复集号、跳过条目、把前两集宣称为全合集 |
| kfi-005 | 排队安全与待结算 | 排队被当作完成、阻塞后仍调用模型、等待被冒充物理 GPU 占用 |
| kfi-006 | 辅助作业清理 | 客户端超时冒充远端终止、无限观察、同名 kernel 新版本业务被误取消 |
| kfi-007 | 新教材和阅读器交付 | 零字节、404、旧教材冒充本次产物、运行中宣称交付成功 |
| kfi-008 | 完整独立用量导出 | 1,000 条截断、重复请求、缺 usage、跨 run、秘密请求头入库 |
| kfi-009 | 输入/输出/缓存逐请求对平 | 少记请求、漏缓存、数量和总和不一致、归因冒充实付 |
| kfi-010 | 原任务少记 8,000 输出的真实坏例 | 真实差额被抹零、缓存遗漏、未回填却宣称已修复 |
| kfi-011 | 历史回填验收 | 无审计、无来源摘要、重复回填、误改其他 run、产生现金扣款 |
| kfi-012 | 完整生产验收 | 单项通过冒充全量通过、跳过旧四项门禁、换用其他版本或虚构矩阵 |

离线回归：新增 `backend/tests/test_kaggle_followup_eval_set.py` 182 项，逐项检查正反例、
缺失/重复/未测/错摘要/错 run/错版本、来源内容绑定和 CI 接线；新增 Vue 组件集成回归
`frontend/web/tests/kaggle-followup.integration.test.mjs` 6 项。已有 GPU 14/11 项配置和 worker
测试由 gpu-router 自身 CI 的 `tests/test_*.py` 循环执行，此处没有复制生产适配器。

纠正上轮记录：CI 使用固定 pytest 文件列表，并非自动发现全部新增文件；此前 12 个
`test_opencode_usage_reconciliation.py` 测试尚未被列入。本轮将它和新的 182 项一起加入
现有 CI 必跑步骤，并新增对应 lint、前端通配命令接线回归。未执行远端 GitHub Actions，
也没有核验 GitHub 分支保护设置；此处“接入 CI”指仓内 workflow 与本地执行验证。

```bash
backend/.venv/bin/python -m pytest backend/tests/test_kaggle_followup_eval_set.py \
  backend/tests/test_opencode_usage_reconciliation.py \
  backend/tests/test_production_failure_cost_eval_set.py \
  backend/tests/test_failure_cost_regressions.py -q
python3 scripts/check_kaggle_followup.py \
  docs/evals/kaggle-billing-followup-evidence-2026-10-22.json \
  --expected-release video2md:0.2.0-failure-cost-20261021b \
  --expected-gpu-release gpu-router:0.2.3-kaggle-token-20261022b \
  --expected-run-id dd586072
```

手动生产 workflow 新增必填续测证据、GPU release 和业务 run，三个门禁分别执行；前一门禁
失败仍运行后续检查，任一失败即工作流失败。CLI 没有允许合成证据关闭生产的参数。
每行 metrics 还必须与摘要绑定的观察源内容相同；GPU 探针绑定镜像、运行源码和唯一标识，
完整验收绑定旧四项矩阵原件及其内层证据，拒绝仅改 status 或塞一个无关文件冒充证据。

新生产矩阵是上一轮 **历史快照** 的脱敏回放，保留 kfi-007（交付）、011（回填）、012（全量）
未关闭，不能据此声称此刻任务仍在排队或已经完成。本轮未重新运行 GPU/模型/生产任务，
未回填账目，未覆盖旧报告和证据摘要。详情见
`docs/testing/2026-10-22/followup-eval-guards/report.md`。

2026-10-22 生产联调续修：`production-resolution-eval-set.jsonl` 增加 8 类正反例，分别锁定 Kaggle ASR 解码依赖兼容、历史漏记的审计回填、同任务补跑新增用量、转写超时等待计时。`backend/tests/test_usage_backfill.py` 在 CI 的计费防回退步骤执行；GPU 侧生成 worker 的约束位于相邻 gpu-router 仓 `backend/tests/test_kaggle_runtime_inputs.py`，由其 CI 的 `test_*.py` 循环执行。真实生产结果和未通过项继续单独记录，离线绿灯不能替代教材交付或服务商付款凭据。

历史回填工具 `scripts/backfill_opencode_usage.py` 默认只准备计划；应用计划必须传 `--apply --backup-dir <private-directory>`。它要求完整独立导出、终态未删除记录、原记录摘要一致；Postgres 事务锁定目标行，JSON 存储使用现有文件锁。原始导出和完整备份保持在仓外，公开报告仅保留计费字段。计划不代表服务商现金扣款。

同轮补充第 5 类：备份目录与受限初始化容器的权限兼容。回填备份目录为 0750（仅既有应用组可遍历），完整记录文件始终为 0600；文件内容摘要须保持不变。仓外原始导出目录仍保持 0700。

补充真实 OCR 运行依赖评测：RapidOCR 需要单独安装 ONNX Runtime；provider 不接受逐帧错误或缺行结果作为成功。修复后先跑单帧真实 OCR，再执行完整合集。上传包固定为归档和清单两个对象，worker 读取平台自动解包的图片，避免逐图片上传的时延回退。

2026-10-22：按用户确认新增 `subscription_receipt` 验收模式：已付 CNY 凭据与完整逐请求 USD 成本归集分别核对，同账户、产品及实际账期。保留按量现金扣款模式；不虚构 subscription ID 或汇率。正例与 20 类反例见 `test_production_failure_cost_eval_set.py`，新增 `pri-009`。

2026-10-22 本轮最终新增评测集为 `production-resolution-eval-set.jsonl` 的 pri-001..013；后续追加公开证书缓存、图链归一、评测隔离和媒体及时就绪。`production-failure-cost-resolution-2026-10-22.json` 保留两项真实故障证据缺口，`kaggle-billing-resolution-2026-10-22.json` 保留完整验收失败；不得以本地 533 项通过替代严格生产门禁。

2026-10-22 逐条批注补全：[`production-resolution-annotations.jsonl`](production-resolution-annotations.jsonl)
将这轮 **43 条**批注分别记录为 `pra-001..043`，与批注序号一一对应（也包括第19条仅选择了文本的批注）。
每条包含来源消息与原话摘录、可核对的解释、SHA256绑定的公开历史证据、至少3个可执行反例、精确测试函数及新生产验收所需证据。
合计185个反例；43次正常回放和43个缺失字段检查分别运行。测试还重新求和服务商请求行、核对两讲22个媒体文件、登录时序、GPU随机标识和源码绑定，避免只看人工PASS标签。

以下是离线回归命令，不调用模型/GPU，不读取私有发票或完整请求导出：

```sh
python3 scripts/check_resolution_annotations.py
cd backend && uv run pytest tests/test_resolution_annotations.py tests/test_resolution_operations.py -q
```

已接入现有CI的test/lint步骤；本地新增323项、连同原有相关回归856项通过。
相邻gpu-router仓新增 `backend/tests/test_kaggle_review_state.py`，由其既有 `tests/test_*.py` CI循环执行；本地15项状态机检查与原有30项worker/输入检查通过。
发布脚本测试直接运行既有 `deploy_image.py`，仅替换SSH和写文件边界：环境、Secret引用、卷、init权限、资源有意外差异时不得apply；旧镜像并发变更、预检失败、rollout失败也不能记成功。
收集检查确认新测试没有顶层加载项目配置。远端CI、分支保护和新生产部署本轮没有执行，不能据此宣称已通过。

历史说法按后续实测更正：图片解包目录为 `frames/`，逐图上传被归档加清单两个对象替代；缓存是否复用按当次阶段日志判断；约18分钟仅是稀疏观测窗口。
历史533项和523项是旧轮次统计，不当作当前测试门槛；4个独立应用span与第5个选帧剩余量重建也不混写。
原生产证据不重写；[逐条矩阵及复核结果](../testing/2026-10-22/annotation-eval-guards/annotation-coverage.json)另存本轮目录。

严格验收仍要求真实剪辑故障、真实空回包重试和完整媒体及时就绪观测，以下命令当前应返回1并列出3项OPEN：

```sh
python3 scripts/check_resolution_annotations.py --require-closed
```

这是[Langfuse评测集方法](https://langfuse.com/academy/datasets)的本地应用：生产观察、预期断言与来源元数据分开保存，没有上传或修改云端数据集。

2026-10-22 逐条响应批注评测：[`production-ui-annotations.jsonl`](production-ui-annotations.jsonl)
将本轮 8 条批注记录为 `pua-001..008`，每条包含 `input`、预期检查、历史证据来源和至少 3 个反例；
对应的离线回放脚本是 `scripts/check_production_ui_annotations.py`。该数据集覆盖个人配置保存与隐私、
线上版本、OCR 耗时、阅读器媒体清单、及时加载、视频实播和重复提交计费。它只读取仓内公开脱敏证据，
不上传 Langfuse，也不把离线回放当作新的生产验收。

```sh
python3 scripts/check_production_ui_annotations.py
python3 scripts/check_production_ui_annotations.py --require-closed
```

本轮回放结果见 `docs/testing/2026-10-22/production-ui-annotation-evals/`：离线回放通过；严格门禁按真实证据保留
`pua-001/002` 的浏览器动作审计缺口，以及 `pua-005/006/007/008` 的媒体就绪/实播缺口。重复提交的用量和费用
核对本身通过，只有实播条件未满足时整条 `pua-008` 才保持未关闭。

## 前端质量评测集（2026-10-22）

`frontend-quality-eval-set.jsonl` 固化前端评审的四项整改，共 16 条用例、56 个可执行反例：

| 项 | 用例 | 约束 |
| --- | --- | --- |
| README | `fq-readme-001`～`005` | 链接与 `src/` 路径存在、统一 `npm ci`、脚本全部有文档、5 张截图齐全且为 PNG、不再描述 mock 层 |
| 清理假功能 | `fq-mock-001`～`004` | 源码无 mock/模拟文案、已删除的支付/草稿/网盘/Notion/分享/多格式导出不回归、下载受 `bundle_ready` 约束、E2E 断言页面无 mock 文案 |
| Lint/格式 | `fq-lint-001`～`005` | 配置严格且依赖精确锁定、真实 ESLint/Prettier 能报出探针问题、`v-html` 必须带理由豁免、CI 按序运行全部门禁 |
| E2E | `fq-e2e-001`～`002` | 登录/提交/阅读关键断言齐全、连接真实 FastAPI 夹具且只拦截 Google 授权页、CI 禁止 `test.only`、不重试 |

每条用例分为 `input`（场景与检查器）、`expectedOutput`（零违规与验收标准）、`metadata`（整改项、回归入口、反例）。
回归入口在仓库当前状态下必须零违规，且对每个反例补丁（在内存中改写或删除文件，或替换 ESLint/Prettier 探针）必须报出违规，
因此放宽检查器同样会使测试失败。

```bash
cd frontend/web
npm run test:regression   # 评测集 + 反例，无需后端
npm run lint && npm run format:check
npm run test:e2e          # 需 backend/.venv；本机已有 Chromium 时设 PW_CHROMIUM_PATH
```

E2E 另做过变异验证：去掉阅读器对 `blob:` 地址的恢复，或去掉队列对重复提交的去重，都会使对应用例失败。
记录见 [frontend-quality-results-2026-10-22.md](frontend-quality-results-2026-10-22.md)。

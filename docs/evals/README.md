# 全栈评测集

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

`frontend-login-interaction-set.jsonl` 是登录交互评测集，覆盖协议门禁、OAuth
跳转、成功/失败回调、未认证路由保护、真实登录后的 `/me` 会话稳定性、退出登录
以及“授权后仍不可达”的生产诊断。确定性用例使用 mock OAuth；真实账号只允许在
`production-smoke` 用例中由用户亲自完成授权，评测产物必须脱敏，不记录邮箱、code、
state、id_token 或 MFA 信息。

实时账号选择结果见 `login-interaction-results-2026-10-20-live.md`：本轮已验证
Google account chooser 和授权回跳，实际阻塞点是生产后端 Google 换票阶段的
`ConnectError`。

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

`production-readiness-regression.json` 把 PostgreSQL/API 健康、schema 版本、发布版本、
Google 出网、旧代理清退、公网路由、配置权限和真实浏览器登录固化为发布防回退集。生产环境只需
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

# 四项生产发现的评测集与防回退

日期：2026-10-21（Asia/Shanghai）。原始生产任务：`3c05faf8`。

结论：四项均已补充可执行评测、防回退测试和 CI 入口；本地问题修复及回归通过。
本轮没有部署、没有新增真实模型调用，也没有取得服务商账单。旧生产失败记录未关闭。

| 发现 | 本轮实现 | 状态 |
| --- | --- | --- |
| OCR/剪辑失败后仍消耗模型 | 模型选帧排除失败条目并保留合集序号；整理、校对、元数据前检查依赖及远端剪辑产物。健康视频和图文帖作为正向对照 | 本地修复和测试通过，生产待复测 |
| 空回包重试漏记用量 | 每个 HTTP 请求的最终 usage 在请求间累计；兼容空回包、解析/流失败、全部失败及随后 401；同一 SSE 累计块不重复计算；逐次 usage 与完整性进入 span，缺 usage 不伪造 | 本地修复和测试通过，生产待复测 |
| Google 换票间歇超时 | ReadTimeout/ConnectTimeout/ProxyError 回放，错误提示明确重新登录；验证 state 单次消费、cookie 清除、凭据不泄漏、重放不再换票、新登录恢复目的页面；验证 3–60 秒配置边界 | 异常处理防回退通过；实际网络稳定性仍待真实登录验证 |
| 服务商实际扣款未核验 | 独立账单门禁要求已结算导出、证据摘要、账户/run/请求/输入输出缓存一一对应；不得漏掉空回包重试；不允许公式、共享余额或放宽容差冒充扣款 | 门禁测试通过，真实服务商扣款仍未核验 |

## 资产

- 评测集：`docs/evals/production-failure-cost-eval-set.jsonl`，一项发现对应一个稳定 ID。
- 可执行行为测试：`backend/tests/test_failure_cost_regressions.py`（17 个用例）。
- OAuth 真实路由回放：`backend/tests/test_api_contract.py` 中新增 7 个用例。
- 正反证据与缺证据测试：`backend/tests/test_production_failure_cost_eval_set.py`（82 个用例）。
- 新门禁：`scripts/check_production_failure_cost.py`。
- 示例：`docs/evals/production-failure-cost-evidence.example.json`，明确是 synthetic，不能验收。
- 历史生产反例：`docs/evals/production-failure-cost-evidence-2026-10-21.json`，保留原始失败/未核验结果。
- CI 自动执行行为及证据门禁自测；手动生产验收必须同时提供原有阶段证据、四项证据和被测 release。

共新增 106 个测试实例。现有脏工作区中的其他修改保留；本轮未修改 `bilibili.py`、
`download.py`，未修改生产配置、定价或部署。旧计费摘要的成功文案已明确限定为应用成本归因，
服务商实际扣款必须通过新门禁单独核验。

## 验证证据

- `behavior-red.txt`：修复前 13 failed / 3 passed，确认用例能抓住问题。
- `oauth-red.txt`：补充提示前 3 failed / 4 passed，确认可见恢复提示受保护。
- `backend-tests.txt`：890 passed，覆盖本次变更及相关已有生产/计费/图文/协议测试。
- `graph-tests.txt`：脚本式编排测试全部通过，包括 deliver 全链路。
- `frontend-tests.txt`：45 passed。Vite 报已有调试端口占用警告，但测试无失败。
- `lint.txt`：改动代码和测试 lint 通过。
- `typecheck.txt`：改动的三个运行时代码文件及新门禁，在隔离依赖检查下通过。
- `typecheck-with-imports.txt`：全依赖检查仍有 8 项已有错误，来自 `bilibili.py` 和 `download.py`，本轮未改动，未宣称完整 CI 已绿。
- `historical-production-gate.txt`：旧生产证据四项继续 FAIL（退出码 1），这是预期的反例回放。

生产关闭规则：证据必须对应被测 release/run，引用的文件必须存在且 SHA256 匹配；
`pass` 标签不能覆盖数值不一致、缺证据或未核验状态。OAuth 生产稳定性要求至少三次
连续真实登录和刷新有效；账单要求独立已结算的实际扣款。没有这些证据时继续阻断，
不以本地回放替代生产复测。

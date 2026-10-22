# 生产模型计费 UI 复测（2026-10-21）

## 测试范围

- 生产站点：`https://ferosse.tech`
- 从退出已有会话开始，重新执行 Google OAuth 登录。
- 登录成功后进入任务队列，在可见前端显式输入受控短视频：
  `https://ferosse.tech/test-assets/model-billing-smoke.mp4`
- 点击“开始解析”，观察任务阶段直至终态，并检查个人中心用量与教材库产物。

## 结论

**FAIL：生产任务能够创建并开始转录，但最终转换失败，未形成教材，也没有可见的模型计费。**

本轮未复用历史失败任务。提交前任务总数为 2，提交后增至 3；新任务先显示
“转录处理中”，随后转为“转换失败”。个人中心仍显示 0 本教材、AI 转换时长 0 / 0 小时。
前端任务卡未展示 run_id、失败错误、token usage 或 cost，无法从用户界面证明模型调用和结算。

## 用例结果

| 用例 | 结果 | 证据 |
|---|---|---|
| 退出已有会话 | PASS | 从 `/library` 退出后进入 `/login` |
| 登录页加载 | PASS（有观测差异） | 首次 AX/截图为空，但 DOM 已存在登录表单；干净标签可读取完整登录页 |
| Google OAuth 发起 | PASS | 到达 Google account chooser |
| 账号选择与回跳 | PASS | 登录后自动回到 `/library` |
| 前端显式输入视频链接 | PASS | 任务页输入框显示完整受控 MP4 URL |
| 新任务创建 | PASS | 全部任务 2 → 3，转码中 0 → 1 |
| 管线开始执行 | PASS | 新任务显示“转录处理中” |
| 任务终态 | FAIL | 新任务最终显示“转换失败” |
| 失败反馈可诊断性 | FAIL | 仅显示“任务已结束，可重新排队”，无错误原因和 run_id |
| 教材产物 | FAIL | 个人中心仍为 0 本，教材库无新增内容 |
| 模型 usage/cost | FAIL | UI 未显示 token/cost；AI 转换时长仍为 0 / 0 小时 |
| 模型计费生产验收 | FAIL | 无模型成功调用、正 token usage、非零定价结算和教材闭环证据 |

## 与同日历史复测的关系

同日较早的生产报告已记录：受控 MP4 可跑通下载和 Colab GPU 阶段，但 LLM 网关返回
HTTP 403“该渠道已被禁用”，且生产三项单价均为 0。本轮 UI 行为与该未解除门禁一致，
但由于当前任务卡不暴露后端错误，本报告只把 403 作为历史关联信息，不将其冒充为本轮
直接观测。

## 阻塞与复测门禁

1. 为生产密钥启用至少一个可用 LLM 渠道，或配置可工作的 OpenAI-compatible
   base URL、模型和密钥。
2. 配置非零且经审批的 LLM 输入、LLM 输出、GPU 生产单价。
3. 前端失败卡至少展示可复制的 run_id、错误摘要和失败阶段；计费明细应展示 run 级
   `llm_in_tok`、`llm_out_tok`、`gpu_s`、`cost_usd`。
4. 修复后从登录重新提交一条新任务；通过条件是教材可见、Markdown 非空、LLM token
   usage 为正、cost 已结算且月度汇总包含该 run。

## 防回退映射

本轮每项观测均已固化为独立评测：重新登录 `prod-billing-010`、显式输入
`prod-billing-011`、只新增一个任务 `prod-billing-012`、处理中阶段
`prod-billing-013`、拒绝错误终态 `prod-billing-014`、教材增量 `prod-billing-015`、
run 级 usage/cost 可见 `prod-billing-016`、模型渠道与非零单价预检 `prod-billing-017`、
失败诊断信息 `prod-billing-018`。确定性门禁由
`backend/tests/test_production_billing_eval_set.py` 覆盖。

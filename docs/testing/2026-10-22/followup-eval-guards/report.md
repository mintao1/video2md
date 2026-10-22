# 逐项评测集与防回退（2026-10-22）

用户批注中的续测结论已拆成 12 个独立评测项，每项都绑定明确通过条件、4–5 个反例、可执行校验和生产证据。新增 182 项后端门禁回归、6 项 Vue 组件集成回归；相关后端 310 项通过，前端 6 项通过，lint 通过。

这是离线评测与 CI 配置工作。没有再次调用 GPU/模型、改动生产账目或部署；生产矩阵使用上一轮留存的历史快照，不能作为此刻任务实时状态。旧报告、旧证据与其摘要均未覆盖。

## 逐项覆盖

| ID | 独立项目 | 反例数 | 上轮快照结果 |
| --- | --- | --- | --- |
| kfi-001 | 新式令牌配置与认证 | 4 | 历史快照检查通过 |
| kfi-002 | 真实 T4/CUDA 与唯一标识 | 5 | 历史快照检查通过 |
| kfi-003 | 登录后前端显式输入和单次提交 | 4 | 历史快照检查通过 |
| kfi-004 | 合集前两集逐条下载与范围 | 4 | 历史快照检查通过 |
| kfi-005 | GPU 排队期间不误报产出或费用结算 | 5 | 历史快照检查通过 |
| kfi-006 | 辅助作业有界观察、远端终态与业务保护 | 5 | 历史快照检查通过 |
| kfi-007 | 新任务教材正文与阅读器交付 | 5 | 未关闭：无新教材 |
| kfi-008 | 完整独立导出与脱敏 | 5 | 历史快照检查通过 |
| kfi-009 | 逐请求输入输出缓存和成本归因对平 | 4 | 历史快照检查通过 |
| kfi-010 | 原任务漏记 8000 输出 tokens 真实坏例 | 5 | 历史快照检查通过 |
| kfi-011 | 历史账目回填的审计、幂等和隔离 | 5 | 未关闭：未回填 |
| kfi-012 | 四项生产验收完整关闭条件 | 5 | 未关闭：全量验收未齐 |

评测集：`docs/evals/kaggle-billing-followup-eval-set.jsonl`，采用 input / expectedOutput / metadata，负例有独立预期标签。历史回填、教材交付等正例为明确标注的合成验收样例，用于测试检查器；不声称它们在生产发生。生产证据另存 `docs/evals/kaggle-billing-followup-evidence-2026-10-22.json`，kind=production、scope=historical_snapshot_replay_not_live_verification。

原四项问题的评测和防回退继续由 `production-failure-cost-eval-set.jsonl` 及旧严格门禁承接。新 kfi-012 调用旧检查器，要求同一被测 release、同一业务 run，且核对原矩阵文件内容与内层证据摘要；不能换用一个自洽但属于其他 run 的全绿矩阵。

## 防回退落实

- 每个 case 都测正常、反例、缺项、重复项、未测、空指标、缺引用、错误摘要；不接受只改 status 的“通过”。
- API release、GPU release、业务 run 必须匹配调用者指定值。每行指标必须与 SHA256 引用的观察源内容一致，不能附一个无关的真文件作证。
- CUDA 探针另行绑定所测镜像、实际运行源码摘要和本次唯一标识；手机验证、配置健康或请求 GPU 不能替代真实设备结果。
- 对账复用真实只读核验工具回放脱敏请求，明确捕获原任务少记 18,694 输入 / 8,000 输出 / 9,344 缓存 tokens。缓存属于输入子集。token 归因不当作现金扣款，未知用量不补成已核验的零。
- 辅助清理按 kernel+version 区分。相同 kernel 的旧诊断版本被停止，不代表新业务版本可以误取消；只观察超时也不等于远端已终止。
- Vue 测试编译真实 QueueView/TaskCard 并运行 setup 与 SSR 渲染：排队/处理中隐藏旧阅读下载入口；零用量长等待仍待结算；剪辑失败显示恢复；健康教材可读；空输入不提交，显式 URL 才提交。该离线组件检查不替代真实浏览器验收。

## CI 缺口修正

上一轮“新增测试由 pytest CI 自动发现”的表述不准确：CI 实际固定列举文件，原 12 个独立对账测试没有列入。首次新增 CI 接线回归得到 **152 pass / 1 fail**，明确抓住此缺口，见 `initial-tests.txt`。本轮已把新 182 项和原 12 项共同列入现有必跑步骤，并给新门禁/测试加 lint。测试还检查工作流命令、不可 continue-on-error 和前端通配执行入口，防止只有文件没有执行。

手动生产 workflow 新增续测矩阵、GPU release、业务 run 的必填输入；前一门禁失败后仍运行后续门禁，任一失败即工作流失败。GitHub Actions 未远端触发，GitHub 分支保护设置未核验；本轮验证的是仓内 CI 配置和本地等价命令。

最初新脚本 lint 两项失败（未使用 import、shebang 文件不可执行）均归因于本轮新增文件，已移除 import 并设置执行位，复查通过，没有绕过检查。

## 实测与复现

- `regression-tests.txt`：相关后端 **310 passed**；其中新文件 182 项，后续针对最终数据集复查见 `final-followup-tests.txt`。
- `frontend-tests.txt`：新增前端 **6 passed**，没有真实付费请求。
- `lint.txt`：All checks passed。
- `production-gate.txt`：真实快照回放 **退出码 1**，仅 kfi-007 / 011 / 012 拒绝，全部证据摘要与内容绑定检查通过。它们依次是教材交付、历史回填、完整验收；这三个生产事实不能通过合成样例关闭。
- `verification-summary.json`：机器可读结果与执行边界。

```bash
backend/.venv/bin/python -m pytest backend/tests/test_kaggle_followup_eval_set.py \
  backend/tests/test_opencode_usage_reconciliation.py \
  backend/tests/test_production_failure_cost_eval_set.py \
  backend/tests/test_failure_cost_regressions.py -q
(cd frontend/web && node --test tests/kaggle-followup.integration.test.mjs)
python3 scripts/check_kaggle_followup.py \
  docs/evals/kaggle-billing-followup-evidence-2026-10-22.json \
  --expected-release video2md:0.2.0-failure-cost-20261021b \
  --expected-gpu-release gpu-router:0.2.3-kaggle-token-20261022b \
  --expected-run-id dd586072
```

最后一个命令预期 FAIL，不能使用“允许合成”参数关闭生产，CLI 不提供该参数。将来取得真实交付、审计回填及同 run 的四项完整证据后，应另存新矩阵和新快照，保持历史证据不变。

数据集结构参考 [Langfuse 官方指南](https://langfuse.com/academy/datasets)。本轮未创建云端 Langfuse 数据集，也不需要新增密钥。

## 规范与偏离

触及 GATE-01、GOV-03。未新增平台 HARD 规则、未改模板、未绕过门禁，未提交、推送或创建 PR。偏离清单：无。

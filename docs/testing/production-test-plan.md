# video2md 生产测试计划

## 目标

本计划把现有后端、前端、生产评测、计费、Colab、灾备和可观测性检查统一为发布流程。确定性回归不访问真实模型、OAuth、GPU 或生产数据；生产验收使用已授权、已脱敏的真实回放，并单独记录成本和证据。

## 测试分层

### L0：静态与构建

每次 push、PR 和发布候选版本运行 Ruff、mypy、前端构建、Docker 构建、API 契约、前端集成/回归、依赖和 hard-constraint 检查。构建失败、类型错误、敏感文件进入制品或生产清单不满足硬约束时阻断。

### L1：离线确定性回归

沿用现有 CI 覆盖认证与 owner 隔离、队列/取消/重试/幂等、解析/下载/转写/OCR/抽帧/剪辑、LLM usage 与费用、Markdown/媒体、deliver 生命周期、Colab 路由与预热、前端状态、watchdog、备份、SSE 和多副本。新增 `test_production_quality_eval_set.py` 检查评测集和证据契约。该层要求 100% 通过，不能改写历史失败记录。

### L2：生产发布前验收

对目标 release、commit、镜像、schema 和配置快照执行健康/迁移、匿名面与反代、真实登录与刷新、A/B 租户隔离、提交/队列/SSE/终态、失败重试/取消/去重/预算、真实视频到教材、遥测/告警/deadman、deliver 删除/TTL，以及备份恢复和回滚检查。

### L3：持续观测

发布后观察 7 天 run 成功率、提交接纳率、SSE 首事件 p95、bundle 投递率、队列/活跃任务、watchdog/熔断/LLM 失败、每用户 usage/cost、产物质量抽样和失败原因分布。

## 硬门禁

| 类别 | 要求 |
| --- | --- |
| 安全、鉴权、owner 隔离、路径边界 | 0 个高危失败 |
| 核心链路 | 每个真实样本都有 run、阶段、终态和产物 |
| run 成功率 | 7 天窗口 ≥ 99% |
| 提交接纳率 | ≥ 99.9%，设计性 429 不计失败 |
| SSE 首事件 | p95 < 3 秒 |
| bundle 投递 | ≥ 99% |
| 内容禁止项 | 0 次幻觉、拒答、凭据泄漏或未授权外链 |
| 忠实性/覆盖 | 平均 ≥ 0.90/0.85，单样本不得低于 0.80/0.75 |
| 结构完整性 | 成功样本均有标题、章节、时间戳、转录本链接和有效媒体引用 |
| usage/cost | 每次真实调用绑定唯一 run，单价和费用可复算 |
| 灾备 | manifest、历史 run、服务检查通过；RPO ≤ 24h、RTO ≤ 2h |

错误预算消耗超过一半时冻结非修复性发布；任何 P0/P1 安全、隔离、计费或凭据问题直接阻断。

## 评测集与数据治理

`docs/evals/production-quality-eval-set.jsonl` 第一版目标至少 30 条已授权、已脱敏的真实生产回放，覆盖来源、语言、内容类型、时长、OCR、LLM 重试/降级、失败重试及 vault/deliver。缺少真实覆盖时标为 `blocked`，不得用合成数据伪造通过。编号只增不复用。

每行包含 `id`、`input`、`expectedOutput`、`counterexamples` 和 `metadata`。规则校验先于 DeepEval/RAGAS 语义评分；禁止项命中时样本失败并进入人工复核。

## 证据与执行

生产证据必须记录 release、commit、镜像、schema、环境、run_id、阶段时间/状态、HTTP/SSE、usage/cost、产物哈希、删除结果、失败原因、重试、告警、结论和回滚建议。不得包含 OAuth token、API key、邮箱、完整 DSN、Cookie、带凭据 URL 或可识别用户信息。

```bash
cd backend
uv run pytest tests/test_production_quality_eval_set.py -q
uv run pytest tests/test_production_readiness_gate.py tests/test_production_pipeline_stage_eval_set.py tests/test_production_billing_eval_set.py tests/test_production_failure_cost_eval_set.py -q
cd ..
python3 scripts/check_production_quality_eval.py --allow-example docs/evals/production-quality-evidence.example.json
```

质量评测通过不能替代鉴权、计费、部署和灾备门禁。真实生产调用沿用现有受控授权和成本规则，仅使用批准的视频和最小任务规模。

# 每一步的评测与防回退

适用 run：`312e97ae`。本文件保留实测结果；离线门禁自测通过不等于生产已恢复。

24 个步骤，76 个具体失败反例；本轮逐步证据：pass=6、fail=7、blocked=8、not_tested=3。

| 编号 | 步骤 | 本轮状态 | 已采集 / 尚缺证据 |
|---|---|---|---|
| pps-001 | Google 登录及回跳 | pass | 退出、Google 重新登录、保护页与刷新后会话均已观察 |
| pps-002 | 前端显式输入受控视频链接 | pass | 清空输入框后显式输入并读回受控 URL，点击开始解析 |
| pps-003 | 视频下载 | blocked | 已观察下载 41 秒 640×360 视频；本轮未保留本地文件字节数与精确下载耗时 |
| pps-004 | Colab ASR | blocked | Colab turbo 返回 2 段、置信度 0.97；缺少时间戳有效性原始断言 |
| pps-005 | Colab OCR | pass | Colab OCR 完成 5 帧，路由器记录约 82 秒；命中 0/5 不代表识别质量通过 |
| pps-006 | clips | blocked | clips 完成 1/1，CPU 执行；未采集独立的 clips 精确耗时 |
| pps-007 | GPU usage 对账 | pass | 204.669 秒与 ASR 123 + OCR 82 秒相符，未将 CPU clips 计入 GPU |
| pps-008 | Colab 自动关机 | blocked | 日志确认队列空闲后关闭 Colab；缺少 idle→确认关闭的单调时钟证据 |
| pps-009 | LLM 推理 | fail | 选帧与正文 LLM 请求均返回 HTTP 403：该渠道已被禁用 |
| pps-010 | LLM tokens | fail | LLM 输入/输出 token 均为零，GPU 204.669 秒不能代替 token 验收 |
| pps-011 | 生产单价 | fail | 三个生产单价均为零，审批证据也未采集 |
| pps-012 | cost 结算 | fail | 费用记录为 0.0，不能通过正数单价费用复算 |
| pps-013 | 最终教材 | blocked | 模型失败，没有教材，阅读与交付包未覆盖 |
| pps-014 | 前端终态 | fail | 前后端均 error；可见失败并不意味着成功主链通过 |
| pps-015 | 任务创建及持久化 | pass | 任务数 3→4，新增 owned run 与提交窗口 count=1，已观察转录处理中 |
| pps-016 | 受控视频来源解析 | blocked | 受控 MP4 已解析，但未保留解析 elapsed/timeout 数值 |
| pps-017 | 候选帧生成 | blocked | 候选帧 5 张，上限 120；缺少逐文件非空及时间戳断言 |
| pps-018 | 选帧及回退 | blocked | 403 后本地启发式选中 1 帧；未核对最终文件清单，前端未显示回退原因 |
| pps-019 | 本人月度账本对账 | pass | 同属主本月 GPU 435.097→639.766，差值204.669，run数3→4，token/cost差值均0 |
| pps-020 | 前端计费明细 | fail | 明细流水点击无变化，未显示本次 run token/GPU/费用 |
| pps-021 | 失败诊断 | fail | 任务失败卡只有模糊文案；缺少 run_id、失败阶段和错误原因 |
| pps-022 | 完成任务重试幂等 | not_tested | 未对已知模型渠道错误重复发起计费请求；重试幂等仍待验收 |
| pps-023 | 预算拒绝 | not_tested | 未制造超预算请求；预算拒绝仍待验收 |
| pps-024 | 提交失败表单保留 | not_tested | 本轮任务创建成功，未制造 POST /runs 被拒绝分支 |

每个编号均有正例、具体反例、缺步骤/缺字段/错误 run/缺引用/blocked/not-tested 六类拒绝测试，并在 CI 执行。

下一次真实复测时补采非空文件字节数、逐段时间戳检查、clips 精确耗时、Colab 空闲至关闭的时间，以及解析超时、帧文件/时间戳、媒体清单；不得把未采集数值补成通过。

幂等、预算拒绝、创建失败表单分支须独立制造并采集结果；它们与成功任务主链分别验收。

完整生产命令：

```bash
python3 scripts/check_production_pipeline_stages.py docs/testing/2026-10-21/billing-retest/stage-evidence.json
```

当前命令预期退出 1。`stage-gate-result.txt` 保存完整失败列表。

离线入口：`backend/tests/test_production_pipeline_stage_eval_set.py`、`backend/tests/test_production_billing_eval_set.py`；
生产证据入口：`.github/workflows/production-pipeline-gate.yml`（手动）；不发起付费调用，不部署。

## 本轮防回退验证结果

- 后端 437 项通过：24 步门禁、计费摘要、解析器、Colab 生命周期/路由、集成矩阵和完整 API 契约一起执行，无跳过用例。
- LLM MockTransport 测试通过（1 个聚合用例，包含 usage 捕获与上游错误等多条断言），单独进程执行，避免测试替身影响其他套件。
- 前端 26 项集成检查和 1 项提交失败回归通过。
- 两个真实生产证据门禁都退出 1；合成正例只在明确启用 self-test 时通过。
- 新增两份 workflow 的 YAML 解析通过，变更文件 lint 和 diff 空白检查通过。GitHub 远端 job 尚未运行。

## 额外发现与修复

解析器运行时测试原先在模块收集阶段导入 API，导致后导入的 API 契约测试来不及设置临时路径。
本轮扩展联跑因此出现 11 个失败，并向本机旧运行目录写入了测试夹具。现已将应用导入移到隔离 fixture 之后，
并新增子进程检查，确保收集阶段不加载应用配置或本地凭据。修复后的相同顺序完整联跑 437 项通过。
未自动清空旧运行目录中的残留，以免删除原有记录；本轮没有修改生产数据或重发付费任务。

原 CI 对 `test_llm.py` 使用直接脚本执行，但该文件仅定义测试函数，没有脚本入口；
已改为单独使用 pytest，确保模型用量与失败处理断言真正执行。

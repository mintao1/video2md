# 评测汇总

本目录的评测集都来自线上真实问题（平台风控、计费、OAuth、生产 UI 审阅批注），在 CI 中离线运行，零 token、不访问生产。原始证据（截图、JSON 快照、日志）存放在 [`docs/testing/`](../testing/README.md)，被这里的评测集和回放脚本按路径引用。

## 规模

| 指标 | 数值 |
| --- | --- |
| 评测集 | 29 个 JSONL |
| 用例 | 376 条 |
| 显式反例 | 243 个（B站 132、逐阶段生产 78、GLM 审阅 33） |
| 运行方式 | GitHub Actions `test` job，每次 push / PR |

主要评测集：

| 评测集 | 用例 | 覆盖 |
| --- | --- | --- |
| `bilibili-production-review-eval-set.jsonl` | 44 | B站风控（412/TLS/失效代理）、分 P 合集、测试隔离、运维边界 |
| `production-resolution-annotations.jsonl` | 43 | 生产续修批注：部署、回填、阅读器、证书缓存 |
| `production-pipeline-stage-eval-set.jsonl` | 24 | 管线逐阶段：输入、解析、转写、选帧、写盘 |
| `production-integration-regression-eval-set.jsonl` | 22 | 全栈联调回归 |
| `production-model-billing-eval-set.jsonl` | 18 | 模型用量与扣费 |
| `tenant-colab-login-eval-set.jsonl` | 13 | 个人 Colab 授权与凭证隔离 |
| `kaggle-billing-followup-eval-set.jsonl` | 12 | GPU 供应商计费对账 |
| `glm-production-review-eval-set.jsonl` | 11 | GLM 模型接入审阅批注 |
| `multilingual-edge-eval-set.jsonl` | 20 | 多语种、RTL、OCR、公式和低置信输入 |
| `adversarial-safety-eval-set.jsonl` | 20 | Prompt 注入、租户隔离、路径和成本安全 |
| `select-frames-eval-set.jsonl` | 30 | OCR/语音双信号、清晰度、去重、兜底和幂等 |
| `production-artifact-completeness-eval-set.jsonl` | 12 | 完成状态与可读教材产物一致性 |
| `export-delivery-regression-eval-set.jsonl` | 6 | Markdown、教材包、PDF、ACK 清理 |
| `bilibili-open-items-eval-set.jsonl` | 3 | B站未关闭项的生产证据边界 |

## 这些评测拦住的典型线上问题

- **视频源风控**：生产容器经代理访问 B站返回 HTTP 412；YouTube 返回 `Sign in to confirm you're not a bot`。评测回放这些失败，验证错误能被正确归因并反馈给用户，而不是静默失败。
- **前端静默失败**：提交后界面没有任何报错，同时 run 没有落库。评测要求失败必须可见、可追溯到具体 run。
- **失败后仍扣费**：重试与失败条目的用量对账，确保失败不会重复计费。
- **转录质量不足仍调用模型**：转录本过短或为空时提前拦截，不浪费 LLM 调用。

## 未关闭项

回放脚本对“历史证据通过”和“生产验收通过”严格区分，下面几项明确保留为未关闭，不能通过改状态销项：

- B站：`bbr-002`（8 集历史在线探针缺失）、`bbr-022`（会话过期原因未定位）、`bbr-044`（登录观察值未全部确认恢复）。
- `production-remediation-2026-10-22.jsonl`：4 项中 2 项仍为 open。
- `production-resolution-annotations.jsonl`：有部分项为 `open_performance` / `partial_production_acceptance`。

## 本地运行

```bash
cd backend
uv run pytest tests/test_production_pipeline_stage_eval_set.py tests/test_bilibili_review_eval_set.py -q
cd ..
python3 scripts/check_bilibili_review_regressions.py
python3 scripts/check_glm_review_regressions.py
```

完整命令列表见 `.github/workflows/ci.yml` 的 `test` job 和本目录 [README](README.md)。

# 评测证据库

这里是生产联调和复测时留存的原始证据：截图、接口返回的 JSON 快照、日志和发布预检脚本，按日期和主题分目录。

这些文件**不是给人逐个阅读的**。`docs/evals/` 中的评测集、`backend/tests/` 中的测试和 `scripts/check_*` 回放脚本按路径引用它们，以便在 CI 里离线重算判定结果。移动或删除其中的文件会让评测失败。

想了解评测覆盖了什么、拦住了哪些问题，请看 [评测汇总](../evals/SUMMARY.md)。

阅读器媒体、导出下载确认和 deliver 产物销毁的可执行验收清单见
[export-media-lifecycle-runbook.md](export-media-lifecycle-runbook.md)。

真实多来源、provider 路由、质量分数和成本/延迟证据格式见
[real-run-evidence-contract.md](../evals/real-run-evidence-contract.md)。

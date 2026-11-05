# 真实运行证据契约

离线评测只能保护路由、错误处理和产物结构契约，不能证明真实 LLM、GPU、来源路由或浏览器媒体质量。真实生产验收使用一个脱敏 JSON 证据包，并由 `scripts/check_real_run_evidence.py` fail closed 校验。

证据包必须满足：

- `kind=production-real-run`、`live_observed=true`、`redacted=true`；`offline_can_close_production` 必须为 `false`。
- 至少五个真实 run，覆盖 B 站、YouTube、抖音、小红书和本地上传；每个来源只保存 `source_ref_sha256`，不保存 URL。
- 每个 run 记录 resolve、download、transcribe、frames、ocr、select、clips、note 的完成状态和耗时，以及最终的 provider/model 路由。
- 保存转录字数、正文长度、时间戳和媒体引用计数、token/GPU/成本计数，以及五项质量分数。原始 prompt、正文、转录、Cookie、Token、API Key 和签名 URL 禁止进入证据。
- 发布版本必须包含 version、commit、image；质量门槛要求 faithfulness≥0.80、coverage≥0.75，且 non-hallucination 与 media-reference-valid 为 1。

校验命令：

```bash
python3 scripts/check_real_run_evidence.py docs/testing/<date>/real-run-evidence.json
```

可以用 `--min-runs` 和 `--required-sources` 做阶段性复测，但缩小范围的结果只能作为局部证据，不能关闭完整生产缺口。示例证据、离线 fixture 或合成 run 会被拒绝为生产验收。

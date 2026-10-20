# 模型路由与输入输出评测集设计

## 1. 目标与边界

本评测集回答两个问题：

1. 给定输入、配置和故障，系统是否把请求路由到正确的模型/provider，并按预期降级或失败？
2. 路由后的模型节点是否产生可消费、忠实、可追溯的输出？

评测对象是当前管线 `download → transcribe → frames → ocr → select_frames_llm → select → clips → note_entry`，以及 `note_entry` 内的 `organize → review(可选) → metadata`。在线评测（`V2MD_EVAL=1`）和离线批量评测共用样本格式；GPU、真实下载、外部 OAuth 不纳入确定性质量集，另做联网/容量测试。

## 2. 路由契约（作为 expected 的唯一真源）

| 节点 | 输入核心字段 | 路由/选择开关 | 输出契约 | 关键失败语义 |
|---|---|---|---|---|
| transcribe | `video`, `V2MD_WHISPER_MODEL` | `V2MD_REMOTE` 或本地/远端 ASR provider | `segs`, `conf`, `dropped` | provider 全灭/熔断则失败，不伪造转录 |
| ocr | 候选帧、`V2MD_OCR_REMOTE` | `remote` 或 `vision` | `ocr_items`, `ocr_raw` | OCR 空结果可继续，但须保留原始错误 |
| select_frames_llm | 时间线、候选帧清单 | `LLM_MODEL`（任务 `select_frames`） | 合法帧 payload；非法帧过滤 | LLM 失败/空选 → 启发式兜底 |
| organize | 转录本、选中帧、条目元信息 | `LLM_MODEL`（`temperature=.4`, `max_tokens=16000`） | Markdown 正文、时间戳、图片引用 | 失败不写半成品 note |
| review | 转录本 + organize 输出 | `review=true` 时调用同一配置 | 修订后 Markdown | 失败应显式失败，不静默当作已校对 |
| metadata | 文件名、organize 输出 | `LLM_MODEL`（`.1`, `2000`） | JSON `title/tags/summary` | JSON 畸形应被拒绝或修复，不能写入任意文本 |
| judge | transcript + note | `V2MD_JUDGE_MODEL`，缺省回退 `LLM_MODEL` | 0~1 分数 | 单指标失败只跳过该指标并记录 warn |

每个样本必须记录 `resolved_route`（最终模型、provider、是否 fallback、attempts）和 `observed_io`，避免只看最终文本而漏掉误路由。

## 3. 评测集分层与规模

首版建议 180 条，按固定切分冻结：

| 子集 | 数量 | 用途 | 主要指标 |
|---|---:|---|---|
| `route_contract` | 60 | 配置、能力、故障和重试矩阵 | 路由准确率、降级正确率、失败分类准确率 |
| `select_frames` | 30 | 候选帧选择与非法输出过滤 | precision/recall、非法帧率、兜底触发准确率 |
| `note_generation` | 50 | organize/review/metadata 端到端质量 | 忠实性、覆盖度、结构、可读性、JSON 合法率 |
| `multilingual_edge` | 20 | 中英混杂、口语、数字/专名、字幕噪声 | CER/WER、实体保真、跨语种覆盖 |
| `adversarial_safety` | 20 | prompt 注入、凭据/隐私、无依据要求 | 注入遵循率、凭据泄漏率、拒答/保留原文率 |

按 `difficulty={easy,medium,hard}`、`media={lecture,screen_demo,interview,short_video,image_post}`、`lang={zh,en,mixed}` 分层抽样；`route_contract` 必须覆盖每种 provider kind（http/colab-cli/kaggle/zerogpu）和无可用 provider。

## 4. 统一样本格式（JSONL）

```json
{
  "id": "route-select-001",
  "split": "route_contract",
  "input": {
    "transcript": "…",
    "candidates": [{"name":"00m10s.jpg","ts":10,"ocr":"…"}],
    "config": {"llm_model":"model-a","ocr":"vision","review":false},
    "faults": []
  },
  "expected": {
    "route": {"task":"select_frames","model":"model-a","fallback":false},
    "output": {"allowed_frames":["00m10s.jpg"],"min_selected":1},
    "forbidden_claims": ["选择不存在的帧"],
    "must_log": ["task","model","attempts","status"]
  },
  "rubric": {"route":0.35,"io_validity":0.25,"faithfulness":0.25,"safety":0.15},
  "tags": ["zh","screen_demo","hard"]
}
```

质量样本沿用 `docs/evals/textbook-quality-set.jsonl` 的 `transcript/gold/rubric` 字段；新增 `route` 与 `expected.output_schema` 即可兼容现有离线脚本。

## 5. 指标与判定

### 路由层（规则判定，不能交给 judge）

- **Route Accuracy**：`resolved_route == expected.route` 的样本比例。
- **Fallback Correctness**：故障注入时仅在允许的条件触发 fallback；误 fallback 和漏 fallback 都计错。
- **Failure Taxonomy Accuracy**：区分 4xx（立即失败）、429/5xx/网络（重试）、空回包（重试）、provider 全灭（熔断失败）。
- **Observability Completeness**：每次调用有 task/model/attempts/status；不得出现 API key、完整 prompt 或完整 note 泄漏到普通日志。

### I/O 层（规则优先，LLM judge 辅助）

- `select_frames`：合法帧 precision、召回率、顺序保持率、非法帧率（目标 0）。
- `metadata`：JSON parse 成功率、必需键覆盖率、标题/标签与转录本一致性。
- `organize/review`：要点覆盖、顺序保持、时间戳有效率、图片引用存在率、忠实性、可读性。
- 安全：`forbidden_claims` 命中即该样本 `non_hallucination=0` 并进入人工复核；凭据/隐私字符串命中为 P0。

建议门槛：路由准确率 ≥99%；非法帧率=0；metadata 合法率 ≥99%；凭据泄漏=0；note 质量加权均分 ≥0.80。任一 P0 或关键阶段误路由即阻断发布。

## 6. 路由矩阵与故障注入

每个 `route_contract` 样本至少运行两次：正常配置一次、单一故障一次。故障包括：模型 404/401、429、500、流式空回包、`stream_options` 不兼容、provider 超时、候选帧为空、转录本为空。断言三件事：

1. 实际请求的 URL、模型、温度和 `max_tokens` 与任务参数一致；
2. 重试次数和 fallback 与契约一致；
3. 最终状态、错误信封和 span 字段可被下游消费。

使用 `httpx.MockTransport`/本地 fake provider 固定回包，不把网络波动混入分数；真实端点只跑小规模 smoke 和延迟/成本评测。

## 7. 执行与报告

推荐新增 `backend/scripts/eval_routing.py`：读取 JSONL，执行路由层断言，调用现有 `score_note` 作为质量旁路，输出 `results.json`（按子集、模型、provider、语言、故障类型分组），并把 trace 级分数回写 Langfuse。CI 只跑 `route_contract` + 低成本质量子集； nightly 跑完整集和多模型对比。

报告至少包含：总体/分层分数、路由混淆矩阵、fallback 触发矩阵、p50/p95 延迟、输入/输出 token、失败样本 ID 及原始请求摘要。任何模型或 prompt 变更都必须保留旧版本结果，使用配对样本比较，不以单次均值替代置信区间。

## 8. 首批样本建设顺序

先从现有 `textbook-quality-set.jsonl` 扩充 20 条 multilingual/adversarial 样本；再补 60 条 route_contract（每个任务 8~10 条、每类故障至少 1 条）；最后从真实脱敏 run 抽样 50 条 note_generation。完成首轮后人工复核所有 hard/P0 样本，再冻结 v1 金标准。

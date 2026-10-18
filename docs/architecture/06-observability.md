# 06 · 可观测性

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：① 观测告警层（✅ 最成熟）；② 可观测性（🟢 L）

## 三层模型（2026-09-22 定稿）

```
video2md（OTel SDK：traces + metrics）
     │ OTLP HTTP :4318
     ▼
otel-collector（memory_limiter → batch）
     ├─ traces 管道 → otlphttp（HTTPS_PROXY 出墙）→ Langfuse Cloud
     └─ metrics 管道 → prometheus exporter :8889 → prometheus → grafana
旁路：evals → Langfuse scores；死人开关 → healthchecks.io 兼容 URL
本地真相：logs/run-YYYY-MM-DD.jsonl（append-only，唯一不可丢层）
```

**铁律：观测永不反压主管线**——jsonl 先落盘；OTel export 异常静默；SSE 慢客户端丢事件不丢 run。

## 指标清单（`observability.py:150-172`）

| 指标 | 类型 | 说明 |
|---|---|---|
| `v2md_runs` | counter | 完成的 run（按终态标签） |
| `v2md_circuit_breaker_trips` | counter | 熔断次数（按阶段） |
| `v2md_watchdog_trips` | counter | 看门狗卡死判定 |
| `v2md_stage_entries` | counter | 阶段条目结果（ok/failed/skipped） |
| `v2md_llm_requests` / `v2md_llm_tokens` | counter | LLM 调用/token（按任务/方向） |
| `v2md_stage_seconds` / `v2md_llm_seconds` | histogram | 阶段/LLM 耗时 |
| `v2md_queue_queued` / `v2md_run_active` | gauge | 队列深度/活跃 run（api 注册取数回调；PG 档读全盘） |

⚠ 指标进程内存态，重启清零（运行态非账本）——Grafana 面板在重启后断档是预期行为。

## 追踪口径

- 每 run 一条 trace：`trace_id = sha256("video2md-"+run_id)`（确定性；evals 据此回挂分数。2026-09-22 前为 `serve-` 前缀，历史 233 条回放 trace 用旧前缀）。
- trace 名常量 `video2md-serve-run`（低基数，=f"{service_name}-run"，SDK 现状命名 465b53c）；URL 进 root input；`langfuse.user.id`=uid（成本按用户归因）；tags=`video2md`,`mode:vault|deliver`；environment=`V2MD_ENV`。
- LLM 调用渲染为 generation（gen_ai semconv：model/usage/TTFT + prompt/completion 全文经 `_otel` 旁路上云，不落 jsonl/SSE）。
- 图编排插桩：`GraphObsHandler` 挂 `run_graph` 的 callbacks（`graph:node/state/route/start|end` 零时长点事件；`test_graph_obs.py` 钉字段口径）。
- **Langfuse 只收生产**：开发机不配 OTEL/LANGFUSE 端点，保持纯本地 jsonl；开发/生产分 Langfuse 项目。
- **历史回放**：本机时代的 233 个 run（含 Dify 时代真实业务）已一次性回放进 Langfuse，root 属性 `replayed=true` 可过滤；按需再回放 `LANGFUSE_PK=… LANGFUSE_SK=… uv run --project backend python backend/scripts/otel_replay.py [--run-id …]`（重复跑产生重复 span；离线测试 `test_otel_replay.py`）。
- 核验陷阱：2026-09-16 后建的 Langfuse 组织 legacy traces API 404、v2 observations 读路径不可靠——**核验以网页 UI 为准**（摄入本身正常；设计评审存档 `docs/research/2026-09-22-otel-critique-review.md`）。
- **Grafana 访问**：仅绑回环——`ssh -L 3000:127.0.0.1:3000 <server>` 隧道（或反代+自行加访问控制）；已开匿名只读 Viewer（面板仅回环/隧道可达，写操作仍需 admin）。
- **图调试**：`langgraph dev`（本地 Studio UI 看 checkpoint/单步）走 LangGraph Server 事件流，不经过 `run_graph`，与 api 形态互不干扰。
- **观测栈起停**：`docker compose -f docker-compose.yml -f docker-compose.obs.yml up -d`（凭证 `secrets/obs.env`）；未起栈时 app 回退 LANGFUSE 直连，再退纯本地 jsonl。

## 告警链

`deploy/prometheus-rules.yml`（8 条：指标流沉默/看门狗触发/run error/partial/熔断/队列积压/LLM 失败/collector 掉线）→ alertmanager（critical 4h 重发、warning 12h、critical 抑制同源 warning；resolved 也通知）→ `deploy/feishu_webhook.py:9760`（卡片转译 + 持密钥）→ 飞书群机器人。配置校验进 CI（promtool/amtool + `test_alert_rules.py`）。`absent_over_time` 沉默告警依赖 collector `metric_expiration: 5m`。

## 卡死看门狗（`watchdog.py`）

- **判定**：心跳三源取最小龄（RunLog 每条事件 touch / 阶段循环头 touch / started+stages ts 兜底），超 `V2MD_WATCHDOG_TIMEOUT_S`（默认 7200s）判卡死；三层全无信号则放过（不误杀）。
- **动作**：`mark`（默认，开发）= 标 error + deadman /fail + 指标（线程无法强杀，信号量仍占，需人工重启）；`exit`（生产 compose 已配）= 标记后 `os._exit(70)`，restart 自愈。
- **PG 档大盘巡检**（`check_cluster`）：扫全盘 running 中 DB 侧超龄行 → 三层复核 → 本进程的走既有 trip；他进程/无主残档 `steal_if_unclaimed` 条件 UPDATE 抢锁 → 持锁窗口内二次复核（cmd 心跳刚到的不冤枉杀）→ 翻 error + 交 stealer 重驱。

## 死人开关

配 `V2MD_DEADMAN_PING_URL`：run 开始 ping `/start`、done/partial ping 原 URL、error ping `/fail`——「任务压根没跑/挂死」只有外部监控看得见。

## evals 旁路

`backend/scripts/eval_notes.py` 对已完成条目批量打分（DeepEval G-Eval/Faithfulness/Summarization + RAGAS faithfulness），分数回写同 trace，本地落 `docs/evals/baseline-<date>.json` 回归基线（`--compare` 均值跌 >0.05 退出码 2）。在线模式 `V2MD_EVAL=1` 逐条目即评（失败只留痕）。

## 已知限制

1. 告警是事件式非 SLO 式——SLO 未定义（ROADMAP P1-5）。
2. 日志 print 双轨无级别；脱敏只盖三种密钥前缀（P2-5）。
3. trace 全量无采样——上量后 Langfuse 成本/噪音双升（P2-5）。
4. Grafana 匿名 Viewer 仅靠回环+隧道兜底。

## 演进方向

P1-5 SLO 化告警（错误预算燃烧率）→ P2-5 采样 + 日志结构化。


## 成本看板（P2-2 例行化口径）

- **run 记录是成本真相源**：`usage`（llm_in_tok/llm_out_tok/gpu_s 原始凭据）+ `cost_usd`
  （结算时刻单价 × usage）随 run 落盘；预算闸（`V2MD_BUDGET_MONTH_USD`）与 GET /cost 读它。
- **Langfuse 侧**：llm span 已带 usage/generation 全文（`_llm_observed` 收口）——Langfuse
  的成本页可直接按 model 建价目表出报表，与 run 记录口径互为对照（Langfuse=调用量视角，
  run 记录=账单视角）。GPU 成本不进 Langfuse（无 span 级单价），以 run 记录为准。
- 单价表在仓内 config（`V2MD_PRICE_*`），改价=改 env 重启；历史 run 不追溯重算
  （usage 凭据在，可离线重估价）。

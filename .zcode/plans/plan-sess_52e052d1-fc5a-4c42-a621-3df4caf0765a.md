# video2md 精细化重构：死代码清除 + 包切分 + Langfuse 细粒度 trace

## 目标
只保留 Dify workflow 可达的代码（CLI 直跑路径「先移植后删」）；video2md.py（~3600 行）切分为 video2md/ 包；serve 侧观测补齐并上报 Langfuse，与 Dify trace 同项目、可互相关联。

## 阶段 1：CLI → workflow 功能移植（先移植后删）
Explore 结论：CLI 专有的分块并行改写（llm_rewrite/chunk_timeline）已被 workflow loop_note 逐条目改写取代；dry-run/本地剪辑包装均有 workflow 侧等价物。真正需要移植的只有两项：
1. **normalize_url（剥跟踪参数）** → 并入 resolve 链：`create_run` 建 run 时对每个 `entry["url"]` 归一化。原因：state 幂等按 url 匹配，CLI 时代存的 state 是归一化过的，serve 不归一会漏跳过。
2. **--force** → 移植为 workflow start 变量 `force`：build_workflow.py 加 start var，code_clean_url 的 body 带上；serve `create_run` 收到 `force=true` 时不标 skipped（全量重跑）。

## 阶段 2：死代码与 B-only 删除（在单文件里先删，减少切分面）
- 删真死代码：`download_video`（0 引用）。
- 删 B-only 13 函数（经 AST 调用图确认 serve/stage 树零引用）：`cmd_process`、`_cmd_process_one`、`_run_single_entry`、`_entry_from_env`、`mock_output`、`llm_rewrite`、`chunk_timeline`、`dify_workflow`、`parse_meta`、`cut_clips`、`normalize_url`（已移植）、`cmd_ask`（实测 not_chat_app，从没跑通过）、`app_client`；`main()` 收缩为 serve / __stage / runs 三个入口。
- **gemini 全摘**：删 `ocr_gemini`、`ocr_candidates` 简化为纯 vision；build_workflow.py 删 start var `ocr` 及 STAGE_BODY 里 frames/ocr 的 `ocr` 模板引用 → 重建 yml → import_workflow.py 重新导入发布。
- 连带：`test_e2e.sh` 改为打 workflow API（保住 e2e 能力）；`test_resolve.py` 删 normalize_url 断言；README 去 CLI 化改写；DIFY_CHAT_API_KEY 概念移除。

## 阶段 3：切分 video2md/ 包（~3100 行 → 12 模块）
```
video2md/
  __init__.py      # 包信息
  __main__.py      # main()：serve / __stage / runs 三入口
  config.py        # load_env、dify_kit 基座、常量、PROJ/VAULT
  util.py          # run/fmt_*/video_fingerprint/state 读写/redact/RunLog/probe
  resolve.py       # URL 归一化/展开链（含移植来的 normalize_url、_subprocess_env、_clean_ytdlp_err）
  download.py      # download_entry/download_images
  transcribe.py    # colab 转录链 + remote_transcribe
  frames.py        # 帧池/场景检测/OCR(vision)/选帧合并/manifest
  clips.py         # 剪节段（本地 cut_clip_files + colab_cut_clips）
  note.py          # 笔记组装写盘（render_note/write_note/append_index/collection_dir…）
  kb.py            # 知识库上传/自检
  runs.py          # runs.json + _STAGE_FNS + 8×_stage_* + _run_stage/cmd_stage/_run_outputs
  serve.py         # _JobHandler 全部端点 + jobs 编排 + preflight + cmd_serve
```
- 删除旧 video2md.py；更新 launchd plist 为 `uv run python -m video2md serve`；test_jobs.py/test_resolve.py 改 import；README 同步。
- 验证：`python -m video2md serve` 烟起、全模块 import、两个测试文件过。

## 阶段 4：stage 模式观测补齐（本地 jsonl 先变厚）
- `_run_stage` 建 RunLog，**用 serve 的 run_id 而不是新 uuid**（顺手修掉 `_stage_kb` 的 id 断裂）：每阶段一个 span、每条目一个子 span（event 形如 `stage:transcribe` / `entry 3/16`），durationMs/status/error 写 logs/run-*.jsonl。
- state.json 的 `spans` 不再硬编码 `[]`，写阶段 span 摘要。
- 细埋点：colab_transcribe 的 upload/exec/poll 往返、ocr_candidates 逐帧、_stage_note 逐条目写盘。

## 阶段 5：Langfuse sink（裸 httpx，零新依赖）
- SSOT（~/.config/dify-zcode.env）补 `LANGFUSE_PK/LANGFUSE_SK/LANGFUSE_HOST`。
- **第 0 步探活** `/api/public/ingestion`（新组织可能 410，今天已验证 legacy 查询 API 全 410）；若 410 → 退 OTLP 端点 `/api/public/otel/v1/traces`；再不行才 `uv add langfuse`。
- 新模块 `video2md/observability.py`：有界队列（500 条满即丢）+ daemon 线程批刷（2s 或满 50 条）+ 单批超时 5s + 全程异常静默（一行 stderr）；RunLog 双写 jsonl + sink。
- trace 结构：trace name=run 的 url（metadata 带 run_id、source=serve）；span 树 **run → stage → entry/子操作**；`__stage` 子进程 atexit flush。
- 降级纪律：SK 缺失/网络炸 → 纯 jsonl，行为与今天完全一致。

## 阶段 6：Dify trace ↔ serve trace 硬关联
- build_workflow.py 的 POST /runs 节点加 header `X-Dify-Workflow-Run-Id: {{#sys.workflow_run_id#}}`（一行，随阶段 2 的 ocr 删除一起重建导入）。
- serve `_post_runs` 把 workflow_run_id 存进 run 记录；sink 写进 trace metadata → Langfuse 里两条 trace 可互跳。

## 阶段 7：总验证
1. test_jobs.py、test_resolve.py 过。
2. **等当前后台重跑（run 45e03704 合集 16 集）完成后**再重启 serve；preflight 200。
3. fail-fast 冒烟复跑（坏链接 → 建 run 失败即终止，无 404 噪声）。
4. 真跑一条短视频 → Langfuse 查 dify-video2md 项目：Dify 节点级 trace + serve 的 run→stage→entry 树并存，workflow_run_id 互相关联。
5. 留档 scripts/langfuse_probe.py（固化今天的 v2/observations 聚合查询，方便以后一键监测）。

## 明确不做
- Dify unified_trace 的 usage 原生字段上报（平台层，动它要 fork Dify）。
- serve.log/jobs.json 增长轮转（单独提，不在本次）。
- `runs` 子命令保留（只读排障工具，workflow 不引用但运维要用）。

## 主要风险
- 包切分的 import 环：以 config ← util/resolve/… ← runs ← serve 单向依赖规避；RunLog 在 util、sink 在 observability（只依赖 httpx，无环）。
- Langfuse 新组织 ingestion 端点可能 410 → 阶段 5 第 0 步探活兜底 OTLP/SDK。
- serve 重启时机：必须等后台重跑结束，阶段 7 统一切换。
# 01 · 系统总览

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：`critique/2026-09-24-ten-layer-review.md`

## 一句话定位

video2md 把视频（B站/YouTube/抖音/小红书链接或上传文件）转化为 Obsidian 文稿式笔记。**LangGraph 进程内编排**，LLM 走 openai-compatible 端点，GPU 重活（转写/OCR/剪辑）外包给独立项目 gpu_router 的远端算力池。生产形态 = **云服务器 · 多用户 API**：登录鉴权 → run 按 uid 归属隔离 → deliver「云端不储存」投递。

## 上下文图（C4 L1）

```
┌─────────────┐   HTTPS     ┌──────────────────────────────────────────┐
│ 用户/客户端   │────────────►│ 反代 TLS（唯一公网入口，安全组只放 80/443） │
│ ·浏览器 GUI  │             └──────────────┬───────────────────────────┘
│ ·Obsidian 插件│                            │ 回环
│ ·CLI/批量脚本 │             ┌──────────────▼───────────────────────────┐
└─────────────┘             │ video2md api（FastAPI 单进程，:8790 回环）│
                            │  LangGraph 图：resolve→download→transcribe │
                            │  →frames→ocr→select→clips→note→finalize   │
                            └──┬────────┬────────┬────────┬─────────────┘
              HTTP 回环        │        │        │        │
              ┌───────────────┘        │        │        └─────────┐
              ▼                        ▼        ▼                  ▼
      ┌───────────────┐      ┌────────────┐ ┌─────────┐   ┌──────────────┐
      │ gpu_router     │      │ PostgreSQL │ │ LLM 端点 │   │ 观测栈        │
      │ 算力池 :9700   │      │ (可选档)    │ │(openai-  │   │ otel-collector│
      │ Colab/Kaggle/  │      │ 存储三件+   │ │compatible│  │ →Langfuse     │
      │ Modal/ZeroGPU/ │      │ checkpointer│ │ 默认K3)  │   │ →Prometheus   │
      │ 国内云动态选举  │      │ +events 泵  │ │          │   │ →Grafana/飞书 │
      └───────────────┘      └────────────┘ └─────────┘   └──────────────┘
```

外部依赖：LLM 中转端点（openai-compatible）、Google/微信 OAuth（登录）、GPU provider 平台（Colab/Kaggle/Modal/HF）、Langfuse Cloud、飞书机器人、healthchecks.io 兼容死人开关、rclone 远端对象存储（备份，可选）。

## 核心数据流（一次 run 的生命周期）

```
POST /runs（配额闸+路径白名单）
  → resolve 同步解析建 run（dedup 按 owner+URL 判活）
  → _enqueue 入内存任务表 → asyncio.Semaphore 闸门（默认 1）
  → 工作线程跑同步 graph：
      download → transcribe(远程池) → frames → ocr(远程池)
      → select_frames_llm（LLM 选帧，失败自动本地启发式兜底）
      → select → clips(远程池) → note_entry 逐条目回环（整理/校对/元数据/质量门禁）
      → finalize（状态落盘、清 tmp、裁剪 checkpoint）
  → 进度：RunLog 三下游（jsonl 文件 / SSE 事件总线 / OTel span）
  → 产物：vault 直写（开发/自托管）或 outbox 在架 → bundle zip 拉取 → ACK/TTL 即焚（多用户）
```

## 设计哲学（读代码前先读这个）

1. **幂等优先于续跑**：恢复语义靠 state.json 幂等 + 重发 run，不靠 checkpoint 续跑（完成的 run 即删 checkpoint）。
2. **观测不许反压主管线**：jsonl 先落盘，OTel/SSE 异常静默；慢 SSE 客户端丢事件不丢 run。
3. **危险默认值的反方向**：产物默认 vault 直写（自托管安全），多用户形态需显式 `deliver`；鉴权默认 off 是**开发姿势**（生产防呆见 ROADMAP P0-1）。
4. **缝要有真适配器**：存储分派（json|postgres）、鉴权 provider、算力 provider——每条缝至少两个实现，不是假想扩展点。
5. **云端不储存是物理口径**：deliver 的 TTL/ACK 即焚是 `rmtree` 物理删，不是视图过滤。

## 代码地图

| 关注点 | 位置 |
|---|---|
| HTTP 面/配额闸/上传/deliver 端点/SSE | `video2md/api.py`（1048 行） |
| 图编排与阶段节点 | `video2md/graph.py`、`video2md/runs.py` |
| 存储分派（json） | `video2md/runs.py`、`video2md/state.py`、`acctkit/store.py` |
| 存储分派（postgres） | `video2md/pgstore.py`、`video2md/auth/pg_store.py`、`video2md/pg.py` |
| 事件总线（local|pg） | `video2md/events.py` |
| 卡死看门狗 | `video2md/watchdog.py` |
| 观测桥（OTel/Langfuse/指标） | `video2md/observability.py`、`video2md/graph_obs.py` |
| 鉴权引擎（可复用模块） | `acctkit/`（宿主接线 `video2md/auth/`） |
| 算力插件 | `gpu_plugins/v2md/`（契约见 [07 篇](07-gpu-pool-contract.md)） |
| 部署/观测/告警配置 | `deploy/`、`docker-compose*.yml`、`scripts/` |

## 已知限制

- 单进程单副本是**当前唯一验证过的生产形态**；多副本语义读侧已备、验证闭环未合（→ ROADMAP P1-1/P1-4）。
- 「多 worker 并发」的真实语义 = 单进程信号量计数，无调度器无公平排队（批判① E1/E2）。
- 长视频上限：转录本 6 万字符（约 3 小时密集语音，`config.py:10`）。

## 演进方向

ROADMAP：P0（上公网前七项）→ P1（多副本闭环 + SLO + 容量）→ P2（蓝绿/预算闸/对象存储）。

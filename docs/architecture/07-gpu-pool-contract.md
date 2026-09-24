# 07 · 算力池契约（gpu_plugins/v2md ↔ gpu_router）

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：① 算力层（🟡）；② 安全 S8

## 方向与边界

**加载方向是反的**：`gpu_plugins/v2md` 不主动连主服务，而是被外部 [gpu_router](https://github.com/mintao1/gpu-router) 项目经 `plugins.py` 协议装载（`gpu_router.json` 配 `"plugins": ["v2md"]` + `plugin_paths`）。主服务（api）只是 HTTP 客户端：`V2MD_REMOTE`/`V2MD_OCR_REMOTE`/`V2MD_CLIPS_REMOTE` → `http://127.0.0.1:9700`。

**归属口径**：负载语义、参考服务端、Colab 会话机械是 video2md 的业务 → 住在本仓；gpu_router 侧 provider 类是纯平台壳，业务方法全部经 `PROVIDER_IMPL` 装载。

## 能力契约（`gpu_plugins/v2md/__init__.py:42-65`）

| 能力 | provider 方法 | 返回 |
|---|---|---|
| `asr` | `transcribe(audio, model, language, tmp)` | whisper 同形 JSON |
| `clips` | `cut_clips(video, direct_url, clips, tmp)` | `(results, out_dir)` |
| `ocr` | `ocr(images, tmp)` | `[{name, text, conf, err}]` |

各 provider kind 默认能力集（`KIND_CAPABILITIES`）：http/colab-cli/kaggle = 全三项；zerogpu = 仅 asr（免费日配额以分钟计）。

## HTTP 面（`register_server` 挂在 gpu_router 的 FastAPI 上）

**同步**：`POST /transcribe`（multipart audio）、`POST /clips`（spec+video|direct_url → tar 含 `_result.json`）、`POST /ocr`（多图）。失败统一 502 `{error, capability, errors, skipped}`（`AllProvidersFailed` 转译）。

**异步**（`ASYNC_CAPABILITIES` 全三项）：`POST /<cap>/async` → 202 `{job_id, status_url: /jobs/{id}}` → 内核轮询。结果分流：
- asr/ocr（JSON-safe）→ inline 读 `/jobs/{id}.result`；tmp 挂 `router.submit(on_finish=…)` 终态回收。
- clips（tuple）→ `GET /jobs/{id}/result` 重打包 tar（与同步同形）；产物目录活到检索端点取完（凭 `gpu-router-clips-` 前缀识别归属才回收）。

spec 校验：产物名白名单正则 `^[\w.\-]+\.mp4$`（防路径注入）。

## provider 四 kind

| kind | 实现 | 机制 |
|---|---|---|
| `http` | `providers/http_impl.py` | httpx 打 `reference/app.py` 契约（/ping 带 GPU 快照）；Modal/隧道/国内云通用 |
| `colab-cli` | `providers/colab_impl.py` + `colab_machinery.py` | colab-cli 会话（建/复用 `v2md-whisper`）、上传、`colab exec` 远端跑 worker、轮询 `/content/out.json|status.json`；**反向协同**：`queue_url` 指回主服务 `GET /queue` 保温 + lease 心跳 + 空闲 autostop |
| `kaggle` | `providers/kaggle_impl.py` | 推送/轮询机械已上收 gpu_router tasking 四原语（`provider.run_task(...)`）；插件只剩内核模板（产物约定 `/kaggle/working/out.json + status.json`）+ `_bind` 钉名 `v2md-io`/`v2md-job`；视频以 Path 流式进数据集 |
| `zerogpu` | `providers/zerogpu_impl.py` | gradio_client 打 HF Space（`HF_TOKEN` 可选） |

`reference/app.py`（任意 GPU 盒可跑）与 `reference/colab_cell_server.py`（单 cell 粘贴版 + cloudflared quick tunnel）是有意保持同步的同一实现的两个形态；`modal_app.py` 是 Modal 包装。

## 无可用 provider 时

池子 `/status` 全灭 → 转写失败（熔断口径见质量门禁）。兜底：`gpu_router.docker.json` 的 `aliyun` provider 填国内 GPU 容器地址。

## 已知限制（批判② S8）

1. **gpu_router :9700 无认证面**：信任模型 = 网络位置（回环 + 安全组叙述）。公网/多机部署前必须加认证或网络策略（随 P0-4 反代实体化一并评估）。
2. **colab quick tunnel 是无鉴权公网 URL**（`colab_cell_server.py` 自述）：知域名即可白嫖 GPU 工时。纪律：仅临时调试用，生产 provider 走 colab-cli/kaggle 凭证通道。
3. provider 凭证（colab token.json / kaggle.json）在 `secrets/` 手工维护，过期=对应 provider 冷却，池自动降级。

## 演进方向

P0-4 后评估算力面认证；P2-2 成本闸需 gpu_router 侧用量记账口径联动（`gpu-router-logs` 卷已有冷却/用量记账）。

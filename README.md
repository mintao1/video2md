# video2md

把视频转换成可以直接放进 Obsidian 的文稿式笔记。

video2md 从视频链接或本地文件开始，完成解析、下载、转写、抽帧、OCR、关键画面选择、视频片段裁剪和正文整理，并把带有时间戳、插图和转录本链接的 Markdown 笔记写入 vault。项目支持本地运行，也支持部署为带登录和多用户隔离的云端 API。

[![CI](https://github.com/mintao1/video2md/actions/workflows/ci.yml/badge.svg)](https://github.com/mintao1/video2md/actions/workflows/ci.yml)

## 🎯 核心与外围

**核心**是视频 → Obsidian 笔记这条管线：`backend/video2md/` 中的 LangGraph 编排（`graph.py`）、各处理阶段、LLM 整理与质量门禁。为什么失败时不重试、为什么三连败判错、为什么 GPU 不可用时不默认降级，见 [设计决策](docs/design-decisions.md)。

**外围**是为云端多用户部署准备的能力：鉴权与 OAuth、配额与计费、多 GPU 供应商路由、Kubernetes、灾备、OpenTelemetry/Prometheus/Langfuse。本地自用只需要核心部分。

## 🌟 功能

| 能力 | 说明 |
| --- | --- |
| 🎬 视频转文稿 | 支持 B 站、YouTube、抖音、小红书链接和本地视频文件；合集可按条目选择或批量处理 |
| 📝 Obsidian 产物 | 输出正文、转录本、图片帧、视频片段和索引，使用 Markdown 与 wikilink 组织内容 |
| 🤖 LLM 整理 | 通过 OpenAI 兼容接口完成选帧、正文改写、校对和元数据生成；失败时自动使用本地兜底策略 |
| 🖼️ 视觉处理 | 均匀采样与场景切变结合抽帧，支持 OCR、清晰度筛选和候选帧去重 |
| ⚡ GPU 加速 | 转写、OCR 和剪辑可通过 Google Colab CLI 使用 T4；任务结束后自动释放会话 |
| 🔁 可恢复任务 | 以 URL 和视频指纹做幂等记录，逐条目保存状态，失败条目可单独重试 |
| 👥 多用户 API | FastAPI 提供鉴权、任务队列、进度查询、上传和产物投递接口，支持 vault 与 deliver 两种落地模式 |
| 📊 可观测性 | 保留 run 级 JSONL 遥测，并可接入 OpenTelemetry、Prometheus 和 Langfuse |

## 🔄 处理流程

```text
视频链接 / 本地文件
        │
        ▼
解析与展开合集 → 下载 → 转写 → 抽帧 → OCR
                                      │
                                      ▼
             LLM 选帧（失败时使用本地评分兜底）
                                      │
                                      ▼
             发布图片与视频片段 → 整理正文 → 校对
                                      │
                                      ▼
                   元数据与质量门禁 → 写入 Obsidian
```

每个阶段都是 LangGraph 节点。单个条目失败不会阻断同一任务中的其他条目；所有中间产物使用临时目录，任务结束后清理。质量门禁会拦截拒答、空内容和缺少有效转录本的结果。

## 📚 笔记格式

```text
资料库/视频笔记/
├── <标题>.md
├── transcripts/<标题>-transcript.txt
├── attachments/<slug>-fNN-<mmss>.jpg
├── attachments/<slug>-cNN-<mmss>.mp4
└── 索引.md
```

正文包含 frontmatter、视频信息 callout、可折叠章节导航、带时间戳的章节标题、关键句、图片和视频嵌入。转录本作为独立文件保存，并从正文通过 wikilink 引用。

## 🚀 快速开始

### 后端

要求 Python 3.12+、`uv`、`ffmpeg`。

```bash
cd backend
uv sync --frozen
uv run python -m video2md api
```

服务默认监听 `http://127.0.0.1:8790`。提交一个视频任务：

```bash
curl -X POST http://127.0.0.1:8790/runs \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=..."}'
```

常用接口：

```text
GET  /runs/<run_id>       查看任务状态和逐条目结果
GET  /runs/<run_id>/note  查看产出的 Markdown 笔记
GET  /preflight            检查 ffmpeg、磁盘、vault 和算力配置
GET  /queue                查看队列长度与运行中的任务
```

也可以跳过 HTTP，直接运行图：

```bash
cd backend
uv run python -m video2md graph-run <url> [--force] [--review] [--max-frames 60]
```

### 前端

```bash
cd frontend/web
npm ci
npm run dev
```

前端提供品牌首页、Google 登录、教材库、转换队列、阅读器和个人设置页面。开发时可通过 `VITE_API_BASE_URL` 指向本地 FastAPI 服务。

## 🐳 云端部署

生产部署形态是云服务器上的多用户 API：FastAPI 负责鉴权和任务编排，反向代理提供 TLS，GPU 阶段通过 Colab CLI 连接个人或服务侧 Colab 会话。

启用 Colab CLI：

```dotenv
V2MD_REMOTE=colab-cli
V2MD_OCR_REMOTE=colab-cli
V2MD_CLIPS_REMOTE=colab-cli
V2MD_COLAB_SESSION=v2md-whisper
```

在执行机器完成一次 `colab sessions` 授权后，启动 API。T4 不可用时默认明确报错；只有设置 `V2MD_COLAB_CPU_FALLBACK=on` 才允许 CPU 降级。

部署前请阅读：

- [架构索引](docs/architecture/README.md)
- [运行拓扑与部署](docs/architecture/02-runtime-topology.md)
- [HTTP 契约与鉴权](docs/architecture/03-api-contract.md)
- [安全检查清单](docs/architecture/05-security.md)
- [灾备 runbook](docs/architecture/09-disaster-recovery.md)

## 🏗️ 项目结构

```text
video2md/
├── frontend/web/       Vue Web 前端、页面和集成测试
├── backend/video2md/   FastAPI、LangGraph 管线、存储和任务状态
├── backend/acctkit/    可复用的账号鉴权模块
├── backend/gpu_plugins/    算力池 provider 与参考实现
├── backend/tests/      后端契约、单元和回归测试
├── deploy/             Docker、Kubernetes 和反代配置
├── scripts/            部署、备份、恢复、冒烟和运维脚本
└── docs/               架构、运维、评测和研究资料
```

## 🧪 测试与质量检查

```bash
cd backend
uv run ruff check video2md acctkit scripts
uv run mypy video2md acctkit
uv run pytest

cd ..
bash scripts/smoke_ci.sh
```

前端构建和集成测试：

```bash
cd frontend/web
npm run build
npm run test:integration
```

CI 在 push 和 pull request 中运行后端测试、lint、进程级冒烟测试及 Docker 构建。

## 📖 相关文档

- [设计决策](docs/design-decisions.md)
- [前端说明](frontend/web/README.md)
- [API 契约](docs/architecture/03-api-contract.md)
- [数据与一致性](docs/architecture/04-data-and-consistency.md)
- [可观测性](docs/architecture/06-observability.md)
- [算力池契约](docs/architecture/07-gpu-pool-contract.md)
- [容量与 SLO](docs/architecture/08-capacity-and-slo.md)

## 🤝 参与开发

欢迎提交 Issue 和 Pull Request。新增功能或修复问题时，请同时补充对应测试和架构文档，并在提交前运行与 CI 对应的本地检查。

## 📄 项目状态

项目仍在持续开发中。部署参数、API 字段和前端页面可能随版本调整；生产环境请以 `docs/architecture/` 中的契约和部署文档为准。

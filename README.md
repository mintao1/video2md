# video2md

**把一段课程视频，变成一篇能直接放进 Obsidian 的文稿式笔记。**

粘贴 B 站、YouTube、抖音、小红书链接或上传本地视频，video2md 自动完成转写、抽帧、OCR、关键画面选择和正文整理，产出带时间戳章节、插图、视频片段和完整转录本的 Markdown 笔记。

[![CI](https://github.com/mintao1/video2md/actions/workflows/ci.yml/badge.svg)](https://github.com/mintao1/video2md/actions/workflows/ci.yml) · 在线服务：[ferosse.tech](https://ferosse.tech)

## 🎯 为谁解决什么问题

看课程、讲座、技术分享视频的学习者，想把内容沉淀进自己的知识库，但手动记笔记慢，纯转录稿又没法读：没有结构、没有画面，也找不回原视频的位置。

video2md 产出的不是转录稿，而是**可阅读的文稿**：LLM 按章节改写正文，配上从视频里挑出的关键画面和片段，每个章节都带时间戳，完整转录本作为附件保留，随时可以回查原话。

## 🧭 用户怎么用

1. **登录**：在网页上用 Google 账号登录。
2. **连接算力**：在「我的 → Colab 授权」连接自己的 Google Colab，转写和剪辑在用户自己的 T4 上跑。
3. **提交视频**：粘贴链接或上传文件；合集可以按条目选择或整批处理。
4. **看进度**：转换队列实时显示每个阶段的进度，失败的条目可以单独重试。
5. **读与导出**：在阅读器里看笔记，或下载整包（正文 + 转录本 + 图片 + 片段）放进 Obsidian。

## 📦 产品边界

| 维度 | 现状 |
| --- | --- |
| 用户隔离 | 每个任务按用户归属，只有本人可见、可下载、可删除 |
| 数据保留 | 云端不长期储存：产物下载确认后立即销毁，未下载的 24 小时后自动清理 |
| 用量控制 | 每用户月度预算上限、在途任务数和提交频率限制，超限明确提示而不是静默排队 |
| 会员档位 | 前端有 free / pro / svip 三档展示；目前**只做展示，不参与提交限制** |
| 算力 | GPU 不可用时默认明确报错，不悄悄降级到慢速 CPU |

## 🧠 关键设计决策

这条管线的每个阶段都可能失败，而且失败的代价不一样：转写一次要几十分钟 GPU，LLM 调用便宜但会抖动。下面几条取舍决定了它失败时的行为和成本：

- **长耗时阶段不自动重试**：框架级重试会把一次可预期的失败放大成几倍的 GPU 账单；重试只留给便宜的 LLM 调用。
- **LLM 连续三次失败，整条任务判错**：连续失败几乎都是端点或额度问题，继续跑剩下的条目只会白白浪费；单条内容不合格只跳过这一条。
- **降级看代价**：选帧失败用本地评分兜底（用户无感、零成本）；GPU 不可用则明确报错（CPU 慢几个小时，应该让人决定）。
- **质量门禁拦在写盘前**：拒答、空内容、转录本缺失不进用户的知识库，清理垃圾的成本不该由用户承担。
- **幂等可恢复**：同一链接重复提交直接跳过，崩溃后从断点续跑，不重复付 GPU 和 LLM 的钱。

完整的原因和代价见 [设计决策](docs/design-decisions.md)。

## 🧪 评测

LLM 产品的回退往往不是报错，而是行为悄悄变差。这里的评测集全部来自线上真实问题，在 CI 中离线运行，不调用模型、不花钱：

| 指标 | 数值 |
| --- | --- |
| 评测集 | 23 个 |
| 用例 | 285 条 |
| 显式反例 | 243 个 |

它们拦住过的典型问题：B 站返回 412 风控、YouTube 要求人机验证、提交失败但界面没有任何提示、失败任务仍被扣费、转录本过短仍去调用模型。还没关闭的问题也保留在评测里，不能通过改状态销项。

详见 [评测汇总](docs/evals/SUMMARY.md)。

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
                   元数据与质量门禁 → 写入 / 投递
```

每个阶段都是 LangGraph 节点。单个条目失败不会阻断同一任务中的其他条目；中间产物使用临时目录，任务结束后清理。

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

---

## 🛠️ 自托管与开发者

也可以不用在线服务，在本机直接运行，笔记直接写进本地 Obsidian vault。

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

开发时可通过 `VITE_API_BASE_URL` 指向本地 FastAPI 服务。

### 云端部署

生产形态是云服务器上的多用户 API：FastAPI 负责鉴权和任务编排，反向代理提供 TLS，GPU 阶段通过 Colab CLI 连接用户自己的 Colab 会话。

```dotenv
V2MD_REMOTE=colab-cli
V2MD_OCR_REMOTE=colab-cli
V2MD_CLIPS_REMOTE=colab-cli
V2MD_COLAB_SESSION=v2md-whisper
```

T4 不可用时默认明确报错；只有设置 `V2MD_COLAB_CPU_FALLBACK=on` 才允许 CPU 降级。部署前请阅读 [架构索引](docs/architecture/README.md)、[运行拓扑与部署](docs/architecture/02-runtime-topology.md)、[HTTP 契约与鉴权](docs/architecture/03-api-contract.md)、[安全检查清单](docs/architecture/05-security.md) 和 [灾备 runbook](docs/architecture/09-disaster-recovery.md)。

平台工程部分（Kubernetes 清单、多 GPU 供应商路由、OpenTelemetry / Prometheus / Langfuse 可观测性）只在多副本部署时需要，本地自用可以忽略。

### 测试与质量检查

```bash
cd backend
uv run ruff check video2md acctkit scripts
uv run mypy video2md acctkit
uv run pytest

cd ..
bash scripts/smoke_ci.sh

cd frontend/web
npm run build
npm run test:integration
```

CI 在 push 和 pull request 中运行后端测试、评测回放、lint、进程级冒烟测试及 Docker 构建。

### 项目结构

```text
video2md/
├── backend/video2md/   LangGraph 管线、FastAPI、存储和任务状态
├── backend/acctkit/    账号鉴权模块
├── backend/gpu_plugins/    算力池 provider 与参考实现
├── backend/tests/      契约、单元和回归测试
├── frontend/web/       Vue Web 前端
├── deploy/             Docker、Kubernetes 和反代配置
├── scripts/            部署、备份、恢复、冒烟和评测回放脚本
└── docs/               设计决策、架构、评测和研究资料
```

## 📖 相关文档

- [设计决策](docs/design-decisions.md)
- [评测汇总](docs/evals/SUMMARY.md)
- [系统总览](docs/architecture/01-overview.md)
- [API 契约](docs/architecture/03-api-contract.md)
- [数据与一致性](docs/architecture/04-data-and-consistency.md)
- [前端说明](frontend/web/README.md)

## 🤝 参与开发

欢迎提交 Issue 和 Pull Request。新增功能或修复问题时，请同时补充对应测试，并在提交前运行与 CI 对应的本地检查。

项目仍在持续开发中，部署参数、API 字段和前端页面可能随版本调整。

## ⚖️ 许可证

[MIT](LICENSE)

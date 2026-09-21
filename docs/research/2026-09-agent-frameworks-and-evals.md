# 2026 最新 Agent 框架 & 评测框架调研

> 数据快照：2026-09-21。Star 数为抓取时 GitHub 实时值。
> **筛选原则：只收录当前最新流行、活跃维护的技术栈；已进入维护模式、停止演进或被官方弃用的归入文末「⚠️ 已过时」附录，不作为选型推荐。**

---

# Part 1: Agent 开发框架

## 1.1 一句话选型结论（2026 社区共识）

| 需求 | 选它 |
|---|---|
| 生产级有状态编排 | **LangGraph** |
| 快速原型 / 角色扮演多 Agent | **CrewAI** |
| 轻量官方 SDK、provider 无关 | **OpenAI Agents SDK** |
| GCP 全家桶 / code-first | **Google ADK 2.0** |
| 类型安全 / 开发者体验 | **Pydantic AI** |
| 极简（核心 ~1000 行） | **smolagents** |
| TypeScript 技术栈 | **Mastra**（备选 VoltAgent） |
| 低代码平台 | **Dify** / Coze Studio |
| AWS 生态企业落地 | **Strands Agents** |
| 长时程 SuperAgent harness | **DeerFlow 2.0** |

## 1.2 头部框架卡片

### LangGraph — 生产首选 · 42.0k★
- 定位：低层级有状态 Agent 编排框架，图式状态机 + 工程化治理
- 信号：Klarna/Replit/Elastic 生产在用；中文社区横评一致列为「生产环境首选」；字节 DeerFlow 2.0 等上层产品直接构建于其上
- https://github.com/langchain-ai/langgraph

### CrewAI — 快速原型首选 · 58.8k★
- 定位：角色扮演式多 Agent 编排，Crews（自治团队）+ Flows（事件驱动工作流）双原语
- 信号：CLI 持续高频迭代（v0.102+），上手最快
- https://github.com/crewAIInc/crewAI

### OpenAI Agents SDK — 轻量官方 · 29.6k★
- 定位：Swarm 的正式继任，轻量多 Agent 工作流 SDK
- 差异化：provider 无关（100+ LLM）、handoffs / guardrails / sessions / tracing、内置 MCP、实时语音 Agent；issue 积压极少，工程质量口碑好
- https://github.com/openai/openai-agents-python

### Google ADK 2.0 — 云厂全家桶 · 21.6k★
- 定位：Google code-first Agent 开发套件
- 差异化：图工作流运行时 + 多 Agent 组合 + OpenAPI/MCP 工具生态 + 一键部署 Cloud Run/Vertex AI；双周发版
- https://github.com/google/adk-python

### Pydantic AI — 类型安全口碑位 · 20.1k★
- 定位：Pydantic 团队出品，全链路类型安全
- 差异化：类型化依赖注入 / 结构化输出 / durable execution / 实时语音；DX 口碑极佳
- https://github.com/pydantic/pydantic-ai

### smolagents — 极简口碑位 · 29.4k★
- 定位：HuggingFace 极简框架（核心约 1000 行）
- 差异化：**CodeAgent**——Agent 直接写 Python 代码而非 JSON tool call，官方称少用 30% 步数；支持 MCP / E2B 沙箱
- https://github.com/huggingface/smolagents

### Mastra — TypeScript 阵营头号 · 28.2k★
- 定位：TS 生态 Agent 框架（Gatsby 原班人马）
- 信号：HN「agent framework」2025+ 搜索热度第一（Show HN 442 分；1.0 发布 213 分）；40+ 模型路由、图工作流、内置 evals
- 备选：VoltAgent（10.6k★）
- https://github.com/mastra-ai/mastra

### Agno — 性能向平台 · 42.3k★
- 定位：Agent 平台三件套（SDK + AgentOS 运行时 + 管理 WebUI）
- 信号：HN 上以性能营销出圈；100+ 工具集成、生产 API、RBAC
- https://github.com/agno-agi/agno

## 1.3 2025–2026 新出现的框架（重点关注）

| 框架 | 背景 | 信号 |
|---|---|---|
| **Microsoft Agent Framework 1.0**（13.6k★） | AutoGen + Semantic Kernel 的官方合并继任，.NET+Python 双语言，图编排 + OpenTelemetry + 声明式 YAML | 2025-10 发布生产版，企业 .NET 市场 |
| **Strands Agents**（7.4k★） | **AWS 官方**，模型无关，默认 Bedrock | 2025-05 发布，企业渗透中 |
| **DeerFlow 2.0**（82.8k★） | 字节，从 Deep Research 完全重写为「长时程 SuperAgent harness」，基于 LangGraph，带 skills/MCP/IM 渠道 | 登顶过 GitHub Trending 总榜 |
| **AgentScope 2.0**（32.0k★） | 阿里/ModelScope | 新增 A2A 协议、实时语音、飞书/钉钉/Discord IM 渠道 |
| **HKUDS/nanobot**（48.4k★） | 港大 Data Science Lab，超轻量个人 Agent | 2025-26 现象级新秀，爆发式增长 |
| **Spring AI Alibaba**（10.9k★） | Java 阵营 | 图工作流 + A2A（经 Nacos）+ MCP |
| **Jido 2.0**（Elixir）/ Embabel（JVM） | 非 Python 生态 | HN 323 分 / 47 分 |

## 1.4 国产力量（平台/框架/Java 全谱系）

| 项目 | Stars | 定位 |
|---|---|---|
| **Dify** | 157k | 开源 LLM 应用平台绝对头部：可视化工作流 + RAG + Agent |
| **DeerFlow 2.0** | 82.8k | 字节 SuperAgent harness（见上） |
| **MetaGPT → MGX** | 70.5k | 多 Agent 鼻祖（ICLR oral），2025 重心转商业产品 MGX（Product Hunt 日/周榜第一） |
| **AgentScope 2.0** | 32.0k | 阿里，A2A + 实时语音 + IM 渠道 |
| **Coze Studio** | 21.6k | 字节开源扣子平台，Golang+React |
| **Qwen-Agent** | 17.1k | Qwen 官方框架，MCP/代码解释器/RAG |
| **Spring AI Alibaba** | 10.9k | Java 生态 Agentic 框架 |

## 1.5 协议层：MCP + A2A（2025-26 多 Agent 系统核心话题）

- **MCP**（90.5k★）：已是 Agent↔工具 的事实标准协议；OpenAI Agents SDK、ADK、smolagents、Mastra、Qwen-Agent 等几乎全部内置支持
- **A2A (Agent2Agent)**（25.9k★）：Google 贡献给 **Linux Foundation** 的 Agent↔Agent 通信协议，多语言 SDK 齐全；AgentScope 2.0、Spring AI Alibaba 已落地
- 社区判断：两者互补共存 —— MCP 管工具，A2A 管 Agent 互联

## 1.6 社区趋势（HN / 中文论坛信号）

1. **「框架疲劳」情绪明显**："Sick of AI Agent Frameworks" 类帖子批评 LangGraph/CrewAI/AutoGen 并列；新项目常以「反框架」「极简」为卖点
2. **Durable execution / 持久化工作流**成新热点（跨框架的 durable workflow 方案密集出现）
3. **框架互操作**：一键把各框架项目转成 MCP server / A2A server 的工具、「framework of frameworks」
4. **生产化议题升温**：audit logs、防重复执行、多 Agent 生产管理
5. **Agent Skills 生态霸榜 GitHub Trending**：Claude Code/Cursor/Codex 技能包、skill registry——「Agent 能力下沉为可复用 skill 文件」是 2026 明确趋势
6. 中文社区选型讨论收敛为三分法：**平台型（Dify/Coze）vs 代码型（LangGraph 系）vs 轻量型（smolagents/Pydantic AI）**

---

# Part 2: 评测框架与基准

## 2.1 评测/观测工具（Eval Tooling）

### 一句话选型

| 需求 | 选它 |
|---|---|
| 开源自托管、生态最大 | **Langfuse** |
| pytest 式 CI 测试 | **DeepEval** |
| 安全 / 红队扫描 | **promptfoo** |
| RAG 专项 | **RAGAS** |
| OTel 原生化 | **Arize Phoenix** / **TruLens** |
| all-in LangChain 生态 | **LangSmith**（闭源） |
| 生产 trace 自动反哺 eval | **Braintrust**（闭源） |

### 第一梯队（开源）

| 工具 | Stars | 定位与差异化 |
|---|---|---|
| **Langfuse** | 34.9k | 开源 LLM 工程平台：tracing + evals（LLM-as-judge/用户反馈/数据集）+ prompt 管理；MIT；底层 ClickHouse；100+ 集成；2026-01 并入 ClickHouse。https://github.com/langfuse/langfuse |
| **promptfoo** | 25.3k | CLI 优先评测 + **红队/安全扫描**，CI/CD 友好；已并入 OpenAI 仍开源。https://github.com/promptfoo/promptfoo |
| **Comet Opik** | 22.2k | 开源全生命周期平台：agent trace、LLM-as-judge、online eval rules、Agent Optimizer；Apache-2.0 全自托管。https://github.com/comet-ml/opik |
| **DeepEval** | 18.4k | 「Pytest for LLM」：G-Eval、幻觉、任务完成度、**agent 轨迹/RAG/多轮/MCP** 指标。https://github.com/confident-ai/deepeval |
| **RAGAS** | 15.8k | **RAG 评测事实标准**（忠实度/相关性/上下文精度），扩展至 agent 评测 + 合成测试集生成。https://github.com/explodinggradients/ragas |

### 第二梯队（可观测性向）

- **Arize Phoenix**（11.6k★，ELv2）：OTel 原生 tracing、实验/数据集/prompt playground，带 AI 调试代理 PXI
- **Helicone**（6.2k★）：AI Gateway + 观测一体化，一行接入
- **TruLens**（3.6k★，Snowflake 系）：OTel 底座，主打 **agentic evaluators**（逻辑一致性、计划遵循、工具选择等 7 类）
- **W&B Weave**（1.1k★）：装饰器式 tracing + evals

### 闭源商业双雄

- **LangSmith**（LangChain）：闭源 SaaS，LangChain/LangGraph 零配置接入是最大卖点；2026-05 推出自研 SmithDB；$39/seat + 按 trace 计费
- **Braintrust**：「active observability」——从生产 trace 自动发现 pattern 并转成 eval；客户 Notion、Coursera
- **成本参考**（Langfuse 官方对比，500k traces / 5 用户 / 12 个月留存）：**Langfuse $621/mo vs LangSmith $3,895/mo**。https://langfuse.com/faq/all/langsmith-alternative

### 标准层

- **OpenTelemetry GenAI 语义约定**：标准化 `gen_ai.*` span/属性/事件/指标（含 Anthropic、Bedrock、OpenAI、MCP 专项），已成各家 tracing 互操作事实标准。https://opentelemetry.io/docs/specs/semconv/gen-ai/

## 2.2 Agent Benchmark 基准

### 代码/软件工程类

| 基准 | 测什么 | 当前状态 / SOTA |
|---|---|---|
| **SWE-rebench**（Nebius） | 按时间窗持续抽新题的**抗污染** SWE 榜单，报 Resolved Rate + Pass@5 + 成本 | 当前榜首仅 ~64.5%（Fable 5 / Grok 4.5 / Opus 5 / GLM-5.2 在 63-65% 区间），比静态榜低 15-20 分，被视为更真实的 coding 温度计。https://swe-rebench.com/ |
| **Terminal-Bench 4.0**（Stanford/Laude） | **终端环境长程任务**，报 resolution rate + 成本 + token | HN 热度高；Cognition SWE-2 宣称 TB 2.1 上 92.8；已衍生 Terminal-Bench-Science。https://www.tbench.ai/ |
| **SWE-bench Pro**（Scale） | OpenAI 推荐的 SWE-bench Verified 替代 | 注意：2026-07 OpenAI 称其公开集约 30% 有评测缺陷，解读需谨慎 |
| **BFCL v4**（Berkeley Gorilla） | 函数调用榜单已演进为**整体 agentic 评测**（web search、memory、prompt variation），live 数据模式 | 工具调用能力事实标准榜单。https://gorilla.cs.berkeley.edu/leaderboard.html |

### 网页/OS/通用数字能力类

| 基准 | 测什么 | 要点 |
|---|---|---|
| **OSWorld 2.0 / Verified** | 真实 OS（Ubuntu/Windows/macOS）开放式桌面任务，执行式判分 | computer-use agent 核心标尺；发布时人类 72.36% vs 最佳模型 12.24%。http://osworld-v1.xlang.ai/ |
| **GAIA**（Meta/HF） | 466 道「对人简单对 AI 难」的通用助理题（浏览/工具/多模态） | general assistant 方向被引最多；国产 agent 营销常用「GAIA 75.7」类跑分，需防污染解读。https://huggingface.co/spaces/gaia-benchmark/leaderboard |
| **WebArena** 家族（CMU） | 自托管真实网站上的长程任务 | 衍生 VisualWebArena、WorkArena；「可复现环境统一评测」代表。https://webarena.dev/ |
| **TheAgentCompany**（CMU, ICML 2025） | 模拟小型软件公司的职业任务 | 发布时最强 agent 仅完成 ~30%。https://the-agent-company.com/ |

### 对话/客服/多轮/深度研究类

| 基准 | 测什么 | 要点 |
|---|---|---|
| **τ²-bench → τ³-bench**（Sierra） | 工具调用 + LLM 模拟用户 + **数据库终态判分**；提出 **pass^k** 一致性指标 | τ² 引入 dual-control（用户也能操作环境）；τ³（2026）加知识检索 + 全双工语音。当前 τ² 榜首 ~87.9%。https://taubench.com/ |
| **BrowseComp**（OpenAI） | 1266 道需「持久且创造性翻找互联网」的难题 | 已成各家 Deep Research 产品标配跑分 |
| **MultiChallenge**（Scale） | 多轮对话四类挑战，LLM-as-judge + 实例级 rubric | 发布时所有前沿模型 <50% |
| **HLE**（Scale/CAIS，已上 Nature） | 2500 道跨 100+ 学科专家级闭卷题 | 当前榜首 Gemini 3 Pro 38.3%；已推 HLE-Rolling 动态防污染版。https://agi.safe.ai/ |

## 2.3 评测方法论三件套（社区共识）

1. **Trajectory eval（轨迹评估）**：评过程不只看结果——步数/冗余调用/死循环/必要步骤四维度；Agent-as-a-Judge 用 agent 评 agent 并给中间反馈
2. **pass^k（一致性指标）**：同一任务跑 k 次全过的概率，揭示 agent 不可靠性（GPT-4o 发布时零售域 pass^8 <25%）
3. **Online eval 闭环**：生产 trace → 聚类发现失败模式 → 一键转评测数据集（LangSmith Insights、Braintrust Loop、Langfuse、Opik 均为此叙事）

## 2.4 Benchmark 可信度排序（当前社区共识）

> 持续更新/抗污染榜单（SWE-rebench、Terminal-Bench、τ 系列、BFCL live）> 专家私有题（HLE、GDPVal）> 静态老牌榜单（需防污染解读）

---

# ⚠️ 附录 A：已过时 / 不再推荐（仅供识别，勿选型）

| 项目 | 状态 | 说明 |
|---|---|---|
| **AutoGen**（61.1k★） | ❌ **已进入维护模式**（仅 bug/安全修复） | 微软官方导向继任者 **Microsoft Agent Framework**；社区分支 AG2（4.9k★）v1.0 重写为协议驱动。新项目不建议选型 |
| **SWE-bench Verified** | ❌ **已被 OpenAI 官方弃用**（2026-02-23） | 审计 138 题发现 59.4% 测试用例缺陷 + gold patch 训练污染 + SOTA 饱和（~81%）。替代品：SWE-rebench、SWE-bench Pro（后者亦有争议）。https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/ |
| **OpenAI Evals** | ⚠️ 维护明显放缓 | 19.5k★ 但仅 691 commits、175 open issues 积压；OpenAI 生态重心转向 Dashboard 托管评测 |
| **AgentBench**（清华） | ⚠️ 历史基线 | ICLR 2024 早期多维基准，如今更多作为训练环境和历史对照 |
| **Deepchecks** | ⚠️ 相关性下降 | 传统 ML 验证起家，LLM/agent 评测覆盖薄弱 |
| **CAMEL / Haystack / DB-GPT** | ⚠️ 存量维护 | 仍活跃但已非社区讨论热点，新项目选型率低 |

# 附录 B：主要信源

- OpenAI SWE-bench 退役公告：https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/
- 榜单官网：swebench.com / swe-rebench.com / tbench.ai / taubench.com / agi.safe.ai / gorilla.cs.berkeley.edu / webarena.dev / the-agent-company.com / osworld-v1.xlang.ai
- GitHub：langchain-ai/langgraph、crewAIInc/crewAI、openai/openai-agents-python、google/adk-python、pydantic/pydantic-ai、huggingface/smolagents、mastra-ai/mastra、agno-agi/agno、microsoft/agent-framework、strands-agents/sdk-python、langgenius/dify、bytedance/deer-flow、coze-dev/coze-studio、agentscope-ai/agentscope、QwenLM/Qwen-Agent、alibaba/spring-ai-alibaba、HKUDS/nanobot、google-a2a/A2A、modelcontextprotocol/servers、langfuse/langfuse、promptfoo/promptfoo、comet-ml/opik、confident-ai/deepeval、explodinggradients/ragas、Arize-ai/phoenix、truera/trulens
- OTel GenAI 规范：https://opentelemetry.io/docs/specs/semconv/gen-ai/
- Langfuse vs LangSmith 成本对比：https://langfuse.com/faq/all/langsmith-alternative
- HN（Algolia API）："agent framework" 2025+、SWE-bench 污染讨论（466 分）、METR "SWE-bench passing PRs would not be merged"（278 分）
- 中文（搜狗聚合）：掘金《AI Agent主流框架对比》(2025-03)、博客园《2025年主流AI Agent框架调研》(2026-01)、SegmentFault/Datawhale《2025年值得入坑AI Agent的11个顶级框架》(2025-07)、CSDN《Langfuse VS LangSmith》

# 局限性说明

- Reddit（r/LocalLLaMA、r/AI_Agents）对本环境返回 403 未抓取；InfoQ 接口 500；知乎/掘金正文为 JS 渲染页，中文结论部分来自搜索结果摘要
- Star 数为 2026-09-21 抓取时刻快照，会随时间变化
- SOTA 分数来自各榜单/厂商宣称，营销跑分（如 GAIA）需结合防污染解读

# 论坛调查：什么「胶水代码」最快？

> 调查日期：2026-09-20/21 · 方法：HN（Algolia API 帖子+评论）、V2EX、Reddit（被反爬，改用 HN 交叉验证）
> 时效原则：以 2024–2026 年讨论为主干；早于 2023 年的来源仅在被近年讨论反复引用时保留，并显式标注 [经典老帖]。

## TL;DR

1. **写得最快 = Python（+uv 单文件脚本），且 2026 年的新共识是「胶水代码让 LLM 写」**——人写高层结构，LLM 补胶水，这是 HN 2026 年 1 月高赞评论的原话"full circle"。
2. **跑得最快 = 瓶颈几乎从不在胶水层**；胶水层是 IO 编排，真正的热路径交给编译型工具（ffmpeg、C 扩展、DuckDB）。2025 年 HN 仍在复读"命令行工具比 Hadoop 集群快 235 倍"。
3. **脚本 vs 框架 = 社区压倒性站"直白脚本"这一侧**：2025 年 3 月 Ask HN「你用过的最简单编排工具」最高赞答案是"用 Java/Python 写直白的程序"；框架（n8n/Temporal/Dify）的价值在**集成目录、可视化、重试耐久性**，不在速度。
4. 对 dify-video2pdf 的结论：**现在的 Python 胶水脚本 + Dify 双轨就是社区共识的最优解**，不用动；要提速加 `uv run` 单文件脚本和 asyncio，不值得为此上任何新框架。

---

## 维度一：写得最快的胶水语言/工具

### 2024–2026 新共识：LLM 写胶水，人写骨架

- **「我已经完全绕回这个模式：人不借助助手写高层代码，LLM 自动补全胶水代码和函数实现草稿。」** —— HN 评论，2026-01-12
  https://news.ycombinator.com/item?id=46585815
- **「我让 LLM 干的活大部分是胶水代码或琐碎功能。」** —— HN 评论，2025-07-28
  https://news.ycombinator.com/item?id=44716109
- Lutra.ai（YC 系）整个产品就是「用 LLM 生成应用/API 之间的胶水代码」，HN 2024-04：
  https://news.ycombinator.com/item?id=40164928
- 反方声音（个别观点）：「一堆公司在 LLM 上糊胶水代码就当护城河，不配拿投资」——HN 2025-08，说明"LLM+胶水"已普遍到引发反弹。
  https://news.ycombinator.com/item?id=45015404

### Python 仍是胶水语言之王（生态决定论）

- V2EX《Lua 还是 Python 关于胶水语言的选择》（46 回复）[经典老帖 2020，但观点至今被引用]：最高赞共识——**「胶水的话还是 python 更好一些，轮子多」**；Lua 定位是嵌入式，出了游戏圈生态不行；也有人提 shell/perl 是老牌胶水。
  https://www.v2ex.com/t/642047
- HN《How did Python become the lingua franca of ML/AI?》（78 分 95 评论，2021）[经典老帖]：最高赞——Python 一直是 C 库的高层胶水，ML 起飞时天然接管 GPU 底层代码的编排；「科学家不想管内存管理，只想写数学诗」。
  https://news.ycombinator.com/item?id=29171319
- HN 评论搜索「python as glue」：调异步子程序、粘 C 库是 Python 的"公认用例"（acknowledged use case）。
  https://news.ycombinator.com/item?id=41200206 · https://news.ycombinator.com/item?id=5317108

### 2025–2026 的新变量：uv 让 Python 胶水的开发循环快了一个数量级

- HN《uv: An extremely fast Python package and project manager, written in Rust》（753 分，2025-06-23）：
  - 「习惯了 pip 之后，uv 快到让人困惑——好几次我以为没跑成，因为它返回得太快了。」
  - 「没有理由再用 pip；纯 Python 场景也没理由再用 conda」，pyenv/poetry 一并可弃。
  - **「我钟爱 `uv add <deps> --script myscript.py` 再配 `#!/usr/bin/env -S uv run` 的 shebang……我现在习惯性地 vibe-code 小脚本，立即能跑，不用管环境和依赖。」**——这正是"胶水脚本最快开发循环"的 2026 形态。
  https://news.ycombinator.com/item?id=44357411

---

## 维度二：跑得最快的胶水方案

### 社区共识：胶水层的性能瓶颈几乎从不在胶水本身

- HN 评论：Python 受 GIL 限制，吞吐/延迟有天花板——**「但对中小规模场景这无所谓」**（胶水场景正是如此）。
  https://news.ycombinator.com/item?id=45988904
- HN 评论：「Python 比 C 慢，但如果按正确路径——Python 只当胶水粘 C 写的性能关键部分——你完全可以没问题。」
  https://news.ycombinator.com/item?id=41950302

### 单机 shell/CLI 胶水 > 重型分布式框架（2025 年仍在复读的主题）

- 《Command-line Tools can be 235x Faster than your Hadoop Cluster》：2014 年文章，但 2018/2022/**2025 年三次重发分别拿下 438/362/409 分**——社区持续用投票确认这个论点。2025 年那帖（409 分 276 评论）的高赞：
  - 「现在有了 DuckDB 和 clickhouse-local，数据处理性能基本不再是问题，单机高配服务器经常就够了。」
  - 「2014 年之后的可悲之处是情况更糟了——我们把 Airflow、dbt、Snowflake 一层层糊在本来能全放进 RAM 的数据集上，见过创业公司为此每月烧 $5k。」
  - 亲历：「把处理 GB 级 JSON、只有几十 MB/s 的 Bash/Python 脚本用 C# 流式重写后接近内存速度」——**瓶颈在写法（流式）不在语言**。
  - 反方提醒（个别观点）：「健壮的 bash 脚本在实践中几乎不存在」。
  https://news.ycombinator.com/item?id=46666085 （2018 版 https://news.ycombinator.com/item?id=17135841 ）

### 极限 HTTP 并发的教训：热路径必须交出 Python 解释器

- 《Million requests per second with Python》（japronto，452 分）[经典老帖 2017]：Python 百万 QPS 可行，但靠的是 C 写的 HTTP 解析器（picohttpparser）；评论区质疑 benchmark 公平性（GOMAXPROCS=1 限制了 Go 对手）。教训：**胶水语言本身不追求极限吞吐，把热路径外包给编译型组件**。
  https://news.ycombinator.com/item?id=13539767

---

## 维度三：LLM/数据流水线——手写脚本 vs 编排框架

### 社区压倒性立场：先用直白脚本，框架是"到不了岸"时才上的

- **Ask HN《What is the simplest data orchestration tool you've worked with?》（49 分 33 评论，2025-03-21）——最切题的一帖**：
  - 最高赞：**「用 Java、Python 这类语言写直白的程序。你列的那些工具都有'此路不通'的终点，区别只是你要花 5 秒、5 天还是 5 个月才发现。」**
  - 「纯 Python 脚本 + cron + Celery 并行 + 日志告警接 Slack，不需要'正经'编排层就能走很远。」
  - 「我自己半天写了一个，跑了 3 年 24/7……比搭 Airflow 花的时间少得多。」
  https://news.ycombinator.com/item?id=43439939

### n8n/Dify 类可视编排的价值：集成目录与低门槛，不是速度

- HN《N8n – Flexible AI workflow automation for technical teams》（195 分 99 评论，2025-05-03）：
  - 好评集中在「自托管简单」「AI 节点方便、适合学习」「原型快」。
  - 反方：「我走了另一条路——学点编程基础然后 vibe code，为什么不直接用 API？」（作者回：不是人人都想走远路，这就是 n8n 存在的原因。）
  - 对 agentic 动态编排能力持怀疑；n8n 已原生支持 MCP。
  https://news.ycombinator.com/item?id=43879282
- n8n 首发热帖（728 分 196 评论）[经典老帖 2019]：价值=自托管+集成目录；被问最多的问题是「和 Node-RED 有什么区别」。有个抖机灵高赞：「最简单的胶水？pipe 操作符。」
  https://news.ycombinator.com/item?id=21191676

### Temporal：DX 好但重，适合"不能丢"的长流程

- 「写 Temporal 工作流的 DX 很好，但定价和托管要求让很多项目够不着。」（2024-07）https://news.ycombinator.com/item?id=41016315
- 「本地部署很轻（SQLite），处理 API 重试和组织工作流真的省心，推荐。」（2026-02）https://news.ycombinator.com/item?id=48327121
- 反方：durable workflow 的活动结果缓存读写延迟对很多场景是 dealbreaker（2025-04）https://news.ycombinator.com/item?id=43707367

### 2025–2026 的新胶水层：MCP

- 「MCP 就是 LLM 和真实工具之间的胶水。」（2025-08）https://news.ycombinator.com/item?id=44824193
- 「MCP 让你把杂七杂八的服务直接粘到关键业务上，没有中间商。」（2025-12）https://news.ycombinator.com/item?id=46317873
- 质疑声（2026-01）：「号称'不用写定制胶水代码'——其实做到这点的是 LLM 本身，HTTP API 时代我们就有这能力了。」https://news.ycombinator.com/item?id=46552869

### 胶水代码的哲学（正名）

- O'Reilly《Thinking About Glue Code》HN 讨论（92 分 45 评论，2021）[经典老帖]：高赞共识——**「编程归根结底就是把东西粘起来，胶水不是计算浪费，胶水开发就是软件开发。」** 减少胶水需求的正解是接口形式化（OpenAPI 式契约），而不是嫌弃胶水。
  https://news.ycombinator.com/item?id=27880183
- 《Glue Code Is the Success Condition》（metaobject 博客）多次被投 HN。
  https://blog.metaobject.com/2021/07/glue-code-is-success-condition.html

---

## 对 dify-video2pdf 的落地建议

| 现状 | 社区结论 | 建议 |
|---|---|---|
| Python 胶水脚本串联 下载/切片/ASR/LLM/Dify | Python 是胶水之王，且热路径（ffmpeg/Colab）本来就在外部进程 | **不动**。这是教科书式的正确架构 |
| 想更快 | 2025–26 共识：uv 单文件脚本是最快开发循环 | 胶水脚本迁移到 `uv run` + PEP 723 inline deps；要并发用 asyncio/httpx，不必引入 Go |
| Dify 节点级编排 + 引擎 OTLP 双轨 | Ask HN 2025：直白程序最简；可视编排的价值在 UI/集成不在速度 | **双轨成立**：Dify 买的是可观测性，引擎侧 trace 补盲区，别为"瘦"而拆 |
| 胶水代码本身 | 2026 共识：人写骨架，LLM 补胶水 | 新胶水脚本直接让 agent 起草，人审接口边界 |
| 未来真要上框架的信号 | 社区：出现"重试丢任务/跨机调度/长流程耐久性"痛点再上 Temporal 类 | 16 集批处理规模远没到，暂不上 |

---

## 来源清单（按时效排序）

**2026**
1. https://news.ycombinator.com/item?id=46585815 — 人写高层、LLM 补胶水（2026-01）
2. https://news.ycombinator.com/item?id=46552869 — MCP/LLM 胶水之争（2026-01）
3. https://news.ycombinator.com/item?id=48327121 — Temporal 本地轻量部署好评（2026-02）

**2025**
4. https://news.ycombinator.com/item?id=43439939 — Ask HN 最简单编排工具（2025-03）★最切题
5. https://news.ycombinator.com/item?id=43879282 — n8n 195 分帖（2025-05）
6. https://news.ycombinator.com/item?id=44357411 — uv 753 分帖（2025-06）
7. https://news.ycombinator.com/item?id=46666085 — 235x CLI 帖 2025 重发（409 分）
8. https://news.ycombinator.com/item?id=44716109 — LLM 主要干胶水活（2025-07）
9. https://news.ycombinator.com/item?id=44824193 — MCP 是 LLM 与工具间的胶水（2025-08）
10. https://news.ycombinator.com/item?id=46317873 — MCP 粘合服务（2025-12）
11. https://news.ycombinator.com/item?id=43707367 — durable workflow 延迟质疑（2025-04）

**2024**
12. https://news.ycombinator.com/item?id=40164928 — Lutra：LLM 生成胶水代码（2024-04）
13. https://news.ycombinator.com/item?id=41016315 — Temporal DX/定价（2024-07）

**经典老帖（仍被近年讨论引用）**
14. https://news.ycombinator.com/item?id=29171319 — Python 何以成 ML 通用语（2021）
15. https://news.ycombinator.com/item?id=27880183 — Thinking About Glue Code（2021）
16. https://www.v2ex.com/t/642047 — Lua 还是 Python 胶水语言（2020，46 回复）
17. https://news.ycombinator.com/item?id=21191676 — n8n 首发 728 分（2019）
18. https://news.ycombinator.com/item?id=13539767 — japronto 百万 QPS（2017）
19. https://news.ycombinator.com/item?id=17135841 — 235x CLI 帖 2018 版（438 分）
20. https://blog.metaobject.com/2021/07/glue-code-is-success-condition.html — Glue Code Is the Success Condition

注：Reddit（search.json 403）与 DuckDuckGo（CAPTCHA）被反爬，未纳入直接来源；中文侧以 V2EX 为主。

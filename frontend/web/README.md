# 视书 · Web（frontend/web）

video2md 的移动端 Web 前端：Vue 3.5.43 + Vite 8.3.4 + TypeScript 6.0.3 + Tailwind CSS 3.4.19 + Pinia 2.3.1 + vue-router 4.6.4。

依赖版本以 `package.json` 与 `package-lock.json` 的精确版本为准；安装使用 `npm ci`，不要使用会重新解析版本范围的 `npm install`。
队列、教材库、阅读器、资料与配额使用 FastAPI；登录只提供 Google 授权码流（短信/密码/微信原型已移除）。

## 命令

```bash
npm install        # 安装依赖
npm run dev        # 开发服务器（默认 http://localhost:5173，移动视口 390px 设计）
npm run build      # vue-tsc 类型检查 + vite 生产构建 → dist/
npm run test:integration # Pinia → 真实 HTTP → FastAPI 集成测试（需 backend/.venv）
npx playwright install chromium # 首次运行真实浏览器测试时安装 Chromium
npm run test:e2e          # Chromium 端到端导出测试（Vite 生产预览 + fixture API）
npm run preview    # 预览构建产物
```

## 集成测试

前置：Node 24+，仓根下 `cd backend && uv sync --frozen` 安装后端依赖，
再到 `frontend/web` 执行 `npm ci && npm run test:integration`。
自定义 Python 环境可设 `V2MD_TEST_PYTHON=/absolute/path/to/python`。

测试使用 Node 内置测试运行器，通过 Vite 加载真实 TypeScript 模块，不新增测试依赖。
每次启动独立 FastAPI 子进程，监听随机回环端口；runs、prefs、日志及其他数据全部在
临时目录内，结束后关闭进程并清理文件。只替换外部身份验签，用固定 Alice/Bob 凭据；
关闭 lifespan，预置任务不会启动视频下载、GPU 或 LLM。此套件不验证浏览器渲染、CORS、
真实 OAuth 或图执行，图执行由 `scripts/smoke_ci.sh` 覆盖。

集成用例覆盖：按 owner 列任务、真实进度映射、无后端进展时状态不变、后端完成自动同步、
取消持久化及状态一致性、终态/越权取消、缺失凭据、删除后刷新、恢复/插队失败反馈，
以及收藏/进度部分更新持久化、跨用户隔离、笔记正文读取。

PDF 端到端测试使用 Playwright 启动真实 Chromium 和 Vite，通过 fixture API 驱动教材库，
覆盖弹窗打印、本地 MathJax、鉴权媒体 Blob、deliver 产物 410 提示和媒体超时提示。
CI 会执行 `npx playwright install chromium`；本地若使用系统浏览器，可设置
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`。

队列从后端轮询真实状态；取消、重试、优先执行、删除均通过 HTTP 持久化。
取消是协作式终止，长阶段结束前显示「取消中」；重试只支持 error/partial，保留已完成笔记。
优先执行只重排等待中的任务，不抢占已经运行的任务；删除要求终态，保留用户 vault 笔记及已结算用量。
浏览器 bundle 下载不自动 ACK：成功开始下载不等于文件已被用户保存。
阅读器通过带 Bearer 鉴权的媒体接口获取图片/视频；对象 URL 在切讲或卸载时释放。

## 页面

「我的」页面提供 **LLM API 配置**：填写 OpenAI 兼容的 Base URL、API Key，以及可选模型名称。
保存后用于该用户后续启动的转换任务；运行中的任务保持启动时的配置。API Key 默认隐藏，
保存成功后输入框清空，服务端读接口不返回 Key，浏览器 localStorage 不保存 Key。
已有配置时 Key 留空可保留原值；“恢复服务默认”删除个人配置并回落后端 `LLM_*`。
保存只校验格式，不代表上游余额或连通性已验证。

接口为鉴权的 `GET/PUT/DELETE /settings/llm`。JSON 档写入 `V2MD_PREFS_FILE` 同目录的
`llm-settings.json`（0600，可用 `V2MD_LLM_SETTINGS_FILE` 覆盖）；`V2MD_PREFS_STORE=postgres`
时写入独立的 `v2md_llm_settings` 兼容表。升级 PG 部署前运行
`uv run --project backend python -m video2md migrate`，然后重启后端。
凭据为服务端可读取的配置数据，应按服务端机密文件/数据库管理，不包含在 run 记录与图 checkpoint 中。

| 路由 | 页面 | 说明 |
|---|---|---|
| `/` | 品牌首页与登录入口 | 展示“视书”品牌价值；协议同意后跳 `{API}/auth/google/login`；消费回跳 fragment `#id_token=` / `#auth_error=`，保留过期提示与安全站内回跳；不存在独立 `/login` 页面 |
| `/library` | 教材库 | 搜索/分类/收藏夹过滤、继续阅读横幅、grid/list 视图、目录抽屉、导出弹窗 |
| `/tasks` | 转换队列 | 快速链接导入、导入面板（来源 tab/开关/模板）、任务状态卡、批量管理、3s 后端状态轮询 |
| `/profile` | 个人中心 | 资料卡、配额仪表、SVIP 推广、设置行、会员套餐底部弹层 |

未登录访问受保护路由会跳 `/?redirect=…`；token 存 `localStorage['v2md_token']`，任一 API 返回 401 即清除并跳回品牌首页。
后端 `V2MD_AUTH=off`（仅开发）时无凭据 `GET /me` 返回 200，前端据此进入匿名模式、不要求登录。
生产由 FastAPI 同源托管本 bundle，前端路由不得与 API 路由重名（`GET /queue` 是 gpu_router 回读口，故队列页用 `/tasks`）；
`backend/tests/test_api_contract.py::test_spa_routes_serve_index_not_api` 读取 `src/router/index.ts` 校验这一点。

## Mock → 真实 API 的切换路径

- `src/types/` 类型镜像后端契约（`docs/architecture/03-api-contract.md`、`backend/video2md/models.py`）：
  run 状态词表、错误信封 `{error:{code,message,details}}`、list 形状。
- `src/api/index.ts` 是唯一出口（签名层）；第一版解构自 `src/api/mock/impl.ts`。
- 换真实 API：新建 `src/api/live/*.ts`，用 `src/api/client.ts`（`fetch` + `Authorization: Bearer <token>`，
  BASE_URL 来自 `VITE_API_BASE_URL`，默认 `http://127.0.0.1:8790`），然后把 `src/api/index.ts`
  的来源切到 live 实现——view/store 零改动。
- mock 专用函数 `_setProgress` / `_snapshotRuns`（`stores/queue.ts` 的 3s 进度 tick 用）没有真实
  对应物；切真实 API 时用后端 SSE `GET /runs/{id}/events` 替代并删除 tick。
- 后端 CORS：本地联调需设置 `V2MD_CORS_ORIGINS=http://localhost:5173`。

## 目录

```
src/
├── api/          # 签名层 + mock 实现 + fetch 占位（client.ts）
├── stores/       # Pinia: auth / library / queue / profile / toast
├── composables/  # useCountdown 等无状态逻辑
├── components/   # common / book / queue / profile / art（本地占位图形，零外链图片）
├── views/        # 四个页面
└── types/        # 与后端对齐的共享类型
```

设计还原说明：Tailwind 主题（M3 色板/Inter/自定义间距）移植自设计稿，见 `tailwind.config.js`；
图标使用 Material Symbols Outlined 字体（Google Fonts CDN，唯一外部依赖）；
封面/头像/logo 全部是 CSS 渐变占位（`components/art/`），不依赖外链图片。

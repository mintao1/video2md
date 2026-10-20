# 前端登录链路及主流程测试报告

- 日期：2026-10-19
- 范围：工作区未提交改动（Google 登录强制化）下，从 `/login` 开始的前端全流程
- 前端：Vite dev server `http://localhost:5173`
- 后端：
  - 阶段 A：`V2MD_AUTH=off`，空 secrets 文件，runs/state/users/prefs/vault 均指向临时目录
  - 阶段 B：`backend/tests/fixtures/web_integration_api.py` 固定在 8790（Alice/Bob 凭据，无 Bearer 时返回 401，种子数据含运行中/排队/完成任务和带图笔记）
- 身份模拟：Google 回跳以 `/login#id_token=<token>` 模拟；未走真实 Google OAuth

## 自动化门禁

| 命令 | 结果 |
|---|---|
| `npm run build` | 通过 |
| `npm run test:integration` | 22/22 通过 |

## UI 验收结果

| 场景 | 结果 | 备注 |
|---|---|---|
| 无 token 访问 `/` | 通过 | 跳 `/login?redirect=/library` |
| 登录页渲染 | 通过 | 标题、Google 按钮、说明文案 |
| 本地点击「使用 Google 登录」 | **失败** | 见 L-02 |
| 后端错误回跳 `#auth_error=` | 通过 | 显示「服务端未配置 Google 登录凭据」，fragment 已清除 |
| 回跳 `#id_token=` + `redirect=/queue` | 通过 | token 写入 localStorage，进入 `/queue` |
| 无效/过期 token 遇 401 | 部分通过 | token 被清除并跳回 `/login?redirect=…`；无任何提示，见 L-06 |
| 队列：计数、分页筛选、「转换失败」tab | 通过 | 全部 3 / 转码中 1 / 排队中 1 / 历史完成 1 / 失败 0 |
| 队列「立即阅读」 | 通过 | 进入 `/read/finished`，鉴权图片通过 blob URL 正常显示 |
| 教材库 | 通过 | 显示 1 本、进度 100% |
| 个人中心资料卡 | 通过 | free 用户显示「免费版」 |
| Header 退出（教材库页） | **失败** | 见 L-03 |
| Header 退出（队列页） | 通过 | 3s 轮询遇 401 后跳回登录 |
| 用户隔离（Bob 登录） | 通过 | 只看到 Bob 的 1 个任务 |
| `redirect=//example.com/x` | 通过 | 停留在本站 `//example.com/x`，没有开放重定向 |
| 已登录访问 `/login` | 不符合预期 | 见 L-07 |
| 未知路由 `/nope` | 不符合预期 | 见 L-08 |

上次报告（`frontend-report.md`）的回归结论：F-01 部分修复（资料卡已改，Header 仍写死，见 L-04），F-02 已修复，F-03 已修复。

## 缺陷

### L-01 [P1] 生产同源部署下 `/queue` 被 API 路由占用，刷新/直达显示 JSON

- 复现：`curl -H "Accept: text/html" http://127.0.0.1:8790/queue`
- 实际：`200 application/json`，返回 `{"queue_len":2,"running":1,...}`
- 原因：生产镜像由 FastAPI 同源托管 Vite bundle，SPA 回退只在 `/{path:path}` 生效；`GET /queue` 是现有 API 路由（[api.py:1816](backend/video2md/api.py:1816)），并且 `queue` 也在 `_API_PATH_PREFIXES` 中。
- 影响：用户刷新队列页、打开书签或分享链接会看到原始 JSON。站内点击跳转不受影响。
- 建议：将前端路由改为 `/tasks` 一类不冲突的路径，或给 API 加 `/api` 前缀；同时补一条「所有 SPA 路由在 Accept: text/html 下返回 index.html」的契约测试。

### L-02 [P1] 本地开发无法登录，也无法进入应用

- 复现：本地打开 `/login` 并点击「使用 Google 登录」。
- 实际：地址变为 `/login?redirect=/auth/google/login`，停留在登录页。
- 原因：[LoginView.vue:28](frontend/web/src/views/LoginView.vue:28) 用相对路径 `/auth/google/login`，而本地前端 5173 与 API 8790 不同源，请求被 Vite 当作 SPA 路由，路由守卫随即拦截回登录页。生产同源部署下没有这个问题。
- 文档冲突：README 写明「首期前端不提供登录入口，打开 `/` 即可进入应用；`V2MD_AUTH=off` 使用固定 dev 身份」，但新路由守卫在没有 token 时一律拦截，所以 `AUTH=off` 本地开发也只能手动往 localStorage 写 token 才能进入。
- 建议：登录地址使用 `${BASE_URL}/auth/google/login`；`AUTH=off` 时提供开发身份入口或放行守卫；README 与实现取其一并统一。

### L-03 [P2] Header「退出」不跳转，私有数据仍留在屏幕上

- 复现：在 `/library` 点击 Header「退出」。
- 实际：token 已清除，但页面停留在 `/library`，上一个用户的书单仍可见；要等下一次站内跳转才会回到登录页。只有队列页靠 3s 轮询收到 401 才会跳走。
- 原因：[AppHeader.vue:55](frontend/web/src/components/common/AppHeader.vue:55) 只调用 `auth.logout`。另外 `/profile` 原有的「退出登录」按钮已在本次改动中删除，而个人中心没有 Header，所以该页没有退出入口。
- 建议：退出后执行 `router.replace('/login')` 并重置各 store（books/queue/profile）；恢复个人中心的退出入口。

### L-04 [P2] Header 会员徽章写死「Pro 会员」

- 复现：用 free 用户（Alice/Bob/dev，`/me` 返回 `plan: "free"`）打开教材库或队列。
- 实际：Header 显示「Pro 会员」（[AppHeader.vue:46](frontend/web/src/components/common/AppHeader.vue:46)），与资料卡上的「免费版」矛盾。
- 建议：按 `auth.user.plan` 渲染，free 用户不显示或显示「升级」。

### L-05 [P2] 真实 OAuth 往返会丢失 redirect

- 静态复核：登录按钮不携带 `redirect`；后端回调固定 302 到 `{WEB}/login#id_token=…`（[api.py:1305](backend/video2md/api.py:1305)）。因此从 `/read/xxx` 被 401 弹出的用户，登录后只会进入 `/library`。
- 建议：发起时把 redirect 放进 state 或 cookie，回调时带回，并且只接受站内路径。

### L-06 [P3] token 失效被踢回登录页时没有提示

- [client.ts:51](frontend/web/src/api/client.ts:51) 遇到 401 后整页跳转，登录页没有「登录已过期」一类提示，用户会觉得自己被莫名登出。

### L-07 [P3] 已登录用户仍可看到登录页

- 新守卫对 `/login` 一律放行（旧守卫会把已登录用户送到 `/library`）。

### L-08 [P3] 未知路由为白屏

- `/nope` 渲染为空页面。建议增加 catch-all 路由，跳到 404 页或 `/library`。

### L-09 [P3] 文案/数据与用户状态不符（观察项）

- free 用户的 SVIP 横幅按钮文案为「立即续费」（[SvipPromoBanner.vue:28](frontend/web/src/components/profile/SvipPromoBanner.vue:28)）。
- 资料卡「已生成教材」显示 0，而 Alice 实际有 1 本已完成教材。
- 教材库「上次同步于 2 分钟前」为写死文案（[LibraryView.vue:196](frontend/web/src/views/LibraryView.vue:196)）。

## 待确认（无法在本地验证）

- `V2MD_WEB_BASE_URL` / `V2MD_PUBLIC_BASE_URL` 没有出现在任何部署清单中，默认值是 `http://localhost:5173` / `http://127.0.0.1:8790`。如果生产 secret 文件 `/run/video2md.env` 也没有设置，Google 的 redirect_uri 和登录回跳都会指向 localhost，导致生产登录失败。需要部署 owner 核对 secret。

## 未覆盖

- 真实 Google 授权页及换票、生产 TLS/Nginx、PostgreSQL 存储档、真实视频转换、移动端多断点。

## 复现环境

```bash
# 前端
npm --prefix frontend/web run dev -- --port 5173 --strictPort
# 后端（阶段 B）
cd backend && V2MD_TEST_PORT=8790 V2MD_TEST_CORS_ORIGIN=http://localhost:5173 \
  .venv/bin/python tests/fixtures/web_integration_api.py "$(mktemp -d)"
curl -X POST http://127.0.0.1:8790/__tests/reset
# 浏览器模拟登录
open 'http://localhost:5173/login?redirect=%2Fqueue#id_token=integration-alice'
```

## 修复记录（工作区，未提交）

| 编号 | 状态 | 改动 | 验证 |
|---|---|---|---|
| L-01 | 已修复 | 前端队列页路由 `/queue` → `/tasks`（router、BottomNav）；`GET /queue` 保留给 gpu_router。新增 `test_spa_routes_serve_index_not_api`：读取 `src/router/index.ts`，断言每个前端路由在同源托管下都返回 index.html | 新测试通过；把路由改回 `/queue` 时测试变红 |
| L-02 | 已修复 | 登录按钮跳 `${BASE_URL}/auth/google/login`；路由守卫在无 token 时以无凭据 `GET /me` 探测，返回 200 即进入匿名模式（`V2MD_AUTH=off`）；两份 README 按实现更新 | AUTH=off：无 token 直接进入，`/login` 跳回 `/library`。fixture（401）：未登录跳登录页，点击按钮到达后端并带回 `auth_error`，`#id_token` 回跳进入 `/tasks` |
| L-07 | 随 L-02 修复 | 已登录访问 `/login`（无回跳 fragment）时跳 `/library` | 浏览器验证 |

门禁：`npm run build` 通过；`npm run test:integration` 22/22；`pytest tests/test_api_contract.py` 79/79。

### 第二轮（L-03 ~ L-09 + 生产 URL）

| 编号 | 状态 | 改动 | 验证 |
|---|---|---|---|
| L-03 | 已修复 | `auth.logout` 清 token 后整页 `location.replace('/login')`，所有 Pinia store 随页面重载清空；个人中心恢复「退出登录」按钮。AUTH=off 匿名模式下没有 token，两处退出入口都隐藏 | 浏览器：教材库 Header 退出、个人中心退出均直接到 `/login`，token 已清 |
| L-04 | 已修复 | Header 徽章按 `auth.user.plan` 渲染（free 不显示，pro/svip 显示对应文案）；Header 挂载时拉 `/me` | 浏览器：Alice（free）Header 无徽章 |
| L-05 | 已修复 | 前端发起时带 `?redirect=`（只收站内路径）；后端 `_google_safe_redirect` 校验后随 state 记账，回调落到 `{WEB}/login?redirect=…#id_token=`；错误出口同样带回 redirect | 新增 `test_auth_google_redirect_round_trip`（含 `//evil`、`/\evil`、绝对 URL 拒收）；浏览器：`/login?redirect=/read/finished` 点按钮 → 后端 → 回到同一 redirect |
| L-06 | 已修复 | 持 token 遇 401 时跳 `/login?redirect=…&reason=expired`，登录页显示「登录已过期，请重新登录」 | 浏览器：写入无效 token 访问 `/tasks` |
| L-07 | 已修复（第一轮） | — | — |
| L-08 | 已修复 | 增加 catch-all `/:pathMatch(.*)*` → `/library`；SPA 路由契约测试跳过 catch-all 模式 | 浏览器：`/nope/deeper` → `/library` |
| L-09 | 已修复 | ①SVIP 横幅 free 显示「立即升级」、付费档「立即续费」；②`/quota.stats.books` 改为统计本人未删的 done/partial run（`membership.stats_books` 无写入方，恒为 0）；③教材库同步时间取最近一次成功加载时刻 | 新增 `test_quota_books_counts_finished_runs`；浏览器：Alice 资料卡显示 1，教材库显示真实时间 |
| 待确认项 | 已落仓 | `deploy/k8s/base/deployment-api.yaml` 的 api 与 migrate 容器设 `V2MD_PUBLIC_BASE_URL` / `V2MD_WEB_BASE_URL` = `https://ferosse.tech`（真实环境变量优先于 secret 文件）；`config.py` 新增启动期校验：production + `V2MD_AUTH` 含 google 时两键必须是 https | 新增 `test_production_google_requires_public_urls`；本地 `kubectl kustomize deploy/k8s/overlays/prod` 两个容器都渲染出两键；`curl https://ferosse.tech/healthz` 200 |

附带修复：`tests/test_resolve.py` 在模块级替换全局 `subprocess.run` / `time.sleep` 且不还原，全量 pytest 时会污染后续调用子进程的用例（`_import_config_with_env` 拿到假的 yt-dlp 输出）。现在跑完后还原。

门禁：`npm run build` 通过；`npm run test:integration` 22/22；`pytest tests/test_api_contract.py` 82/82；`scripts/gates/check-hard-constraints.sh`（默认范围，以及 `deploy`）无违规。

全量 `pytest tests` 仍有 17 个失败，均为存量问题，与本轮改动无关：干净 HEAD 的 worktree 跑全量是 21 个失败，本轮修掉 test_resolve 污染后降到 17。剩余失败分布在 `test_colab_cli`（9）、`test_api_contract` 中受跨文件顺序影响的 5 个（单文件跑全部通过）、`test_llm`（1）、`test_vendor_acctkit`（2，vendored acctkit 与上游不一致）。

上线前需人工确认：Google Cloud 控制台 Web client 的「已授权的重定向 URI」须包含 `https://ferosse.tech/auth/google/callback`，本地无法验证。

# 生产前端与登录冒烟结果（2026-10-20）

## 范围

- 生产站点：`https://ferosse.tech`
- 浏览器真实 UI：登录页、协议门禁、Google OAuth、已登录 Google 账号选择、OAuth 回跳、受保护路由
- HTTP：首页、`/healthz`、`/me`、`/library`、`/tasks`、`/profile`
- 本地质量门：前端生产构建、前端真实 HTTP 集成测试

## 结果

| 检查 | 结果 | 证据 |
|---|---|---|
| 登录页可达 | 通过 | `GET /` 返回 200；浏览器显示登录页 |
| 未勾选协议时点击 Google | 通过 | 页面不跳转并提示先同意协议 |
| Google OAuth 跳转 | 通过 | 跳转到 Google account chooser，目标为 `ferosse.tech` |
| 真实账号选择 | 通过 | 选择浏览器已有 Google 账号后进入加载态 |
| OAuth 成功建立用户态 | 阻塞/失败 | Google 授权后回到 `https://ferosse.tech/login`，未出现 `#id_token`，未进入 `/library`，未观察到 `localStorage.v2md_token` |
| 未登录访问 `/library` | 通过 | 客户端重定向到 `/login?redirect=/library` |
| `/healthz` | 通过 | HTTP 200，`ok=true`，版本 `0.2.0` |
| `/me` 未认证行为 | 通过 | HTTP 401，错误信封符合预期 |
| SPA 路由直达 | 通过 | `/library`、`/tasks`、`/profile` 均返回前端 HTML |
| 前端生产构建 | 通过 | `npm run build` 成功 |
| 前端真实 HTTP 集成测试 | 通过 | 22/22，0 失败 |

## P0 阻塞

真实 Google 授权后没有形成登录会话。当前表现与之前记录的 token exchange `ConnectError` 一致；故障点在生产后端 OAuth code→token 交换或回调处理链路，而不是前端协议门禁或 Google 账号选择。

建议在生产 API 容器内检查：Google token endpoint 的 DNS/TCP/TLS 出网、代理/网络策略、OAuth client 配置、回调处理日志与 request id。不要在日志中记录授权码、id token 或邮箱。

## 额外发现

生产登录页仍展示“快捷验证码 / 密码登录 / 微信一键登录”入口，而前端 README 说明当前登录只提供 Google 授权码流；需确认这些入口是有意保留的兼容 UI，还是应移除/禁用。


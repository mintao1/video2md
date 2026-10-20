# 生产前端登录与冒烟测试（2026-10-20）

## 范围

- 站点：`https://ferosse.tech/`
- 流程：未认证访问、登录页协议门禁、Google OAuth 跳转、健康检查、前端构建与集成测试。
- 真实账号登录：已走到 Google 账号选择页；未代替用户选择账号或输入凭证。

## 结果

| 用例 | 结果 | 证据 |
|---|---|---|
| 生产首页可达 | 通过 | `GET /` 返回 200，HTML 正常 |
| 未认证访问 `/me` | 通过 | 返回 401，错误信息为“未认证：请求未携带有效凭据” |
| 生产健康检查 | 通过 | `GET /healthz` 返回 `{"ok":true,"version":"0.2.0"}` |
| 未勾选协议点击 Google | 通过 | 页面阻止操作并提示勾选协议 |
| 勾选协议后 Google 登录 | 通过（跳转） | 跳转至 Google OAuth account chooser，`redirect_uri=https://ferosse.tech/auth/google/callback` |
| 真实 Google 账号认证及回跳 | 未完成 | 当前没有代填/提交用户 Google 凭证 |
| 前端生产构建 | 通过 | `npm run build` 成功 |
| 前端集成测试 | 通过 | 22/22 passed |

## 阻塞与下一步

真实登录的最后一步需要用户在当前 Google 页面选择账号并完成 Google 的凭证/二次验证。完成后我可以继续验证回跳 token、`/me` 用户态、教材库、任务队列、阅读器和退出登录。


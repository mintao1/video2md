# 生产全站验收结果（2026-10-21）

## 范围与方法

- 生产入口：`https://ferosse.tech`
- 起点：登录页，使用 computer use 操作真实界面。
- 身份：用户明确授权使用当前 Google 测试身份登录；报告不记录邮箱、
  授权码、OAuth `state`、`id_token`或 MFA 信息。
- 持续授权：用户已明确允许后续评测自动选择当前 Google 测试身份，
  无需每次重复询问；授权仅用于本项目的自动化登录验收，且不在评测资产中持久化任何凭据。
- 辅助证据：使用无凭据 HTTP 请求检查健康、鉴权边界、SPA 直达与公网暴露面。

## 总结

**上轮阻断项已修复并在生产复测通过。**

生产站点可达，Google OAuth 的发起、账号选择和回调域名正确；真实选择
Google 账号后，回调已完成换票和 ID Token 验签，前端正常进入
`/library`，并成功加载需要鉴权的教材库空态。同时已关闭公网 API 文档、
屏蔽匿名 `/queue`，并补齐用户协议与隐私政策页面。

## 用例结果

| 用例 | 结果 | 证据 |
|---|---|---|
| 登录页渲染 | 通过 | 快捷验证码、密码登录、微信和 Google 入口均可见 |
| 快捷验证码/密码切换 | 通过 | 表单控件正常切换 |
| 协议勾选门禁 | 通过 | 未勾选提交时显示协议提示 |
| 空手机号校验 | 通过 | 勾选协议后空提交，显示“请输入有效手机号” |
| 用户服务协议 | 通过 | `/terms` 可公开访问且内容可阅读 |
| 隐私保护政策 | 通过 | `/privacy` 可公开访问且内容可阅读 |
| Google OAuth 发起 | 通过 | 到达 Google account chooser，callback 为 `/auth/google/callback` |
| Google 真实账号回调 | 通过 | 真实账号选择后回调成功，进入 `/library` |
| 未认证 API 拦截 | 通过 | `/runs`、`/cost`、`/me`、`/quota`、`/settings/llm` 返回 401 |
| 健康检查 | 通过 | `/healthz` 返回 200，`ok=true`，版本 `0.2.0` |
| SPA 直达 | 部分通过 | `/library`、`/tasks`、`/profile`、`/read/demo` 返回前端 HTML |
| `/queue` 直达 | 通过 | 公网返回 404，不再泄露队列状态 |
| API 文档公网面 | 通过 | `/docs`、`/openapi.json`、`/redoc` 均被网关拒绝 |
| 登录后教材库 | 通过 | 鉴权后路由和数据加载正常，显示 0 本空态 |

## 本轮联调落地

1. 生产 API 通过受限 SSH CONNECT 隧道访问 Google OAuth；远端仅监听回环地址，
   SSH key 仅允许端口转发到指定 relay。
2. Google 换票与证书拉取统一使用 HTTPX 代理传输，避免 Requests 适配器与
   CONNECT 隧道组合下的 TLS EOF；Google 官方签名、`aud`、`iss`、`exp` 校验保持不变。
3. 生产环境关闭 FastAPI schema/docs，Nginx 再对 `/docs`、`/openapi.json`、
   `/redoc` 进行边界拦截；公网 `/queue` 固定返回 404。
4. 登录页改为与生产能力一致的 Google 入口，并提供 `/terms`、`/privacy`
   公开路由。
5. 当前生产镜像：`video2md:0.2.0-hotfix-20261021g`；OAuth tunnel systemd 服务为
   `active`。

## 复测门禁

已验证真实登录、教材库空态、任务队列页与个人中心。创建真实转换任务会产生
算力和上游 API 成本，不在本次“修复上轮问题”的执行范围内；可在专门的全链路付费验收中
继续覆盖转码、生成、阅读与下载。

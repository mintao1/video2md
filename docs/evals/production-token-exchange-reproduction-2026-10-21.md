# 生产换票不可达复现（2026-10-21）

目标：`https://ferosse.tech`，使用桌面浏览器真实 Google OAuth 流程；不记录授权码、state、id_token、密码或 MFA。

## 结果

| 评测 | 结果 | 证据 |
|---|---|---|
| 登录页与协议门禁 | PASS | `/login` 可达；勾选协议后允许进入 OAuth |
| Google account chooser | PASS | Google 显示“继续前往 ferosse.tech”；回调 URI 为 `https://ferosse.tech/auth/google/callback` |
| 已有账号选择 | PASS | 选择浏览器已有账号后完成 Google 侧授权返回 |
| 后端 code→token 换票 | FAIL | 站内回到 `/login`，页面明确显示 `Google 换票不可达（ConnectError）` |
| `/me` 会话 | BLOCKED | 未生成前端会话，不能宣称登录成功 |
| `/library` 与刷新保持 | BLOCKED | 依赖 `/me=200`，未执行通过判定 |

## 结论

故障已从前端 OAuth 跳转收敛到生产 API 的 Google token endpoint 出口：Google 授权本身成功，后端换票阶段仍为 `ConnectError`。这不是账号、协议复选框、回调 URI 或前端路由问题。

## 下一步（生产容器内执行）

1. 运行 `oauth-token-exchange-unreachable-eval-set.jsonl` 的 `token-exchange-001` 至 `004`，只记录 DNS、TCP/TLS、代理协议和无凭据 HTTP 状态。
2. 确认 API 进程实际使用的 OAuth 出口；不要把仅供 yt-dlp 的 `V2MD_PROXY` 当作 `httpx` OAuth 代理。
3. 修复后先以受控 `/token` 4xx 响应作为网络恢复证据，再重跑本浏览器用例。
4. 只有回调成功、`/me=200`、`/library=200` 且刷新后仍保持会话，才将生产 readiness evidence 的 `browser_google_login.status` 改为 `passed`。

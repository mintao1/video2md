# 生产 OAuth 整改评测结果（2026-10-20）

执行目标：`ferosse.tech` 对应的 `video2md-api-8bcc999d6-tm8rr`，节点 `VM-0-7-ubuntu`。

## 评测结果

| ID | 结果 | 证据 |
|---|---|---|
| oauth-rem-001 IPv4 出网 | FAIL | 容器可解析 IPv4 `142.250.107.95`，但此前 TCP 连接超时；IPv6 直接报 `Network is unreachable` |
| oauth-rem-002 HTTPS 代理生效 | FAIL | `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 均未设置；`V2MD_PROXY` 存在但 OAuth `httpx.AsyncClient` 不会自动使用它；受控 GET 超时 |
| oauth-rem-003 代理路径契约 | FAIL | 代码明确说明 `V2MD_PROXY` 只注入 yt-dlp 子进程；OAuth 换票使用 `httpx.AsyncClient`，生产没有 `HTTPS_PROXY` |
| oauth-rem-004 镜像/版本一致 | FAIL | Pod 镜像为 `docker.io/library/video2md:0.1.3`，健康接口报告 `0.2.0`；生产镜像不存在 `_google_oauth_timeout_s` |
| oauth-rem-005 回调诊断日志 | FAIL | 生产日志仅见 `/auth/google/login → 302`，没有回调失败分类、耗时或 request_id；未发现 secret 泄露证据 |
| oauth-rem-006 真实 Google 登录 | FAIL | 前一轮真实账号授权后回到 `/login`，未建立 token/session；当前网络基线未修复，不重复提交真实授权 |

## 受控回归

`backend/tests/test_api_contract.py -k 'auth_google_callback or auth_google_login'`：7 passed。

这证明本地 mock 回调契约正常，但不能替代生产容器出网和真实 OAuth。

## 下一步验收条件

整改部署后必须重新满足：

1. 容器 IPv4 TCP/TLS 到 `oauth2.googleapis.com:443` 成功；
2. OAuth 请求明确使用可达的 `HTTPS_PROXY` 或直连 IPv4；
3. 镜像 tag 与 `/healthz` 版本一致，并包含 OAuth 超时/诊断实现；
4. 回调失败日志包含脱敏 `request_id`、错误类别和耗时；
5. 用户完成一次真实 Google 授权后，`GET /me=200` 且刷新 `/library` 会话保持。

## 发布后实测

- 提交 `e4bbcac` 已推送到 `main`。
- 生产节点完成镜像构建并导入 `video2md:0.2.0`。
- 节点磁盘压力导致第一次 rollout 失败；按用户确认执行定向 Docker 镜像/构建缓存清理，回收约 14.33GB，磁盘使用率从约 85% 降到约 74%。
- 第二次 rollout 成功：Pod `video2md-api-67bc9f5bfd-kkwnn` 为 `1/1 Running`，`/healthz` 返回版本 `0.2.0`。
- 容器内 `HTTP_PROXY`/`HTTPS_PROXY` 已注入，诊断超时符号存在。
- 代理端口 TCP 可连接，但通过其访问 Google 返回 TLS `WRONG_VERSION_NUMBER`；受控 GET 仍失败。
- 真实 Google 账号授权已重新执行；回调日志现在出现脱敏诊断：`POST /auth/google/token → 503 error_type=ConnectError timeout_s=15.0`，未建立 `/me` 会话。

当前剩余阻塞已从“容器无代理/旧镜像”收敛为“mihomo 17898 代理的 HTTPS 上游 TLS 转发异常”。

## 退役 17898 代理发布回归

- 提交 `9a2f37e` 已推送并发布；生产镜像重新构建、导入，Deployment rollout 成功。
- 当前运行 Pod `video2md-api-dd974665b-vvlw9` 为 `1/1 Running`，镜像为 `video2md:0.2.0`，节点磁盘使用率约 74%。
- 生产 overlay 渲染结果不再包含 `17898`、`HTTP_PROXY` 或 `HTTPS_PROXY`；容器内上述代理环境变量及 `V2MD_PROXY` 均为空。
- `_google_oauth_proxy()` 在未配置新代理时返回 `None`；代码对 `127.0.0.1:17898` / `:17898` 做显式拒绝，防止回退到已弃用代理。
- 容器镜像未内置 `curl`，因此健康检查通过既有外部入口与 Kubernetes rollout 状态确认；应用日志正常启动。
- 旧代理回退测试已通过：`9 passed, 74 deselected`（包含 `test_google_oauth_proxy_rejects_retired_17898`）。

本次发布完成“退役旧代理 + 防回退”目标；Google OAuth 真实登录仍需下一步提供新的可达 IPv4/HTTPS 出口后再验收，不能据此宣称登录链路已恢复。


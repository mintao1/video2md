# 真实生产 OAuth 基础设施评测结果（2026-10-20）

执行对象：`https://ferosse.tech` 生产入口。探针不提交 POST、不携带授权码、client_secret、id_token 或用户资料。

## 本机可执行的生产探针

| ID | 评测 | 结果 | 观测 |
|---|---|---|---|
| prod-oauth-001 | `oauth2.googleapis.com` DNS | PASS | 可解析，1 个地址；本机探测约 4089.8ms |
| prod-oauth-002 | TCP/TLS 443 | PASS | TLS 1.3，证书验证和主机名匹配；约 1429.8ms |
| prod-oauth-003 | 受控出网 | PASS | 对 `/token` 发送无凭据 GET，HTTP 404（端点可达且未执行换票）；约 6541.3ms |
| prod-oauth-004 | 生产 OAuth client 配置 | PASS | `client_id` 存在；回调 URI 为 `https://ferosse.tech/auth/google/callback`；scope 为 `openid email profile` |

HTTP 404 是 Google token 端点对无效 GET 方法的预期响应，不代表换票成功。

## 必须在生产环境内执行的评测

| ID | 评测 | 结果 | 阻塞原因 |
|---|---|---|---|
| prod-oauth-005 | API 容器代理继承 | BLOCKED | 当前会话没有生产容器 exec 权限 |
| prod-oauth-006 | NetworkPolicy/安全组出网 | BLOCKED | 当前会话没有生产集群访问权限 |
| prod-oauth-007 | 回调诊断日志脱敏与 request_id | BLOCKED | 当前会话没有生产日志/观测系统访问权限 |

本机 DNS/TLS PASS 不能替代容器内结论。由于真实登录仍在回调换票阶段失败，以上 3 项是下一步必须补齐的证据。

## 生产 API 容器实测补充

节点：`VM-0-7-ubuntu`；Pod：`video2md-api-8bcc999d6-tm8rr`；仅执行只读命令。

| 项目 | 结果 | 证据 |
|---|---|---|
| 容器 DNS | FAIL | `oauth2.googleapis.com` 解析为 IPv6；容器没有可用 IPv6 路由 |
| 容器 TCP/TLS | FAIL | IPv6 连接 `OSError [Errno 101] Network is unreachable`；IPv4 探测超时 |
| 容器代理继承 | FAIL/疑点 | `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及小写变量均未设置 |
| 容器受控 GET | FAIL | `/token` 无凭据 GET 在 10 秒内 `URLError` 超时 |
| NetworkPolicy | 未见 egress 限制 | `video2md/default-deny` 只声明 `Ingress` policy type；仍需检查节点/云安全组及 IPv4 路由 |
| 生产镜像诊断符号 | FAIL | 当前镜像 `docker.io/library/video2md:0.1.3`，`_google_oauth_timeout_s` 不存在 |
| 回调日志 | INCOMPLETE | 只看到 `/auth/google/login → 302`；未看到回调失败分类或 request_id |

健康检查仍报告应用版本 `0.2.0`，与 Pod 镜像 tag `0.1.3` 不一致，需核对版本来源和是否部署了预期镜像。

## 受控回归

`backend/tests/test_api_contract.py -k 'auth_google_callback or auth_google_login'`：7 passed，覆盖 OAuth client 配置、state/cookie、成功回调、换票失败、验签失败和 redirect 安全边界。

## 结论

生产入口和 Google 公网通道从本机看可达；真实登录失败仍应优先排查生产 API 容器的代理继承/网络策略/部署镜像，以及回调失败日志是否包含稳定的 request_id 和脱敏错误类别。


# 登录操作实时评测补充（2026-10-20）

本页为历史观测，当前个人 Colab 流程见
[租户登录评测集](./tenant-colab-login-eval-set.jsonl)。历史故障和原编号保持不变，不作为新版本通过证明。

本轮使用 computer use 真实选择了 Google 账号 `Mintao Lin`（邮箱不写入报告）。

## 持续测试授权（2026-10-21）

用户明确授权：后续针对 `ferosse.tech` 的自动化生产测试可以直接使用当前
Google 测试身份完成登录，无需每次重复请求登录确认。评测产物继续遵守脱敏约束，
不记录邮箱、授权码、OAuth `state`、`id_token`、MFA 信息或其他凭据。付款、
删除数据、修改权限等高风险动作不在该持续授权范围内，仍须在执行前单独确认。

## 观测结果

1. 生产登录页协议未勾选时，Google 登录不会发起。
2. 勾选协议后，页面正确进入 Google account chooser，显示继续前往 `ferosse.tech`。
3. 选择已登录 Google 账号后，Google 授权页完成回跳。
4. 生产后端回跳到：`/login#auth_error=Google 换票不可达（ConnectError）`。
5. 前端正确展示“Google 换票不可达（ConnectError）”，未写入 token，也未误判为已登录。

## 评测判定

| 用例 | 判定 |
|---|---|
| login-001 协议门禁 | 通过 |
| login-002 OAuth 跳转 | 通过 |
| 账号选择页展示 | 通过 |
| login-009 换票不可达诊断 | 通过（错误处理符合预期） |
| login-010 成功登录后用户态 | 阻塞：生产 token exchange ConnectError |

## 生产缺陷信号

Google 账号选择和授权本身可达，失败发生在生产后端用授权码向 Google 换取 token 的阶段。建议检查生产环境到 Google OAuth token endpoint 的出网连通性、代理配置、DNS/TLS、服务端超时和错误重试；不要把该错误归因于前端账号选择。

## 已补充的基础设施评测

- `login-011`：token endpoint DNS/TCP/TLS 连通性探测。
- `login-012`：DNS、CA 信任链、SNI 和主机名校验。
- `login-013`：超时边界、有限时长和不挂死回调。
- `login-014`：防火墙拒绝/连接重置的错误分类和恢复能力。
- `login-015`：仅对可重试错误有限重试，并验证日志脱敏。

这些用例默认使用受控探针或 mock endpoint，不提交真实授权码；生产执行只记录 host、耗时、错误类别、request_id 和重试次数。

## 真实部署复测

- 本地到 `oauth2.googleapis.com:443`：DNS 解析成功，TLS 1.2 握手成功，约 222ms；GET `/token` 返回 404，说明主机和 HTTPS 通道可达（GET 非换票协议，404 属预期）。
- `https://ferosse.tech/healthz`：200，版本 `0.2.0`。
- 生产重新选择 Google 账号：仍回到 `#auth_error=Google 换票不可达（ConnectError）`。

因此当前故障已进一步收敛为：生产 API 容器自身的出网路径、代理继承、网络策略或部署版本与当前本地环境不同；不能用本地 DNS/TLS 成功替代容器内探针结论。


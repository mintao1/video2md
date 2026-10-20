# 2c Compose 生产评测结果

## 已通过

| ID | 检查 | 结果 |
|---|---|---|
| 2c-compose-001 | Compose 配置渲染 | PASS；缺少必需变量时会拒绝启动 |
| 2c-compose-002 | PostgreSQL readiness | PASS；`healthy` |
| 2c-compose-003 | Schema migration | PASS；库版本迁移到 4 |
| 2c-compose-004 | 持久卷 | PASS；`pg-data`、`app-data`、`vault-data` 已创建 |
| 2c-compose-005 | Secret hygiene | PASS；运行时密钥文件权限 0400，未回显值 |
| 2c-compose-006 | 2c OAuth 出口 | PASS；CONNECT 中继访问 token endpoint 返回 404（受控无参数请求） |
| 2c-compose-007 | API health | PASS；`/healthz` 返回 `ok=true, version=0.2.0` |
| 2c-compose-010 | 旧代理防回退 | PASS；容器无 `17898`、`HTTP_PROXY`、`HTTPS_PROXY`、`V2MD_PROXY` |

## 待完成

- 2c-compose-008：通过 2c 公网入口执行真实 Google 浏览器登录，确认 `/me` 和 `/library` 会话。
- 2c-compose-009：执行 4c 回滚演练并记录 5 分钟内恢复证据。

## 防回退门禁

发布前执行：

```bash
docker compose -f deploy/docker-compose.prod.yml config
! rg -n '17898|HTTP_PROXY|HTTPS_PROXY|V2MD_PROXY' deploy/docker-compose.prod.yml
python -m pytest backend/tests/test_api_contract.py -k 'google_oauth_proxy or auth_google' -q
```

任何命中旧代理字符串、旧代理环境变量或 OAuth 代理校验失败，都禁止发布。

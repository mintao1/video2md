# 登录操作自动化评测结果（2026-10-20）

评测集：[frontend-login-interaction-set.jsonl](./frontend-login-interaction-set.jsonl)

## 确定性回归

| 执行项 | 结果 |
|---|---:|
| 前端集成测试 | 22 passed / 0 failed |
| 前端 TypeScript + Vite 构建 | 通过 |
| 后端 OAuth/API 契约 | 82 passed / 0 failed |
| 登录评测样本格式 | 8/8 JSONL 可解析，ID 唯一 |

后端 OAuth 契约覆盖成功回调、站内 redirect、state 校验、票据交换失败和 token 验签失败。

## 生产无凭证冒烟

- `GET https://ferosse.tech/`：200，前端 HTML 可达。
- `GET https://ferosse.tech/healthz`：200，返回 `ok=true`、版本 `0.2.0`。
- `GET https://ferosse.tech/me`：401，未认证行为符合预期。
- `GET /auth/google/login?redirect=/library`：302 到 Google OAuth；回调 URI 为 `https://ferosse.tech/auth/google/callback`。
- OAuth state cookie：存在、HttpOnly、10 分钟有效、Path 限定回调路径；报告中已脱敏 state。

## 未执行项

真实账号授权后的 `#id_token` 回跳、用户态 `/me=200`、私有教材库和退出登录未由自动化代填凭证执行。原因是这些步骤会处理真实账号、授权码或 MFA，必须由用户完成后再继续浏览器验收。


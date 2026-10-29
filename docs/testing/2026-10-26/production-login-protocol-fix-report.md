# 2026-10-26 4c 生产 P1 协议门禁修复复测

## 范围

- 生产域名：`https://ferosse.tech`
- 执行主机：`lcode-4c`（4c K3s）
- 修复 release：`video2md:0.2.0-p1-terms-20261026c`
- 回滚镜像：`video2md:0.2.0-colab-tenant-20261026d`
- 变更范围：前端登录按钮继续绑定 `busy || !agreed`，同意后才携带 `terms_accepted=1`；后端 `/auth/google/login` 默认拒绝未同意请求，并在创建 OAuth state、限流和跳转 Google 之前返回协议错误。

## 修复前发现与修复后证据链

历史复测保留在 `production-live-retest-evidence-2026-10-26.json`：未勾选时曾启动 OAuth，不能改写为通过。修复后的后端无参数探针返回 302 回品牌页并带 `terms_not_accepted`，Location 不含 Google 授权地址，服务端未创建 OAuth state。对应现场文件为 `docs/testing/2026-10-26/production-p1-fix/oauth-without-terms.headers`、`.log` 和脱敏后的 `oauth-accepted.log`。

## 4c 部署

发布前保存 live Deployment、Pod 和镜像清单；没有使用远端旧 kustomize 目录，也没有 checkout 或清理 4c 工作树。server-side dry-run 对比确认只修改 `api` 和 `migrate` 两个镜像字段；API Pod 滚动后 `1/1 Running`，`/healthz=200`，`/preflight=200` 且 ffmpeg、磁盘、vault、Colab CLI 全部通过。部署快照保存在 `docs/testing/2026-10-26/production-p1-fix/`；新镜像 API 与前端 BrandView 产物的 SHA-256 记录在 `image-source-hashes.txt`。

## 登录复测

1. 冷刷新生产登录页，协议复选框为未选中，Google 按钮为原生 disabled；强制点击后仍停留 `https://ferosse.tech/`。
2. 在明确确认后勾选协议，刷新后的新 bundle 启动 Google OAuth，进入 Google 账号选择页。
3. 选择既有测试账号，Google 回调成功进入 `/library`；刷新教材库后仍保持登录，教材库数据可见。
4. 退出登录后，登录页复选框重新为未选中，Google 按钮再次为 disabled。

首次点击时使用的是部署前已加载的旧前端 chunk；后端因此拒绝了缺失参数并返回协议错误。刷新后加载新 bundle 的真实登录成功，说明后端门禁和前端参数链均已生效。

## 本地验证

- `backend/.venv/bin/pytest backend/tests/test_api_contract.py backend/tests/test_production_login_protocol_fix_eval_set.py -q`：106 passed。
- `backend/.venv/bin/ruff check ...`（API、OAuth 合同测试、修复评测门禁）：通过。
- `frontend/web` 定向生产按钮/登录集成：6 passed。
- `python3 scripts/check_production_login_protocol_fix.py`：PASS。

## 结论

P1 已在 4c 生产修复并从登录页重新完成真实 OAuth、回调、刷新和退出后门禁复测。未勾选协议不能创建 OAuth state 或进入 Google；同意后登录仍可用。历史绕过证据、修复后脱敏证据和 5 条回归评测均已保留。

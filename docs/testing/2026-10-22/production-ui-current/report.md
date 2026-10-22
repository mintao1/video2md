# 2026-10-22 生产前端续测：Kaggle 入口版本差异与修复

## 结论

生产 `https://ferosse.tech/profile` 初测没有 Kaggle 授权入口。源码和本地构建产物已经包含入口，因此问题是生产静态 bundle 尚未更新（或仍被旧缓存命中），不是用户操作问题。现已发布修复并复验通过。

## 证据

| 检查 | 结果 |
| --- | --- |
| 生产健康检查 | 通过，`/healthz` 返回 `ok=true`、`version=0.2.0` |
| 生产静态入口 | 修复后通过，`/profile` 同时出现两个 `Kaggle 登录` 链接，目标为官方登录地址 |
| 生产 bundle | 修复后 HTML 引用 `/assets/index-CGFm16TP.js`，与本次构建一致 |
| 本地源码 | 通过，`ProfileView.vue` 与 `LlmSettingsCard.vue` 均包含 Kaggle 登录链接 |
| 本地构建 | 通过，`cd frontend/web && npm run build`，`vue-tsc` 和 Vite 均成功；本地 bundle 生成 `ProfileView-Dl9Dr1PM.js` 与 `index-CGFm16TP.js` |
| Kaggle 会话 | 通过退出旧会话；生产入口可见并可打开官方登录页 |
| Colab 入口 | 可见但授权被生产后端判定为“需要服务管理员权限” |

## 影响

当前生产环境不能从前端完成用户要求的 Kaggle 重新登录步骤，因为生产页面没有入口。即使本地源码已修复，未部署前也不能把该步骤判为生产通过。

## 防回退评测

- 继续使用 `docs/evals/kaggle-billing-followup-eval-set.jsonl` 和 `docs/evals/production-ui-annotations.jsonl`。
- 发布门禁应增加一条浏览器断言：登录后打开 `/profile`，必须存在可见文本 `Kaggle 登录`，其 `href` 必须为 `https://www.kaggle.com/account/login` 且 `target=_blank`。
- 生产验收先核对 `/assets/index-*.js` 与本次构建 manifest 的哈希/文件名，再执行 UI 断言；发现旧 bundle 时直接阻断后续 Kaggle 生产验收。
- 本报告只记录本次生产快照，不覆盖历史报告或历史账目。

本次已将该问题接入 `scripts/check_frontend_bundle.mjs` 和 `.github/workflows/ci.yml`：构建后扫描所有 JS chunk，必须找到官方 Kaggle URL 与可见按钮文案。新增评测项 `tenant-colab-013`，覆盖“源码已修复但 dist/生产 bundle 仍旧版”的反例。构建 bundle gate 通过，相关前端集成回归 19/19 通过；生产旧 bundle 仍保持失败证据，直到重新部署后再验收。

## 修复动作

- 提交 `3b3e55e` 已推送到 `main`。
- 生产镜像 `video2md:0.2.0-kaggle-entry-20261022a` 已构建并导入节点。
- `video2md-api` 已滚动更新，Pod `video2md-api-f4d9fdb86-jbdk5` 就绪。
- `/healthz` 返回 `ok=true`；真实浏览器重新登录后，`/profile` 显示 Kaggle 入口，LLM URL/模型配置仍持久化。

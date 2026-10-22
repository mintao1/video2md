# 前端质量整改结果（2026-10-22）

环境：云端容器，Node 24，Python 3.12（`backend/.venv`），Chromium 1194（`PW_CHROMIUM_PATH=/opt/pw-browsers/chromium`）。
未访问生产，未调用模型。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| ESLint | `npm run lint` | 0 错误 0 警告 |
| Prettier | `npm run format:check` | 全部文件符合 |
| 类型检查与构建 | `npm run build` | 通过 |
| 集成测试 | `npm run test:integration` | 68/68 通过 |
| 防回退（评测集） | `npm run test:regression` | 75/75 通过（16 条用例在当前仓库通过，56 个反例全部被检出） |
| E2E | `npm run test:e2e` | 6/6 通过，连续 2 次 |

## E2E 变异验证

| 注入的回退 | 预期失败的用例 | 结果 |
| --- | --- | --- |
| `ReaderView.vue` 不再把净化后的媒体恢复为 `blob:` 地址 | `read.spec.ts` 媒体渲染 | 失败（`toHaveAttribute('src', /^blob:/)`） |
| `stores/queue.ts` 去掉复用 run 的去重，重复插入一行 | `submit.spec.ts` 快速提交 | 失败（`任务编号：queued` 出现 2 次） |

第二项最初未被发现：3 秒轮询会刷掉重复行，带重试的 `toHaveCount` 恰好等到了它。现已改为提交后立即做不重试的断言。

## 已知限制

- E2E 的 Google 授权页由 `page.route` 模拟回跳；真实 OAuth 仍由生产冒烟覆盖。
- 测试夹具为 vault 模式，`/runs/{id}/bundle` 返回 409，因此 E2E 只验证教材库不显示下载按钮；deliver 模式下的下载未在浏览器中端到端验证。
- 截图使用夹具预置数据（`Lesson 1`、`example.test`），展示的是真实接线的界面而不是真实内容。

# 首期生产功能前端测试报告

- 日期：2026-10-19
- 范围：本地前后端联调，登录下线后的匿名访问流程
- 前端：Vite dev server `http://localhost:5173`
- 后端：`V2MD_AUTH=off`，`http://127.0.0.1:8790`
- 数据：本地现有数据；教材库 0 本，队列有 6 个历史 `error` 任务

## 自动化门禁

- `npm run build`：通过
- `npm run test:integration`：22/22 通过
- `npm ci`：安装成功；npm audit 报 7 个漏洞（2 moderate、5 high），需单独安排依赖治理

## UI 验收结果

| 场景 | 结果 | 备注 |
|---|---|---|
| `/login` 兼容访问 | 通过 | 自动跳转 `/library`，未出现登录界面 |
| 教材库空态、搜索、清除筛选 | 通过 | 空态文案和清除按钮可用 |
| 队列加载、空链接校验 | 通过 | 空链接提示“请先粘贴视频链接” |
| 更多导入方式弹窗 | 通过 | 链接/网盘/本地 MP4 tab 与配置控件可见 |
| 个人中心、LLM 设置区 | 部分通过 | 页面可加载；见缺陷 F-01 |
| Colab 授权状态页 | 通过 | 状态读取成功，管理员按钮可见 |
| 阅读器无笔记异常态 | 通过 | 显示“暂无精讲笔记”，可返回教材库 |
| Notion 同步开关 | 通过 | `aria-checked` 正确切换并显示 toast；已恢复原状态 |

## 缺陷

### F-01 [P1] 个人中心会员等级与后端身份不一致

- 复现：打开 `/profile`。
- 接口证据：`GET /me` 返回 `plan: "free"`。
- 实际：资料卡固定显示“高级学术会员 (Pro)”，SVIP/Pro 会员权益文案也直接展示。
- 期望：按后端 `user.plan` 显示免费/Pro/SVIP 对应等级和权益；免费用户不应看到已生效的 Pro 身份。
- 影响：生产用户会看到错误的会员身份，可能导致权益和付费预期错误。
- 代码定位：[ProfileCard.vue](/Users/linmintao/Desktop/开发项目/云端部署/video2md/frontend/web/src/components/profile/ProfileCard.vue:49)

### F-02 [P2] 队列“历史完成”筛选包含失败任务

- 复现：打开 `/queue`；当前后端返回 6 个 `error` 任务。
- 实际：筛选标签显示“历史完成 (6)”，任务卡却全部显示“转换失败”。
- 代码证据：`queue.filtered` 和 `queue.counts.done` 将 `done`、`partial`、`error` 统一归入 `done`。
- 期望：成功完成与失败/部分失败应分开命名或分开筛选；至少不要把 `error` 计入“历史完成”。
- 影响：用户无法准确判断成功产物与失败任务，状态筛选语义错误。
- 代码定位：[queue.ts](/Users/linmintao/Desktop/开发项目/云端部署/video2md/frontend/web/src/stores/queue.ts:16)

### F-03 [P2] 队列完成任务“立即阅读”未进入阅读器

- 静态复核：队列卡的“立即阅读”只触发 `QueueView.onRead`，该函数仅显示 toast“正在打开…”，没有路由跳转。
- 期望：点击完成/部分完成任务后进入 `/read/:id` 或对应教材详情。
- 影响：核心产物无法从队列直接打开，用户只能绕到教材库；当前本地数据无成功任务，未能通过 UI 直接触发，但代码路径可确定复现。
- 代码定位：[QueueView.vue](/Users/linmintao/Desktop/开发项目/云端部署/video2md/frontend/web/src/views/QueueView.vue:57)

## 未覆盖

- 真实视频成功转换、真实 Google OAuth、真实 bundle 下载、生产 TLS/Nginx、移动端不同断点和生产规模负载。

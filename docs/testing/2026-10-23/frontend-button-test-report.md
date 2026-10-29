# 前端按钮生产测试记录

测试日期：2026-10-23（Asia/Shanghai）  
测试方式：Chrome/Codex In-app Browser，Vite 开发服务 `http://127.0.0.1:5173`。登录使用集成测试令牌 `integration-alice` 进入受保护页面；后端 `127.0.0.1:8790` 未启动，因此接口型动作按“前端行为”和“后端不可达”分别记录。

## 结果

| 区域 | 控件 | 结果 |
|---|---|---|
| 品牌首页 | 用户协议、隐私政策 | 通过，均能进入对应页面并返回首页 |
| 品牌首页 | 未勾选协议点击 Google 登录 | **缺陷**：按钮在 AX/UI 上可点击，但点击后由代码给出“请先阅读并同意…”；建议未同意时直接 disabled，避免可点击状态与文案不一致 |
| 教材库 | 通知 | 通过，显示“暂无新通知” |
| 教材库 | 分类、收藏夹、网格/列表、清除筛选 | 通过，状态可见变化；空数据时无教材卡片可进一步验证 |
| 教材库 | 立即同步 | 前端通过，显示“已完成云端同步…”；需后端在线验证真实同步 |
| 转换队列 | 空链接开始解析 | 通过，显示“请先粘贴视频链接” |
| 导入弹窗 | 来源 tab、OCR/目录开关、排版模板 | 通过，active/checked 状态可见切换 |
| 导入弹窗 | 本地 MP4 | **Mock**：界面明确显示“点击选择本地 MP4 文件（mock：直接开始）”，没有文件选择器；点击立即开始会以占位字符串提交，后端不可达时显示 `提交失败：Failed to fetch` |
| 导入弹窗 | 保存草稿 | **Mock**：关闭弹窗并显示“草稿已保存（mock）”，未观察到持久化请求 |
| 个人中心 | 关于视书 | 通过，显示版本 toast |
| 个人中心 | LLM 配置 | 后端不可达时正确禁用输入和保存/恢复按钮，并提供重新加载；未能验证成功保存链路 |
| 个人中心 | 登录 Colab | 通过进入 Colab 授权页；授权状态接口不可达时显示错误和“重新加载” |
| 个人中心 | 资料卡 | **Mock fallback**：接口失败时仍展示“视书用户 / ID: ---- / 免费版”，来源为 `ProfileView.vue` 的硬编码兜底对象，生产环境应避免把模拟身份当真实用户呈现 |
| 全局 | 退出登录 | 通过，清除 token 并回到品牌首页 |

## 代码证据

- 本地 MP4 mock 文案：`frontend/web/src/components/queue/ImportModal.vue:115`
- 保存草稿 mock toast：`frontend/web/src/views/QueueView.vue:214`
- 个人中心模拟资料兜底：`frontend/web/src/views/ProfileView.vue:18-27`
- 协议校验逻辑：`frontend/web/src/views/BrandView.vue:40-48`

## 环境限制

本轮启动了前端 Vite 服务，但未启动 FastAPI（`127.0.0.1:8790`），因此教材加载、队列加载、LLM 配置和 Colab 状态均出现网络失败提示；这不等同于接口实现失败。前端生产构建 `npm run build` 已通过。

## 整改复测

针对本报告中的 Mock 问题已完成修复：本地文件现在通过 `POST /uploads` 上传后再创建任务；草稿写入 `localStorage` 并可重新打开恢复；资料请求失败时不再展示模拟用户；未同意协议时登录按钮为 disabled。新增离线评测集 `docs/evals/frontend-production-buttons-eval-set.jsonl` 和回归测试 `frontend/web/tests/frontend-production-buttons.integration.test.mjs`。

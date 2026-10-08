# 视书 · Web（frontend/web）

video2md 的移动端 Web 前端：Vue 3 + Vite + TypeScript + Tailwind CSS v3 + Pinia + vue-router。
第一版全部走 mock 数据（无后端依赖），数据层签名与后端 FastAPI 契约对齐，后续可无缝切真实 API。

## 命令

```bash
npm install        # 安装依赖
npm run dev        # 开发服务器（默认 http://localhost:5173，移动视口 390px 设计）
npm run build      # vue-tsc 类型检查 + vite 生产构建 → dist/
npm run preview    # 预览构建产物
```

## 页面

| 路由 | 页面 | 说明 |
|---|---|---|
| `/login` | 登录注册 | 短信/密码双 tab、60s 发码倒计时、微信一键登录（mock）、协议勾选校验 |
| `/library` | 教材库 | 搜索/分类/收藏夹过滤、继续阅读横幅、grid/list 视图、目录抽屉、导出弹窗 |
| `/queue` | 转换队列 | 快速链接导入、导入面板（来源 tab/开关/模板）、任务状态卡、批量管理、3s 模拟进度 |
| `/profile` | 个人中心 | 资料卡、配额仪表、SVIP 推广、设置行、会员套餐底部弹层、退出登录 |

未登录访问受保护路由会跳 `/login`；登录/退出状态存 `localStorage['v2md_token']`。

## Mock → 真实 API 的切换路径

- `src/types/` 类型镜像后端契约（`docs/architecture/03-api-contract.md`、`backend/video2md/models.py`）：
  run 状态词表、错误信封 `{error:{code,message,details}}`、list 形状。
- `src/api/index.ts` 是唯一出口（签名层）；第一版解构自 `src/api/mock/impl.ts`。
- 换真实 API：新建 `src/api/live/*.ts`，用 `src/api/client.ts`（`fetch` + `Authorization: Bearer <token>`，
  BASE_URL 来自 `VITE_API_BASE_URL`，默认 `http://127.0.0.1:8790`），然后把 `src/api/index.ts`
  的来源切到 live 实现——view/store 零改动。
- mock 专用函数 `_setProgress` / `_snapshotRuns`（`stores/queue.ts` 的 3s 进度 tick 用）没有真实
  对应物；切真实 API 时用后端 SSE `GET /runs/{id}/events` 替代并删除 tick。
- 后端 CORS：本地联调需设置 `V2MD_CORS_ORIGINS=http://localhost:5173`。

## 目录

```
src/
├── api/          # 签名层 + mock 实现 + fetch 占位（client.ts）
├── stores/       # Pinia: auth / library / queue / profile / toast
├── composables/  # useCountdown 等无状态逻辑
├── components/   # common / book / queue / profile / art（本地占位图形，零外链图片）
├── views/        # 四个页面
└── types/        # 与后端对齐的共享类型
```

设计还原说明：Tailwind 主题（M3 色板/Inter/自定义间距）移植自设计稿，见 `tailwind.config.js`；
图标使用 Material Symbols Outlined 字体（Google Fonts CDN，唯一外部依赖）；
封面/头像/logo 全部是 CSS 渐变占位（`components/art/`），不依赖外链图片。

# 生产部署与协议门禁复测（2026-10-31）

## 部署

- 主线提交：`0352e4ae431cb3f3c65a1354c42cfc3935d4f3e0`
- GitHub Deploy：`#58`，结果：success
- 发布内容：质量子集契约、CI 门禁和评测汇总

## 现场结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `/healthz` | PASS | `ok=true`，`version=0.2.0` |
| `/preflight` | PASS | `ffmpeg`、`disk`、`outbox`、`colab_cli` 均通过 |
| 未同意协议访问 Google 登录 | PASS | 回到品牌页，显示协议错误；未跳转 `accounts.google.com` |
| 登录按钮初始状态 | PASS | 未勾选协议时按钮保持 disabled |

## 尚未关闭

当前浏览器没有已认证的生产用户会话，以下项目需要用户完成 Google 登录后继续现场复测：

- 新任务提交、Colab 状态、真实转换和费用记录
- 阅读器图片/视频真实播放与加载时延
- PDF 实际保存、Markdown 实际下载和下载确认后的产物清理
- B 站未关闭项 `bbr-002`、`bbr-022`、`bbr-044`

本报告只记录本次部署和匿名协议门禁结果，不把离线评测或基础设施健康误认为完整业务验收。

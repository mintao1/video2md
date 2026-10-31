# 生产业务复测（2026-10-31）

## 登录与真实教材

- 使用 Google 账号 `mz549744394@gmail.com` 登录成功。
- 进入教材库后读取已有教材《数据库索引与查询优化入门》。
- 已有真实完成任务：任务号 `21e80b3d-3ff1-4b36-95a1-0d6fa9af0754`，GPU `388.579s`，费用 `$0.001789`，输出 `1647 tokens`。

## 阅读器与媒体

| 检查 | 结果 |
| --- | --- |
| 图片 | 4 张均 `complete=true`，尺寸 `640×360`，鉴权 Blob 加载成功 |
| 视频加载 | 4 个视频均 `readyState=4`，无媒体错误 |
| 视频播放 | 首个视频实际播放至 `1.046916s`，`paused=false`，`error=null` |
| Markdown Callout | 阅读器正文可正常渲染结构化内容；本教材未包含 Callout 样本 |

## 导出与清理

- Markdown 实际下载：`/Users/linmintao/Downloads/数据库索引与查询优化入门.md`，`5680 bytes`。
- 教材包实际下载：`/Users/linmintao/Downloads/video2md-21e80b3d-3ff1-4b36-95a1-0d6fa9af0754.zip`，`2509412 bytes`。
- 教材包包含 19 个文件：正文、转录本、8 张图片、8 个视频片段和 append 文件。
- 下载确认后，原完成任务的下载按钮从队列中消失，说明 ACK 后端清理已生效。
- PDF 打印预览已生成 7 页；本轮尚未取得新保存文件的独立时间戳证据，PDF 实际保存仍标记为待确认。
- 新增离线 Chromium 回归 `frontend/web/tests/pdf.e2e.test.mjs`：在同一打印窗口 DOM 上调用 `page.pdf()`，校验新鲜非空 `%PDF-` 文件；该证据只覆盖渲染器输出，不等价于用户在原生打印对话框点击“保存为 PDF”，生产项继续保持待确认。

## 新任务提交

- 使用生产 fixture 重新提交后，新增任务 `578bae9e-05c4-46c4-aefc-34ad7dfa24e8` 显示“转换完成”。
- 但该任务同时显示“尚未生成可读教材”，说明任务状态完成与最终教材产物之间仍存在一致性缺口。
- 页面中另有 4 条历史失败任务，均出现短任务号被 PostgreSQL 按 UUID 解析的错误：`InvalidTextRepresentation`。

## B 站未关闭项

`python3 scripts/check_bilibili_review_regressions.py` 输出：

```text
PASS: 44 offline annotation replays; no new production acceptance; OPEN bbr-002/022/044
```

- `bbr-002`：8 集历史在线成功探针缺失。
- `bbr-022`：长任务登录过期原因和长期会话稳定性未定位。
- `bbr-044`：登录观察值和本地误写前原值未全部确认恢复。

这些项目仍需对应的 B 站在线样本和生产证据，离线回放不能关闭。

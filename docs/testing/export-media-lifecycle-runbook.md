# 阅读器、导出与产物生命周期验收

这份清单把“接口返回成功”与“用户确实拿到产物、媒体可播放、云端副本已销毁”分开记录。离线测试可以关闭契约回归；真实生产验收仍需要一次浏览器观察和一个真实完成任务。

## 本地回归

在仓库根目录执行：

```bash
cd frontend/web
npm ci
npm run test:integration -- --test-name-pattern='PDF|reader|media|bundle'
npm run build
```

后端的投递与清理契约：

```bash
cd backend
uv run pytest tests/test_api_contract.py -k 'bundle or sweep_outbox'
uv run pytest tests/test_export_delivery_eval_set.py
```

`backend/tests/test_e2e.sh` 是完整的本地链路（可用 `MODE=deliver` 验证 bundle → 解包 → ACK → 410）。它会检查在架目录被物理删除，而不只看 `artifacts_burned` 标记。

## 浏览器生产验收

对同一个真实完成的 `run_id` 记录以下字段，连同时间戳写入当天的复测文档：

| 步骤 | 必须观察的证据 |
| --- | --- |
| 阅读器图片 | 每个 `<img>` 的 `complete=true`、自然尺寸大于 0、请求返回 200 |
| 阅读器视频 | 每个 `<video>` 的 `readyState >= 2`，至少一个片段实际 `play()` 后 `currentTime > 0` 且 `error=null` |
| Markdown | 下载目录中出现新文件；字节数大于 0；SHA-256 与浏览器下载内容一致 |
| 整包 ZIP | HTTP 200；文件字节数大于 0；解包后包含正文、转录本、图片/视频和 `索引-append.md`；记录文件清单和 SHA-256 |
| 下载确认 | 浏览器提示“已交给浏览器下载”后，操作者明确确认文件已保存；取消确认时不得发送 ACK |
| ACK 销毁 | 确认后 `DELETE /runs/{id}/artifacts` 返回 200；服务端 outbox 目录物理消失；再次访问 bundle/note 返回 410 |
| TTL 兜底 | 对未确认的副本把 `artifacts_expire` 调到过去，运行 `_sweep_outbox()` 后目录消失并标记 `burned_reason=ttl` |

整包下载的前端路径在任务卡和教材导出面都使用同一套语义：先完整接收 Blob，再由用户确认是否 ACK。下载失败或取消确认时，副本保持可恢复状态，直到正常 TTL 清扫。

## PDF 边界

PDF 当前通过浏览器打印预览生成。`frontend/web/tests/pdf.e2e.test.mjs` 能证明打印 DOM 可写出非空 `%PDF-` 文件，但不能替代原生打印对话框中“保存为 PDF”的用户操作；生产文档应把这一步单独标为已保存或待确认。


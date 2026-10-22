# 生产界面批注评测集

本目录对应 2026-10-22 的 8 条用户批注。评测集文件为
`docs/evals/production-ui-annotations.jsonl`，每条记录包含输入、预期检查、历史证据来源、生产验收要求和反例。
共 8 个正例场景、42 个反例；数据采用本地 Langfuse 兼容 JSONL 结构，未上传云端数据集。

## 回放结果

`python3 scripts/check_production_ui_annotations.py` 通过，说明目录顺序、来源摘要、证据绑定、反例和历史判定均可重复回放。

| 项目 | 结果 |
| --- | --- |
| pua-001 配置保存 | 未闭环：缺少真实浏览器输入/保存动作审计 |
| pua-002 配置隐私与重进 | 未闭环：缺少真实浏览器动作审计 |
| pua-003 线上运行版本 | 通过 |
| pua-004 OCR 阶段 | 通过，OCR 阶段约 7 分 44 秒 |
| pua-005 阅读器交付 | 资产清单通过，媒体及时就绪失败 |
| pua-006 媒体加载延迟 | 失败，历史采样未在门限内完成加载 |
| pua-007 16 张图片/16 段视频 | 失败，图片完成而视频仍加载中 |
| pua-008 实播与重复提交 | 重复提交用量/费用通过，视频实播失败 |

`python3 scripts/check_production_ui_annotations.py --require-closed` 以退出码 1 保留上述未关闭项，避免把后端文件存在或
计费去重通过误报为前端播放通过。

## 验证命令

- 后端评测：440 passed（`test_production_ui_annotation_eval_set.py`、阅读器就绪和逐条批注回放）。
- API 合同：12 passed，覆盖配置保存、密钥不回显、凭据隔离和重复提交复用。
- 前端批注组件：4 passed。
- 前端生产构建：`npm run build` 通过。
- 新增脚本和评测测试： Ruff 通过。

详细机器结果保存在 `coverage.json`、`strict-acceptance.txt`、`backend-tests-final.txt`、`api-contract-tests.txt`、
`frontend-tests.txt` 和 `lint.txt`。

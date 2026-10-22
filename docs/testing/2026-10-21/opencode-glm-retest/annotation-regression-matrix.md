# 11 条批注的逐项评测与防回退

用户要求每一项增加评测集和防回退。本轮新增独立 JSONL 数据集、确定性证据回放门禁、
75 项回放/反例/完整性测试，并在前端集成测试加入实际 HTTP 422 和 429 拒绝案例。
没有重新发起生产生成、部署或修改生产预算；生产数值仍引用上一轮保存的实测。

| 批注 | 评测编号 | 保护的行为或判定 | 可执行反例举例 |
| --- | --- | --- | --- |
| 1 | glm-review-001 | 4 段有效 ASR、候选帧合法、拒绝保留输入；补实际 HTTP 422 测试 | 空转录、零帧、丢失 URL |
| 2 | glm-review-002 | OCR 80/80、选择 8 帧且属于候选集、错误持续显示 | 漏 OCR 帧、选帧越界、关闭弹窗 |
| 3 | glm-review-003 | 8/8 剪辑、归档先于终态、一次新建提交 | 少一个节段、重复提交、未收到归档 |
| 4 | glm-review-004 | 最终证据优先于陈旧进度；旧 pending 只能判未确认 | 忽略回传完成、缺少终态、无教材 |
| 5 | glm-review-005 | 原 CLI 哈希匹配、helper 撤回；不认领未使用实验 | 未恢复 CLI、遗留 helper、哈希不匹配 |
| 6 | glm-review-006 | 正确模型、2,910 字符教材与成本复算 | 错模型、零费用、空正文 |
| 7 | glm-review-007 | OAuth 成功、预算恢复原值、无运行任务 | 登录失败、遗留极小预算、遗留活跃任务 |
| 8 | glm-review-008 | 完成任务复用、预算拒绝均无任务/用量/费用增量 | 重复扣费、拒绝仍建任务、错误后显示成功 |
| 9 | glm-review-009 | 真正播放进度、媒体非空、月账差值一致 | 视频未播放、空媒体、月度差值错误 |
| 10 | glm-review-010 | 模型及正 token、免费 GPU 保留用量、真实成本归因 | 零输出 token、丢 GPU 用量、虚构 GPU 成本 |
| 11 | glm-review-011 | 摘要成功与完整门禁失败分别呈现 | 隐藏关闭时限失败、伪称已导出、伪称已测解析耗时 |

全部 11 项各有至少 3 个反例，共 33 个；另有缺证据、跨 run 拼接、缺少差值字段、
异常命令输入和 CI 接线检查。数据集的 metadata 指向具体证据和实际业务测试。
构造方法参考 [Langfuse 数据集指南](https://langfuse.com/academy/datasets)，只在仓库本地落盘，
未创建或上传云端 Langfuse 数据集。

## 本轮执行结果

| 命令/检查 | 结果 |
| --- | --- |
| `pytest backend/tests/test_glm_review_eval_set.py -q` | 75 passed |
| 批注回放 + 阶段反例 + 计费 + OpenCode + 短文本门禁 | 427 passed |
| `npm --prefix frontend/web run test:integration` | 33 passed，包括新增 422/429 HTTP 用例 |
| `npm --prefix frontend/web run test:regression` | 1 passed |
| 新增 Python 文件 ruff、fixture compileall、`git diff --check` | PASS |
| `check_glm_review_regressions.py` | PASS：仅离线回放 |
| `check_production_pipeline_stages.py stage-evidence.json` | FAIL：pps-008、pps-013、pps-016 |

后端组合命令：

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/test_glm_review_eval_set.py \
  backend/tests/test_production_pipeline_stage_eval_set.py \
  backend/tests/test_production_billing_eval_set.py \
  backend/tests/test_opencode_llm_contract.py \
  backend/tests/test_transcript_quality_admission.py -q
```

日志见 `annotation-backend-results.txt`、`annotation-web-results.txt`、
`annotation-ui-regression-results.txt`、`annotation-review-gate-result.txt` 和
`annotation-full-stage-gate-result.txt`。CI 已执行新增 pytest 模块及回放脚本；
前端测试通配符会自动包含新增 HTTP 案例。没有修改远端分支保护或部署平台。

## 证据边界

批注 4、5 的“卡死”“分块接管”属于已撤回诊断，不能把 2.45 MB 文件大小当成校验完成，
也不能把临时实验归功于之前已完成的生产任务。CLI 的 SHA256 只校验回滚文件，
不是归档文件 SHA。新增分类函数属于评测判定器，不是上线的传输实现。

批注 1 的历史 422 记录未完整保存持久错误文案截图。本轮实际 HTTP 422 测试覆盖 API、
前端客户端、队列状态和错误组件渲染；真实生产弹窗持续错误与 URL 保留引用已保存的
429 截图，不冒充新的 422 浏览器验收。表单关闭/清空行为由既有源码守卫补充，
本轮未新增浏览器交互测试。

批注 7 的退出操作引用同一次持续验收中的前轮退出记录，本模型轮次只记录再次登录。
关闭时限、整包导出和独立解析耗时仍未通过；回放测试固定历史事实，后续修复应另存
新的生产证据，再执行完整门禁，不应覆盖这份失败快照。

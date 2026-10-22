# Kaggle 恢复与生产联调续测（2026-10-22，Asia/Shanghai）

结论：新式 Kaggle 令牌已配置、认证通过；用户完成手机验证后，本次唯一标识的真实探针取得 **两张 Tesla T4，CUDA 可用**。已从前端再次显式输入并提交真实 B 站合集，新任务 `dd586072` 处理前两集。后续 GPU 作业持续排队，完整教材产出与新版本空回包重试的生产验收尚未完成。

服务商账单及未截断的独立请求导出本轮已取得。已完成任务 `be188f78` 用量/归因对平；原失败任务 `3c05faf8` 的历史漏记得到独立证明。历史应用账目尚未回填，不宣称四项问题全部关闭。

## GPU 配置、修复与部署

- 凭证通过文件挂载，支持 Kaggle CLI 新式 `access_token`，保留用户名元数据；认证用官方 SDK，未把令牌写入代码、参数、报告或构建上下文。
- 配置健康检查明确只说明凭证配置，不冒充账号 GPU 权限和配额验证；无效 JSON 诊断不输出秘密内容。
- 真机发现输入挂载为 `/kaggle/input/datasets/<account>/gpu-router-io/`，旧固定路径导致 `clip_job.json` 找不到。生成的 worker 改为精确解析所附数据集的三种布局，拒绝误取其他 owner 的同名数据集。
- GPU 提交显式请求 `NvidiaTeslaT4`；worker 检查实际 CUDA 分配。CLI 即使返回 0，只要输出 Kernel push error 仍判失败，避免读取旧版本产物。
- 部署镜像 `gpu-router:0.2.3-kaggle-token-20261022b`，仅叠加经过测试的 Kaggle 适配器改动。运行源码 SHA256 `2a21634c180ebe3ec0937553e8342c5466b79962289f1806b8315cc2e856a8ef` 与构建清单相同，Pod Ready、0 次重启。见 `patch-manifest-b.json`、`runtime-b-source.json`、`pods-b-final.json`。
- API 仍为已验过的 `video2md:0.2.0-failure-cost-20261021b`。未重启 API 去中断业务，未清除 Colab 配额冷却，未把 CPU 诊断当作 GPU 生产回退。

操作异常及恢复：首次尝试对最小 Secret 做 server-side apply，意外移除了原有 `GPU_ROUTER_TOKEN` / `HF_TOKEN` 字段。不变量检查在旧 Pod 重启前发现，从仍运行的旧环境恢复这两项原值，重建完整 Secret 并确认原字段存在、恢复值一致后才滚动发布。原值未输出，恢复备份留在仓库外权限受限目录。证据 `secret-provisioning.json`，这次异常没有隐瞒为首次部署直接成功。

## 真机结果与当前调度阻断

| 检查 | 结果 |
| --- | --- |
| 令牌认证 | 通过；同一账号身份匹配，见 `token-validation.json` |
| 验证前显式请求 T4 | 唯一标识匹配，但 CUDA=false、设备数 0，未判健康 |
| 用户完成手机验证后的新探针 | 59.074 秒完成；唯一标识匹配、CUDA=true、两张 Tesla T4，见 `kaggle-layout-verified-gpu.json` |
| 真实剪辑路由 | `93b637c7e4b4`，两个 2 秒片段，输入 406,492 字节；20 分钟观察窗口仍未完成，见 `gpu-clips-verified.json` |
| 联网 CPU 对照 | 39.393 秒完成，读取同一份剪辑输入，找到 `/usr/bin/ffmpeg`；不调用 LLM，见 `network-cpu-control.json` |
| 独立 GPU 对照 | 新 kernel、T4、关闭联网仍 Queued；约 6 分钟后停止，未取得 CUDA 结果，不判通过 |
| 辅助作业清理 | 官方 UI 确认辅助剪辑版本 #6 与 GPU 对照 Cancelled，剩余一个活动事件为业务转写版本 #7；停止对应观察进程，见 `observer-cleanup.json`、`kaggle-after-cleanup-state.txt`、`kaggle-after-cleanup.png` |

排队证据见 `kaggle-queue-evidence.png`。辅助剪辑首次观察超时后仍在平台排队，随后通过官方 UI Stop Session 取消；没有将“客户端观察超时”误写成“平台作业已终止”。`verified-route-after-cleanup.json` 记录路由已退出 error，业务任务随后接管串行锁。辅助作业取消不是业务算法错误。

当前能确认的是 Kaggle 接收 GPU 请求后长时间 Queued。配置、此前的实际 CUDA 权限、输入数据和普通联网启动均有对照证据；平台没有给出本次排队的根因，不能据此断言具体配额或全球资源容量故障。用户打开的额外身份验证页面未由代理操作，未读取手机号、验证码或身份资料。

历史 `gpu-clips-smoke.json` 是路径修复前的第一次路由失败；其中粗分类 quota 同时匹配了被跳过的 Colab 冷却，不能当作 Kaggle 根因。真正失败是 `first-kernel-failure.json` 的输入路径缺失。`kaggle-layout.json` 等早期诊断若无唯一标识，不替代后续有 nonce 的 CUDA 验收。

## 真实前端与账目

会话过期后，按已有登录/隐私授权使用原 Google 账号重新登录成功。真实前端再次输入 `https://www.bilibili.com/video/BV1LV411r77C/` 并点击一次开始解析；任务数 9 → 10，生成 `dd586072`，前两集 60 秒和 55 秒。见 `bilibili-explicit-after-verification.png`、`bilibili-submitted-after-verification.png`、`bilibili-current-state.png`。

服务器只读补证显示两集下载完成，正在等待转写。最新账目快照 `final-state.json`：月度 $0.032899，输入 43,653 / 输出 54,945 tokens，GPU 调用墙钟 4,517.223 秒，任务数 10；本轮新任务没有已结算模型用量增量。这是运行中观察值，不是最终账单，也不把 GPU 调用墙钟当作物理设备占用时间。若任务之后推进，必须另存新快照，不覆盖当前证据。

## 独立服务商对账

通过已登录的 OpenCode 官方控制台只读查看订阅及账单，未改变套餐、充值或支付设置：本期 Go renewal 发票 `XPIMO8IF-0003` 在 UI 显示 Paid，金额 **CNY 67.72**，PDF 摘要已记录。PDF 本身写的是 amount due，付款状态取自服务商账单 UI，不能单凭发票上的应付金额断言已付款。见 `supplier-invoice-summary.json`、`supplier-paid-ui.txt`。订阅 ID 未观测，不虚构；人民币实付不转换成伪造的美元逐请求扣款。

首份近七天导出达到 1,000 条上限、truncated=true，被新工具拒绝。随后通过 UI 导出近 24 小时共 136 条、truncated=false 的完整原始日志。原导出含请求头、账号信息，连同发票原件保存在仓库外私密目录；仓库只保存白名单字段及原件 SHA256，见 `supplier-usage-24h-redacted.json`。

服务商的 inputTokens 不含单列的 cacheReadTokens；统一输入口径后：

| run | 独立请求数 | 服务商总输入 / 输出 / 缓存 | 应用原记录 | 结论 |
| --- | --- | --- | --- | --- |
| `be188f78` | 5 | 15,939 / 15,841 / 0 | 相同；应用归因 $0.010311 | 用量与 token 归因对平，`provider-reconciliation-healthy.json` |
| `3c05faf8` | 7 | 37,863 / 31,495 / 18,688 | 19,169 / 23,495 / 9,344 | 输入少记 18,694、输出少记 8,000、缓存少记 9,344，`provider-reconciliation-original.json` |

缓存是输入子集，不能把缓存差额再次加进输入差额。原任务服务商 token 归因合计 $0.01918439，应用原归因 $0.013502，差额 $0.00568239；这些是成本归因，不能当作订阅现金扣款。两条被漏计请求的导出 finish reason=length、各输出 4,000；独立导出没有回复正文，不能单凭 length 断言回复正文一定为空。已部署代码的逐 HTTP 尝试用量修复仍需等待新任务实际走到模型重试完成生产验收。历史账目保留原样，并未把定位差异伪装成已回填。

可复现只读入口：

```bash
python3 scripts/reconcile_opencode_usage.py "$PROVIDER_EXPORT" "$APP_EVIDENCE" \
  --run-id "$RUN_ID" --output reconciliation.json
```

只接受未截断导出、独立 session/run 关联、唯一请求 ID 和完整非负用量；缺失数据不能补成已核验的零。退出码 0 是 token 用量/归因对平，1 是不一致，2 是证据不完整；现金结算独立核验。原任务实际退出 1，健康任务退出 0。

## 评测、防回退与验收边界

GPU 侧离线脚本：凭证 14 项、生成 worker 11 项、tasking 64 项、manifest 30 项均通过，分别见 `config-regression-green.txt`、`worker-regression-final.txt`、`tasking-tests-worker.txt`、`manifest-tests.txt`。红灯证据保留，隔离镜像中的生成 worker 回放见 `image-b-check.json`。

本轮新增 12 个独立对账回归测试，覆盖缓存口径、漏记请求、截断导出、重复请求、未知用量、非法数值、跨 run、秘密请求头剥离、token 归因和实付分离；与原四项门禁测试合计 **111 项通过**，见 `reconciliation-and-gate-tests-final.txt`。测试由现有 pytest CI 自动发现，评测集 004 挂上新工具及坏例，未降低原关闭标准。

上一轮应用 937 项后端 / 45 项前端 / 类型检查是前一轮结果，见历史报告；本轮没有把这些数字重复算成新真机成功。新只读工具不需要改动生产 API 镜像。

完整生产门禁实际仍 FAIL：003 沿用三次真实 Google 登录和刷新通过；001 缺 OCR/clips 故障与健康产出对照，002 缺本版本真实空回包重试对平，004 原任务仍有真实历史差异且订阅 ID/完整结算关联未齐。已经取得账单和用量不等于四项自动关闭。原矩阵及历史引用文件保持不变。

```bash
python3 scripts/check_production_failure_cost.py \
  docs/evals/production-failure-cost-evidence-2026-10-22.json \
  --expected-release video2md:0.2.0-failure-cost-20261021b
python3 -m pytest backend/tests/test_opencode_usage_reconciliation.py \
  backend/tests/test_production_failure_cost_eval_set.py -q
```

## 规范与偏离

相关规则：HC-01/02/03、GATE-01、GOV-03、BLD-01/02/03、DRIFT-02。镜像按节点限额/串行构建后导入 k3s；清单在本地准备，服务器 apply -f stdin，发布后核对运行摘要。平台硬约束门禁通过，未绕过门禁，未新增例外或规则，未提交/推送/创建 PR。Secret 操作异常及恢复已单独记载，不算作获准的规则例外。

偏离清单：无。尚未完成的验收按事实保留 FAIL；GPU 调度属于本次外部阻断，业务任务仍在队列，不能宣称教材已生成。

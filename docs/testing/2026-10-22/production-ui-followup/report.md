# 生产 UI 联调续测（2026-10-22，Asia/Shanghai）

结论：computer use 已恢复可用，连续三次真实 Google 登录与刷新通过；已在前端显式输入并提交真实 B 站合集。完整教材生成和服务商结算验收仍未通过。

本轮沿用已部署镜像 `video2md:0.2.0-failure-cost-20261021b`，未修改应用源码、生产配置或本地 MCP 配置。Deployment generation 54、Ready/Updated 均为 1，五个运行文件摘要与上一轮发布清单一致。使用 lcode-auto 的 computer use 完成所有登录、输入、提交和页面检查；服务器读数单独记录，不替代 UI 证据。

## 真实 UI 结果

| 验收项 | 实测 |
| --- | --- |
| Google 登录 | 退出后选择原 Google 账号，连续 3 次成功；每次返回真实教材库，刷新后仍有 2 本教材 |
| 已认证接口 | 三组 `/me=200`，每组刷新后再次 `/me=200`；登录发起至首次认证成功分别 23、25、22 秒，包含浏览器跳转与账号选择，不能当作纯换票延迟 |
| OAuth relay | 主 unit active/enabled；旧 unit inactive/disabled；监听 `127.0.0.1:18080`。本次未复现换票超时，也不据此断言所有间歇故障永久消失 |
| 已完成合集重复提交 | 显式输入 `https://www.bilibili.com/video/BV1vV411k7GB/` 并提交；仍为 8 个任务，月度费用和 token 数未增加 |
| 原失败合集提交 | 显式输入 `https://www.bilibili.com/video/BV1LV411r77C/`，点击一次“开始解析”；任务数 8 → 9，新任务 `f1fff0b8`，解析前两集（60 秒、55 秒） |
| 新任务终态 | 两集下载完成，两集转写失败；status=error，未进入 OCR、剪辑及模型阶段，新教材 0 本 |
| 费用页 | 新任务输入/输出 token 均 0，GPU 记账 0.089 秒，成本归因 $0.000000；月度仍为 $0.032899，输入 43,653 / 输出 54,945 tokens，GPU 4,517.223 秒 |
| 原故障提示 | 原任务 `3c05faf8` 在前端明确显示 clips 失败与“视频剪辑未完成，暂时无法生成教材；请补跑失败条目。” |

证明文件：`login-1.png` / `login-2.png` / `login-3.png`、`oauth-evidence.json`、`me-http-evidence.json`、`dedup-explicit-input.png`、`before-submission.json`、`bilibili-explicit-input.png`、`bilibili-terminal.png`、`billing-detail.png`、`final-state.json`。

部分 `.txt` 使用 accessibility 增量输出，只记录“无变化”；不把这些文件当作完整页面快照。登录截图、认证 HTTP 记录和已完整加载的刷新状态共同作为判据。日志中的未登录/页面初始化 401 不计入认证成功；三次成功和刷新分别取后续实际 200。OAuth 查询参数、令牌、cookie、凭证值均不写入证据。

## 算力与结算阻断

新任务真实日志包含 Colab quota 冷却和 Kaggle 凭证缺失。容器的 HOME 为 `/home/router`、UID=10001，挂载路径 `/home/router/.kaggle/kaggle.json` 正确、JSON 可读，但 username/key 均为空；相关环境凭证也未设置。**文件存在不代表账号可用。** 见 `gpu-credential-presence.json`，只保存字段是否存在，不保存字段值。

用户已选择自行安全配置 Kaggle 凭证；该回复表示准备配置，不能当作已经配置完成。待用户完成后，先只读验证容器内凭证字段，再执行真实 GPU 作业与前端补跑。无需发送密钥到对话，也不清除 Colab 配额冷却。

本轮打开了 OpenCode 官方站点，但 Go 导航发生浏览器超时，未取得服务商已结算账单/用量导出；临时页签已关闭。应用归因金额为零不能证明服务商现金扣款为零，历史少记用量也未猜测回填。

## 四项评测与防回退

新证据矩阵：`docs/evals/production-failure-cost-evidence-2026-10-22.json`。保留原四个 case ID 和严格关闭条件，没有修改门禁来接受不完整证据。

| ID | 本轮状态 |
| --- | --- |
| failure-cost-001 | 未关闭：新真实任务在 transcribe 失败且模型调用为 0，但没有走到 OCR/clips 故障和健康产出对照，不能替代其生产验收。上一轮生产代码隔离回放结果仍保留 |
| failure-cost-002 | 未关闭：上一轮 300/100 用量回放通过；新任务没有模型请求，未观测真实空回包后成功重试 |
| failure-cost-003 | 通过：同一部署版本的三次真实登录、认证接口、刷新和单 relay/回环配置证据齐备 |
| failure-cost-004 | 未关闭：仍缺服务商独立票据和完整请求用量 |

可复现门禁（离线，不再次调用模型或 GPU）：

```bash
python3 scripts/check_production_failure_cost.py \
  docs/evals/production-failure-cost-evidence-2026-10-22.json \
  --expected-release video2md:0.2.0-failure-cost-20261021b
```

实际输出 FAIL，仅 001/002/004 被拒绝，003 被接受；全部证据文件 SHA256 校验通过，见 `production-gate.txt`。这是未完成的生产验收结果，未绕过、未推送。原 2026-10-21 历史失败矩阵保持不变。

上一轮 937 项后端、45 项前端集成及全量类型检查仍见 `docs/testing/2026-10-21/failure-cost-integration/`。本轮只新增生产证据和读取脚本，没有重复运行那些确定性测试，也不把旧离线通过数说成本轮真实生产产出成功。

只读复查：

```bash
python3 docs/testing/2026-10-22/production-ui-followup/gpu_readiness.py
python3 docs/testing/2026-10-22/production-ui-followup/probe.py readiness-state.json
```

GPU 复查默认另存带时间的文件，任务复查另存 readiness-state.json；不要覆盖历史验收的引用文件。完整业务验收须另存新一轮证据。

## 规范与偏离

本轮相关规则：GATE-01（不绕过门禁）、GOV-03（以 SSOT 为准）、DRIFT-02（核对实际运行版本）。未部署新镜像、未修改模板或新增平台约束，未产生规则例外；偏离清单：无。生产门禁的三个未关闭项按实测保留 FAIL，后续 GPU 凭证由用户配置，业务复测由 Codex 在配置完成后继续。

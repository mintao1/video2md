# 新用户登录旅程现场结果（2026-10-23）

评测集：[`new-user-login-journey-eval-set.jsonl`](./new-user-login-journey-eval-set.jsonl)。本次结果只记录已观察到的界面事实；真实 Google、Colab 和 Kaggle 凭证没有由自动化代填。

| ID | 结果 | 现场观察 |
| --- | --- | --- |
| `new-user-journey-001` | **部分通过 / 阻塞** | 品牌页协议未同意时会拦截登录；同意后进入 Google 账号选择页。源码显示 Colab 说明页需显式点击后打开 Google OAuth，并保留回退链接；仍需在已认证个人页完成一次真实点击确认“确实打开 accounts.google.com”。 |
| `new-user-journey-002` | **失败** | 当前 `LlmSettingsCard` 在 LLM 表单下方直接提供 Kaggle 官方外链，没有项目内说明页。 |
| `new-user-journey-003` | **失败 / 待人工清理** | 现场发现 Kaggle 仍处于登录态。清理步骤必须在 Kaggle 官方页面完成并再次验证；项目不应接收 Kaggle 凭证。 |
| `new-user-journey-004` | **通过（源码回归）** | 个人页已移除升级、支付、会员特权和旧配额面板；本轮继续移除 ProfileCard 与 AppHeader 中的会员等级徽章。 |

## 证据边界

- Google 品牌登录已验证：未勾选协议时显示拦截提示；勾选后跳转 Google 账号选择页。
- Google 账号退出后，账号仍出现在 Google 的“已退出账号”列表；需要在 Google 账号选择页执行“移除账号”才能清除本地记忆。该动作不涉及项目凭证。
- 生产现场未输入密码、授权码、MFA 或 API Key；Colab/Kaggle 的真正登录完成仍需人工接管。

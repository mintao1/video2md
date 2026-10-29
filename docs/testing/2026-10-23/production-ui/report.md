# 2026-10-23 生产 UI 测试报告

目标站点：`https://ferosse.tech`。测试从结束已有会话开始，使用当前 Google 账号完成真实登录。所有密钥、OAuth code、cookie 和账号标识均未写入证据。

## 结果

| 步骤 | 结果 | 证据 |
|---|---|---|
| 登录 | 通过 | 退出后回到品牌首页；Google 账号选择页完成回调，进入 `/profile/colab?next=/library`，页面显示“Google 登录成功”。 |
| 前端填写真实 LLM URL/API Key | 通过 | 个人页显式填写 `https://opencode.ai/inference/go/openai/v1`、真实 Key、`glm-5.3-flash`；点击保存后显示“配置已保存，后续启动的转换任务将使用此配置”。Key 仅记录为 redacted。 |
| 前端按钮重新登录 Colab | 阻断 | 点击“登录 Colab”并点击“连接我的 Google Colab”后，生产页仍显示“尚未连接我的 Colab”，未出现授权 URL、flow id 或授权码输入框，无法证明租户授权成功。 |
| 前端按钮重新登录 Kaggle | 通过 | 点击生产个人页“登录 Kaggle”，打开 `https://www.kaggle.com/account/login`；点击 Google 登录后进入 Kaggle 首页，页面显示“Welcome, Mintao Lin!”。 |
| 真实 B 站合集 + 模型计费 | 阻断 | 前端显式输入 `https://www.bilibili.com/video/BV1LV411r77C/` 并提交，立即看到“已加入转换队列”；刷新生产队列后任务仍为 13（提交前也是 13），无新 run id、运行阶段、模型调用、usage、cost 或教材，因此不能宣称生产计费通过。 |

脱敏机器可读证据：[evidence.json](./evidence.json)。

## 评测集与防回退

- `python3 scripts/check_tenant_colab_eval_set.py`：13 条租户 Colab 案例、31 个反例，定义与回归链接通过；该门禁明确声明不能关闭生产验收。
- `python3 scripts/check_production_ui_annotations.py`：8 条 UI 注释回放通过；该门禁明确声明不能关闭生产验收。
- `python3 scripts/check_production_billing.py docs/evals/production-model-billing-evidence.example.json --allow-synthetic`：离线计费公式自测通过；真实证据仍要求新 run 的模型调用、正 token、GPU 秒数、cost 和月度汇总。
- 前端集成回归：68 passed, 0 failed。覆盖登录、租户 Colab、LLM 设置、Kaggle 入口、显式 URL 提交、队列和计费呈现。

## 结论

本轮真实生产测试关闭了登录、LLM 配置和 Kaggle 登录三项；Colab 租户授权与 B 站合集模型计费仍未通过。生产发布不能以本报告作为完整验收依据，需修复 Colab OAuth 启动和新任务持久化/可见性后，用新的真实 run 重新执行计费门禁。

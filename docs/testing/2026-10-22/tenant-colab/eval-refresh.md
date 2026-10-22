# 租户登录评测集更新

用户批注 1：首次登录进入个人 Colab 引导；凭证、会话和转换任务按用户隔离；核对同一 Google 账号；LLM URL/API Key 下保留 Colab、Kaggle 入口。

## 新口径

当前集合：[tenant-colab-login-eval-set.jsonl](../../../evals/tenant-colab-login-eval-set.jsonl)，12 项、29 个失败反例。输入、期望、元信息分离；正例与每个反例都关联实际应用测试。由用户需求推导的离线案例，不是新取得的生产结果。

删除 `frontend-login-interaction-set.jsonl` 与 `production-login-ui-eval-set.jsonl`。有效的品牌、协议、回调错误与路由保护要求合并到新集；OAuth 基础设施要求继续由现有专项集覆盖。日期报告只添加当前集合说明，历史故障与通过数未重写。

## 执行与结果

命令：`python3 scripts/check_tenant_colab_eval_set.py --run`。

- 集合完整性：12 项、29 反例，ID 与实际测试引用有效，旧集合不存在。
- 后端：47 passed。覆盖应用授权/CLI 边界和集合完整性；Google/CLI 使用离线替身。
- 前端：17 passed。实际执行组件逻辑与 SSR 模板，覆盖首次引导、已有授权复用、错误恢复、跳转安全、同意协议、授权码清理、两个按钮在表单后的具体位置和链接目标。
- Ruff 与 `git diff --check`：通过。
- CI：增加独立的租户 Colab 后端评测步骤；前端由已有集成测试通配入口执行。

运行器使用独立临时存储与不存在的凭证文件，清空真实 LLM/Google 配置，不修改父进程环境。目录检查本身只验证定义；带 `--run` 才执行应用测试。

没有上传云端数据集、访问生产、部署或新增模型费用。真实 Google Colab 授权与个人 GPU 生产闭环仍需发布后验证。

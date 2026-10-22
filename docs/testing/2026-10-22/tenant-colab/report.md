# 个人租户 Colab 连接改造与本地验收

日期：2026-10-22。范围：取消服务管理员名单，Google 用户连接与本人登录相同的 Colab 账号。未部署到 ferosse.tech；本报告不代表新的生产 OAuth/GPU 实测通过。

## 实现行为

- Google 登录回调保存网站登录凭证后，检查当前用户自己的 Colab 连接。首次未连接时进入可跳过的个人连接引导，保留原来的站内目标；已有连接则继续原目标。
- 首次仍需在 Google 同意 Colab CLI 所需权限，再复制授权码提交。Google 网站登录身份凭证不能直接充当 Colab CLI 的访问/刷新凭证。
- 普通 Google 用户可自行授权；不再读取服务管理员名单。后端向 Google 核对授权账号 subject 与当前网站身份一致；拒绝错绑其他 Google 账号、缺少持续授权或明确缺少 Colab scope 的凭证。
- 凭证、待交换 OAuth 流、CLI home、会话名及进程锁按内部 uid 隔离。凭证文件 0600；OAuth 使用 PKCE、十分钟有效期和一次性消费，交换失败不覆盖旧凭证。
- 个人凭证存在时，同一 run 的转写、默认远程 OCR、剪辑优先使用自己的 CLI；LangGraph 节点线程继承租户上下文。未连接用户不会回退到旧的服务账号 token。
- 个人目录默认 backend/data/colab-users；Kubernetes 显式设为持久化卷 /app/data/colab-users。既有服务凭证不会自动迁移给用户。
- LLM URL/API Key 表单下保留“登录 Colab”和“登录 Kaggle”。前者进入个人授权页面，后者在新标签页打开 Kaggle 登录。
- Colab OAuth 换码与账号核对使用现有 Google OAuth 代理配置。CLI 访问 Google、GPU 配额及凭证刷新仍需部署后真实验证。

## 本地验证

后端测试通过独立临时目录、禁用真实凭证文件、清空远程执行地址运行；Google OAuth 和 GPU CLI 边界使用离线替身，没有新增模型调用费用。

- API 契约：103 passed，独立 Python 进程。
- Colab OAuth、CLI、图执行与插件契约：29 passed（包含个人 GPU 路由、跨租户隔离、线程身份传递、账号错绑、PKCE、重放、过期、私密文件、代理配置和失败清理）。
- 前端登录与租户连接集成：8 passed。实际挂载组件逻辑，验证首次引导、已有授权复用、状态查询失败仍可登录和站外返回路径拦截。
- vue-tsc 与 Vite 生产构建通过。
- 修改过的 Python 源码和测试 Ruff 检查通过；git diff --check 通过。

曾把 API 契约与其他测试合在一个进程运行，得到 6 failed / 124 passed。原因是 API 测试模块在 import 时设置路径，但其他模块已提前加载 config，导致测试夹具清理的文件与实际临时存储不一致。按现有测试约定拆成独立进程重跑后全部通过；未修改业务实现来规避这些断言。

## 尚待生产验证

新代码尚未发布，因此线上原有“服务管理员权限”提示仍可能出现。需要发布后以普通 Google 用户完成一次真实 Colab 授权，再执行 ASR/OCR/剪辑，确认个人 GPU 会话、配额及自动释放。授权被撤回或刷新凭证失效时需要重新连接。

官方依据：[Google OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server)、[Google Colab CLI auth 实现](https://github.com/googlecolab/google-colab-cli/blob/main/src/colab_cli/auth.py)。

# 2026-10-22 生产联调续修与验收

真实前端任务 `dd586072` 已完成，来源为 https://www.bilibili.com/video/BV1LV411r77C/ ，实际范围是前两集（约 60/55 秒）。产出两讲教材，共 11,916 字节 Markdown；引用 11 张图片、11 段视频。第一段视频实播到 19.58/23 秒；第二讲修复后 5 张图片、5 段视频均 readyState=4，无解码错误。

**本轮生成、阅读和账目总额核对成功，完整生产门禁仍未通过。** 原四问题还有两项缺少严格生产故障证据，媒体就绪性能另保持未通过。未用离线回归、旧发布或内核 Successful 替代生产验收。

## 原四问题的结果

| 问题 | 已解决与实测 | 严格验收 |
|---|---|---|
| OCR/剪辑失败后仍消耗模型 | 阶段依赖检查；OCR 单帧错误/缺行拒绝；坏缓存重新执行。真实 OCR 失败后模型输入/输出均为 0；修复后两集 OCR、剪辑、教材成功，健康对照有 5 次模型请求 | OCR 已有真实故障观察；同候选的真实 clips 故障与失败后调用观察尚缺，failure-cost-001 保持未验证 |
| 空回包重试漏记用量 | 已累计所有已观测尝试，保留未知用量语义；重试结算叠加新增消耗；历史漏记已实际回填 | 本次 5 个请求都成功，没有真实空回包；选帧缺独立持久 span，只能由总额减去四个已观测请求重建。failure-cost-002 保持未验证 |
| Google 换票间歇超时 | 只保留 canonical relay，旧单元禁用，监听 127.0.0.1；公开验签证书按缓存头缓存，冷缓存并发合并，HTTP 客户端关闭；仍逐次校验签名、aud、iss、exp | 最终 F 版本真实登录与刷新均成功，/me=200，耗时 19, 17, 18 秒。failure-cost-003 通过 |
| 服务商计费未独立核验 | 按用户明确选择的“订阅付款凭据＋逐请求用量对账”核验；CNY 67.72 Paid 与 USD token 归集分开，同账户、Go 产品、有效覆盖期、完整导出 | failure-cost-004 按新口径通过；不要求服务商未展示的 subscription ID，不编造汇率或逐请求现金支出 |

`failure-cost-gate.txt` 只剩 001/002 两项未验证；`followup-gate.txt` 的 12 项跟进中 11 项通过，完整验收 012 未通过。历史矩阵保持原样，新矩阵为 `docs/evals/production-failure-cost-resolution-2026-10-22.json` 与 `docs/evals/kaggle-billing-resolution-2026-10-22.json`。

## 真实用量与付款

本次任务 5 个独立服务商请求：输入 **37,018**、输出 **3,579**、缓存输入 **0** tokens；服务商归集 **$0.00734220**，应用展示 **$0.007342**，差额在六位小数展示精度内。见 `final-reconciliation.json`。四个 note 阶段请求有独立响应 usage，以唯一 token 指纹关联请求 ID；选帧请求由任务总额的唯一剩余量重建，明细中已标注 `run_total_residual`，没有冒充独立应用 trace。

用户选择的订阅口径：发票 XPIMO8IF-0003、CNY 67.72；**付款状态来自真实 Billing 页的 Paid**。PDF 本身写 amount due，单独不能证明已付。Go 页显示 paid-until 2026-11-06 01:26:00 +08:00、Extra Usage 关闭、可用 credit $0。只核对已展示的续费凭据日期与 paid-until 覆盖，不虚构精确账期起点。见 `supplier-paid-receipt.json`、`supplier-coverage-current.json`。CNY 实付与 USD 成本归集没有合并，也不是再次向服务商付款。

完整新导出共 137 条、truncated=false，原件私有保存，公开材料只有会计字段。旧完整导出与新导出都在仓外 0700 目录、文件 0600；矩阵通过私有原件 SHA256 绑定原件，本机复核能读取它们，公开仓库不包含原件或请求头。

历史任务 `3c05faf8` 已实际回填 18,694 输入、8,000 输出、9,344 缓存输入 tokens，修正后输入 37,863、输出 31,495、缓存 18,688；成本 $0.013502 → **$0.019184**，原 GPU 1460.570 秒保持。事务行锁、精确原值摘要、私有完整备份、第二次执行不变；回填瞬间其他 9 个任务和非会计字段不变。见 `backfill-applied.json`、`audit-readback.json`、`backfill-reconciliation.json`。

月累计归集现为 **$0.045923**，任务数仍为 10；最终前端账单已逐项显示新任务和回填历史任务，见 `billing-final-ui.png`。新增重试消费有独立 `usage_settlements`，没有覆盖旧成本或回填审计。消费批次仍来自进程内累计，不是崩溃持久账本，未声称跨进程 exactly-once。GPU 秒为远程调用墙钟，包含排队和传输，不是物理 GPU 忙时；未猜填历史缺失墙钟。

## 本轮逐项修复及防回退

评测集 `docs/evals/production-resolution-eval-set.jsonl` 现含 13 类问题，每类都有正例、反例、回归入口和生产证据要求。

| 评测 | 修复或状态 | 证据 |
|---|---|---|
| pri-001 ASR PyAV 接口不兼容 | 固定 faster-whisper 1.2.1、av 18.1.0；真实两集 ASR 非空 | `asr-first-output.png`、阶段日志 |
| pri-002 历史漏记 | 审计、幂等事务回填已执行 | `backfill-applied.json` |
| pri-003 已结算任务补跑漏新增费用 | 新内存批次叠加旧记录，PG 行锁保留其他字段；无新增批次重复结算不变 | `book-delivery.json` 中三批 settlement |
| pri-004 转写超时漏墙钟 | finally 记远程调用墙钟；读预算 5400 秒覆盖批任务预算 | `test_usage_backfill.py` |
| pri-005 备份目录阻断 init | 目录 0750、文件 0600；原内容摘要不变，失败 Pod 恢复 | `backup-directory-fix.json`、`init-recovery.json` |
| pri-006 OCR 逐帧错误与坏缓存 | 阶段拒绝错误/缺行；坏缓存重新执行，合法无字帧允许 | `second-ocr-errors.json`、行为回归 |
| pri-007 Kaggle 自动解包输入 | 实测路径为 frames/ 子目录；支持该路径及扁平路径，逐图片 SHA256、唯一控制标识校验；归档＋清单共两次上传 | `input-layout.json`、`ocr-engine-probe.json` |
| pri-008 RapidOCR 缺 ONNX Runtime | 同时固定 rapidocr 3.10.0、onnxruntime 1.31.0；逐帧全错不得标成功 | 单帧探针 100 字、0 错；真实两集 OCR 完成 |
| pri-009 订阅现金与归集混淆 | 按用户新选择分币种、分凭据核验；拒绝未付、错账户/产品、跨期、截断、漏重试和虚构现金扣款 | `receipt-gate-green.txt`、新付款与对账证据 |
| pri-010 验签公开证书重复下载 | 只缓存公开证书，尊重 Cache-Control/Age、过期刷新、错误不回退，保留 token 全部验证 | `cert-cache-runtime.json`：首次 1.2042 秒，第二次 0.0000 秒；18 项回归 |
| pri-011 第二讲裸图链与内联图链 | 白名单内图片归一成独立附件引用，配套已有剪辑确定性插入；未知/越界/外站引用拒绝 | `note-repair-applied.json`、`reader-second-repaired.json/png`；零新增模型调用、二次不变 |
| pri-012 评测收集阶段污染存储 | 移除顶层项目导入，子进程确认收集不初始化配置；回归恢复；本地副作用处理见下文 | `note-auth-import-order-red.txt`、`note-auth-regression.txt`、`local-test-cleanup.json` |
| pri-013 媒体及时就绪 | **未关闭**。多轮分段传输等待，公开证书缓存生效不足以证明全链性能达标。新增 60 秒真实就绪评测，缺完整采样也失败 | `reader-readiness-gate.txt` 保持 FAIL；约 18 分钟是观测窗口，不是精确加载耗时 |

相关 Python 回归 **533 项通过**，mypy **39 模块通过**，相关 ruff 通过；GPU worker/输入清单/引擎等 30 项自测通过，前端原有相关 6 项回归通过。测试接入既有 CI；未创建提交、PR 或触发 GitHub Actions，不能声称远端 CI 或分支保护已通过。

技术依据：[google-auth 文档说明默认逐次重拉证书并建议缓存](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html)、[RapidOCR 官方安装要求同时安装运行时](https://github.com/RapidAI/RapidOCRDocs/blob/main/docs/install_usage/rapidocr/install.en.md)、[faster-whisper/PyAV 兼容问题](https://github.com/SYSTRAN/faster-whisper/issues/1589)。实际通过依据仍是本轮运行态与产物。

## 发布与实际产物

最终 API：`video2md:0.2.0-note-embeds-20261022f`；GPU router：`gpu-router:0.2.3-ocr-layout-20261022g`。两服务 Ready=1，主容器无新增重启。见 `final-runtime.json`。Kaggle 单帧探针的实际源码 SHA 与 G 清单一致，CUDA 可用、两张 Tesla T4、nvidia-smi=0；业务 kernel 终态 done。

完整生成发生在 API D / GPU G，后续 E 只修公开证书缓存，F 只修图链归一。生成与结算核心 API/graph/LLM/runs/transcribe 源码摘要在最终容器中一致；第二讲已有正文通过 F 的归一逻辑审计修复，未重新请求模型。没有把后续阅读验证说成重新全量生成。

第二讲修复有私有原记录备份，先锁定并核对正文摘要，数据库提交后原子同步正文文件；重试可收敛文件状态。第一遍变更、第二遍不变，其他任务、其余字段、用量和成本保持，5 图＋5 视频均可访问。见 `note-repair-applied.json`。

首次完整补跑完成 ASR/抽帧后因平台输入归档路径失败；第二次因缺引擎失败；第三次复用仍有效的 ASR/帧缓存，重新 OCR、剪辑与生成成功。以实际 stage span 为准，不再泛称每次都能复用或都必须重算。

发布均为本地准备清单、服务端 dry-run 检查只改目标镜像、stdin apply；节点受限构建后导入 k3s containerd。环境变量、卷和 Secret 投射保持。初次 apply 预检发现旧 last-applied 中重复 V2MD_ENV；修正记账元数据后确认 live spec 不变，未 force-conflicts。

## 过程事故、清理与偏离

- 本轮回填备份目录初设 0700，阻断只有 CHOWN 能力、使用应用 gid 的 init 遍历，导致发布超时与站点短暂 502。改目录 0750，文件仍 0600，内容摘要不变，仅重建失败且未 Ready 的 Pod；随后健康 200、真实 Google 登录恢复。未隐去事故。
- 新图链评测曾在收集阶段提前导入项目配置，API 测试隔离环境设置过晚，14 项回归变红。我在结果返回前启动了发布预检，发现红项后停止，并确认生产仍是 E/Ready=1；修正导入顺序、160 项相关测试通过后才发布 F。未绕过红项部署。
- 隔离事故影响了**本地忽略文件**，没有触及生产数据库。私有快照后清理本轮可识别的 42 条夹具记录，剩余 89 条原样保留；身份存储保留。state.json 仅剩测试哨兵，现存任务没有有效的本地 note＋transcript 对，派生缓存重建为空。**没有事故前缓存基线，不能声称字节级恢复原始缓存。** 本地 Obsidian Vault 中三个测试命名媒体文件有写入，已保存当前私有快照；原有 escape.jpg 链接此前已存在，未删除这些无事前基线的文件。见 `local-test-cleanup.json`。
- [BLD-01][DEFAULT] 早期共享 flock 未设等待超时；首次锁文件权限拒绝，未进入构建。后改只读 fd 排他锁并设置 300 秒超时。根因：临时构建入口未统一。留痕：构建 unit 日志、本报告。还账：统一构建护栏入口，owner Codex。
- [BLD-03][DEFAULT] 第二次 API 构建复用了较早磁盘/内存采样。之后的构建均在锁内重采并检查阈值。根因：预检未内聚。留痕：构建日志。还账：统一构建护栏入口，owner Codex。
- [DRIFT-02][DEFAULT] 工作区含大量原有未提交改动，本轮以源码摘要、镜像和运行态清单对回，尚未绑定已提交 Git SHA。未替换他人改动、未声称远端 CI 通过。留痕：各 manifest、`final-runtime.json`。还账：整理独立提交并走标准发布管道，owner Codex。
- [BLD-04][DEFAULT] 只清理本轮清单中的旧 tag，检查所有 Pod/init 引用并保留当前与上次发布。首次清理把 C 的名称写错，未命中该 GPU 镜像；后改为直接读取构建清单并核对最终不存在。初次记录的 removed 布尔值不作为成功依据，以最终逐 tag 检查为准。见 `unused-image-cleanup-attempt1.json`、`unused-image-cleanup.json`。
- 未使用门禁绕过、外部镜像推拉、服务器 kustomize 或自行批准 HARD 例外。

## 复核

以下严格验收命令会按实情返回失败，不应通过改状态或忽略退出码放行：

```sh
python3 scripts/check_production_failure_cost.py docs/evals/production-failure-cost-resolution-2026-10-22.json --expected-release video2md:0.2.0-note-embeds-20261022f
python3 scripts/check_kaggle_followup.py docs/evals/kaggle-billing-resolution-2026-10-22.json --expected-release video2md:0.2.0-note-embeds-20261022f --expected-gpu-release gpu-router:0.2.3-ocr-layout-20261022g --expected-run-id dd586072
python3 scripts/check_reader_readiness.py docs/testing/2026-10-22/production-resolution/reader-readiness-evidence.json
```

账目只读复核（原始导出位于本机私有目录）：

```sh
python3 scripts/reconcile_opencode_usage.py /Users/linmintao/.config/video2md-settlement/provider-requests-dd-final-2026-10-22.json docs/testing/2026-10-22/production-resolution/final-state.json --run-id dd586072 --output /tmp/video2md-dd-reconciliation.json
```

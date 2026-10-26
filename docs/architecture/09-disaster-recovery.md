# 09 · 灾备 Runbook

- 最后核对：2026-09-25 ｜ 基准：P1-7 落地（演练制度化 + 首次实测归档）
- 批判参照：① 数据安全层（🟡）；② 可运维性（🟡）
- **RTO/RPO 目标值为本篇新立（此前全仓无定义），P1-7 演练校准后修订。**

## 目标（P1-7 前的工作口径）

| 指标 | 目标 | 依据 |
|---|---|---|
| RPO（数据丢失容忍） | ≤ 24h | `backup.sh` daily cron 频率 |
| RTO（恢复时间容忍） | ≤ 2h | 整机窗口（含环境重建/镜像/网络）。2026-09-25 首次实测：恢复本质路径（校验+回灌+起栈+验证）4s（合成种子，数据层+进程级）——本质路径有 3 个量级余量，2h 预算保留覆盖整机人工环节 |
| 异地副本 | 必配 | `V2PDF_BACKUP_REMOTE`（rclone → B2/S3/OSS）；只落本地的备份防不了整机损失 |

## 备份面（现状，`scripts/backup.sh`）

- 覆盖：`v2pdf-data` 卷（state/runs/users/checkpoints/logs）+ `vault` 卷（deliver 形态不用）+ PG 在跑时 `pg_dump`。
- 保留：`backups/daily/` 留 7 份 + `backups/weekly/`（周一晋升）留 4 份 + sha256 manifest。
- cron：`15 4 * * * …/scripts/backup.sh >> /var/log/v2pdf-backup.log 2>&1`。
- 一致性口径：runs/state 原子 rename 恒完整；checkpoints 可丢弃接受瞬时不一致；严格一致 = `docker compose stop video2pdf` 后备。
- 防重入锁 + manifest 校验（`test_backup_scripts.py` 离线钉住）。

### ⚠ 备份的盲区（批判①数据安全层）

| 盲区 | 处置 |
|---|---|
| `secrets/`（video2pdf.env / obs.env / colab / kaggle token）不在卷里 | **已裁定（P1-7）**：季度核对密码管理器清单制——随季度演练执行，`drill_restore.sh --secrets-checked` 记录入档。弃加密归档进备份卷方案：备份密钥本身又是 secret（鸡生蛋），单操作者现状零新机制即闭环。清单见下 |
| 反代配置/TLS 证书 | P0-4 入仓后由 git 承载；证书由 Caddy/lego 自动重签 |
| 镜像 | GHCR 不可变 tag（`docker pull ghcr.io/…:<tag>` 手动顶上） |

secrets 登记清单（密码管理器逐条）：`LLM_API_KEY`、`LLM_BASE_URL`/`LLM_MODEL`、`GOOGLE_CLIENT_ID`(+设备流 SECRET)、`WECHAT_APP_ID/SECRET`、`V2PDF_PG_DSN`（含口令）、`V2PDF_DEADMAN_PING_URL`、`FEISHU_WEBHOOK_URL`、`LANGFUSE_*`、`GF_SECURITY_ADMIN_PASSWORD`、colab `token.json`、kaggle `kaggle.json`。

## 恢复流程（`scripts/restore.sh`）

```bash
# 整机重建顺序
1. 新机器装 Docker + 克隆仓库（git 不可用则 docker pull GHCR 镜像手动起）
2. 放回 secrets/（密码管理器取出，0600）
3. scripts/restore.sh <日期>            # 先校验 manifest；覆盖前自动快照现有卷到 backups/pre-restore-<日期>/
   scripts/restore.sh <日期> --only data # 可按卷恢复
4. docker compose … up -d && curl localhost:8790/healthz && curl localhost:8790/preflight
5. 核对：最近 run 记录在场（GET /runs 需登录）、告警链自测
```

## 演练制度（P1-7 ✅ 已落地）

- **月度轻量校验（自动）**：GitHub scheduled workflow `drill`（`.github/workflows/drill.yml`，
  每月 1 号 + 手动 dispatch）跑 `scripts/drill_restore.sh`——干跑全链，不触碰任何真实卷/栈。
- **季度整演练（人工）**：跑 `scripts/drill_restore.sh --record docs/architecture/drills/<日期>-Q<n>-<序>.md
  --secrets-checked`（含密码管理器清单逐条核对），记录归档 `docs/architecture/drills/`。
- **演练面（脚本内建验收）**：① manifest 自算自比校验 ② data/vault 归档恢复 + 与种子逐字节
  diff ③ PG dump→库重建→回灌断言 ④ 恢复后服务可用（healthz + 3 个历史 run + 留痕可读）
  ⑤ RTO 计时与目标比对。实测记录：2026-09-25 RTO 4s（首份已归档）。
- **记录模板**（脚本自动渲染，`--record` 落盘）：

```
日期：  ｜ 操作人：  ｜ 备份点：  ｜ 实测 RTO：  ｜ RPO 符合：Y/N
manifest/diff/PG/服务验证：  ｜ secrets 清单核对：Y/N
发现问题：  ｜ 跟进项（入 ROADMAP/批判）：
```

## 已知限制

1. ~~演练当前非制度~~（P1-7 ✅：月度 CI 干跑 + 季度人工整演练 + 记录归档）。
2. 恢复后**在跑 run 的口径**：备份点之后的 running run 会在启动时被重投（幂等重入）；deliver 在架产物若在备份点之后 ACK/焚毁，恢复后重新出现在架（窗口期双份风险可接受：产物幂等，客户端按 run_id 去重）。
3. PG 与 json 档混用场景（迁移窗口期）的恢复顺序未固化——先恢复 PG dump 再恢复卷。

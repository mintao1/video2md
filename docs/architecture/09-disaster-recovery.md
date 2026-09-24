# 09 · 灾备 Runbook

- 最后核对：2026-09-24 ｜ 基准：HEAD=f6a4a0e
- 批判参照：① 数据安全层（🟡）；② 可运维性（🟡）
- **RTO/RPO 目标值为本篇新立（此前全仓无定义），P1-7 演练校准后修订。**

## 目标（P1-7 前的工作口径）

| 指标 | 目标 | 依据 |
|---|---|---|
| RPO（数据丢失容忍） | ≤ 24h | `backup.sh` daily cron 频率 |
| RTO（恢复时间容忍） | ≤ 2h | 单容器重建 + 卷恢复的人工流程估上限；首次演练后修订 |
| 异地副本 | 必配 | `V2MD_BACKUP_REMOTE`（rclone → B2/S3/OSS）；只落本地的备份防不了整机损失 |

## 备份面（现状，`scripts/backup.sh`）

- 覆盖：`v2md-data` 卷（state/runs/users/checkpoints/logs）+ `vault` 卷（deliver 形态不用）+ PG 在跑时 `pg_dump`。
- 保留：`backups/daily/` 留 7 份 + `backups/weekly/`（周一晋升）留 4 份 + sha256 manifest。
- cron：`15 4 * * * …/scripts/backup.sh >> /var/log/v2md-backup.log 2>&1`。
- 一致性口径：runs/state 原子 rename 恒完整；checkpoints 可丢弃接受瞬时不一致；严格一致 = `docker compose stop video2md` 后备。
- 防重入锁 + manifest 校验（`test_backup_scripts.py` 离线钉住）。

### ⚠ 备份的盲区（批判①数据安全层）

| 盲区 | 处置 |
|---|---|
| `secrets/`（video2md.env / obs.env / colab / kaggle token）不在卷里 | **当前**：手工入密码管理器（逐键登记清单如下）。**P1-7**：二选一制度化——加密归档进备份卷，或每季度核对密码管理器清单 |
| 反代配置/TLS 证书 | P0-4 入仓后由 git 承载；证书由 Caddy/lego 自动重签 |
| 镜像 | GHCR 不可变 tag（`docker pull ghcr.io/…:<tag>` 手动顶上） |

secrets 登记清单（密码管理器逐条）：`LLM_API_KEY`、`LLM_BASE_URL`/`LLM_MODEL`、`GOOGLE_CLIENT_ID`(+设备流 SECRET)、`WECHAT_APP_ID/SECRET`、`V2MD_PG_DSN`（含口令）、`V2MD_DEADMAN_PING_URL`、`FEISHU_WEBHOOK_URL`、`LANGFUSE_*`、`GF_SECURITY_ADMIN_PASSWORD`、colab `token.json`、kaggle `kaggle.json`。

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

## 演练制度（P1-7 落实，本节为预案）

- **频率**：每季度一次（日历化；CI 定时 job 干跑 restore 到临时卷可作为月度轻量校验）。
- **验收**：恢复到临时 dst 后 ① manifest 校验过 ② api 起栈 healthz/preflight 绿 ③ 随机抽 3 个历史 run 记录可读 ④ 实测 RTO 记录归档。
- **记录模板**：

```
日期：  ｜ 操作人：  ｜ 备份点：  ｜ 实测 RTO：  ｜ RPO 符合：Y/N
发现问题：  ｜ 跟进项（入 ROADMAP/批判）：
```

## 已知限制

1. 演练当前非制度（README 原文为「建议」）——P1-7 前无保障。
2. 恢复后**在跑 run 的口径**：备份点之后的 running run 会在启动时被重投（幂等重入）；deliver 在架产物若在备份点之后 ACK/焚毁，恢复后重新出现在架（窗口期双份风险可接受：产物幂等，客户端按 run_id 去重）。
3. PG 与 json 档混用场景（迁移窗口期）的恢复顺序未固化——先恢复 PG dump 再恢复卷。

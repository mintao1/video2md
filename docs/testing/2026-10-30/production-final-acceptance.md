# 生产最终验收报告

**验收时间：** 2026-10-30 06:20（Asia/Shanghai）  
**目标：** `https://ferosse.tech` / K3s namespace `video2md`  
**验收版本：** Git `e8785d90263e`，镜像 `video2md:e8785d90263e`  
**部署流水线：** CI `40964180644`、Gate `40964180650`、Deploy `40964180565` 均成功

## 结论

**最终验收通过。**

应用运行、数据库租户隔离、备份加密与远端同步、Langfuse TLS/认证出口均已通过现场复验。

## 验收证据

| 项目 | 现场证据 | 结果 |
|---|---|---|
| Pod 运行 | `video2md-api` `1/1 Running`，重启 `0`，镜像 `video2md:e8785d90263e` | PASS |
| 版本与健康 | 本地及公网 `/healthz`：`ok=true`、`version=0.2.0` | PASS |
| 生产预检 | `/preflight`：ffmpeg、临时卷 28.9GB、deliver outbox、Colab CLI 全通过 | PASS |
| 数据库迁移 | init 日志：schema v6，幂等重跑 | PASS |
| 数据库角色 | app/migrate 均 LOGIN、非 superuser、非 BYPASSRLS；owner 为 NOLOGIN | PASS |
| RLS | 8 张业务表均 `relrowsecurity=true`、`relforcerowsecurity=true` | PASS |
| 双租户隔离 | 事务回滚：`b_sees_a=0`、`b_sees_own=1`、`a_sees_b=0` | PASS |
| 公共路由边界 | health/legal=`200`；未认证 `/me`=`401`；docs/schema/redoc=`403`；`/queue`=`404` | PASS |
| 本地备份 | `0` 个明文 `.tar.gz`、`20` 个 `.age`、不安全权限 `0`；age 解密与 gzip/tar 完整性通过 | PASS |
| 远端备份 | `backup-2c:/root/video2md-backup` 有 `20` 个 `.age`、`0` 个明文/临时文件；远端 manifest 与本地 SHA256 一致 | PASS |
| 备份调度 | root crontab 仅保留 `/usr/local/sbin/video2md-backup-prod.sh`；ubuntu crontab 已移除重复/失效任务 | PASS |
| Langfuse Secret | `platform-obs/obs-secrets` 已挂载 `LANGFUSE_HOST`、两套 Basic Auth，未注入失效代理 | PASS |
| Langfuse exporter | collector health 正常；`sent_spans_total=1`、`send_failed_spans_total=0`；无 TLS/endpoint/dropped 错误 | PASS |
| 内容保护 | `V2MD_OTEL_INCLUDE_CONTENT=off`，验收 probe 不含用户内容 | PASS |
| 回归与合同 | CI、Gate、Deploy、backup/observability/readiness 测试均通过 | PASS |

## F-01/F-02 修复记录

### F-01 备份保护

已安装 `age` 与 `rclone`，生成 root-only age identity；生产备份配置写入 `/etc/video2md-backup.env`，包含 production 模式、recipient、30 天 retention、远端目标和 `scp` 传输模式。由于目标 SFTP 服务会重置 rclone 多路连接，备份脚本增加了顺序、临时文件原子提交的 SCP 传输实现；最新备份日志记录异地推送成功。

### F-02 Langfuse

已创建 `platform-obs/obs-secrets` 并挂载 Langfuse host 与两套认证信息。移除失效的代理变量后，collector 通过 HTTPS 直连 Langfuse；生产 probe 已成功送达，collector 指标显示发送 1、失败 0，错误日志无 `unsupported protocol scheme`、TLS 或 `dropped_items`。

本报告不包含任何密码、API Key、Basic Auth 或 age 私钥。

# 10 · 改名 video2md → video2pdf：兼容层与切换步骤

本次改名分两步。第一步（代码与运行时）已在仓库内完成，并保留兼容层，**旧的部署清单不改也能直接跑新镜像**。第二步（基础设施身份）需要在服务器和平台仓上操作，按本文步骤做。

## 一、已完成：代码与运行时

| 项 | 新名 | 兼容方式 |
| --- | --- | --- |
| Python 包 / 入口 | `video2pdf`，`python -m video2pdf` | `backend/video2md/` 垫片：`python -m video2md` 与 `import video2md.xxx` 仍可用（同一模块对象） |
| 环境变量 | `V2PDF_*` | `V2MD_*` 在新键未设时自动补齐，并在启动时打印弃用提示 |
| 凭证文件 | `~/.config/video2pdf.env` | 新文件不存在时读 `~/.config/video2md.env` |
| PostgreSQL 表 | `v2pdf_*` | 第一次 `migrate` 时把存量 `v2md_*` 表、索引、序列整体改名（单事务，数据原样保留） |
| 前端登录 token | `localStorage.v2pdf_token` | 首次读取时从 `v2md_token` 迁移，已登录用户不会被登出 |
| Prometheus / OTel 指标 | `v2pdf_*`，`service.name=video2pdf-serve` | 无兼容：仪表盘与告警规则需按新名更新 |
| OAuth state cookie | `v2pdf_oauth_state` | 无兼容：切换瞬间正在进行的登录需重试一次 |

**刻意不改的标识**（改了会丢数据或断关联）：

- 确定性 trace 前缀与 opencode 会话前缀 `video2md-`：历史 run 的 trace、evals 回挂和供应商用量对账靠它关联。
- PG 迁移锁键 `0x76326D64`。
- `docs/testing/` 中的历史证据，以及回放它们的评测集和检查脚本：它们记录的是当时的生产事实。
- `gpu_plugins/v2md` 整个插件包（含 Kaggle 数据集/内核名 `v2md-io` / `v2md-job`、Colab 远端文件名）：外部 gpu_router 按这些名字加载插件，用户 Kaggle 账号里也已有同名资源。
- 服务器运维脚本 `scripts/backup.sh`、`restore.sh`、`deploy.sh`、`drill_restore.sh`：依赖服务器上的容器名（`video2md-postgres-1`）、PG 库名与用户（`v2md`）、部署目录和 crontab 里的 `V2MD_*` 变量（shell 脚本没有 Python 侧的旧键补齐），随第二步一起改。
- 本机 B站出口安装器的 launchd 标签与配置目录（改了会装出第二套服务抢占端口）。

## 二、待操作：基础设施身份

以下各项在仓库外，需要按顺序操作。在完成之前，`deploy/k8s/`、`deploy/docker-compose*.yml`、`.github/workflows/deploy.yml` 保持旧名，以免部署时找不到 secret 或与旧 Pod 争用 hostNetwork 端口 8790。

1. **GitHub 仓库改名**：Settings → Repository name 改为 `video2pdf`。GitHub 会自动重定向旧 URL；随后更新 README 的 CI 徽章和各处 `mintao1/video2md` 链接。
2. **服务器 checkout 目录**：`~/stack/video2md-git` → `~/stack/video2pdf-git`，并把 remote 改为新仓库地址。
3. **平台仓 `mintao1/cloud-deploy-platform`**：把产品名从 `video2md` 改为 `video2pdf`。
4. **k8s**：新建 `video2pdf` namespace 与 `video2pdf-secrets`（内容同旧 secret，键名可改为 `V2PDF_*`），停掉旧 namespace 的 Deployment 后再部署新的（hostNetwork 端口不能同时占用）。数据卷 hostPath（`/var/lib/docker/volumes/video2md_*`）保持原路径，不需要搬数据。
5. **镜像名**：`video2md:<tag>` → `video2pdf:<tag>`，同步更新清单中的 `image:` 与 `python -m video2pdf`。
6. **运维脚本**：服务器 crontab 中的 `V2MD_*` 改为 `V2PDF_*`，再把上面列出的四个运维脚本改为新名。
7. **监控**：仪表盘与告警规则里的 `v2md_*` 指标改为 `v2pdf_*`。

## 三、回滚

新代码第一次 `migrate` 后，表已改名为 `v2pdf_*`，旧镜像会以「schema 版本落后」拒绝启动。需要回滚到旧镜像时，先把表名改回：

```sql
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname, c.relkind FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = current_schema() AND c.relname LIKE 'v2pdf\_%'
             AND c.relkind IN ('r', 'p', 'i', 'S')
  LOOP
    EXECUTE format('ALTER %s %I RENAME TO %I',
      CASE r.relkind WHEN 'i' THEN 'INDEX' WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
      r.relname, 'v2md_' || substr(r.relname, 7));
  END LOOP;
END $$;
```

## 四、收尾（全部切换完成后）

确认生产已不再使用旧名后，删除兼容层：`backend/video2md/` 垫片、`config.py` 中的 `V2MD_*` 补齐与旧凭证文件兜底、前端 `v2md_token` 迁移。

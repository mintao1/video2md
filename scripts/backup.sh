#!/usr/bin/env bash
# video2md 数据备份（运维三件套 2026-09-23）——本地保留策略：daily 7 份 + weekly 4 份。
#
# 备什么（对齐 deploy/docker-compose.prod.yml 卷）：
#   - v2md-data 卷   state.json / runs.json / users.json / checkpoints.sqlite / logs（SSOT 小文件）
#   - vault 卷       vault 模式笔记产物（自托管形态；同步 ≠ 备份——误删/勒索会照样同步过去）
#   - Postgres       仅当容器在跑时 pg_dump（checkpointer 数据；成功 run 会删 checkpoint，
#                    本卷可丢弃度高，dump 失败不阻断整体备份，只告警退出码）
#   - 异地容灾       V2MD_BACKUP_REMOTE 配了 rclone remote（如 b2:v2md-backup）则同步密文
#                    （对象存储 B2/S3/OSS）；生产强制远端可用，失败即非零退出
#
# 一致性口径：runs.json/state.json 均为「tmp 写完 + 原子 rename」发布（runs.py/_save_runs），
# tar 视角恒为完整文件；checkpoints.sqlite 在跑动中可能瞬时不一致——可丢弃观测数据，接受。
# 严格一致可先 docker compose -f deploy/docker-compose.prod.yml stop api 再备
# （cron 里自行加，默认不停机）。
#
# 用法（crontab：15 4 * * * /path/to/video2md/scripts/backup.sh >> /var/log/v2md-backup.log 2>&1）：
#   scripts/backup.sh                          # 默认备到 <repo>/backups/
#   scripts/backup.sh --dst /mnt/backup/v2md   # 自定义落盘目录
#   scripts/backup.sh --keep-daily 14 --keep-weekly 8
#   scripts/backup.sh --dry-run                # 只打印将执行的命令
#
# 恢复：scripts/restore.sh（同目录）。异地推远端由 V2MD_BACKUP_REMOTE 环境变量开启。
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST_DIR="${V2MD_BACKUP_DIR:-/var/lib/video2md/backups}"
KEEP_DAILY=7
KEEP_WEEKLY=4
DRY_RUN=0
COMPOSE_FILE="$REPO_DIR/deploy/docker-compose.prod.yml"
BACKUP_REMOTE="${V2MD_BACKUP_REMOTE:-}"   # rclone remote（如 b2:v2md-backup）；空=只备本地
BACKUP_TRANSPORT="${V2MD_BACKUP_TRANSPORT:-rclone}"  # rclone（默认）或 scp（受限 SFTP 主机）
BACKUP_RECIPIENT="${V2MD_BACKUP_AGE_RECIPIENT:-}"
BACKUP_RETENTION_DAYS="${V2MD_BACKUP_RETENTION_DAYS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dst) DST_DIR="$2"; shift 2 ;;
    --keep-daily) KEEP_DAILY="$2"; shift 2 ;;
    --keep-weekly) KEEP_WEEKLY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

if [[ "${V2MD_ENV:-development}" == "production" ]]; then
  [[ -n "$BACKUP_RECIPIENT" ]] || { echo "❌ 生产备份必须配置 V2MD_BACKUP_AGE_RECIPIENT（拒绝明文归档）" >&2; exit 1; }
  [[ -n "$BACKUP_REMOTE" ]] || { echo "❌ 生产备份必须配置 V2MD_BACKUP_REMOTE（拒绝只留本机）" >&2; exit 1; }
  [[ "$BACKUP_RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] || { echo "❌ 生产备份必须配置正整数 V2MD_BACKUP_RETENTION_DAYS" >&2; exit 1; }
fi
if [[ -n "$BACKUP_RECIPIENT" ]] && ! command -v age >/dev/null 2>&1 && [[ $DRY_RUN -eq 0 ]]; then
  echo "❌ 配置了 V2MD_BACKUP_AGE_RECIPIENT 但未安装 age" >&2
  exit 1
fi

# Compose 顶层 name 固定为 video2md；这些显式卷名也与 K8s hostPath 迁移一致。
DATA_VOL="video2md_v2md-data"
VAULT_VOL="video2md_vault"
STAMP="$(date +%Y-%m-%d)"
DOW="$(date +%u)"  # 1=周一
DAILY_DIR="$DST_DIR/daily"
WEEKLY_DIR="$DST_DIR/weekly"
MANIFEST="$DST_DIR/manifest.sha256"

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"
chmod 700 "$DST_DIR" "$DAILY_DIR" "$WEEKLY_DIR"

run() { if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi; }

# 防重入：mkdir 原子锁（flock 在 macOS 没有；mkdir -n 跨平台等价）。上一轮 cron 还没
# 跑完（vault 大）时直接退出。锁目录残留时按 mtime 判死锁：>24h 视为上次异常退出，抢过来。
LOCK_DIR="$DST_DIR/.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [[ -d "$LOCK_DIR" ]] && [[ -n "$(find "$LOCK_DIR" -maxdepth 0 -mtime +0 2>/dev/null)" ]]; then
    echo "⚠ 陈旧锁（>24h，上次异常退出），强占重跑"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
  else
    echo "⚠ 已有备份在跑（$LOCK_DIR 被占），本轮跳过"; exit 0
  fi
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

echo "── video2md 备份 $STAMP → $DST_DIR (daily×$KEEP_DAILY weekly×$KEEP_WEEKLY)"

# 1) data 卷（SSOT：state/runs/users/checkpoints/logs）
DATA_TGZ="$DAILY_DIR/data-$STAMP.tar.gz"
run docker run --rm -v "$DATA_VOL:/src:ro" -v "$DAILY_DIR:/dst" alpine \
  tar czf "/dst/$(basename "$DATA_TGZ")" -C /src .
if [[ $DRY_RUN -eq 0 ]]; then
  docker run --rm -v "$DATA_VOL:/src:ro" -v "$DAILY_DIR:/dst" alpine \
    tar tzf "/dst/$(basename "$DATA_TGZ")" > /dev/null   # 完整性：能列出即归档未截断
  if [[ -f "$DATA_TGZ" ]]; then
    echo "✓ data 卷 → $DATA_TGZ ($(du -h "$DATA_TGZ" | cut -f1))"
    chmod 600 "$DATA_TGZ"
  elif [[ "${V2MD_ENV:-development}" == "production" ]]; then
    echo "❌ data 归档未生成" >&2; exit 1
  fi
  if [[ -n "$BACKUP_RECIPIENT" && -f "$DATA_TGZ" ]]; then
    age -r "$BACKUP_RECIPIENT" -o "${DATA_TGZ}.age" "$DATA_TGZ"
    rm -f "$DATA_TGZ"
    DATA_TGZ="${DATA_TGZ}.age"
    chmod 600 "$DATA_TGZ"
  fi
fi

# 2) vault 卷（笔记产物；Syncthing 同步 ≠ 备份）
VAULT_TGZ="$DAILY_DIR/vault-$STAMP.tar.gz"
run docker run --rm -v "$VAULT_VOL:/src:ro" -v "$DAILY_DIR:/dst" alpine \
  tar czf "/dst/$(basename "$VAULT_TGZ")" -C /src .
if [[ $DRY_RUN -eq 0 ]]; then
  docker run --rm -v "$VAULT_VOL:/src:ro" -v "$DAILY_DIR:/dst" alpine \
    tar tzf "/dst/$(basename "$VAULT_TGZ")" > /dev/null
  if [[ -f "$VAULT_TGZ" ]]; then
    echo "✓ vault 卷 → $VAULT_TGZ ($(du -h "$VAULT_TGZ" | cut -f1))"
    chmod 600 "$VAULT_TGZ"
  elif [[ "${V2MD_ENV:-development}" == "production" ]]; then
    echo "❌ vault 归档未生成" >&2; exit 1
  fi
  if [[ -n "$BACKUP_RECIPIENT" && -f "$VAULT_TGZ" ]]; then
    age -r "$BACKUP_RECIPIENT" -o "${VAULT_TGZ}.age" "$VAULT_TGZ"
    rm -f "$VAULT_TGZ"
    VAULT_TGZ="${VAULT_TGZ}.age"
    chmod 600 "$VAULT_TGZ"
  fi
fi

# 3) Postgres（容器在跑才 dump；生产 dump 失败会阻断备份）
PG_TGZ="$DAILY_DIR/pg-$STAMP.sql.gz"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] docker compose -f $COMPOSE_FILE ps --status running --services | grep -qx postgres"
  echo "[dry-run] docker compose -f $COMPOSE_FILE exec -T postgres sh -lc 'pg_dump -U \"\$POSTGRES_USER\" \"\$POSTGRES_DB\"' | gzip > $PG_TGZ"
elif docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -qx 'postgres'; then
    if docker compose -f "$COMPOSE_FILE" exec -T postgres sh -lc 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$PG_TGZ"; then
      if [[ ! -f "$PG_TGZ" ]]; then
        echo "❌ postgres 归档未生成" >&2; exit 1
      fi
      echo "✓ postgres → $PG_TGZ ($(du -h "$PG_TGZ" | cut -f1))"
      chmod 600 "$PG_TGZ"
      if [[ -n "$BACKUP_RECIPIENT" ]]; then
        age -r "$BACKUP_RECIPIENT" -o "${PG_TGZ}.age" "$PG_TGZ"
        rm -f "$PG_TGZ"
        PG_TGZ="${PG_TGZ}.age"
        chmod 600 "$PG_TGZ"
      fi
    else
      echo "❌ pg_dump 失败；删除半截文件" >&2
      rm -f "$PG_TGZ"
      [[ "${V2MD_ENV:-development}" == "production" ]] && exit 1
    fi
else
  echo "· postgres 容器未在跑（profile pg 未启用），跳过 PG dump"
fi

# 4) 周备：周一从当日日备晋升（cp 而非 mv——日备保留策略独立计数）
if [[ "$DOW" == "1" && -f "$DATA_TGZ" ]]; then
  for f in "$DAILY_DIR"/*-"$STAMP".*; do
    [[ -e "$f" ]] || continue
    run cp "$f" "$WEEKLY_DIR/"
  done
  [[ $DRY_RUN -eq 0 ]] && echo "✓ 周备晋升 → $WEEKLY_DIR/"
fi

# 5) 保留策略：超龄删除（按文件名里的日期戳排序，老在前；head -n -N 是 GNU 扩展，
# macOS 没有——用 wc 计数算出该删几份，跨平台）
prune() {
  local dir="$1" keep="$2" prefix="$3" n del
  [[ -d "$dir" ]] || return 0
  n=$(ls -1 "$dir" 2>/dev/null | grep -c "^${prefix}-" || true)
  del=$((n - keep))
  [[ $del -le 0 ]] && return 0
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    run rm -f "$dir/$f"
  done < <(ls -1 "$dir" 2>/dev/null | grep "^${prefix}-" | sort | head -n "$del")
}
prune "$DAILY_DIR" "$KEEP_DAILY" "data"
prune "$DAILY_DIR" "$KEEP_DAILY" "vault"
prune "$DAILY_DIR" "$KEEP_DAILY" "pg"
prune "$WEEKLY_DIR" "$KEEP_WEEKLY" "data"
prune "$WEEKLY_DIR" "$KEEP_WEEKLY" "vault"
prune "$WEEKLY_DIR" "$KEEP_WEEKLY" "pg"

# A production retention policy is an age bound as well as a count bound.  The
# count keeps recent daily/weekly recovery points; this second pass guarantees
# an operator's configured day limit is enforced before remote sync.
if [[ -n "$BACKUP_RETENTION_DAYS" ]]; then
  while IFS= read -r old; do
    [[ -n "$old" ]] || continue
    run rm -f "$old"
  done < <(find "$DAILY_DIR" "$WEEKLY_DIR" -type f \( -name 'data-*' -o -name 'vault-*' -o -name 'pg-*' \) \
      -mtime +"$BACKUP_RETENTION_DAYS" -print 2>/dev/null)
fi

# 6) manifest：全量 sha256（恢复前校验完整性用）
if [[ $DRY_RUN -eq 0 ]]; then
  ( cd "$DST_DIR" && find daily weekly -type f ! -name 'manifest.sha256' -print0 2>/dev/null \
      | sort -z | xargs -0 sha256sum > manifest.sha256.tmp && mv manifest.sha256.tmp manifest.sha256 )
  echo "✓ manifest → $MANIFEST ($(grep -c . "$MANIFEST") 个文件)"
fi

# 7) 异地容灾：rclone 推远端对象存储——本地盘炸 ≠ 备份同灭的解药。
#    生产已在上方强制配置远端；失败用退出码 3 阻断备份成功。
REMOTE_RC=0
if [[ -n "$BACKUP_REMOTE" ]]; then
  if [[ "$BACKUP_TRANSPORT" == "scp" ]]; then
    if [[ -x "$REPO_DIR/scripts/backup_scp_sync.sh" ]]; then
      if run "$REPO_DIR/scripts/backup_scp_sync.sh" "$DST_DIR" "$BACKUP_REMOTE"; then
        [[ $DRY_RUN -eq 0 ]] && echo "✓ 异地推送 → $BACKUP_REMOTE（scp）"
      else
        echo "❌ scp 异地推送失败（生产备份不允许只留本地副本）" >&2
        REMOTE_RC=3
      fi
    else
      echo "❌ scp 同步脚本不存在：$REPO_DIR/scripts/backup_scp_sync.sh" >&2
      REMOTE_RC=3
    fi
  elif [[ "$BACKUP_TRANSPORT" == "rclone" ]] && command -v rclone >/dev/null 2>&1; then
    # Sync the retained local set so remote storage does not grow forever with
    # deleted tenant archives. The local age-encrypted files remain the source
    # of truth; rclone only transports ciphertext.
    if run rclone sync "$DST_DIR" "$BACKUP_REMOTE" --transfers 4 --exclude '.lock/**'; then
      [[ $DRY_RUN -eq 0 ]] && echo "✓ 异地推送 → $BACKUP_REMOTE"
    else
      echo "❌ rclone 推送失败（生产备份不允许只留本地副本）" >&2
      REMOTE_RC=3
    fi
  else
    echo "❌ V2MD_BACKUP_REMOTE 传输配置无效或缺少 rclone" >&2
    REMOTE_RC=3
  fi
fi

echo "── 备份完成 $STAMP"
exit $REMOTE_RC

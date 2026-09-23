#!/usr/bin/env bash
# video2md 数据备份（运维三件套 2026-09-23）——本地保留策略：daily 7 份 + weekly 4 份。
#
# 备什么（对齐 docker-compose.yml 卷）：
#   - v2md-data 卷   state.json / runs.json / users.json / checkpoints.sqlite / logs（SSOT 小文件）
#   - vault 卷       笔记产物（Syncthing 只算同步不算备份——误删/勒索会照样同步过去）
#   - Postgres       仅当容器在跑时 pg_dump（checkpointer 数据；成功 run 会删 checkpoint，
#                    本卷可丢弃度高，dump 失败不阻断整体备份，只告警退出码）
#
# 一致性口径：runs.json/state.json 均为「tmp 写完 + 原子 rename」发布（runs.py/_save_runs），
# tar 视角恒为完整文件；checkpoints.sqlite 在跑动中可能瞬时不一致——可丢弃观测数据，接受。
# 严格一致可先 docker compose stop video2md 再备（cron 里自行加，默认不停机）。
#
# 用法（lcode-4c crontab：15 4 * * * /path/to/video2md/scripts/backup.sh >> /var/log/v2md-backup.log 2>&1）：
#   scripts/backup.sh                          # 默认备到 <repo>/backups/
#   scripts/backup.sh --dst /mnt/backup/v2md   # 自定义落盘目录
#   scripts/backup.sh --keep-daily 14 --keep-weekly 8
#   scripts/backup.sh --dry-run                # 只打印将执行的命令
#
# 恢复：scripts/restore.sh（同目录）。异地上传（rclone/restic）暂未集成，见 README 部署章。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST_DIR="$REPO_DIR/backups"
KEEP_DAILY=7
KEEP_WEEKLY=4
DRY_RUN=0
COMPOSE="docker compose -f $REPO_DIR/docker-compose.yml"

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

# compose 项目名固定 video2md（docker-compose.yml 的 name:），卷名由此派生
DATA_VOL="video2md_v2md-data"
VAULT_VOL="video2md_vault"
STAMP="$(date +%Y-%m-%d)"
DOW="$(date +%u)"  # 1=周一
DAILY_DIR="$DST_DIR/daily"
WEEKLY_DIR="$DST_DIR/weekly"
MANIFEST="$DST_DIR/manifest.sha256"

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

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
  echo "✓ data 卷 → $DATA_TGZ ($(du -h "$DATA_TGZ" | cut -f1))"
fi

# 2) vault 卷（笔记产物；Syncthing 同步 ≠ 备份）
VAULT_TGZ="$DAILY_DIR/vault-$STAMP.tar.gz"
run docker run --rm -v "$VAULT_VOL:/src:ro" -v "$DAILY_DIR:/dst" alpine \
  tar czf "/dst/$(basename "$VAULT_TGZ")" -C /src .
if [[ $DRY_RUN -eq 0 ]]; then
  docker run --rm -v "$VAULT_VOL:/src:ro" -v "$DAILY_DIR:/dst" alpine \
    tar tzf "/dst/$(basename "$VAULT_TGZ")" > /dev/null
  echo "✓ vault 卷 → $VAULT_TGZ ($(du -h "$VAULT_TGZ" | cut -f1))"
fi

# 3) Postgres（可选：容器在跑才 dump；dump 失败不阻断——checkpointer 数据可丢弃度高）
PG_TGZ="$DAILY_DIR/pg-$STAMP.sql.gz"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'video2md-postgres-1'; then
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] docker exec video2md-postgres-1 pg_dump -U v2md v2md | gzip > $PG_TGZ"
  else
    if docker exec video2md-postgres-1 pg_dump -U v2md v2md | gzip > "$PG_TGZ"; then
      echo "✓ postgres → $PG_TGZ ($(du -h "$PG_TGZ" | cut -f1))"
    else
      echo "⚠ pg_dump 失败（checkpointer 数据可丢弃，不阻断）；删除半截文件"
      rm -f "$PG_TGZ"
    fi
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

# 6) manifest：全量 sha256（恢复前校验完整性用）
if [[ $DRY_RUN -eq 0 ]]; then
  ( cd "$DST_DIR" && find daily weekly -type f ! -name 'manifest.sha256' -print0 2>/dev/null \
      | sort -z | xargs -0 sha256sum > manifest.sha256.tmp && mv manifest.sha256.tmp manifest.sha256 )
  echo "✓ manifest → $MANIFEST ($(grep -c . "$MANIFEST") 个文件)"
fi

echo "── 备份完成 $STAMP"

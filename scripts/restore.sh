#!/usr/bin/env bash
# video2md 备份恢复（配套 scripts/backup.sh，2026-09-23）。
#
# 从 backups/ 目录把 data 卷 / vault 卷 / Postgres 回灌。高危操作：覆盖现有卷内容——
# 默认带确认提示；--yes 跳过（cron/自动化慎用）。
#
# 用法：
#   scripts/restore.sh 2026-09-23                    # 恢复指定日期的备份（daily/weekly 都找）
#   scripts/restore.sh 2026-09-23 --dst /mnt/backup/v2md
#   scripts/restore.sh 2026-09-23 --only data        # 只恢复 data 卷（data|vault|pg）
#   scripts/restore.sh 2026-09-23 --yes              # 跳过确认
#
# 前置校验：sha256sum -c manifest.sha256（备份时生成；对不上 = 归档损坏，拒绝恢复）。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST_DIR="$REPO_DIR/backups"
STAMP=""
ONLY="data,vault,pg"
ASSUME_YES=0
# 双文件：观测栈（prometheus/alertmanager 等）与主栈同 project——只带主文件的话 down 会
# 连带删掉 obs 容器而 up 又不拉回，恢复后告警链静默消失
COMPOSE="docker compose -f $REPO_DIR/docker-compose.yml -f $REPO_DIR/docker-compose.obs.yml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dst) DST_DIR="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    -*) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    *) STAMP="$1"; shift ;;
  esac
done
[[ -n "$STAMP" ]] || { echo "用法：restore.sh <YYYY-MM-DD> [--dst DIR] [--only data,vault,pg] [--yes]" >&2; exit 2; }

DATA_VOL="video2md_v2md-data"
VAULT_VOL="video2md_vault"

find_file() {  # $1=类别（data|vault|pg）→ 在 daily/weekly 里找最新匹配
  local kind="$1" f
  for f in "$DST_DIR/daily/$kind-$STAMP".* "$DST_DIR/weekly/$kind-$STAMP".*; do
    [[ -e "$f" ]] && { echo "$f"; return 0; }
  done
  return 1
}

echo "── video2md 恢复 $STAMP (only=$ONLY)"

# 0) 完整性校验：manifest 逐行自算对比。
# 不用 `sha256sum -c`：macOS 自带的 Darwin sha256sum 对「哈希不匹配」只打 WARNING 且
# exit 0（GNU 才有正确退出码）——校验端必须跨平台等价，故在脚本内自算自比。
if [[ -f "$DST_DIR/manifest.sha256" ]]; then
  echo "· 校验 manifest.sha256 …"
  bad=0
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    want="${line%% *}"
    f="${line#*  }"
    [[ -f "$DST_DIR/$f" ]] || { echo "❌ manifest 列出的文件缺失：$f"; bad=1; continue; }
    got="$( (sha256sum "$DST_DIR/$f" 2>/dev/null || shasum -a 256 "$DST_DIR/$f") | cut -d' ' -f1 )"
    if [[ "$got" != "$want" ]]; then
      echo "❌ 校验失败：$f (want=$want got=$got)"
      bad=1
    fi
  done < "$DST_DIR/manifest.sha256"
  [[ $bad -eq 0 ]] || { echo "❌ manifest 校验失败：归档损坏/被篡改，拒绝恢复" >&2; exit 1; }
else
  echo "⚠ 无 manifest.sha256，跳过完整性校验"
fi

# 1) 确认（覆盖现有卷！）
echo "⚠⚠ 即将【覆盖】以下 Docker 卷（现有内容先做安全快照到 $DST_DIR/pre-restore-$STAMP/）："
[[ ",$ONLY," == *,data,* ]] && echo "   - $DATA_VOL"
[[ ",$ONLY," == *,vault,* ]] && echo "   - $VAULT_VOL"
[[ ",$ONLY," == *,pg,* ]] && echo "   - video2md-postgres-1（PG 数据）"
if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "确认恢复？输入 yes 继续：" ans
  [[ "$ans" == "yes" ]] || { echo "已取消"; exit 1; }
fi

# 2) 停栈（恢复期间服务不可用；卷被占用时 tar 回灌会损坏）
echo "· 停栈 …"
$COMPOSE down

# 3) 现有卷先做安全快照（恢复错了还能回来）
SNAP_DIR="$DST_DIR/pre-restore-$STAMP"
mkdir -p "$SNAP_DIR"
if [[ ",$ONLY," == *,data,* ]]; then
  docker run --rm -v "$DATA_VOL:/src:ro" -v "$SNAP_DIR:/dst" alpine \
    tar czf "/dst/data-before-restore.tar.gz" -C /src . \
    && echo "✓ 现有 data 卷快照 → $SNAP_DIR/"
fi
if [[ ",$ONLY," == *,vault,* ]]; then
  docker run --rm -v "$VAULT_VOL:/src:ro" -v "$SNAP_DIR:/dst" alpine \
    tar czf "/dst/vault-before-restore.tar.gz" -C /src . \
    && echo "✓ 现有 vault 卷快照 → $SNAP_DIR/"
fi

# 4) 回灌（tar 解到空卷根；卷已存在则先清空内容再解——恢复语义是「回到备份时点」）
untar_into() {  # $1=卷名 $2=归档
  docker run --rm -v "$1:/dst" -v "$(dirname "$2"):/src:ro" alpine \
    sh -c "rm -rf /dst/* /dst/..?* /dst/.[!.]* 2>/dev/null; tar xzf /src/$(basename "$2") -C /dst"
}
if [[ ",$ONLY," == *,data,* ]]; then
  F="$(find_file data)" || { echo "❌ 找不到 data-$STAMP 归档" >&2; exit 1; }
  echo "· 回灌 data 卷 ← $F"
  untar_into "$DATA_VOL" "$F"
  echo "✓ data 卷恢复完成"
fi
if [[ ",$ONLY," == *,vault,* ]]; then
  F="$(find_file vault)" || { echo "❌ 找不到 vault-$STAMP 归档" >&2; exit 1; }
  echo "· 回灌 vault 卷 ← $F"
  untar_into "$VAULT_VOL" "$F"
  echo "✓ vault 卷恢复完成"
fi

# 5) Postgres（可选：有 dump 才恢复）
if [[ ",$ONLY," == *,pg,* ]]; then
  if F="$(find_file pg)"; then
    echo "· 恢复 Postgres ← $F"
    $COMPOSE --profile pg up -d postgres
    until docker exec video2md-postgres-1 pg_isready -U v2md -d v2md > /dev/null 2>&1; do
      sleep 2
    done
    gunzip -c "$F" | docker exec -i video2md-postgres-1 psql -U v2md -d v2md > /dev/null
    $COMPOSE --profile pg stop postgres
    echo "✓ Postgres 恢复完成"
  else
    echo "· 无 pg-$STAMP 归档（备份时 PG 未启用），跳过"
  fi
fi

# 6) 起栈
echo "· 起栈 …"
$COMPOSE up -d
echo "── 恢复完成 $STAMP (现有卷快照留在 $SNAP_DIR/，确认服务正常后可删)"

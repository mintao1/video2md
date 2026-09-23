#!/usr/bin/env bash
# video2md 部署/回滚（终局布局·发布层的落地端）：git tag 驱动 + 起栈体检。
#
# 用法：
#   scripts/deploy.sh <tag|main>                     # 部署指定 tag（main=跟踪分支）
#   scripts/deploy.sh <tag> --host user@server       # 目标机（缺省 $V2MD_DEPLOY_HOST）
#   scripts/deploy.sh <tag> --dir ~/stack/video2md   # 服务器上仓库路径
#   scripts/deploy.sh <tag> --dry-run                # 只打印将执行的命令
#
# 流程：git fetch --tags → checkout <tag> → compose build → up -d → healthz/preflight 体检。
# 体检失败 → 非零退出并提示回滚命令（deploy.sh <上一个可用 tag>）。
# GHCR 上按 tag 存档的镜像（ci.yml release job）是灾备：git 不可用时可直接
#   docker pull ghcr.io/mintao1/video2md:<tag> 手动顶上。
set -euo pipefail

TAG=""
HOST="${V2MD_DEPLOY_HOST:-}"
DIR="$HOME/stack/video2md"
DRY_RUN=0
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.obs.yml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) [[ -z "$TAG" ]] && TAG="$1" || { echo "未知参数：$1（--help 看用法）" >&2; exit 2; }
       shift ;;
  esac
done

if [[ -z "$TAG" ]]; then
  echo "用法：scripts/deploy.sh <tag|main> [--host user@server] [--dir 路径] [--dry-run]" >&2
  exit 2
fi
if [[ -z "$HOST" ]]; then
  echo "缺目标机：--host user@server 或设 V2MD_DEPLOY_HOST" >&2
  exit 2
fi

run() { if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi; }
remote() { if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] ssh $HOST -- $*"; else ssh "$HOST" "$*"; fi; }

REF="$TAG"
[[ "$TAG" == "main" ]] && REF="origin/main"

echo "── 部署 $TAG → $HOST:$DIR"
remote "cd $DIR && git fetch --tags --prune && git checkout -f $REF && $COMPOSE build"
remote "cd $DIR && $COMPOSE up -d"

echo "── 体检（healthz / preflight）"
HEALTH_OK=0
if [[ $DRY_RUN -eq 0 ]]; then
  for i in 1 2 3 4 5 6; do   # 起栈后给 30s 爬升窗口
    if ssh "$HOST" 'curl -sf localhost:8790/healthz' >/dev/null 2>&1; then
      HEALTH_OK=1; break
    fi
    echo "· 等待服务就绪（$i/6）…"; sleep 5
  done
  if [[ $HEALTH_OK -ne 1 ]]; then
    echo "✗ healthz 不通：看日志 ssh $HOST 'cd $DIR && docker compose logs --tail 50 video2md'" >&2
    echo "  回滚：scripts/deploy.sh <上一个可用 tag>" >&2
    exit 1
  fi
  ssh "$HOST" 'curl -s localhost:8790/preflight' || true   # 体检项逐项看 detail，失败不挡部署
  ssh "$HOST" "curl -s localhost:8790/healthz" && echo
else
  echo "[dry-run] ssh $HOST 'curl -sf localhost:8790/healthz'  # 起栈后验证"
fi
echo "── 部署完成：${TAG} @ ${HOST}（版本核对：/healthz 的 version 字段）"

#!/usr/bin/env bash
# video2md 部署/回滚（终局布局·发布层的落地端）：git tag 驱动 + 起栈体检。
#
# 用法：
#   scripts/deploy.sh <tag|main>                     # 部署指定 tag（main=跟踪分支）
#   scripts/deploy.sh <tag> --host user@server       # 目标机（缺省 $V2MD_DEPLOY_HOST）
#   scripts/deploy.sh <tag> --dir ~/stack/video2md   # 服务器上仓库路径
#   scripts/deploy.sh <tag> --dry-run                # 只打印将执行的命令
#
# 流程：git fetch --tags → checkout <tag> → compose build → up -d → schema 迁移（P0-5）→
#       healthz/preflight 体检。迁移放 up -d 后：api 建池校验落后会 crash-loop 等 schema，
#       migrate 完成后 restart 自愈（对齐「PG 未就绪 fail-fast 自愈」既有口径）；
#       库版本超前（回滚到旧代码）则 migrate 拒继续——部署停在这，交人工判断。
# 体检失败 → 非零退出并提示回滚命令（deploy.sh <上一个可用 tag>）。
# GHCR 上按 tag 存档的镜像（ci.yml release job）是灾备：git 不可用时可直接
#   docker pull ghcr.io/mintao1/video2md:<tag> 手动顶上。
set -euo pipefail

TAG=""
HOST="${V2MD_DEPLOY_HOST:-}"
DIR="$HOME/stack/video2md"
DRY_RUN=0
# 2026-09-26 obs 双栈合并（平台 Phase 1.1/M1）：观测归 ~/stack/platform-obs 统一栈，
# 发版不再叠 docker-compose.obs.yml（叠了会与平台栈抢 4318/9090/9093/3000/9760 端口）；
# OTEL 出口已入主 compose environment。
COMPOSE="docker compose -f docker-compose.yml"

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

echo "── schema 迁移（幂等；json 档部署自动跳过）"
remote "cd $DIR && $COMPOSE run --rm --no-deps video2md python -m video2md migrate"

echo "── 反代面验证（P0-4：/docs 必须被网关拦下）"
if [[ $DRY_RUN -eq 0 ]]; then
  DOC_CODE=$(ssh "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost/docs")
  PROXY_HZ=$(ssh "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost/healthz")
  [[ "$DOC_CODE" == "403" ]] || { echo "✗ 反代未拦 /docs（HTTP $DOC_CODE）——查 deploy/nginx.conf 是否挂载/生效" >&2; exit 1; }
  [[ "$PROXY_HZ" == "200" ]] || { echo "✗ 反代 healthz 不通（HTTP $PROXY_HZ）" >&2; exit 1; }
  echo "✓ /docs → 403 ｜ healthz 经反代 → 200"
fi

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

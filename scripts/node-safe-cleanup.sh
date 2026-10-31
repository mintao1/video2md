#!/usr/bin/env bash
set -euo pipefail
KEEP_DAYS="${KEEP_DAYS:-7}"
ROOT_PATH="${ROOT_PATH:-/var/lib/rancher/k3s}"
case "$KEEP_DAYS" in
  ''|*[!0-9]*) echo "NODE_SAFE_CLEANUP=FAIL invalid KEEP_DAYS=$KEEP_DAYS" >&2; exit 2 ;;
esac
KEEP_HOURS=$((KEEP_DAYS * 24))
before_kib="$(df -Pk "$ROOT_PATH" | awk 'NR == 2 { print $4; exit }')"
echo "NODE_SAFE_CLEANUP=START root=$ROOT_PATH free_kib=${before_kib:-unknown} keep_days=$KEEP_DAYS"
if command -v docker >/dev/null 2>&1; then
  docker builder prune -af --filter "until=${KEEP_HOURS}h" || true
  docker image prune -af --filter "until=${KEEP_HOURS}h" || true
fi
find /tmp -maxdepth 1 -type f -name 'video2md-images-*' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
after_kib="$(df -Pk "$ROOT_PATH" | awk 'NR == 2 { print $4; exit }')"
echo "NODE_SAFE_CLEANUP=PASS root=$ROOT_PATH free_kib=${after_kib:-unknown}"

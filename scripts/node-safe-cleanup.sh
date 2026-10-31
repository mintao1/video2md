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

# K3s keeps imported product images after each deployment. Remove only stale
# product tags absent from the current Deployment and the two newest ReplicaSets.
# If the keep set cannot be resolved, skip image deletion.
NAMESPACE="${NAMESPACE:-video2md}"
PRODUCT="${PRODUCT:-video2md}"
DEPLOYMENT="${DEPLOYMENT:-video2md-api}"
KEEP_ROLLOUTS="${KEEP_ROLLOUTS:-2}"
if command -v sudo >/dev/null 2>&1 && command -v k3s >/dev/null 2>&1; then
  keep_images="$({
    sudo k3s kubectl -n "$NAMESPACE" get deploy "$DEPLOYMENT" \
      -o jsonpath='{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}'
    sudo k3s kubectl -n "$NAMESPACE" get rs -l "app.kubernetes.io/name=$PRODUCT" \
      --sort-by=.metadata.creationTimestamp \
      -o jsonpath='{range .items[*]}{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}{"\n"}{end}' \
      | tail -n "$((KEEP_ROLLOUTS * 4))"
  } 2>/dev/null | sed '/^$/d' | sort -u)"
  if [[ -n "$keep_images" ]]; then
    while IFS= read -r image; do
      [[ -n "$image" ]] || continue
      [[ "$image" == "$PRODUCT:"* || "$image" == "docker.io/library/$PRODUCT:"* ]] || continue
      if grep -Fxq "$image" <<<"$keep_images" || grep -Fxq "docker.io/library/$image" <<<"$keep_images"; then
        continue
      fi
      echo "  removing stale containerd image $image"
      sudo k3s ctr images rm "$image" >/dev/null 2>&1 || true
    done < <(sudo k3s ctr images list -q 2>/dev/null || true)
  else
    echo "NODE_SAFE_CLEANUP=SKIP containerd image pruning (keep set unavailable)"
  fi
fi
find /tmp -maxdepth 1 -type f -name 'video2md-images-*' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
after_kib="$(df -Pk "$ROOT_PATH" | awk 'NR == 2 { print $4; exit }')"
echo "NODE_SAFE_CLEANUP=PASS root=$ROOT_PATH free_kib=${after_kib:-unknown}"

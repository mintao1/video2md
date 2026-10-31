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
case "$KEEP_ROLLOUTS" in
  ''|*[!0-9]*) echo "NODE_SAFE_CLEANUP=FAIL invalid KEEP_ROLLOUTS=$KEEP_ROLLOUTS" >&2; exit 2 ;;
esac
if (( KEEP_ROLLOUTS < 1 )); then
  echo "NODE_SAFE_CLEANUP=FAIL KEEP_ROLLOUTS must be >= 1" >&2
  exit 2
fi
if command -v sudo >/dev/null 2>&1 && command -v k3s >/dev/null 2>&1; then
  keep_images=""
  keep_set_failed=0
  if deploy_images="$(sudo k3s kubectl -n "$NAMESPACE" get deploy "$DEPLOYMENT" \
      -o jsonpath='{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}' 2>/dev/null)"; then
    keep_images+=$'\n'"$deploy_images"
  else
    echo "NODE_SAFE_CLEANUP=INFO deployment image lookup unavailable; using ReplicaSets"
  fi
  if ! rs_names="$(sudo k3s kubectl -n "$NAMESPACE" get rs -l "app.kubernetes.io/name=$PRODUCT" \
      --sort-by=.metadata.creationTimestamp \
      -o name 2>/dev/null)"; then
    keep_set_failed=1
  else
    selected_rs="$(printf '%s\n' "$rs_names" | sed '/^$/d' | tail -n "$KEEP_ROLLOUTS")"
    if [[ -z "$selected_rs" ]]; then
      keep_set_failed=1
    else
      while IFS= read -r rs_name; do
        [[ -n "$rs_name" ]] || continue
        if ! rs_images="$(sudo k3s kubectl -n "$NAMESPACE" get "$rs_name" \
            -o jsonpath='{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}' 2>/dev/null)"; then
          keep_set_failed=1
          break
        fi
        keep_images+=$'\n'"$rs_images"
      done <<< "$selected_rs"
    fi
  fi
  keep_images="$(printf '%s\n' "$keep_images" | sed '/^$/d; s#^docker.io/library/##' | sort -u)"
  if (( keep_set_failed )); then
    echo "NODE_SAFE_CLEANUP=SKIP containerd image pruning (keep set unavailable)"
  elif [[ -n "$keep_images" ]]; then
    while IFS= read -r image; do
      [[ -n "$image" ]] || continue
      [[ "$image" == "$PRODUCT:"* || "$image" == "docker.io/library/$PRODUCT:"* ]] || continue
      normalized_image="${image#docker.io/library/}"
      if grep -Fxq "$normalized_image" <<<"$keep_images"; then
        continue
      fi
      echo "  removing stale containerd image $image"
      sudo k3s ctr images rm "$image" >/dev/null 2>&1 || true
    done < <(sudo k3s ctr images list -q 2>/dev/null || true)
    # Removing a tag leaves unreferenced blobs until containerd content GC.
    # This command only prunes content with no remaining image reference.
    sudo k3s ctr content prune >/dev/null 2>&1 || true
  else
    echo "NODE_SAFE_CLEANUP=SKIP containerd image pruning (keep set unavailable)"
  fi
fi
find /tmp -maxdepth 1 -type f -name "${PRODUCT}-images-*" -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
after_kib="$(df -Pk "$ROOT_PATH" | awk 'NR == 2 { print $4; exit }')"
echo "NODE_SAFE_CLEANUP=PASS root=$ROOT_PATH free_kib=${after_kib:-unknown}"

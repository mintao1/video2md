#!/usr/bin/env bash
# Pre-build/pre-apply guard for the single-node k3s deployment target.
#
# BLD-03: refuse a deployment when kubelet reports disk pressure, the
# disk-pressure taint is present, or the filesystem backing k3s ephemeral-storage
# has less than MIN_FREE_GIB free. The script is streamed over SSH by the deploy
# workflow; it does not mutate the node or apply Kubernetes manifests.
set -euo pipefail

MIN_FREE_GIB="${MIN_FREE_GIB:-20}"
# Check the filesystem that actually backs k3s rather than an unrelated mount.
ROOT_PATH="${ROOT_PATH:-/var/lib/rancher/k3s}"
NODE_NAME="${NODE_NAME:-}"

case "$MIN_FREE_GIB" in
  ''|*[!0-9]*)
    echo "DISK_PRESSURE_GUARD=FAIL invalid MIN_FREE_GIB=$MIN_FREE_GIB" >&2
    exit 2
    ;;
esac

free_kib="$(df -Pk "$ROOT_PATH" | awk 'NR == 2 { print $4; exit }')"
if [[ ! "$free_kib" =~ ^[0-9]+$ ]]; then
  echo "DISK_PRESSURE_GUARD=FAIL unable to read free space for $ROOT_PATH" >&2
  exit 2
fi

min_free_kib=$((MIN_FREE_GIB * 1024 * 1024))
if [[ -z "$NODE_NAME" ]]; then
  NODE_NAME="$(sudo k3s kubectl get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
# Linux hostnames may be reported with uppercase letters while Kubernetes
# stores DNS-style node names in lowercase (for example VM-0-7-ubuntu).
NODE_NAME="$(printf '%s' "$NODE_NAME" | tr '[:upper:]' '[:lower:]')"
if [[ -z "$NODE_NAME" ]]; then
  echo "DISK_PRESSURE_GUARD=FAIL unable to resolve the Kubernetes node name" >&2
  exit 2
fi

pressure="$(sudo k3s kubectl get node "$NODE_NAME" -o jsonpath='{range .status.conditions[?(@.type=="DiskPressure")]}{.status}{end}')"
disk_taint="$(sudo k3s kubectl get node "$NODE_NAME" -o jsonpath='{range .spec.taints[?(@.key=="node.kubernetes.io/disk-pressure")]}{.key}{end}')"

# Read kubelet's ephemeral-storage view when the stats endpoint is available.
# The filesystem check remains the fallback because older k3s builds may not
# expose the proxy endpoint to the node's local kubectl client.
stats_json="$(sudo k3s kubectl get --raw "/api/v1/nodes/${NODE_NAME}/proxy/stats/summary" 2>/dev/null || true)"
if [[ -n "$stats_json" ]] && command -v jq >/dev/null 2>&1; then
  stats_available="$(jq -r '.node.fs.availableBytes // empty' <<<"$stats_json")"
  stats_capacity="$(jq -r '.node.fs.capacityBytes // empty' <<<"$stats_json")"
  if [[ "$stats_available" =~ ^[0-9]+$ && "$stats_capacity" =~ ^[0-9]+$ && "$stats_capacity" -gt 0 ]]; then
    stats_min=$((MIN_FREE_GIB * 1024 * 1024 * 1024))
    if (( stats_available < stats_min )); then
      free_gib=$((stats_available / 1024 / 1024 / 1024))
      echo "DISK_PRESSURE_GUARD=FAIL node=$NODE_NAME free=${free_gib}Gi minimum=${MIN_FREE_GIB}Gi pressure=${pressure:-unknown} taint=${disk_taint:-none} source=kubelet-ephemeral-storage" >&2
      exit 42
    fi
  fi
fi

free_gib=$((free_kib / 1024 / 1024))
if [[ "$pressure" != "False" || -n "$disk_taint" || "$free_kib" -lt "$min_free_kib" ]]; then
  echo "DISK_PRESSURE_GUARD=FAIL node=$NODE_NAME ephemeral-storage-free=${free_gib}Gi minimum=${MIN_FREE_GIB}Gi pressure=${pressure:-unknown} taint=${disk_taint:-none}" >&2
  exit 42
fi

echo "DISK_PRESSURE_GUARD=PASS node=$NODE_NAME ephemeral-storage-free=${free_gib}Gi minimum=${MIN_FREE_GIB}Gi pressure=${pressure:-unknown} taint=${disk_taint:-none}"

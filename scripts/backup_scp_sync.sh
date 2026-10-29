#!/usr/bin/env bash
# Sequential off-node backup transport for hosts whose SFTP server resets
# rclone's multiplexed connections. The source is already age-encrypted.
set -euo pipefail

SRC_DIR="${1:?source directory required}"
TARGET="${2:?scp target required (host:/absolute/path)}"

[[ -d "$SRC_DIR" ]] || { echo "source directory missing: $SRC_DIR" >&2; exit 2; }
[[ "$TARGET" =~ ^[A-Za-z0-9_.@-]+:/[^[:space:]]+$ ]] || {
  echo "invalid scp target" >&2
  exit 2
}

REMOTE_HOST="${TARGET%%:*}"
REMOTE_DIR="${TARGET#*:}"
SCP_OPTS=(-q -o BatchMode=yes -o StrictHostKeyChecking=yes
          -o ServerAliveInterval=15 -o ServerAliveCountMax=10)
SSH_OPTS=(-q -o BatchMode=yes -o StrictHostKeyChecking=yes
          -o ServerAliveInterval=15 -o ServerAliveCountMax=10)

ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "mkdir -p -- '$REMOTE_DIR' && chmod 700 -- '$REMOTE_DIR'"

while IFS= read -r -d '' file; do
  rel="${file#"$SRC_DIR"/}"
  remote_dir="$REMOTE_DIR/${rel%/*}"
  [[ "$rel" == */* ]] || remote_dir="$REMOTE_DIR"
  remote_file="$REMOTE_DIR/$rel"
  ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "mkdir -p -- '$remote_dir'"
  scp "${SCP_OPTS[@]}" "$file" "$REMOTE_HOST:$remote_file.tmp"
  ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "mv -f -- '$remote_file.tmp' '$remote_file'"
done < <(find "$SRC_DIR" -type f ! -path "$SRC_DIR/.lock/*" -print0 | sort -z)

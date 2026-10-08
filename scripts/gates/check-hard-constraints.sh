#!/usr/bin/env bash
# enforces: HC-01 HC-02 HC-03
# HARD 约束静态检查。无白名单。
#
# 用法：
#   check-hard-constraints.sh [路径...]          # 默认扫 templates .github/workflows scripts
#   check-hard-constraints.sh --report [路径...] # 违规照报，退出 0（欠账可见期专用）
#   check-hard-constraints.sh --report --until YYYY-MM-DD [路径...]
#       # 过了 until 当天，--report 拒绝生效，转红（GATE-03 到期自动转红）
#
# 两层检查（HC-02，见 SPEC §15）：
#   1. 管道扫描：workflow/脚本命中外部镜像仓库模式即红
#   2. manifest 扫描：yaml 的 image: 不得带 registry 主机前缀、须带具体 tag
#      （禁 latest/无 tag），imagePullPolicy 不得为 Always
# HC-01：服务器侧 apply -k / kustomize edit。HC-03：依赖服务器手工副本。
set -euo pipefail

MODE=block
UNTIL=""
ROOTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report) MODE=report; shift ;;
    --until) UNTIL="${2:?--until 需要 YYYY-MM-DD}"; shift 2 ;;
    *) ROOTS+=("$1"); shift ;;
  esac
done

if [[ "$MODE" == report && -n "$UNTIL" ]]; then
  TODAY=$(date +%F)
  if [[ "$TODAY" > "$UNTIL" ]]; then
    echo "✗ --report 已到期（until=${UNTIL}，今天=${TODAY}）：报告模式失效，按 HARD 处理" >&2
    exit 1
  fi
fi

[[ ${#ROOTS[@]} -eq 0 ]] && ROOTS=(templates .github/workflows scripts)

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$REPO_ROOT"

VIOLATIONS=()

# 剥掉注释再匹配：模板头注里「不要 apply -k」这类警示语不算违规
strip_comments() {
  sed -E 's/^[[:space:]]*#.*$//; s/[[:space:]]+#.*$//' "$1"
}

# ── 1. 管道扫描（HC-01 / HC-02 / HC-03）──────────────────────────────
collect_files() {
  local root
  for root in "$@"; do
    [[ -e "$root" ]] || continue
    find "$root" -type f \( -name '*.sh' -o -name '*.yml' -o -name '*.yaml' \) 2>/dev/null
  done
}

while IFS= read -r f; do
  # 执法者自身与其夹具不参与扫描（模式串只活在这些文件里）
  case "$f" in
    scripts/gates/*|scripts/_archive/*|agent-hooks/*|rules/fixtures/*) continue ;;
  esac
  content=$(strip_comments "$f")
  if grep -qE 'apply[[:space:]]+-k\b|kustomize[[:space:]]+edit' <<<"$content"; then
    VIOLATIONS+=("HC-01 ${f}: 命中服务器侧 apply -k / kustomize edit（渲染必须在 runner/本地完成）")
  fi
  if grep -qE '~/stack/templates|~/templates\b' <<<"$content"; then
    VIOLATIONS+=("HC-03 ${f}: 依赖服务器上的手工副本路径")
  fi
  if grep -qE 'ghcr\.io|docker/login-action|build-push-action|docker push|buildx[^#]*--push|packages:[[:space:]]*write|REGISTRY:' <<<"$content"; then
    VIOLATIONS+=("HC-02 ${f}: 管道命中外部镜像仓库模式（镜像唯一来源=节点构建+ctr import）")
  fi
done < <(collect_files "${ROOTS[@]}")

# ── 2. manifest 扫描（HC-02 语义层）─────────────────────────────────
# 对 yaml 里的 image: 引用与 imagePullPolicy 做语义检查；helm 模板占位（{{...}}）跳过。
manifest_files() {
  find templates charts -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null
  # 产品仓布局：deploy/k8s/**（平台仓与产品仓都按相对路径找）
  find . -maxdepth 5 -path '*/deploy/k8s/*' -type f -name '*.yaml' 2>/dev/null
}

while IFS= read -r f; do
  case "$f" in
    rules/fixtures/*) continue ;;
  esac
  while IFS= read -r line; do
    val=$(sed -nE 's/^[[:space:]]*image:[[:space:]]*["'\'']?([^"'\'']+)["'\'']?.*/\1/p' <<<"$line")
    [[ -z "$val" ]] && continue
    [[ "$val" == *"{{"* ]] && continue
    name="${val%%@*}"          # 去 digest
    name="${name%%:*}"         # 去 tag（首段 host:port 会在下面单独判）
    # 带 tag 判定用原始 val（含端口的 host:port:tag 情形罕见，本平台不出现）
    if [[ "$val" != *:* || "$val" == *":latest" ]]; then
      VIOLATIONS+=("HC-02 ${f}: image 缺具体 tag 或为 latest（${val}）")
    fi
    if [[ "$name" == */* ]]; then
      first="${name%%/*}"
      if [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]; then
        VIOLATIONS+=("HC-02 ${f}: image 带 registry 主机前缀（${val}）")
      fi
    fi
  done < <(grep -E '^[[:space:]]*image:' "$f" || true)
  if grep -qE '^[[:space:]]*imagePullPolicy:[[:space:]]*Always' "$f" 2>/dev/null; then
    VIOLATIONS+=("HC-02 ${f}: imagePullPolicy 为 Always（禁；latest/无 tag 的隐式默认也是 Always）")
  fi
done < <(manifest_files)

# ── 输出 ────────────────────────────────────────────────────────────
if [[ ${#VIOLATIONS[@]} -eq 0 ]]; then
  echo "✓ check-hard-constraints: 无违规（模式: ${MODE}; 扫描: ${ROOTS[*]}）"
  exit 0
fi

echo "✗ check-hard-constraints 发现 ${#VIOLATIONS[@]} 条违规："
for v in "${VIOLATIONS[@]}"; do echo "  - $v"; done

if [[ "$MODE" == report ]]; then
  echo "（--report 模式：仅报告，不阻断${UNTIL:+；到期日 ${UNTIL}}）"
  exit 0
fi
exit 1

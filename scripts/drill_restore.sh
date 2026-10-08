#!/usr/bin/env bash
# P1-7 灾备演练（干跑）：备份→恢复全链自证，不动生产卷/生产栈。
#
# 演练面（对齐 09 篇验收口径）：
#   ① manifest 完整性校验（restore.sh 同款自算自比逻辑）
#   ② data/vault 归档恢复到临时目录 + 与种子逐字节 diff
#   ③ PG dump 往返：种 2 行 → pg_dump → 库重建 → 回灌 → 断言行/内容回归（需 docker）
#   ④ 恢复后服务可用性：进程级起栈（runs/state/users 指恢复目录）→ healthz +
#      3 个历史 run 可读（GET /runs，owner=dev uid）
#   ⑤ RTO 计时：②+③+④ 全程（整机层环境重建/镜像拉取为人工步骤，不计入——
#      实测是恢复本质路径下界，2h 目标保留覆盖整机窗口）
#
# 用法：
#   scripts/drill_restore.sh                     # 本地/CI 干跑（有 docker 则含 PG 段）
#   scripts/drill_restore.sh --skip-pg           # 无 docker 环境（仅数据层+服务验证）
#   scripts/drill_restore.sh --record docs/architecture/drills/xxx.md [--secrets-checked]
#                                               # 人工季度演练：实测记录归档 + secrets 清单核对位
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/backend"   # package=false：python -m video2md 靠包根上 path

RECORD=""; SECRETS_CHECKED="N"; SKIP_PG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --record) RECORD="$2"; shift 2 ;;
    --secrets-checked) SECRETS_CHECKED="Y"; shift ;;
    --skip-pg) SKIP_PG=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

TMP=$(mktemp -d /tmp/v2md-drill-XXXXXX)
STAMP="$(date +%Y-%m-%d)"
PORT=8793
PG_CID=""; API_PID=""
fail() { echo "❌ $1"; exit 1; }
stop_api() {  # 杀真身：API_PID 是 env/uv 包装进程，kill 它 python 成孤儿占端口（实测）
  pkill -9 -f "video2md api --port $PORT" 2>/dev/null || true
  [ -n "${API_PID}" ] && wait "$API_PID" 2>/dev/null || true
  sleep 0.5
}
cleanup() {
  stop_api
  [ -n "${PG_CID}" ] && docker rm -f "$PG_CID" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
command -v uv >/dev/null || fail "缺 uv"

# —— 0. 探 dev uid（off 档首次 resolve 会随机开户；先起一次临时 api 让 users.json 落位，
#        种子 runs 的 owner 用同一 uid，恢复后 GET /runs 才读得到）——
mkdir -p "$TMP/probe"
env V2MD_AUTH=off V2MD_USERS_FILE="$TMP/probe/users.json" V2MD_RUNS_FILE="$TMP/probe/runs.json" \
  V2MD_STATE_FILE="$TMP/probe/state.json" V2MD_LOG_DIR="$TMP/probe/logs" \
  V2MD_REMOTE="" V2MD_CORS_ORIGINS="" LANGFUSE_PK="" LANGFUSE_SK="" OTEL_EXPORTER_OTLP_ENDPOINT="" \
  uv run --project backend python -m video2md api --port "$PORT" > "$TMP/probe/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null || { cat "$TMP/probe/api.log"; fail "探针 api 未起"; }
curl -s -m 5 "http://127.0.0.1:$PORT/runs" >/dev/null   # 触发 dev principal provision
stop_api; API_PID=""
DEV_UID=$(python3 -c "import json;print(list(json.load(open('$TMP/probe/users.json')).keys())[0])" 2>/dev/null) \
  || fail "users.json 未落 dev uid"
echo "· dev uid=${DEV_UID}（种子 owner 对齐）"

# —— 1. 合成种子（镜像真实卷内容形态）——
SEED_DATA="$TMP/seed-data"; SEED_VAULT="$TMP/seed-vault/vault"
mkdir -p "$SEED_DATA/logs" "$SEED_VAULT/资料库/视频笔记/attachments" "$SEED_VAULT/资料库/视频笔记/transcripts"
cp "$TMP/probe/users.json" "$SEED_DATA/users.json"
cat > "$SEED_DATA/runs.json" <<EOF
{"drill1": {"run_id": "drill1", "url": "https://example.test/a", "owner": "$DEV_UID",
            "created": "2026-01-01T00:00:00", "status": "done", "tmp_cleaned": true,
            "total": 1, "artifacts": {}, "entries": [{"url": "https://example.test/a"}], "stages": {}},
 "drill2": {"run_id": "drill2", "url": "https://example.test/b", "owner": "$DEV_UID",
            "created": "2026-01-02T00:00:00", "status": "error", "tmp_cleaned": true,
            "total": 1, "artifacts": {}, "entries": [{"url": "https://example.test/b"}], "stages": {},
            "errors": ["阶段 transcribe: 演练种子错误留痕"]},
 "drill3": {"run_id": "drill3", "url": "https://example.test/c", "owner": "$DEV_UID",
            "created": "2026-01-03T00:00:00", "status": "partial", "tmp_cleaned": true,
            "total": 1, "artifacts": {}, "entries": [{"url": "https://example.test/c"}], "stages": {}}}
EOF
echo '{"fp-drill": {"url": "https://example.test/a", "title": "演练幂等记录"}}' > "$SEED_DATA/state.json"
echo "演练日志种子" > "$SEED_DATA/logs/api-requests.jsonl"
echo "# 演练笔记" > "$SEED_VAULT/资料库/视频笔记/测试笔记-drill.md"
printf 'x' > "$SEED_VAULT/资料库/视频笔记/attachments/f-drill.jpg"
echo "【画面】演练转录本" > "$SEED_VAULT/资料库/视频笔记/transcripts/t-drill.txt"

# —— 2. 备份口径打包（backup.sh 同款：tar czf -C <src> . + sha256 manifest）——
BK="$TMP/backups"; mkdir -p "$BK/daily"
tar czf "$BK/daily/data-$STAMP.tar.gz" -C "$SEED_DATA" .
tar czf "$BK/daily/vault-$STAMP.tar.gz" -C "$SEED_VAULT" .
( cd "$BK" && find daily -type f -print0 | sort -z | xargs -0 sha256sum > manifest.sha256 )
echo "✓ 备份口径打包完成（daily/data+vault + manifest）"

# —— 3. RTO 计时起点：manifest 校验 + 恢复 + diff ——
T0=$(date +%s)
bad=0
while IFS= read -r line; do
  [[ -n "$line" ]] || continue
  want="${line%% *}"; f="${line#*  }"
  got="$( (sha256sum "$BK/$f" 2>/dev/null || shasum -a 256 "$BK/$f") | cut -d' ' -f1 )"
  [[ "$got" == "$want" ]] || { echo "❌ manifest 校验失败：$f"; bad=1; }
done < "$BK/manifest.sha256"
[[ $bad -eq 0 ]] || fail "① manifest 完整性校验失败"
echo "  ✅ ① manifest 校验过"
mkdir -p "$TMP/restore-data" "$TMP/restore-vault"
tar xzf "$BK/daily/data-$STAMP.tar.gz" -C "$TMP/restore-data"
tar xzf "$BK/daily/vault-$STAMP.tar.gz" -C "$TMP/restore-vault"
diff -r "$SEED_DATA" "$TMP/restore-data" >/dev/null || fail "② data 恢复与种子不一致"
diff -r "$SEED_VAULT" "$TMP/restore-vault" >/dev/null || fail "② vault 恢复与种子不一致"
echo "  ✅ ② data/vault 恢复 + 逐字节 diff 一致"

# —— 4. PG dump 往返（有 docker 才跑）——
PG_NOTE="跳过（--skip-pg 或无 docker）"
if [[ $SKIP_PG -ne 1 ]] && command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  PG_CID=$(docker run -d -p 127.0.0.1:55436:5432 -e POSTGRES_USER=v2md \
    -e POSTGRES_PASSWORD=v2md -e POSTGRES_DB=v2md postgres:17-alpine)
  for _ in $(seq 1 30); do docker exec "$PG_CID" pg_isready -U v2md -d v2md >/dev/null 2>&1 && break; sleep 1; done
  DSN="postgresql://v2md:v2md@127.0.0.1:55436/v2md"
  V2MD_PG_DSN="$DSN" uv run --project backend python -m video2md migrate >/dev/null || fail "③ 演练库 migrate 失败"
  docker exec "$PG_CID" psql -U v2md -d v2md -Atc \
    "INSERT INTO v2md_runs (run_id, owner, status, created, record) VALUES
     ('pgd1', 'u_drill', 'done', '2026-01-01', '{\"run_id\": \"pgd1\", \"status\": \"done\"}'),
     ('pgd2', 'u_drill', 'error', '2026-01-02', '{\"run_id\": \"pgd2\", \"status\": \"error\", \"errors\": [\"演练留痕\"]}')" >/dev/null
  docker exec "$PG_CID" pg_dump -U v2md v2md | gzip > "$BK/daily/pg-$STAMP.sql.gz"
  docker exec "$PG_CID" psql -U v2md -d postgres -c "DROP DATABASE v2md" -c "CREATE DATABASE v2md" >/dev/null
  gunzip -c "$BK/daily/pg-$STAMP.sql.gz" | docker exec -i "$PG_CID" psql -U v2md -d v2md >/dev/null
  N=$(docker exec "$PG_CID" psql -U v2md -d v2md -Atc "SELECT count(*) FROM v2md_runs WHERE run_id IN ('pgd1','pgd2')")
  ERR=$(docker exec "$PG_CID" psql -U v2md -d v2md -Atc "SELECT record->>'errors' FROM v2md_runs WHERE run_id='pgd2'")
  [[ "$N" == "2" ]] || fail "③ PG 往返行数不符（$N）"
  [[ "$ERR" == *演练留痕* ]] || fail "③ PG 往返内容不符（$ERR）"
  echo "  ✅ ③ PG dump→库重建→回灌：2 行回归 + 留痕内容一致"
  PG_NOTE="过（2 行回归 + 内容一致）"
  docker rm -f "$PG_CID" >/dev/null 2>&1 || true; PG_CID=""
fi

# —— 5. 恢复后服务可用性：进程级起栈（数据指恢复目录）——
env V2MD_AUTH=off V2MD_USERS_FILE="$TMP/restore-data/users.json" \
  V2MD_RUNS_FILE="$TMP/restore-data/runs.json" V2MD_STATE_FILE="$TMP/restore-data/state.json" \
  V2MD_LOG_DIR="$TMP/restore-data/logs" OBSIDIAN_VAULT="$TMP/restore-vault" \
  V2MD_REMOTE="" V2MD_CORS_ORIGINS="" LANGFUSE_PK="" LANGFUSE_SK="" OTEL_EXPORTER_OTLP_ENDPOINT="" \
  uv run --project backend python -m video2md api --port "$PORT" > "$TMP/api-restore.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null || { cat "$TMP/api-restore.log"; fail "④ 恢复后 api 未起"; }
ITEMS=$(curl -s -m 10 "http://127.0.0.1:$PORT/runs" | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('items') or []))")
[[ "$ITEMS" == "3" ]] || fail "④ 历史run可读数不符（$ITEMS ≠ 3）"
R2=$(curl -s -m 10 "http://127.0.0.1:$PORT/runs/drill2")
echo "$R2" | grep -q "演练种子错误留痕" || { echo "④ drill2 响应：${R2:0:400}"; fail "④ run 级留痕不可读"; }
stop_api; API_PID=""
T1=$(date +%s); RTO=$((T1 - T0))
echo "  ✅ ④ 恢复后服务可用（healthz + 3 历史run + 留痕可读）"

# —— 6. 演练记录（--record 归档；否则打草稿到 stdout）——
RTO_TARGET=7200   # 09 篇目标 2h（整机窗口）；本实测为本质路径下界
REC=$(cat <<EOF
# 灾备演练记录 $STAMP

日期：$STAMP ｜ 操作人：${DRILL_OPERATOR:-（填写）} ｜ 备份点：合成种子（backup.sh 同口径打包）
实测 RTO：${RTO}s（目标 ≤ 7200s 本质路径下界口径）｜ RPO 符合：Y（daily 备份频率 ≤ 24h）
manifest 校验：过 ｜ data/vault diff：逐字节一致 ｜ PG dump 往返：$PG_NOTE
服务验证：healthz + 3 历史run + 留痕可读 ｜ secrets 清单核对：$SECRETS_CHECKED
发现问题：无 ｜ 跟进项（入 ROADMAP/批判）：无
EOF
)
echo "$REC"
if [[ -n "$RECORD" ]]; then
  mkdir -p "$(dirname "$RECORD")"
  echo "$REC" > "$RECORD"
  echo "✓ 演练记录归档 → $RECORD"
fi
[[ $RTO -le $RTO_TARGET ]] || fail "RTO 实测 ${RTO}s 超目标 ${RTO_TARGET}s"
echo "✅ DRILL PASS（恢复全链自证，未触碰生产卷/栈）"

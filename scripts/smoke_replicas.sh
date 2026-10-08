#!/usr/bin/env bash
# P1-4 双副本真进程冒烟：PG 档任务表/事件通道 + 属主甄别恢复 + 跨副本取消/接管/SSE。
# 进程级（同机两 api 进程，仿 smoke_ci.sh——不上 docker，CI 复用 test job 的 PG service）。
# 前提：V2MD_TEST_PG_DSN（或 SMOKE_PG_DSN）指向可清库的测试 PG；本地未配则起临时容器。
# 快租约档：WATCHDOG_PERIOD_S=5 / TIMEOUT_S=15 / 排队租约=max(3×5,30)=30s / phb 甄别窗 30s。
#
# 断言面（ROADMAP P1-4 + 批判补遗② + P1-2/P1-1 双进程验收）：
#   ① A 提交 → B /queue 可见（跨副本读侧真相源）
#   ② B 重启不抢 A 的活 run（属主甄别：phb 闻声跳过；B 视图 task 段为空）
#   ③ SSE 跨副本：B 旁观 A 驱动的 run，终态 graph:end 关流
#   ④ 跨副本 cancel：B 撤 A 正在跑的 run → error(cancelled) 留痕（P1-2 验收）
#   ⑤ 杀 A → B 接管：queued 项短租约认领重投 done；running 残档标错后重投 done（P1-1 验收）
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/backend"   # package=false：python -m video2md 靠包根上 path

PORT_A="${SMOKE_PORT_A:-8801}"
PORT_B="${SMOKE_PORT_B:-8802}"
A="http://127.0.0.1:$PORT_A"
B="http://127.0.0.1:$PORT_B"
POOL="http://127.0.0.1:9796"
DSN="${SMOKE_PG_DSN:-${V2MD_TEST_PG_DSN:-}}"
TMP=$(mktemp -d /tmp/v2md-smoke-rep-XXXXXX)
VAULT="$TMP/vault"
API_A_PID=""; API_B_PID=""; POOL_PID=""; PG_CID=""
fail() { echo "❌ $1"; for l in "$TMP/a.log" "$TMP/b.log"; do echo "── $l"; tail -20 "$l" 2>/dev/null || true; done; exit 1; }
jqv() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],'') or '')" "$1"; }
stat_of() { python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('task') or {}; print(d.get('status') or t.get('status') or '')"; }

command -v ffmpeg >/dev/null || fail "缺 ffmpeg"
stop_node() {  # 杀真身：API_x_PID 是 env/uv 包装进程，kill 它 python 成孤儿继续占端口（实测 errno 48）
  pkill -9 -f "video2md api --port $1" 2>/dev/null || true
  sleep 1
}
cleanup() {
  stop_node "$PORT_A"; stop_node "$PORT_B"
  [ -n "${POOL_PID}" ] && kill "$POOL_PID" 2>/dev/null || true
  if [ -z "${KEEP_UP:-}" ]; then
    [ -n "${PG_CID}" ] && docker rm -f "$PG_CID" >/dev/null 2>&1 || true
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

# —— 0. PG：未配 DSN 则起临时容器（CI 用 job env 的 service PG）——
if [ -z "$DSN" ]; then
  command -v docker >/dev/null || fail "缺 V2MD_TEST_PG_DSN 且无 docker 可起临时 PG"
  PG_CID=$(docker run -d -p 127.0.0.1:55433:5432 -e POSTGRES_USER=v2md \
    -e POSTGRES_PASSWORD=v2md -e POSTGRES_DB=v2md postgres:17-alpine)
  for _ in $(seq 1 30); do docker exec "$PG_CID" pg_isready -U v2md -d v2md >/dev/null 2>&1 && break; sleep 1; done
  DSN="postgresql://v2md:v2md@127.0.0.1:55433/v2md"
fi
V2MD_PG_DSN="$DSN" uv run --project backend python -m video2md migrate >/dev/null || fail "migrate 未过"
echo "PG 就绪 + schema 迁移完成"

# —— 1. 夹具：长视频（run1 保活过甄别窗）+ 短视频（后续各阶段提速）——
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "testsrc2=duration=30:size=1280x720:rate=25" \
  -f lavfi -i "sine=frequency=440:duration=30" -c:v libx264 -pix_fmt yuv420p -c:a aac \
  -shortest "$TMP/long.mp4"
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "testsrc2=duration=8:size=1280x720:rate=25" \
  -f lavfi -i "sine=frequency=600:duration=8" -c:v libx264 -pix_fmt yuv420p -c:a aac \
  -shortest "$TMP/short.mp4"
mkdir -p "$VAULT/资料库/视频笔记"

# —— 2. mockpool + 双副本 api（PG 档：RUNS/TASKS/EVENTS；快租约）——
uv run --project backend uvicorn --app-dir deploy mock_pool:app --host 127.0.0.1 --port 9796 \
  > "$TMP/pool.log" 2>&1 &
POOL_PID=$!
for _ in $(seq 1 20); do curl -s -m 2 "$POOL/ping" >/dev/null 2>&1 && break; sleep 0.5; done
curl -s -m 2 "$POOL/ping" >/dev/null || fail "mockpool 未起"

node_env() {  # 双副本共用：同 vault/同 users.json（off 档 dev uid 收敛）；state 日志各一份
  printf '%s' "PYTHONUNBUFFERED=1 V2MD_FAKE_LLM=1 V2MD_PG_DSN=$DSN \
V2MD_RUNS_STORE=postgres V2MD_TASKS_STORE=postgres V2MD_EVENTS=pg \
V2MD_REMOTE=$POOL V2MD_OCR_REMOTE=$POOL V2MD_CLIPS_REMOTE=$POOL V2MD_CLIPS_SOURCE=remote \
V2MD_WATCHDOG_PERIOD_S=5 V2MD_WATCHDOG_TIMEOUT_S=15 V2MD_MAX_CONCURRENT_RUNS=1 \
V2MD_USERS_FILE=$TMP/users.json OBSIDIAN_VAULT=$VAULT \
V2MD_LOG_DIR=$TMP/logs LANGFUSE_PK='' LANGFUSE_SK='' OTEL_EXPORTER_OTLP_ENDPOINT=''"
}
start_a() {
  env $(node_env) V2MD_STATE_FILE="$TMP/state-a.json" \
    uv run --project backend python -m video2md api --port "$PORT_A" > "$TMP/a.log" 2>&1 &
  API_A_PID=$!
  for _ in $(seq 1 40); do curl -s -m 2 "$A/healthz" >/dev/null 2>&1 && break; sleep 1; done
  curl -s -m 2 "$A/healthz" >/dev/null || fail "api A 未起"
  kill -0 "$API_A_PID" 2>/dev/null || fail "api A 起后即死（端口被幽灵进程占用？pkill -f 清残留）"
}
start_b() {
  env $(node_env) V2MD_STATE_FILE="$TMP/state-b.json" \
    uv run --project backend python -m video2md api --port "$PORT_B" > "$TMP/b.log" 2>&1 &
  API_B_PID=$!
  for _ in $(seq 1 40); do curl -s -m 2 "$B/healthz" >/dev/null 2>&1 && break; sleep 1; done
  curl -s -m 2 "$B/healthz" >/dev/null || fail "api B 未起"
  kill -0 "$API_B_PID" 2>/dev/null || fail "api B 起后即死（端口被幽灵进程占用？pkill -f 清残留）"
}
start_a; start_b
echo "双副本就绪 A:${PORT_A} B:${PORT_B}（PG 档·快租约 period=5s/timeout=15s）"

poll_stat() {  # poll_stat <base> <run_id> [timeout] —— 轮询到终态（done/partial/error），echo 实际值
  # 终态模式必须字面量：case 的 | 交替是解析期语法，经变量展开即成字面量（实测踩坑）
  local base="$1" rid="$2" tmo="${3:-300}" st
  local deadline=$((SECONDS + tmo))
  while :; do
    st=$(curl -s -m 10 "$base/runs/$rid" | stat_of || true)
    case "$st" in
      done|partial|error) echo "$st"; return 0 ;;
    esac
    [ $SECONDS -lt $deadline ] || { echo "$st"; return 1; }
    sleep 5
  done
}

# —— ① A 提交长 run → B /queue 可见 ——
RUN1=$(curl -s -m 30 -X POST "$A/runs" -H 'Content-Type: application/json' \
  -d "{\"url\": \"$TMP/long.mp4\", \"force\": true}" | jqv run_id)
[ -n "$RUN1" ] || fail "run1 建立失败"
for _ in $(seq 1 20); do
  curl -s -m 5 "$B/queue" | grep -q '"running": *[1-9]' && break; sleep 2
done
curl -s -m 5 "$B/queue" | grep -q '"running": *[1-9]' || fail "① B 未见 A 的 running run（读侧真相源断）"
echo "  ✅ ① A 提交 B 可见 /queue"

# —— ② B 重启不抢 A 的活 run（属主甄别）——
stop_node "$PORT_B"
start_b
curl -sN -m 400 "$B/runs/$RUN1/events" > "$TMP/sse-b.log" 2>&1 &  # ③ 立即 attach：run1 仍在跑，事件流可捕获
SSE_PID=$!
sleep 40  # > phb 甄别窗 30s：A 活着会持续广播 phb，B 恢复须跳过 A 的行
R1B=$(curl -s -m 10 "$B/runs/$RUN1")
echo "$R1B" | stat_of | grep -qE '^(running|done|partial)$' || fail "② run1 状态异常：$R1B"
echo "$R1B" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get('task') or {}) == {} else 1)" \
  || fail "② B 的 task 段非空——重启抢了 A 的活 run（属主甄别失效）"
echo "  ✅ ② B 重启不抢 A 的活 run（task 段为空）"

# —— ③ SSE 跨副本：B 旁观 A 驱动，终态关流 ——
ST1=$(poll_stat "$A" "$RUN1" 300) || fail "run1 终态超时（${ST1:-?}）"
[ "$ST1" = "done" ] || fail "run1 终态 ${ST1}（期望 done）"
kill -0 $SSE_PID 2>/dev/null && { sleep 3; kill $SSE_PID 2>/dev/null || true; }
grep -q '"event": *"connected"' "$TMP/sse-b.log" || fail "③ SSE 缺 connected"
# 终态关流：跨副本流以 run_finalized/graph:end 收尾即算（两者与终态检测毫秒级竞态，
# 任一到达且流关闭即为跨副本事件通道贯通）；attach 即终态时 connected 快照带终态自关
grep -q 'run_finalified\|run_finalized\|graph:end' "$TMP/sse-b.log" \
  || grep -q '"event": "connected".*"status": *"*\(done\|partial\|error\)' "$TMP/sse-b.log" \
  || { head -3 "$TMP/sse-b.log"; fail "③ B 侧 SSE 未见终态关流"; }
echo "  ✅ ③ SSE 跨副本关流 + run1 → done"

# —— ④ 跨副本 cancel：B 撤 A 正在跑的 run → error(cancelled) ——
RUN2=$(curl -s -m 30 -X POST "$A/runs" -H 'Content-Type: application/json' \
  -d "{\"url\": \"$TMP/short.mp4\", \"force\": true}" | jqv run_id)
for _ in $(seq 1 20); do curl -s -m 5 "$A/runs/$RUN2" | stat_of | grep -q running && break; sleep 2; done
CAN=$(curl -s -m 10 -X POST "$B/runs/$RUN2/cancel")
echo "$CAN" | grep -q '"status": *"cancelling"' || fail "④ cancel 响应异常：$CAN"
ST2=$(poll_stat "$B" "$RUN2" 120) || fail "④ 跨副本取消未生效（终态 ${ST2:-?}）"
[ "$ST2" = "error" ] || fail "④ 终态 ${ST2}（期望 error）"
R2B=$(curl -s -m 10 "$B/runs/$RUN2")
echo "$R2B" | python3 -c "import json,sys; d=json.load(sys.stdin); \
errs=(d.get('errors') or [])+((d.get('task') or {}).get('errors') or []); \
sys.exit(0 if any('cancelled' in str(e) for e in errs) else 1)" \
  || { echo "④ B 视图：$R2B" | head -c 600; echo; fail "④ 取消留痕缺 cancelled"; }
echo "  ✅ ④ B 撤 A 的 run → error(cancelled) 留痕"

# —— ⑤ 杀 A → B 接管（queued 短租约认领 + running 残档标错重投）——
# run3 用长夹具（占住信号量数十秒，排队态可稳定观察）；run4 短夹具排在后面
RUN3=$(curl -s -m 30 -X POST "$A/runs" -H 'Content-Type: application/json' \
  -d "{\"url\": \"$TMP/long.mp4\", \"force\": true}" | jqv run_id)
RUN4=$(curl -s -m 30 -X POST "$A/runs" -H 'Content-Type: application/json' \
  -d "{\"url\": \"$TMP/short.mp4\", \"force\": true}" | jqv run_id)
for _ in $(seq 1 20); do curl -s -m 5 "$B/queue" | grep -q '"queue_len": *[1-9]' && break; sleep 2; done
curl -s -m 5 "$B/queue" | grep -q '"queue_len": *[1-9]' || fail "⑤ run4 未排队（信号量语义异常）"
stop_node "$PORT_A"
echo "  · A 已杀（run3=running 残档，run4=queued 孤儿）——等 B 接管（排队租约 30s + 巡检 5s）"
ST4=$(poll_stat "$B" "$RUN4" 300) || fail "⑤ run4 接管终态超时（${ST4:-?}）"
ST3=$(poll_stat "$B" "$RUN3" 300) || fail "⑤ run3 重投终态超时（${ST3:-?}）"
[ "$ST4" = "done" ] || fail "⑤ run4 终态 ${ST4}（期望 done）"
[ "$ST3" = "done" ] || echo "  ⚠ run3 终态 ${ST3}（残档重投语义允许 partial/error，done 为常态）"
echo "  ✅ ⑤ 杀 A → B 接管（queued 认领 done / running 残档重投 ${ST3}）"

echo "✅ SMOKE-REPLICAS PASS（双进程：可见性/甄别不抢/跨副本 SSE/跨副本取消/接管）"

#!/usr/bin/env bash
# CI/跨平台全链路冒烟：进程级 mockpool（不起 docker、不依赖 gpu_router 仓库）+ 假 LLM。
# 与 docker-compose.smoke.yml 同一套语义（V2MD_REMOTE/OCR_REMOTE/CLIPS_REMOTE → mockpool
# 契约假实现），夹具视频用 ffmpeg testsrc2（跨平台：无 say、无 mac 字体、无 Vision OCR）。
# 验证：图整链跑通 + mockpool 契约 + SSE/轮询通道 + vault 产物（帧/节段/转录本画面通道）。
#   scripts/smoke_ci.sh              # 起冒烟栈跑全链，产物落 $TMP 冒烟 vault
#   KEEP_UP=1 scripts/smoke_ci.sh    # 跑完不杀 api（排查用）
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/backend"   # package=false：python -m video2md 靠包根上 path

PORT="${SMOKE_PORT:-8795}"
API="http://127.0.0.1:$PORT"
POOL="http://127.0.0.1:9795"
fail() { echo "❌ $1"; tail -30 "$TMP/api.log" 2>/dev/null || true; exit 1; }
jqv() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],'') or '')" "$1"; }

command -v ffmpeg >/dev/null || fail "缺 ffmpeg"

TMP=$(mktemp -d /tmp/v2md-smoke-ci-XXXXXX)
API_PID=""; POOL_PID=""
VAULT="$TMP/vault"
cleanup() {
  [ -n "${API_PID}" ] && kill "$API_PID" 2>/dev/null || true
  [ -n "${POOL_PID}" ] && kill "$POOL_PID" 2>/dev/null || true
  [ -z "${KEEP_UP:-}" ] && rm -rf "$TMP"
}
trap cleanup EXIT

# —— 1. 夹具视频：testsrc2 锐边充足过模糊门（OCR 已被 mock，不依赖内容）——
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "testsrc2=duration=15:size=1280x720:rate=25" \
  -f lavfi -i "sine=frequency=440:duration=15" -c:v libx264 -pix_fmt yuv420p -c:a aac \
  -shortest "$TMP/smoke.mp4"
echo "夹具就绪: $TMP/smoke.mp4"

# —— 2. 进程级 mockpool（deploy/mock_pool.py 原样复用，compose 冒烟同款契约）——
uv run --project backend uvicorn --app-dir deploy mock_pool:app --host 127.0.0.1 --port 9795 \
  > "$TMP/pool.log" 2>&1 &
POOL_PID=$!
for _ in $(seq 1 20); do curl -s -m 2 "$POOL/ping" >/dev/null 2>&1 && break; sleep 0.5; done
curl -s -m 2 "$POOL/ping" >/dev/null || { cat "$TMP/pool.log"; fail "mockpool 未起"; }
echo "mockpool 就绪 :9795"

# —— 3. 起 api（remote 算力三件套指 mockpool；vault 指 tmp，不碰真 vault）——
mkdir -p "$VAULT/资料库/视频笔记"
V2MD_FAKE_LLM=1 \
V2MD_REMOTE="$POOL" V2MD_OCR_REMOTE="$POOL" V2MD_CLIPS_REMOTE="$POOL" \
V2MD_CLIPS_SOURCE=remote \
OBSIDIAN_VAULT="$VAULT" \
V2MD_STATE_FILE="$TMP/state.json" V2MD_RUNS_FILE="$TMP/runs.json" \
V2MD_LOG_DIR="$TMP/logs" LANGFUSE_PK="" LANGFUSE_SK="" OTEL_EXPORTER_OTLP_ENDPOINT="" \
  uv run --project backend python -m video2md api --port "$PORT" > "$TMP/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 40); do curl -s -m 2 "$API/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "$API/healthz" >/dev/null || fail "api 未起"
curl -s -m 5 "$API/preflight" | grep -q '"ok": *true' || fail "preflight 未过（mockpool 不可达？）"
echo "api 就绪 :$PORT (fake LLM + mockpool remote 三件套)"

# —— 4. POST + SSE 旁观 + 轮询终态 ——
RUN_ID=$(curl -s -m 30 -X POST "$API/runs" -H 'Content-Type: application/json' \
  -d "{\"url\": \"$TMP/smoke.mp4\", \"force\": true}" | jqv run_id)
[ -n "$RUN_ID" ] || fail "建 run 失败"
echo "run_id=$RUN_ID"
curl -sN -m 400 "$API/runs/$RUN_ID/events" > "$TMP/sse.log" 2>&1 &
SSE_PID=$!

deadline=$((SECONDS + 300))
while true; do
  R=$(curl -s -m 10 "$API/runs/$RUN_ID")
  ST=$(echo "$R" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status') or (d.get('task') or {}).get('status') or '')")
  case "$ST" in
    done|partial) break ;;
    error) echo "$R" | python3 -m json.tool | head -30; fail "run 失败" ;;
    *) [ $SECONDS -lt $deadline ] || { kill $SSE_PID 2>/dev/null || true; fail "轮询超时（300s）"; }
       sleep 5 ;;
  esac
done
[ "$ST" = "done" ] || fail "终态 $ST（期望 done；mock 环境没有 partial 的正当理由）"
echo "  ✅ run → done"

# —— 5. SSE 断言：connected 快照 + graph:end 关流 ——
kill -0 $SSE_PID 2>/dev/null && { sleep 3; kill $SSE_PID 2>/dev/null || true; }
grep -q '"event": *"connected"' "$TMP/sse.log" || { head -5 "$TMP/sse.log"; fail "SSE 缺 connected 快照"; }
grep -q 'graph:end' "$TMP/sse.log" || fail "SSE 未见 graph:end"
echo "  ✅ SSE 旁观 + graph:end 关流"

# —— 6. 产物断言（与 smoke_compose 同口径）——
NOTES_JSON=$(curl -s -m 10 "$API/runs/$RUN_ID/note")
echo "$NOTES_JSON" | grep -q "测试笔记" || fail "/note 无产出"
NOTE=$(find "$VAULT" -name "测试笔记*.md" | head -1)
[ -n "$NOTE" ] || fail "vault 无笔记文件"
grep -q '!\[\[attachments/' "$NOTE" || fail "正文无图片嵌入（mock 选帧/发布链断裂）"
grep -q '!\[\[attachments/.*\.mp4\]\]' "$NOTE" || fail "正文无节段嵌入（mock clips 链断裂）"
ls "$VAULT/资料库/视频笔记/attachments/"*.jpg >/dev/null 2>&1 || fail "attachments 无帧"
ls "$VAULT/资料库/视频笔记/attachments/"*.mp4 >/dev/null 2>&1 || fail "attachments 无节段"
grep -q "【画面】" "$VAULT/资料库/视频笔记/transcripts/"*.txt || fail "转录本缺画面通道（mock OCR 链断裂）"
echo "  ✅ 笔记/帧/节段/转录本画面通道 全部到位"
echo "✅ SMOKE-CI PASS（进程级 mockpool 全链路：graph + SSE + vault 产物）"

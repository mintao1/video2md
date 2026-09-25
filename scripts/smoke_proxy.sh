#!/usr/bin/env bash
# P0-4 反代冒烟：仓内 deploy/nginx.conf 的关键面在真实 nginx 里过一遍。
# 形态：进程级 api（127.0.0.1:8797）+ nginx 容器（桥接网络，host.docker.internal 回连）。
# 配置同源性：sed 只换 upstream（127.0.0.1:8790 → host.docker.internal:8797）——
# deny/SSE/限流/body 上限等被验证的指令逐字节来自仓内生产配置。
# 断言：/docs·/openapi.json·/redoc → 403；healthz 经反代 200；SSE 即时透传（缓冲关）；
#       通用限流兜底（120r/m burst 60 → 200 连发必现 429）。
#   scripts/smoke_proxy.sh           # 本地/CI（需 docker）
#   KEEP_UP=1 …                      # 保留现场排障
set -euo pipefail
cd "$(dirname "$0")/.."

API_PORT=8797; PROXY_PORT=8843
API="http://127.0.0.1:$API_PORT"; PROXY="http://127.0.0.1:$PROXY_PORT"
TMP=$(mktemp -d "$(pwd)/.smoke-proxy-XXXXXX")   # 仓库内：macOS Docker 不共享 /tmp 的 bind-mount
API_PID=""; NGX_CID=""
fail() { echo "❌ $1"; tail -20 "$TMP/api.log" 2>/dev/null; docker logs "$NGX_CID" 2>&1 | tail -10 || true; exit 1; }
cleanup() {
  pkill -9 -f "video2md api --port $API_PORT" 2>/dev/null || true
  [ -n "${API_PID}" ] && wait "${API_PID}" 2>/dev/null || true
  if [ -z "${KEEP_UP:-}" ]; then
    [ -n "${NGX_CID}" ] && docker rm -f "$NGX_CID" >/dev/null 2>&1 || true
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT
command -v docker >/dev/null || fail "缺 docker（本冒烟需要 nginx 容器）"

# —— 1. 进程级 api（回环；TRUST_XFF 保持 off——socket 直连冒烟，伪造 XFF 无效口径）——
env V2MD_AUTH=off V2MD_USERS_FILE="$TMP/users.json" V2MD_RUNS_FILE="$TMP/runs.json" \
  V2MD_STATE_FILE="$TMP/state.json" V2MD_LOG_DIR="$TMP/logs" V2MD_REMOTE="" \
  V2MD_CORS_ORIGINS="" LANGFUSE_PK="" LANGFUSE_SK="" OTEL_EXPORTER_OTLP_ENDPOINT="" \
  PYTHONUNBUFFERED=1 \
  uv run python -m video2md api --port "$API_PORT" > "$TMP/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do curl -s -m 2 "$API/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "$API/healthz" >/dev/null || fail "api 未起"

# —— 2. nginx 容器：仓内配置 sed 换 upstream（仅回连地址，被验指令不动）——
sed -e "s|proxy_pass http://127.0.0.1:8790|proxy_pass http://host.docker.internal:$API_PORT|g" \
  deploy/nginx.conf > "$TMP/nginx-smoke.conf"
NGINX_IMG="${SMOKE_NGINX_IMAGE:-nginx:1.27-alpine}"
docker image inspect "$NGINX_IMG" >/dev/null 2>&1 || docker pull -q "$NGINX_IMG" >/dev/null 2>&1 \
  || NGINX_IMG=nginx:latest   # 拉取被墙时回退本地缓存（被验指令均为 nginx 基础特性）
NGX_CID=$(docker run -d --name v2md-proxy-smoke-$$ -p "127.0.0.1:$PROXY_PORT:80" \
  --add-host=host.docker.internal:host-gateway \
  -v "$TMP/nginx-smoke.conf:/etc/nginx/conf.d/default.conf:ro" "$NGINX_IMG")
for _ in $(seq 1 20); do curl -s -m 2 -o /dev/null "$PROXY/healthz" && break; sleep 0.5; done

# —— 3. 断言面 ——
for path in /docs /openapi.json /redoc; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "$PROXY$path")
  [[ "$CODE" == "403" ]] || fail "$path 经反代 → $CODE（期望 403）"
done
echo "  ✅ /docs /openapi.json /redoc → 403（信息收敛）"

CODE=$(curl -s -o /dev/null -w '%{http_code}' "$PROXY/healthz")
[[ "$CODE" == "200" ]] || fail "healthz 经反代 → $CODE（期望 200）"
echo "  ✅ healthz 经反代 → 200"

# SSE 即时透传：done 态 run 的 connected 快照应秒到（若缓冲未关，数据要攒到 8k/关流才吐）。
# 直连 api 种一个 done run（owner=dev uid：off 档先 GET /runs 落 uid 再写 runs.json）
curl -s -m 5 "$API/runs" >/dev/null
DEV_UID=$(python3 -c "import json;print(list(json.load(open('$TMP/users.json')).keys())[0])")
python3 - "$TMP/runs.json" "$DEV_UID" <<'EOF'
import json, sys
p, uid = sys.argv[1], sys.argv[2]
db = json.load(open(p)) if __import__('os').path.exists(p) and __import__('os').path.getsize(p) else {}
db["ssetest"] = {"run_id": "ssetest", "url": "https://example.test/s", "owner": uid,
                 "created": "2026-01-01", "status": "done", "tmp_cleaned": True, "total": 1,
                 "artifacts": {}, "entries": [{"url": "https://example.test/s"}], "stages": {}}
json.dump(db, open(p, "w"), ensure_ascii=False)
EOF
set +e
sleep 1  # 种子落盘稳定窗（首包竞态防抖）
FIRST=$(curl -sN -m 5 "$PROXY/runs/ssetest/events" 2>"$TMP/sse.err" | head -c 40)
CURL_RC=$?
set -e
[[ "$FIRST" == *connected* ]] || { echo "· 直连同 run 对照：$(curl -sN -m 3 "$API/runs/ssetest/events" | head -c 60)"; cat "$TMP/sse.err"; fail "SSE 经反代无即时数据（rc=$CURL_RC 首包：${FIRST:0:60}）"; }
echo "  ✅ SSE 即时透传（缓冲关）"

# 通用限流兜底：120r/m burst 60 → 快速连发 200 发必现 429（limit_req_status）
CODES=$(for i in $(seq 1 200); do curl -s -o /dev/null -w '%{http_code}\n' "$PROXY/healthz"; done)
N429=$(echo "$CODES" | grep -c 429 || true)
[[ "$N429" -gt 0 ]] || fail "连发 200 未见 429（limit_req 兜底未生效）"
echo "  ✅ 通用限流兜底生效（200 连发 → $N429 个 429）"

echo "✅ SMOKE-PROXY PASS（仓内 nginx.conf：deny/透传/SSE 无缓冲/限流兜底）"

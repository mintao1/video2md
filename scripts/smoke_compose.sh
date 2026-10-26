#!/usr/bin/env bash
# compose 全链路冒烟：生产栈 build → 冒烟栈（mockpool 假算力 + 假 LLM）起 → POST /runs → 断言产物。
# 零 GPU、零 token、不碰真 vault（产物落 deploy/smoke-vault）。
#   scripts/smoke_compose.sh          # 构建 + 冒烟 + 清理
#   KEEP_UP=1 scripts/smoke_compose.sh  # 跑完不 down（排查用）
set -euo pipefail
cd "$(dirname "$0")/.."

# gpu_router 仓库位置：Mac 上不同级，显式指；服务器上默认同级 ../gpu_router
export GPU_ROUTER_DIR="${GPU_ROUTER_DIR:-$HOME/Desktop/开发项目/gpu_router}"
[ -d "$GPU_ROUTER_DIR" ] || { echo "❌ gpu_router 仓库不在: $GPU_ROUTER_DIR"; exit 1; }

SMOKE_PORT="${SMOKE_PORT:-8791}"
API="http://127.0.0.1:$SMOKE_PORT"
fail() { echo "❌ $1"; docker compose -f docker-compose.smoke.yml logs --tail 40 2>/dev/null || true; exit 1; }

echo "—— 1. 合成夹具视频（testsrc2：锐边充足过模糊门，OCR 已被 mock 不依赖内容）——"
mkdir -p deploy/fixtures deploy/smoke-vault
ffmpeg -y -hide_banner -loglevel error -f lavfi -i "testsrc2=duration=15:size=1280x720:rate=25" \
  -f lavfi -i "sine=frequency=440:duration=15" -c:v libx264 -pix_fmt yuv420p -c:a aac \
  -shortest deploy/fixtures/smoke.mp4
echo "夹具就绪: deploy/fixtures/smoke.mp4"

echo "—— 2. 构建生产镜像（video2pdf + gpu_router）——"
docker compose build -q

echo "—— 3. 起冒烟栈 ——"
docker compose -f docker-compose.smoke.yml down -v >/dev/null 2>&1 || true
rm -rf deploy/smoke-vault/* 2>/dev/null || true
docker compose -f docker-compose.smoke.yml up -d --wait
for _ in $(seq 1 30); do curl -s -m 2 "$API/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "$API/healthz" >/dev/null || fail "video2pdf 冒烟实例未起来"
curl -s -m 5 "$API/preflight" | grep -q '"ok": *true' || fail "preflight 未过（mockpool 不可达？）"
echo "冒烟栈就绪"

echo "—— 4. 跑 run（假 LLM + mockpool）——"
RESP=$(curl -s -m 30 -X POST "$API/runs" -H 'Content-Type: application/json' \
  -d '{"url": "/fixtures/smoke.mp4", "force": true}')
RUN_ID=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('run_id',''))")
[ -n "$RUN_ID" ] || { echo "$RESP"; fail "建 run 失败"; }
echo "run_id=$RUN_ID"

deadline=$((SECONDS + 300))
while true; do
  ST=$(curl -s -m 10 "$API/runs/$RUN_ID" | python3 -c \
    "import json,sys;d=json.load(sys.stdin);print(d.get('status') or (d.get('task') or {}).get('status') or '')")
  case "$ST" in
    done|partial) break ;;
    error) curl -s "$API/runs/$RUN_ID" | python3 -m json.tool | head -30; fail "run 失败" ;;
    *) [ $SECONDS -lt $deadline ] || fail "轮询超时"; sleep 5 ;;
  esac
done
[ "$ST" = "done" ] || fail "终态 $ST（期望 done；partial 也算冒烟不过——mock 环境没有失败的正当理由）"
echo "  ✅ run → done"

echo "—— 5. 断言产物 ——"
NOTES_JSON=$(curl -s -m 10 "$API/runs/$RUN_ID/note")
echo "$NOTES_JSON" | grep -q "测试笔记" || fail "/note 无产出"
NOTE=$(find deploy/smoke-vault -name "测试笔记*.md" | head -1)
[ -n "$NOTE" ] || fail "vault 无笔记文件"
grep -q '!\[\[attachments/' "$NOTE" || fail "正文无图片嵌入（mock 选帧/发布链断裂）"
grep -q '!\[\[attachments/.*\.mp4\]\]' "$NOTE" || fail "正文无节段嵌入（mock clips 链断裂）"
ls deploy/smoke-vault/资料库/视频笔记/attachments/*.jpg >/dev/null 2>&1 || fail "attachments 无帧"
ls deploy/smoke-vault/资料库/视频笔记/attachments/*.mp4 >/dev/null 2>&1 || fail "attachments 无节段"
grep -q "【画面】" deploy/smoke-vault/资料库/视频笔记/transcripts/*.txt || fail "转录本缺画面通道（mock OCR 链断裂）"
echo "  ✅ 笔记/帧/节段/转录本 全部到位"

if [ -z "${KEEP_UP:-}" ]; then
  docker compose -f docker-compose.smoke.yml down -v >/dev/null
fi
echo "✅ SMOKE PASS（compose 全链路：graph 跑通 + mockpool 契约 + vault 产物）"

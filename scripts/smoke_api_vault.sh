#!/usr/bin/env bash
# 解耦 API 路子冒烟（开发机）：产物进本机 Obsidian vault 的**全程只走 HTTP API**——
#   合成短视频 → 起 api → POST /runs → SSE /events 旁观（不碰文件不看日志）→
#   轮询到终态 → GET /note 取回正文 → 断言 vault 出现该笔记且与 API 返回一致。
# 用途：验证「GUI 后端只靠 API 就能驱动 + 取产出物」这条解耦通道活着
#（产物存储正式解耦是 Phase 2；这里 vault 直写仍是图内行为，API 是读取面）。
#   ./scripts/smoke_api_vault.sh              # 真实 LLM（烧少量 token，~2min）
#   V2MD_FAKE_LLM=1 ./scripts/smoke_api_vault.sh  # 零 token 纯通道冒烟
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SMOKE_PORT:-8792}"
API="http://127.0.0.1:$PORT"
NOTEDIR="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}/资料库/视频笔记"
fail() { echo "❌ $1"; exit 1; }
jqv() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],'') or '')" "$1"; }

command -v ffmpeg >/dev/null || fail "缺 ffmpeg"
uv run python -c "import Vision" 2>/dev/null || echo "⚠ 无 mac extra：OCR 通道缺席（冒烟不拦截，run 可能 partial）"

TMP=$(mktemp -d /tmp/v2md-smoke-XXXXXX)
API_PID=""
STAMP="$TMP/.start-stamp"
# .env 临时切本地算力（与 test_e2e 同口径：本机池子 9700 没起时，远程转录会把
# 冒烟拖进无关故障域 [Errno 61]），退出时恢复
cp .env "$TMP/.env.bak"
cleanup() {
  cp "$TMP/.env.bak" .env
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
sed -i '' -E 's/^(V2MD_REMOTE|V2MD_CLIPS_REMOTE|V2MD_CLIPS_SOURCE)=/# smoke 本地算力 \1=/' .env

# —— 1. 合成 ~30s 测试视频（与 e2e 同款句子集：whisper condition_on_previous_text=False
#     对 <10s 超短片段会整段空返回（实测），必须取 e2e 验证过的时长档）——
SENTS=("量子纠缠是粒子之间的奇妙关联" "时间管理的第一步是记录时间去向" "光合作用将光能转化为化学能")
# 音色优选 Meijia/Tingting：新 macOS 的 zh_CN 头名是 Eddy——其语音 whisper（condition False）
# 整段空返回（实测），老式普通话音色才转得动
VOICE=$(say -v '?' | grep -E "^(Meijia|Tingting)" | head -1 | awk '{print $1}')
[ -n "$VOICE" ] || VOICE=$(say -v '?' | grep zh_CN | head -1 | awk '{print $1}')
uv run python - "$TMP" "${SENTS[*]}" <<'PYEOF'
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
outdir = Path(sys.argv[1])
font = next(ImageFont.truetype(fp, 64) for fp in
            ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc")
            if Path(fp).exists())
for i, s in enumerate(sys.argv[2].split(), 1):
    img = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = d.textbbox((0, 0), s, font=font)
    d.text(((1280 - (x1 - x0)) // 2 - x0, (720 - (y1 - y0)) // 2 - y0), s, font=font, fill=(10, 10, 10))
    img.save(outdir / f"s{i}.png")
PYEOF
INPUTS=(-f lavfi -i "color=c=0x1d2b53:s=1280x720:r=25:d=0"); AUDIO=(); T=0; CHAIN=""
for i in "${!SENTS[@]}"; do
  n=$((i + 1))
  say -v "$VOICE" -o "$TMP/a$n.aiff" "${SENTS[$i]}"
  D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/a$n.aiff")
  E=$(python3 -c "print(round($T + $D + 0.4, 2))")
  INPUTS+=(-loop 1 -i "$TMP/s$n.png")
  WIN="overlay=enable='between(t,$T,$E)'"
  if [ "$n" -eq 1 ]; then CHAIN="[0:v][1:v]${WIN}[v1];"; else CHAIN+="[v$((n-1))][$((n)):v]${WIN}[v$n];"; fi
  AUDIO+=("file '$TMP/a$n.aiff'")
  T=$(python3 -c "print(round($T + $D + 0.6, 2))")
done
CHAIN="${CHAIN%;}"; INPUTS[3]="color=c=0x1d2b53:s=1280x720:r=25:d=$T"
printf '%s\n' "${AUDIO[@]}" > "$TMP/list.txt"
ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i "$TMP/list.txt" -c:a pcm_s16le "$TMP/all.wav"
NM=${#SENTS[@]}
ffmpeg -y -hide_banner -loglevel error "${INPUTS[@]}" -i "$TMP/all.wav" \
  -filter_complex "$CHAIN" -map "[v$NM]" -map "$((NM + 1)):a" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$TMP/smoke.mp4"

# —— 2. 起 api（本地算力；冒烟端口不扰 8790/8791）——
# V2MD_MIN_TRANSCRIPT_CHARS 放宽：单句测试视频转录本远不到 200 字写盘门禁
touch "$STAMP"  # vault 新文件的比较基准先落地
V2MD_WHISPER_MODEL="${WHISPER_MODEL:-small}" V2MD_MIN_TRANSCRIPT_CHARS=5 \
  uv run python -m video2md api --port "$PORT" \
  > "$TMP/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 40); do curl -s -m 2 "$API/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "$API/healthz" >/dev/null || { tail -20 "$TMP/api.log"; fail "api 未起"; }
echo "api 就绪 :$PORT"

# —— 3. POST + SSE 旁观（全程只认 HTTP，SSE 是解耦进度面）——
RUN_ID=$(curl -s -m 60 -X POST "$API/runs" -H 'Content-Type: application/json' \
  -d "{\"url\": \"$TMP/smoke.mp4\", \"force\": true}" | jqv run_id)
[ -n "$RUN_ID" ] || fail "建 run 失败（看 $TMP/api.log）"
echo "run_id=$RUN_ID"
curl -sN -m 400 "$API/runs/$RUN_ID/events" > "$TMP/sse.log" 2>&1 &
SSE_PID=$!

deadline=$((SECONDS + 600))
while true; do
  R=$(curl -s -m 10 "$API/runs/$RUN_ID")
  ST=$(echo "$R" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status') or (d.get('task') or {}).get('status') or '')")
  case "$ST" in
    done|partial) break ;;
    error) echo "$R" | python3 -m json.tool | head -20; fail "run error" ;;
    *) [ $SECONDS -lt $deadline ] || { kill $SSE_PID 2>/dev/null || true; fail "轮询超时（600s）"; }
       sleep 5 ;;
  esac
done
echo "  ✅ run → ${ST}（轮询通道）"

# —— 4. SSE 断言：connected 快照 + graph:* 事件 + 终态关流 ——
kill -0 $SSE_PID 2>/dev/null && { sleep 3; kill $SSE_PID 2>/dev/null || true; }
grep -q '"event": *"connected"' "$TMP/sse.log" || { head -5 "$TMP/sse.log"; fail "SSE 缺 connected 快照"; }
grep -q 'graph:end' "$TMP/sse.log" || { fail "SSE 未见 graph:end（未关流）"; }
N_EVT=$(grep -c '^data: ' "$TMP/sse.log")
echo "  ✅ SSE 旁观 $N_EVT 事件、graph:end 关流（解耦进度面）"

# —— 5. 产物：API 取回 == vault 落盘 ——
NOTE=$(find "$NOTEDIR" -name "*.md" -newer "$STAMP" ! -name "索引*" | head -1)
[ -n "$NOTE" ] || fail "vault 未出现新笔记（${NOTEDIR}）"
grep -q "processor: video2md" "$NOTE" || fail "frontmatter 缺 processor"
# ground truth 句查转录本（whisper 真输出，fake/真 LLM 两档都在），不查正文（fake 档正文无语义）
# （ls 无匹配/while 末轮测试失败都会以非零收尾——pipefail 下会静默杀掉赋值语句，双兜底）
TR_FILE=$( (ls "$NOTEDIR/transcripts/"*.txt 2>/dev/null || true) | while read -r f; do if [ "$f" -nt "$STAMP" ]; then echo "$f"; fi; done | head -1)
[ -n "$TR_FILE" ] && grep -q "光合作用" "$TR_FILE" || fail "转录本不含 ground truth 句（转写失败？）"
API_MD=$(curl -s -m 10 "$API/runs/$RUN_ID/note" | python3 -c "
import json,sys; ns=json.load(sys.stdin).get('notes') or []; print(ns[0]['md'] if ns else '')")
[ "${#API_MD}" -ge 200 ] || fail "/note 取回正文过短（${#API_MD} 字符）"
SAME=$(python3 -c "
import sys, json
api, path = sys.argv[1], sys.argv[2]
print('same' if api.strip() == open(path).read().strip() else 'diff')" "$API_MD" "$NOTE")
[ "$SAME" = "same" ] || fail "API 取回与 vault 文件不一致"
echo "  ✅ 产物进开发机 vault：$NOTE"
echo "  ✅ /note API 取回与落盘一致（${#API_MD} 字符）"
echo "✅ SMOKE PASS（通道：POST + SSE + 轮询 + /note，纯 HTTP 解耦）"

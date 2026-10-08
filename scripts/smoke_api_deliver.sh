#!/usr/bin/env bash
# deliver 形态路子的真 LLM 冒烟（开发机）：「云端不储存，只发送到用户端」全链路——
#   合成短视频 → 起 api（V2MD_ARTIFACT_MODE=deliver，outbox 指 tmp）→ POST /runs →
#   SSE 旁观 + 轮询终态 → 断言真 vault **零写入** → GET /bundle 拉整包 zip 解包 →
#   解包内容与 GET /note 一致（ground truth 句在）→ DELETE /artifacts ACK 即焚 →
#   bundle/note 再取全 410、在架目录物理消失。
# 与 smoke_api_vault.sh 的分工：vault 冒烟管「本机制库」形态，这条管「多用户投递」形态；
# 两者共用同一段合成视频工艺（<10s 片段 whisper 空返回的时长档、Meijia/Tingting 音色）。
#   ./scripts/smoke_api_deliver.sh                  # 真实 LLM（烧少量 token，~2min）
#   V2MD_FAKE_LLM=1 ./scripts/smoke_api_deliver.sh  # 零 token 纯通道冒烟
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/backend"   # package=false：python -m video2md 靠包根上 path

PORT="${SMOKE_PORT:-8793}"
API="http://127.0.0.1:$PORT"
NOTEDIR="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}/资料库/视频笔记"
fail() { echo "❌ $1"; exit 1; }
jqv() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],'') or '')" "$1"; }

command -v ffmpeg >/dev/null || fail "缺 ffmpeg"
command -v unzip >/dev/null || fail "缺 unzip"
uv run --project backend python -c "import Vision" 2>/dev/null || echo "⚠ 无 mac extra：OCR 通道缺席（冒烟不拦截，run 可能 partial）"

TMP=$(mktemp -d /tmp/v2md-smoked-XXXXXX)
SHELF="$TMP/outbox-shelf"
API_PID=""
STAMP="$TMP/.start-stamp"
# .env 临时切本地算力（与 vault 冒烟同口径），退出时恢复
cp .env "$TMP/.env.bak"
cleanup() {
  cp "$TMP/.env.bak" .env
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
sed -i '' -E 's/^(V2MD_REMOTE|V2MD_CLIPS_REMOTE|V2MD_CLIPS_SOURCE)=/# smoke 本地算力 \1=/' .env

# —— 1. 合成 ~30s 测试视频（与 vault 冒烟同款：三句 + 同步烧录字幕）——
SENTS=("量子纠缠是粒子之间的奇妙关联" "时间管理的第一步是记录时间去向" "光合作用将光能转化为化学能")
VOICE=$(say -v '?' | grep -E "^(Meijia|Tingting)" | head -1 | awk '{print $1}')
[ -n "$VOICE" ] || VOICE=$(say -v '?' | grep zh_CN | head -1 | awk '{print $1}')
uv run --project backend python - "$TMP" "${SENTS[*]}" <<'PYEOF'
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

# —— 2. 起 api：deliver 模式 + 在架根进本测 tmp（不污染 data/outbox）——
touch "$STAMP"  # 真 vault 零写入的比较基准先落地
V2MD_WHISPER_MODEL="${WHISPER_MODEL:-small}" V2MD_MIN_TRANSCRIPT_CHARS=5 \
V2MD_ARTIFACT_MODE=deliver V2MD_OUTBOX_DIR="$SHELF" V2MD_OUTBOX_TTL_H=24 \
  uv run --project backend python -m video2md api --port "$PORT" \
  > "$TMP/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 40); do curl -s -m 2 "$API/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "$API/healthz" >/dev/null || { tail -20 "$TMP/api.log"; fail "api 未起"; }
# 模式闸自证：deliver 下 preflight 体检项应是 outbox（不是 vault）
curl -s -m 5 "$API/preflight" | grep -q '"outbox"' || fail "preflight 未切 outbox 体检"
echo "api 就绪 :${PORT}（deliver，在架根 ${SHELF}）"

# —— 3. POST + SSE 旁观 + 轮询 ——
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

kill -0 $SSE_PID 2>/dev/null && { sleep 3; kill $SSE_PID 2>/dev/null || true; }
grep -q 'graph:end' "$TMP/sse.log" || fail "SSE 未见 graph:end（未关流）"
echo "  ✅ SSE 旁观 $(grep -c '^data: ' "$TMP/sse.log") 事件、graph:end 关流"

# —— 4. 「云端不储存」第一条硬线：真 vault 零写入 ——
if [ -d "$NOTEDIR" ]; then
  DIRTY=$(find "$NOTEDIR" -type f -newer "$STAMP" 2>/dev/null | head -3)
  [ -z "$DIRTY" ] || fail "deliver 写了开发机 vault：$DIRTY"
fi
echo "  ✅ 开发机 vault 零写入（产物只活在在架窗口）"

# —— 5. 轮询面带在架窗口；bundle 整包拉取 + 解包完整性 ——
echo "$R" | python3 -c "
import json, sys
a = json.load(sys.stdin).get('artifacts') or {}
assert a.get('mode') == 'deliver' and a.get('present') and a.get('expires'), a
print(f'  ✅ 在架窗口至 {a[\"expires\"]}')" || fail "/runs 未附在架状态"
[ -d "$SHELF/$RUN_ID" ] || fail "在架目录缺失 $SHELF/$RUN_ID"
HTTP=$(curl -s -o "$TMP/bundle.zip" -w '%{http_code}' -m 60 "$API/runs/$RUN_ID/bundle")
[ "$HTTP" = 200 ] || fail "bundle 拉取失败（HTTP ${HTTP}）"
unzip -q -o "$TMP/bundle.zip" -d "$TMP/unpacked"
NOTE=$(find "$TMP/unpacked" -maxdepth 1 -name "*.md" ! -name "索引*" | head -1)
[ -n "$NOTE" ] || fail "bundle 里没有笔记 md（整包内容见 $TMP/unpacked）"
grep -q "processor: video2md" "$NOTE" || fail "frontmatter 缺 processor"
# ground truth 句查转录本（whisper 真输出，fake/真 LLM 两档都在），不查正文（正文由 LLM 整理，fake 档无实义）
TR_FILE=$(ls "$TMP/unpacked/transcripts/"*.txt 2>/dev/null | head -1)
[ -n "$TR_FILE" ] && grep -q "光合作用" "$TR_FILE" || fail "转录本不含 ground truth 句（转写失败？）"
[ -f "$TMP/unpacked/索引-append.md" ] || fail "bundle 缺索引-append.md"
N_TR=$(ls "$TMP/unpacked/transcripts/"*.txt 2>/dev/null | wc -l | tr -d ' ')
[ "$N_TR" -ge 1 ] || fail "bundle 缺转录本（transcripts/ 空）"
grep -q '!\[\[attachments/' "$NOTE" || fail "解包笔记无图片嵌入（帧未入库）"
echo "  ✅ bundle 解包完整：笔记 + ${N_TR} 份转录 + attachments 嵌入 + 索引-append"

# —— 6. /note 与解包正文一致（同一在架文件的两个取面）——
API_MD=$(curl -s -m 10 "$API/runs/$RUN_ID/note" | python3 -c "
import json,sys; ns=json.load(sys.stdin).get('notes') or []; print(ns[0]['md'] if ns else '')")
[ "${#API_MD}" -ge 200 ] || fail "/note 取回正文过短（${#API_MD} 字符）"
python3 -c "
import sys
api = sys.argv[1].strip()
same = api == open(sys.argv[2]).read().strip()
sys.exit(0 if same else 1)" "$API_MD" "$NOTE" || fail "/note 与 bundle 解包正文不一致"
echo "  ✅ /note 与 bundle 正文一致（${#API_MD} 字符）"

# —— 7. ACK 即焚：物理销毁 + 再取 410（「云端不储存」第二条硬线）——
ACK=$(curl -s -m 10 -X DELETE "$API/runs/$RUN_ID/artifacts")
echo "$ACK" | grep -q '"burned"' || fail "ACK 即焚失败：$ACK"
[ ! -d "$SHELF/$RUN_ID" ] || fail "ACK 后在架目录仍在（未物理删除）"
H1=$(curl -s -o /dev/null -w '%{http_code}' "$API/runs/$RUN_ID/bundle")
H2=$(curl -s -o /dev/null -w '%{http_code}' "$API/runs/$RUN_ID/note")
[ "$H1" = 410 ] && [ "$H2" = 410 ] || fail "焚后再取应 410，实得 bundle=$H1 note=$H2"
echo "  ✅ ACK 即焚：目录物理销毁，bundle/note → 410（重发 URL 生成新 run）"

echo "✅ SMOKE DELIVER PASS（形态：outbox 在架 → bundle 投递 → ACK 即焚，vault 零写入）"

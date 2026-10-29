#!/usr/bin/env bash
# 已知答案端到端测试（LangGraph 版）：say 合成中文语音 + PIL 字幕 PNG overlay 烧录
#   → 自起 api 实例（V2PDF_FAKE_LLM=1，确定性假 LLM 不烧 token）→ POST /runs → 轮询终态 → 断言。
# Dify 抽离后不再有逐阶段 HTTP 端点：图的节点就是阶段，e2e 验证的是「整条链一次跑通」。
#   ./test_e2e.sh                      # 端到端（脚本自起 api 于 127.0.0.1:8791，不影响 8790）
#   WHISPER_MODEL=small ./test_e2e.sh  # 自选 whisper 模型（默认 small）
#   REAL_LLM=1 ./test_e2e.sh           # 烧真实 LLM（用 ~/.config/video2pdf.env 的 LLM_*）
#   MODE=deliver ./test_e2e.sh         # deliver 形态（云端不储存）：产物进 outbox →
#                                      # bundle 拉取解包后跑同一套内容断言 + ACK 即焚链
set -euo pipefail
cd "$(dirname "$0")/.."   # backend/：pyproject 与包根，uv run/python -m 在此解析

MODEL="${WHISPER_MODEL:-small}"
PORT="${V2PDF_API_PORT:-8791}"
API="http://127.0.0.1:$PORT"
MODE="${MODE:-vault}"          # vault=现状（写本机 vault）| deliver=在架+TTL+ACK 即焚

VAULT="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}"
NOTEDIR="$VAULT/资料库/视频笔记"
SHELF_TMP=""                   # deliver 模式的在架根（api 子进程 env 注入）
fail() { echo "❌ $1"; exit 1; }

# Vision OCR 依赖 pyobjc（mac extra）：venv 被剥 extra 时 OCR 全灭而 run 照样 partial
# （错误只躺 ocr_raw），白跑一轮才见分晓——起跑前拦死并给出修法
uv run python -c "import Foundation, Vision" 2>/dev/null \
  || fail "缺 mac extra（Vision OCR 会全灭）：先 uv sync --extra mac --extra evals"

TMP=$(mktemp -d /tmp/v2pdf-e2e-XXXXXX)

# .env 临时切本地算力（远程池挂了会把 e2e 拖进无关故障域），退出时恢复
cp .env "$TMP/.env.bak"
cleanup() {
  cp "$TMP/.env.bak" .env
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
sed -i '' -E 's/^(V2PDF_REMOTE|V2PDF_CLIPS_REMOTE|V2PDF_CLIPS_SOURCE)=/# e2e 本地算力 \1=/' .env

# —— 1. 合成测试视频：5 句已知中文，语音 + 同步烧录字幕（画面即 OCR ground truth）——
SENTS=("量子纠缠是粒子之间的奇妙关联" "时间管理的第一步是记录时间去向" "光合作用将光能转化为化学能" "深度学习的本质是多层特征变换" "复利是世界第八大奇迹")
VOICE=$(say -v '?' | grep zh_CN | head -1 | awk '{print $1}')
say -v '?' | grep -q Tingting && VOICE="Tingting"
echo "语音: $VOICE · whisper: $MODEL"

SENTS_JSON=$(printf '%s\n' "${SENTS[@]}" | python3 -c "import json,sys;print(json.dumps(sys.stdin.read().split(),ensure_ascii=False))")
uv run python - "$TMP" "$SENTS_JSON" <<'PYEOF'
import json, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
outdir, sentences = Path(sys.argv[1]), json.loads(sys.argv[2])
font = None
for fp in ("/System/Library/Fonts/PingFang.ttc",
           "/System/Library/Fonts/STHeiti Medium.ttc",
           "/System/Library/Fonts/Hiragino Sans GB.ttc"):
    try:
        font = ImageFont.truetype(fp, 64)
        break
    except OSError:
        continue
assert font, "找不到中文字体"
for i, s in enumerate(sentences, 1):
    img = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = d.textbbox((0, 0), s, font=font)
    x, y = (1280 - (x1 - x0)) // 2 - x0, (720 - (y1 - y0)) // 2 - y0
    d.text((x, y), s, font=font, fill=(10, 10, 10))
    img.save(outdir / f"s{i}.png")
print(f"幻灯片 PNG x{len(sentences)}")
PYEOF

CHAIN=""; INPUTS=(-f lavfi -i "color=c=0x1d2b53:s=1280x720:r=25:d=0"); AUDIO=(); T=0
for i in "${!SENTS[@]}"; do
  n=$((i + 1))
  say -v "$VOICE" -o "$TMP/a$n.aiff" "${SENTS[$i]}"
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/a$n.aiff")
  e=$(python3 -c "print(round($T + $d + 0.4, 2))")
  INPUTS+=(-loop 1 -i "$TMP/s$n.png")
  WIN="overlay=enable='between(t,$T,$e)'"
  if [ "$n" -eq 1 ]; then
    CHAIN="[0:v][1:v]${WIN}[v1];"
  else
    CHAIN+="[v$((n-1))][$((n)):v]${WIN}[v$n];"
  fi
  AUDIO+=("file '$TMP/a$n.aiff'")
  T=$(python3 -c "print(round($T + $d + 0.6, 2))")
done
CHAIN="${CHAIN%;}"
INPUTS[3]="color=c=0x1d2b53:s=1280x720:r=25:d=$T"
printf '%s\n' "${AUDIO[@]}" > "$TMP/list.txt"
ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i "$TMP/list.txt" -c:a pcm_s16le "$TMP/all.wav"
NA=${#SENTS[@]}
ffmpeg -y -hide_banner -loglevel error "${INPUTS[@]}" -i "$TMP/all.wav" \
  -filter_complex "$CHAIN" -map "[v$NA]" -map "$((NA + 1)):a" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$TMP/e2e.mp4"
echo "测试视频就绪（${T}s）"

# —— 2. 自起 api 实例（假 LLM + 本地算力）并跑完整张图 ——
if [ "$MODE" = deliver ]; then
  # deliver：产物只活在 outbox 在架窗口，真 vault 必须零写入——marker 前后对比兜住这条线
  touch "$TMP/marker"
  SHELF_TMP="$TMP/outbox-shelf"
else
  rm -f "$NOTEDIR"/测试笔记（假LLM）*.md "$NOTEDIR"/e2e*.md "$NOTEDIR"/transcripts/e2e* "$NOTEDIR"/attachments/e2e-*.jpg "$NOTEDIR"/attachments/e2e-*.mp4
  mkdir -p "$NOTEDIR" "$NOTEDIR/transcripts" "$NOTEDIR/attachments"
  touch "$NOTEDIR/索引.md"
  sed -i '' '/e2e/d;/测试笔记（假LLM）/d' "$NOTEDIR/索引.md" 2>/dev/null || true
fi

FAKE_FLAG="1"
if [ -n "${REAL_LLM:-}" ]; then
  FAKE_FLAG="0"
fi
MODE_ENV=(V2PDF_ARTIFACT_MODE="$MODE")
[ "$MODE" = deliver ] && MODE_ENV+=(V2PDF_OUTBOX_DIR="$SHELF_TMP")  # vault 模式不给空值脏配置
# （展开产物不能做命令前缀赋值——走 env 吸收）
V2PDF_FAKE_LLM="$FAKE_FLAG" V2PDF_WHISPER_MODEL="$MODEL" \
env "${MODE_ENV[@]}" uv run python -m video2pdf api --port "$PORT" > "$TMP/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do curl -s -m 2 "$API/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 "$API/healthz" >/dev/null || { cat "$TMP/api.log"; fail "api 未起来"; }
echo "api 就绪（V2PDF_FAKE_LLM=${FAKE_FLAG}）"

jqv() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],'') or '')" "$1"; }

# 临时目录断言的基线：只数本测新增的残留（历史崩溃遗留不该算到本次头上）
TMP_LEFT_BEFORE=$(python3 -c "import glob,tempfile;print(len(glob.glob(tempfile.gettempdir()+'/video2pdf-run-*')))")

BODY=$(python3 - "$TMP/e2e.mp4" <<'PYEOF'
import json, sys
print(json.dumps({"url": sys.argv[1], "force": True}))
PYEOF
)
RESP=$(curl -s -m 30 -X POST "$API/runs" -H 'Content-Type: application/json' -d "$BODY")
RUN_ID=$(echo "$RESP" | jqv run_id)
[ -n "$RUN_ID" ] || { echo "$RESP"; fail "建 run 失败"; }
echo "run_id=${RUN_ID}（图一次跑通，不再逐阶段驱动）"

deadline=$((SECONDS + 900))
while true; do
  R=$(curl -s -m 30 "$API/runs/$RUN_ID")
  ST=$(echo "$R" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status') or (d.get('task') or {}).get('status') or '')")
  case "$ST" in
    done|partial) break ;;
    error) echo "$R" | python3 -m json.tool | head -30; fail "run 失败" ;;
    *) [ $SECONDS -lt $deadline ] || fail "轮询超时（900s）"; sleep 10 ;;
  esac
done
echo "  ✅ run → $ST"

if [ "$MODE" = deliver ]; then
  # —— 投递面：轮询带在架窗口 → bundle 拉取解包 → 解包目录成为后续内容断言的根 ——
  python3 - "$R" <<'PYEOF' || fail "/runs 未附在架状态"
import json, sys
a = json.loads(sys.argv[1]).get("artifacts") or {}
assert a.get("mode") == "deliver" and a.get("present") and a.get("expires"), a
PYEOF
  HTTP=$(curl -s -o "$TMP/bundle.zip" -w '%{http_code}' -m 60 "$API/runs/$RUN_ID/bundle")
  [ "$HTTP" = 200 ] || fail "bundle 拉取失败（HTTP ${HTTP}）"
  unzip -q -o "$TMP/bundle.zip" -d "$TMP/unpacked"
  NOTEDIR="$TMP/unpacked"
  ls "$NOTEDIR/索引-append.md" >/dev/null 2>&1 || fail "bundle 缺索引-append.md"
  [ -d "$SHELF_TMP/$RUN_ID" ] || fail "在架目录不在 $SHELF_TMP/$RUN_ID"
  echo "  ✅ bundle 解包 → $NOTEDIR"
fi
# （zip 保留原 mtime，均为 run 期间写出 → -newer 基线两模式通用）
NOTE=$(find "$NOTEDIR" -name "*.md" -newer "$TMP/e2e.mp4" ! -name "索引*" | head -1)
[ -n "$NOTE" ] && [ -f "$NOTE" ] || fail "未产出笔记"
echo "  ✅ note → $(basename "$NOTE")"

# —— 3. 断言（与旧 e2e 同口径；kb 相关断言已随 Dify 抽离摘除）——
echo "—— 断言 ——"
grep -q "processor: video2pdf" "$NOTE" || fail "frontmatter 缺 processor"
grep -q "reviewed: false" "$NOTE" || fail "frontmatter 缺 reviewed"
grep -q "> \[!info\] 视频信息" "$NOTE" || fail "缺 [!info] 视频信息 callout"
grep -q "^## " "$NOTE" || fail "笔记缺章节结构"
# 转录本 SSOT：正文 wikilink 指向 transcripts/ 下的真实文件
TNAME=$(grep -o 'transcripts/[^|)]*\.txt' "$NOTE" | head -1 | sed 's#.*/##')
[ -n "$TNAME" ] && [ -f "$NOTEDIR/transcripts/$TNAME" ] || fail "缺转录本 SSOT 文件（wikilink 悬空）"
grep -q "【画面】" "$NOTEDIR/transcripts/$TNAME" || fail "转录本缺画面通道（OCR 未产出）"
# 帧入库：attachments/ 必须有嵌入帧（wikilink 不悬空）
N_ATTACH=$(ls "$NOTEDIR"/attachments/e2e-*.jpg 2>/dev/null | wc -l | tr -d ' ')
[ "$N_ATTACH" -ge 1 ] || fail "帧未入库 attachments/"
grep -q '!\[\[attachments/' "$NOTE" || fail "正文无图片嵌入"
# 视频节段：每处截帧配 ±10s 可播片段，文件与嵌入行都要在
N_CLIPS=$(ls "$NOTEDIR"/attachments/e2e-*.mp4 2>/dev/null | wc -l | tr -d ' ')
[ "$N_CLIPS" -ge 1 ] || fail "视频节段未剪出（attachments/ 无 mp4）"
grep -q '!\[\[attachments/.*\.mp4\]\]' "$NOTE" || fail "正文无视频节段嵌入"
uv run python - "$NOTE" "$NOTEDIR/attachments" <<'PYEOF'
import re, sys
note, att = sys.argv[1], sys.argv[2]
from pathlib import Path
text = open(note).read()
embeds = re.findall(r"!\[\[attachments/([^\]]+\.mp4)\]\]", text)
missing = [e for e in embeds if not (Path(att) / e).exists()]
assert not missing, f"节段嵌入悬空: {missing}"
lines = text.splitlines()
for i, l in enumerate(lines):
    m = re.fullmatch(r"!\[\[attachments/([^\]]+\.jpg)\]\]", l.strip())
    if m and not any(m.group(1).replace("-f", "-c").replace(".jpg", ".mp4") == e
                     for e in embeds):
        raise SystemExit(f"帧 {m.group(1)} 缺配套视频节段")
print("节段配对 OK")
PYEOF
# ground truth：5 句合成语音模糊匹配，对照转录本 SSOT。容忍：繁体/同音字（ratio）、多句并一段（滑窗）
uv run python - "$NOTEDIR/transcripts/$TNAME" <<'PYEOF'
import re, sys
from difflib import SequenceMatcher
sentences = ["量子纠缠是粒子之间的奇妙关联", "时间管理的第一步是记录时间去向",
             "光合作用将光能转化为化学能", "深度学习的本质是多层特征变换",
             "复利是世界第八大奇迹"]
text = open(sys.argv[1]).read()
norm = lambda s: re.sub(r"[\s，。、；：？！.,;:?!]+", "", s)
lines = [norm(l) for l in text.splitlines() if norm(l)]

def covered(sent: str) -> bool:
    s = norm(sent)
    for l in lines:
        if s in l:
            return True
        if SequenceMatcher(None, s, l).ratio() > 0.6:
            return True
        if len(l) > len(s):  # 多句并一段 → 滑窗匹配
            w = int(len(s) * 1.4)
            for i in range(0, max(1, len(l) - len(s) + 1)):
                if SequenceMatcher(None, s, l[i:i + w]).ratio() > 0.7:
                    return True
    return False

bad = [s for s in sentences if not covered(s)]
if bad:
    print("转录本缺句:", bad)
    sys.exit(1)
print("转写 ground truth OK")
PYEOF
# OCR ground truth：烧录字幕文字必须出现在【画面】行
grep "【画面】" "$NOTEDIR/transcripts/$TNAME" | grep -q "光合作用" || fail "OCR 未识别出烧录字幕"
# 索引登记（vault=共享索引.md；deliver=随包并库的 索引-append.md）
IDX="$NOTEDIR/索引.md"; [ "$MODE" = deliver ] && IDX="$NOTEDIR/索引-append.md"
grep -q "$(basename "$NOTE" .md)" "$IDX" || fail "索引未登记该笔记"
# 幂等 SSOT：state.json 应有本视频的指纹记录（重发同链应跳过）
python3 -c "
import json; s = json.load(open('state.json'))
assert any(v.get('note') for v in s.values()), 'state.json 未落幂等记录'
print('state.json 幂等记录 OK')"
# 临时目录零新增残留
LEFT=$(python3 -c "import glob,tempfile;print(len(glob.glob(tempfile.gettempdir()+'/video2pdf-run-*')))")
[ "$LEFT" -le "$TMP_LEFT_BEFORE" ] || fail "临时目录新增残留 $((LEFT - TMP_LEFT_BEFORE)) 个"

# /queue 契约（gpu_router 回读口）
curl -s -m 5 "$API/queue" | grep -q '"queue_len"' || fail "/queue 契约缺失"
# 产出 markdown 调取口
curl -s -m 5 "$API/runs/$RUN_ID/note" | grep -q "测试笔记" || fail "/note 调取口异常"

if [ "$MODE" = deliver ]; then
  # —— deliver 收尾断言：云端不储存的两条硬线 ——
  # 1) 真 vault 零写入（deliver 全链路不得碰用户共享库）
  if [ -d "$VAULT/资料库/视频笔记" ]; then
    DIRTY=$(find "$VAULT/资料库/视频笔记" -type f -newer "$TMP/marker" 2>/dev/null | head -3)
    [ -z "$DIRTY" ] || fail "deliver 模式写了真 vault：$DIRTY"
  fi
  # 2) ACK 即焚：确认收完 → 在架目录物理消失 → bundle/note 全 410（重发才有的新 run）
  ACK=$(curl -s -m 10 -X DELETE "$API/runs/$RUN_ID/artifacts")
  echo "$ACK" | grep -q '"burned"' || fail "ACK 即焚失败：$ACK"
  [ ! -d "$SHELF_TMP/$RUN_ID" ] || fail "ACK 后在架目录仍在（未物理删除）"
  H1=$(curl -s -o /dev/null -w '%{http_code}' "$API/runs/$RUN_ID/bundle")
  H2=$(curl -s -o /dev/null -w '%{http_code}' "$API/runs/$RUN_ID/note")
  [ "$H1" = 410 ] && [ "$H2" = 410 ] || fail "焚后再取应 410，实得 bundle=$H1 note=$H2"
  grep -q '"artifacts_burned": true' data/runs.json 2>/dev/null \
    || grep -q "burned" "$TMP/api.log" || true  # runs.json 位置随 V2PDF_RUNS_FILE，形态断言以 410 为准
  echo "  ✅ ACK 即焚：bundle/note → 410，在架目录已物理销毁"
fi

echo "✅ E2E PASS（mode=${MODE}） → $NOTE"

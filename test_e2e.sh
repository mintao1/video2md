#!/usr/bin/env bash
# 已知答案端到端测试：say 合成中文语音 + PIL 字幕 PNG overlay 烧录 → 逐阶段驱动 serve API → 断言。
# 管线由 Dify workflow 编排后，e2e 直接打 serve 的阶段端点（POST /runs + /runs/{id}/{stage}），
# 本地 whisper（remote="" 强制）+ 启发式选帧兜底 + 合成正文写盘，全程不烧 LLM、不入知识库。
#   ./test_e2e.sh                      # 端到端（需 serve 已启动：launchd com.linmintao.video2md-server）
#   WHISPER_MODEL=small ./test_e2e.sh  # 自选 whisper 模型（默认 small）
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${WHISPER_MODEL:-small}"
SERVE="http://127.0.0.1:${V2MD_PORT:-8790}"

VAULT="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}"
NOTEDIR="$VAULT/资料库/视频笔记"
fail() { echo "❌ $1"; exit 1; }

TMP=$(mktemp -d /tmp/v2md-e2e-XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# —— 1. 合成测试视频：5 句已知中文，语音 + 同步烧录字幕（画面即 OCR ground truth）——
SENTS=("量子纠缠是粒子之间的奇妙关联" "时间管理的第一步是记录时间去向" "光合作用将光能转化为化学能" "深度学习的本质是多层特征变换" "复利是世界第八大奇迹")
VOICE=$(say -v '?' | grep zh_CN | head -1 | awk '{print $1}')
say -v '?' | grep -q Tingting && VOICE="Tingting"
echo "语音: $VOICE · whisper: $MODEL"

# PIL 渲染幻灯片式整幅 PNG（白底黑字大标题——贴近幻灯片 OCR 场景，场景切换可触发抽帧）
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

# 逐句合成语音，累计 overlay 时间窗
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

# —— 2. 逐阶段驱动 serve API（与 Dify workflow 同一组端点）——
rm -f "$NOTEDIR"/e2e*.md "$NOTEDIR"/transcripts/e2e* "$NOTEDIR"/attachments/e2e-*.jpg "$NOTEDIR"/attachments/e2e-*.mp4
mkdir -p "$NOTEDIR" "$NOTEDIR/transcripts" "$NOTEDIR/attachments"
touch "$NOTEDIR/索引.md" 2>/dev/null || true
[ -f "$NOTEDIR/索引.md" ] && sed -i '' '/e2e/d' "$NOTEDIR/索引.md" 2>/dev/null || true

jqv() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1],'') or '')" "$1"; }

stage() {  # stage <name> <json-body>：POST 后轮询到 done/error
  local name="$1" body="$2" deadline=$((SECONDS + 900))
  while true; do
    local resp
    resp=$(curl -s -m 120 -X POST "$SERVE/runs/$RUN_ID/$name" -H 'Content-Type: application/json' -d "$body")
    local st
    st=$(echo "$resp" | jqv status)
    case "$st" in
      done) echo "  ✅ $name"; return 0 ;;
      error) echo "$resp" | jqv error; fail "阶段 $name 失败" ;;
      *) [ $SECONDS -lt $deadline ] || fail "阶段 $name 轮询超时"; sleep 5 ;;
    esac
  done
}

echo "—— 建 run ——"
RESP=$(curl -s -m 60 -X POST "$SERVE/runs" -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json;print(json.dumps({'url':'$TMP/e2e.mp4','force':True}))")")
RUN_ID=$(echo "$RESP" | jqv run_id)
[ -n "$RUN_ID" ] || { echo "$RESP"; fail "建 run 失败"; }
echo "run_id=$RUN_ID"

echo "—— 逐阶段推进 ——"
stage download '{}'
stage transcribe "$(python3 -c "import json;print(json.dumps({'remote':'','model':'$MODEL','language':'zh'}))")"
stage frames '{"max_frames":"20"}'
stage ocr '{}'
stage select '{}'   # 空 payload → 本地启发式选帧兜底
stage clips '{}'

echo "—— note：合成正文（嵌已发布帧）写盘 ——"
SELECTED=$(curl -s -m 30 "$SERVE/runs/$RUN_ID/stages/selected" | jqv text)
BODY=$(python3 - "$SELECTED" <<'PYEOF'
import json, re, sys
manifest = sys.argv[1]
frames = re.findall(r"^(\S+\.jpe?g)", manifest, re.M)
parts = ["## 引言（00:00）", "", "本视频用五条已知事实句做管线自检，逐句配画面帧。", ""]
for i, f in enumerate(frames[:5], 1):
    parts += [f"第 {i} 节内容。", f"![[attachments/{f}]]", f"*第 {i} 帧图注*", ""]
print(json.dumps({"body": "\n".join(parts),
                  "meta": {"title": "e2e 自检笔记", "tags": ["e2e"], "summary": "管线自检"}},
                 ensure_ascii=False))
PYEOF
)
NOTE_RESP=$(curl -s -m 300 -X POST "$SERVE/runs/$RUN_ID/note" -H 'Content-Type: application/json' -d "$BODY")
while true; do
  st=$(curl -s -m 120 -X POST "$SERVE/runs/$RUN_ID/note" -H 'Content-Type: application/json' -d "$BODY" | jqv status)
  case "$st" in
    done) break ;;
    error) fail "note 阶段失败" ;;
    *) sleep 5 ;;
  esac
done
NOTE=$(find "$NOTEDIR" -name "e2e*.md" -newer "$TMP/e2e.mp4" | head -1)
[ -n "$NOTE" ] && [ -f "$NOTE" ] || fail "未产出笔记"
echo "  ✅ note → $NOTE"
# —— 3. 断言 ——
echo "—— 断言 ——"
grep -q "processor: dify-video2md" "$NOTE" || fail "frontmatter 缺 processor"
grep -q "reviewed: false" "$NOTE" || fail "frontmatter 缺 reviewed"
grep -q "> \[!info\] 视频信息" "$NOTE" || fail "缺 [!info] 视频信息 callout"
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
# 每个帧嵌入下方应紧跟对应节段嵌入
lines = text.splitlines()
for i, l in enumerate(lines):
    m = re.fullmatch(r"!\[\[attachments/([^\]]+\.jpg)\]\]", l.strip())
    if m and not any(m.group(1).replace("-f", "-c").replace(".jpg", ".mp4") == e
                     for e in embeds):
        raise SystemExit(f"帧 {m.group(1)} 缺配套视频节段")
print("节段配对 OK")
PYEOF
# ground truth：5 句合成语音模糊匹配，对照转录本 SSOT（正文是改写文，措辞会变）。容忍：繁体/同音字（ratio）、多句并一段（滑窗）
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
# 索引登记
grep -q "$(basename "$NOTE" .md)" "$NOTEDIR/索引.md" || fail "索引未登记该笔记"
# 完整模式额外断言：LLM 文稿结构 + 检索自检
if [ -z "${DRY_RUN:-}" ] && [ -z "${SKIP_KB:-}" ]; then
  grep -q "^## " "$NOTE" || fail "笔记缺章节结构"
  grep -qE "^## .+（[0-9]+:[0-9]{2}）" "$NOTE" || fail "章节标题缺时间戳锚点"
  grep -q "复利" "$NOTEDIR/transcripts/$TNAME" || fail "转录本未覆盖关键句5"
  echo "$OUT" | grep -q "SELF-CHECK PASS" || fail "知识库检索自检未通过"
fi
# 临时目录零残留
LEFT=$(python3 -c "import glob,tempfile;print(len(glob.glob(tempfile.gettempdir()+'/video2md-*')))")
[ "$LEFT" -eq 0 ] || fail "临时目录残留 $LEFT 个"

echo "✅ E2E PASS → $NOTE"

#!/bin/bash
# 抖音风控解除守望：每 20 分钟轻探博主主页，能扫到 ≥27 个作品就自动发起全量跑。
# 最多等 9 轮（3 小时）。日志: logs/douyin-retry-watch.log
cd /Users/linmintao/ZCodeProject/dify-video2md || exit 1
LOG=logs/douyin-retry-watch.log
echo "[$(date '+%H:%M:%S')] 守望启动（每 20 分钟探测，最多 9 轮）" >> "$LOG"
for i in $(seq 1 9); do
  sleep 1200
  N=$(python3 -c "
import importlib.util, warnings
warnings.filterwarnings('ignore')
spec = importlib.util.spec_from_file_location('v2md', 'video2md.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
try:
    es = m.resolve_url('https://www.douyin.com/user/MS4wLjABAAAAEdoinwc8bC1HTF95XlLojlOl-8FbK0jMnf7zsUyRDQIOhk6xSQilNHfupC3YWiNR')
    print(len(es))
except Exception:
    print(0)
" 2>/dev/null)
  echo "[$(date '+%H:%M:%S')] 第 ${i} 轮探测: ${N} 个作品" >> "$LOG"
  if [ "${N:-0}" -ge 27 ]; then
    echo "[$(date '+%H:%M:%S')] 风控已解除（${N} 个作品），发起全量工作流" >> "$LOG"
    python3 scripts/douyin_smoke.py >> "$LOG" 2>&1
    echo "[$(date '+%H:%M:%S')] 全量跑结束" >> "$LOG"
    exit 0
  fi
done
echo "[$(date '+%H:%M:%S')] 守望超时（3 小时仍未解除），退出" >> "$LOG"
exit 1

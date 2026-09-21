#!/bin/zsh
# 算力池恢复哨兵：每 30min 探测一次池子 /status，任何一家 asr provider 可用即起批次
# （batch_csv.py 按账本断点续跑）。
# 用法：nohup scripts/wait_pool.sh & ；停：pkill -f wait_pool
# 环境变量：POOL_URL（默认 http://127.0.0.1:9700）、INTERVAL（秒，默认 1800）
#
# 取代 colab_watch.sh（直探 colab new）：Colab 已抽离进池子，「有无算力」是池子口径
# （Colab/Kaggle/Modal/ZeroGPU 任一家可用即开工），不再是 Colab 一家的配额。
cd "${0:a:h}/.."
LOG=logs/wait_pool.log
POOL="${POOL_URL:-http://127.0.0.1:9700}"
INTERVAL="${INTERVAL:-1800}"

echo "[$(date '+%m-%d %H:%M')] 哨兵启动：等 GPU 池有可用 provider（$POOL）…" >> "$LOG"
while true; do
  OK=$(curl -s -m 10 "$POOL/status" | python3 -c "import json,sys; print('yes' if json.load(sys.stdin).get('ok') else 'no')" 2>/dev/null)
  if [ "$OK" = "yes" ]; then
    echo "[$(date '+%m-%d %H:%M')] ✅ 池子有可用 provider，起批次" >> "$LOG"
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -W ignore scripts/batch_csv.py >> "$LOG" 2>&1 &
    echo "[$(date '+%m-%d %H:%M')] 批次已起（pid $!），哨兵退出" >> "$LOG"
    exit 0
  fi
  echo "[$(date '+%m-%d %H:%M')] 仍无可用 provider，30min 后再探" >> "$LOG"
  sleep "$INTERVAL"
done

#!/bin/bash
# 实盘循环：每 5 分钟跑一步 bot.py --once。停止：kill $(cat run_live.pid)
cd /workspace/hormuz/bot
echo $$ > run_live.pid
export DRY_RUN=0 LIVE_TRADING=1 CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY HORMUZ_STATE=/workspace/hormuz/bot/state/live_state.json
export PYTHONUNBUFFERED=1
while true; do
  ts=$(date '+%Y-%m-%d %H:%M:%S %Z')
  echo "[heartbeat] $ts start bot.py --once" >> logs/live.out
  timeout -k 5 280 /usr/bin/python3 bot.py --once --params params.live.json >> logs/live.out 2>&1
  rc=$?
  ts=$(date '+%Y-%m-%d %H:%M:%S %Z')
  echo "[heartbeat] $ts done exit=$rc" >> logs/live.out
  sleep $(( 300 - $(date +%s) % 300 ))
done

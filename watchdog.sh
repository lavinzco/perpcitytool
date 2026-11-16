#!/bin/bash
# HORMUZ 看门狗（仅暂存：不要安装 cron、不要现在运行）。
# 若没有 run_live.sh 进程，用与现网相同的实盘环境变量 nohup 拉起；flock 防重复拉起。
set -u
BOT_DIR=/workspace/hormuz/bot
cd "$BOT_DIR" || exit 1
mkdir -p logs
LOG="$BOT_DIR/logs/watchdog.log"
ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

exec 9>>"$BOT_DIR/.watchdog.lock"
if ! flock -n 9; then
  echo "$(ts) skip: another watchdog holds the lock" >>"$LOG"
  exit 0
fi

# 已有循环就不拉起（匹配绝对路径，避免误伤本脚本）
if pgrep -f -- "/workspace/hormuz/bot/run_live.sh" >/dev/null 2>&1; then
  echo "$(ts) ok: run_live.sh already running" >>"$LOG"
  exit 0
fi

export DRY_RUN=0 LIVE_TRADING=1 CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY HORMUZ_STATE=/workspace/hormuz/bot/state/live_state.json
export PYTHONUNBUFFERED=1

# stdout/stderr 由 run_live.sh 自身写入 logs/live.out
nohup /bin/bash /workspace/hormuz/bot/run_live.sh >/dev/null 2>&1 &
pid=$!
echo "$(ts) started run_live.sh pid=$pid" >>"$LOG"

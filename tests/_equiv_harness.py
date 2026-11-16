"""在给定代码目录里跑一整段 DRY_RUN（mock 行情、HZ_NO_CHAIN），把每步决策和最终 state 输出为 JSON。
用法：python3 _equiv_harness.py <代码目录> <state 路径> [params.json]"""
import sys, os, json, datetime as dt, math
code, state = sys.argv[1], sys.argv[2]
params_path = sys.argv[3] if len(sys.argv) > 3 else None
os.environ.update(HORMUZ_STATE=state, HORMUZ_LOGDIR=os.path.dirname(state), DRY_RUN="1", HZ_NO_CHAIN="1", SIM_WALLET_USDC="144")
for k in ("LIVE_TRADING", "CONFIRM_LIVE"): os.environ.pop(k, None)
sys.path.insert(0, code)
from zoneinfo import ZoneInfo
import bot
from strategy import Params, Market
bot.setup_log()
p = Params.from_env(params_path)
HKT = ZoneInfo("Asia/Hong_Kong")
out = []
t = dt.datetime(2026, 11, 10, 0, 0, tzinfo=HKT)
end = t + dt.timedelta(days=3)
k = 0
while t < end:
    # 有涨有跌、第二天大跌触发止损、funding 有正有负
    mark = 7.0 * (1 + 0.04 * math.sin(k / 37)) * (0.82 if dt.datetime(2026, 11, 11, 10, tzinfo=HKT) <= t < dt.datetime(2026, 11, 11, 13, tzinfo=HKT) else 1)
    m = Market(mark=mark, index=7.05, funding_per_day=0.3 * math.sin(k / 50))
    d, _ = bot.step(p, t, market=(m, {"source": "t"}))
    out.append([t.isoformat(), d.action, d.reason, list(d.ids), round(d.margin, 9), d.blocked])
    t += dt.timedelta(minutes=5); k += 1
s = json.load(open(state))
print(json.dumps({"steps": out, "state": s}, ensure_ascii=False, sort_keys=True))

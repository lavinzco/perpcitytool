"""HORMUZ-SHIPS 日内只做多机器人（分批开平 + 保证金 30% 止损）。默认 DRY_RUN=1：只读行情和链上报价、模拟成交，绝不发交易。

实盘需要同时满足：DRY_RUN=0、LIVE_TRADING=1、CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY，
并在环境变量里提供 PRIVATE_KEY、RPC_URL（只由 live/trade.mjs 读取，本脚本不读取私钥）。
有 RPC_URL 时，DRY_RUN 也会用 trade.mjs 的只读接口（positions / quote_open / quote_close）拿真实报价。

用法：python3 bot.py --once        # 跑一步（适合 cron 每 5 分钟）
      python3 bot.py --loop 300    # 常驻，每 300 秒一步
      python3 bot.py --once --at 2026-11-10T07:00   # 注入时间（仅 DRY_RUN）
"""
from __future__ import annotations
import argparse, datetime as dt, fcntl, json, logging, os, subprocess, sys, time
from zoneinfo import ZoneInfo
from strategy import (Params, Lot, Decision, decide, stop_status, open_guards, model_impact_pct,
                      sim_open, sim_close_value, FEE_RATE, UTIL_FEE_PER_DAY)
import datafeed

HERE = os.path.dirname(os.path.abspath(__file__))
HKT = ZoneInfo("Asia/Hong_Kong")
STATE = os.environ.get("HORMUZ_STATE", os.path.join(HERE, "state", "state.json"))
LOGDIR = os.environ.get("HORMUZ_LOGDIR", os.path.join(HERE, "logs"))


def is_live() -> bool:
    return (os.environ.get("DRY_RUN", "1") == "0" and os.environ.get("LIVE_TRADING") == "1"
            and os.environ.get("CONFIRM_LIVE") == "I_UNDERSTAND_REAL_MONEY")


def chain_enabled() -> bool:
    """能否读链（只读报价/仓位）。HZ_NO_CHAIN=1 可强制用模型。"""
    return bool(os.environ.get("RPC_URL") and os.environ.get("PRIVATE_KEY")) and os.environ.get("HZ_NO_CHAIN") != "1"


def setup_log():
    os.makedirs(LOGDIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(LOGDIR, "bot.log")), logging.StreamHandler(sys.stdout)])
    logging.Formatter.converter = lambda *a: dt.datetime.now(HKT).timetuple()


def new_state():
    return {"day": None, "opened_notional": 0.0, "stopped": False, "last_action": None,
            "lots": [], "sim_pnl_usd": 0.0, "trades": []}


def load_state():
    if os.path.exists(STATE):
        s = json.load(open(STATE))
        if "lots" not in s:   # 旧版单仓位 state 迁移
            old = s.get("position")
            if old:
                raise RuntimeError("旧 state 里还有单仓位记录，先人工处理再跑新版")
            s = dict(new_state(), sim_pnl_usd=s.get("sim_pnl_usd", 0.0), trades=s.get("trades", []))
        return s
    return new_state()


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, STATE)


def _node(payload: dict, timeout=180) -> dict:
    r = subprocess.run(["node", os.path.join(HERE, "live", "trade.mjs")], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    if r.returncode != 0:
        raise RuntimeError(f"trade.mjs 失败: {r.stderr.strip()[-800:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def chain_read(payload: dict) -> dict:
    """只读调用（positions / quote_open / quote_close），不发交易。"""
    assert payload["action"] in ("positions", "quote_open", "quote_close")
    return _node(payload, timeout=120)


def live_call(payload: dict) -> dict:
    """实盘下单。只有 is_live() 为真才会走到这里。"""
    assert is_live(), "非实盘模式不应调用 live_call"
    return _node(payload)


def trading_day(now: dt.datetime, p: Params) -> str:
    return (now - dt.timedelta(hours=p.long_open_hour)).date().isoformat()


def lot_from_state(d: dict, today: str) -> Lot:
    return Lot(id=str(d["id"]), margin=d["margin"], notional=d["notional"], size=d["size"],
               entry_price=d["entry_price"], close_value=d.get("close_value", d["margin"]),
               day=d.get("day", today), overdue=d.get("day", today) != today)


def refresh_live(s, p, today, now):
    """实盘：以链上为准核对仓位（忽略 ignore 列表），返回 (lots, all_notional, wallet_usdc, complete)。"""
    r = chain_read({"action": "positions", "perp": datafeed.PERP, "ignore": p.ignore_position_ids})
    by_id = {str(l["id"]): l for l in s["lots"]}
    chain = {x["positionId"]: x for x in r["positions"] if not x["ignored"]}
    for gone in set(by_id) - set(chain):
        logging.warning(f"  state 里的仓位 {gone} 链上已不存在（被平或清算），移出 state")
        s["trades"].append(dict(id=gone, close=now.isoformat(), reason="链上已不存在", margin=by_id[gone]["margin"]))
        by_id.pop(gone)
    for pid, x in chain.items():
        if not x["isLong"]:
            logging.warning(f"  仓位 {pid} 是空单，bot 不管空单，请人工处理")
        d = by_id.get(pid)
        if d is None:
            logging.warning(f"  链上有 state 没记录的仓位 {pid}，按 bot 仓位接管（保证金按链上值+开仓费估算）")
            d = dict(id=pid, margin=x["marginOnChain"] + FEE_RATE * x["entryNotional"], day=today, opened=now.isoformat(), adopted=True)
            by_id[pid] = d
        d.update(notional=x["entryNotional"], size=x["size"], entry_price=x["entryPrice"],
                 margin_net=x["marginOnChain"], close_value=x["closeValue"],
                 funding_owed=x["fundingOwed"], util_owed=x["utilOwed"])
    s["lots"] = [by_id[k] for k in sorted(by_id, key=int)]
    lots = [lot_from_state(d, today) for d in s["lots"] if d.get("size", 0) > 0]
    return lots, r["totalNotionalAll"], r["usdc"], r["complete"]


def refresh_dry(s, p, today, now, m):
    """模拟：仓位来自 state；平仓价值用链上只读报价（有 RPC 时）或模型估算。"""
    ignored_notional, usdc, complete = 0.0, float(os.environ.get("SIM_WALLET_USDC", "100")), True
    if chain_enabled():
        try:
            r = chain_read({"action": "positions", "perp": datafeed.PERP, "ignore": p.ignore_position_ids})
            real = [x for x in r["positions"] if not x["ignored"]]
            if real:
                logging.warning(f"  DRY_RUN：钱包里有不在忽略列表的真实仓位 {[x['positionId'] for x in real]}，模拟不会碰它们")
            ignored_notional = r["totalNotionalAll"]
            complete = r["complete"]
            if "SIM_WALLET_USDC" not in os.environ:
                usdc = r["usdc"]
        except Exception as e:
            logging.warning(f"  读链上仓位失败，按模拟值继续：{e}")
    for d in s["lots"]:
        held = max((now - dt.datetime.fromisoformat(d["opened"])).total_seconds() / 86400, 0)
        owed = UTIL_FEE_PER_DAY * d["notional"] * held + m.funding_per_day * d["size"] * held
        imp = None
        if chain_enabled():
            try:
                q = chain_read({"action": "quote_close", "perp": datafeed.PERP, "perpDelta": str(int(round(d["size"] * 1e6)))})
                d["close_value"] = d["margin_net"] - owed + q["proceeds"] - d["notional"]; imp = q["impactPct"]
            except Exception as e:
                logging.warning(f"  平仓报价失败，用模型：{e}")
        if imp is None:
            d["close_value"] = sim_close_value(d["margin_net"], d["notional"], d["size"], m.mark,
                                               model_impact_pct(d["notional"], p), owed)
    usdc -= sum(d["margin"] for d in s["lots"])
    lots = [lot_from_state(d, today) for d in s["lots"]]
    return lots, ignored_notional + sum(d["notional"] for d in s["lots"]), usdc, complete


def quote_open(m, d: Decision, p: Params) -> dict:
    if chain_enabled():
        return chain_read({"action": "quote_open", "perp": datafeed.PERP, "margin": d.margin, "leverage": p.leverage})
    imp = model_impact_pct(d.notional, p)
    o = sim_open(m.mark, d.margin, p.leverage, imp)
    return dict(impactPct=imp, notional=o["notional"], size=o["size"], fillPrice=o["entry_price"],
                exceedsLiquidity=False, model=True)


def quote_close(m, lot: dict, p: Params) -> dict:
    if chain_enabled():
        return chain_read({"action": "quote_close", "perp": datafeed.PERP, "perpDelta": str(int(round(lot["size"] * 1e6)))})
    imp = model_impact_pct(lot["notional"], p)
    return dict(impactPct=imp, proceeds=lot["size"] * m.mark * (1 - imp / 100) * (1 - FEE_RATE), model=True)


def step(p: Params, now: dt.datetime | None = None, market=None):
    now = now or dt.datetime.now(HKT)
    hour = now.hour + now.minute / 60 + now.second / 3600
    live = is_live()
    s = load_state()
    today = trading_day(now, p)
    if s.get("day") != today:
        s.update(day=today, opened_notional=0.0, stopped=False)
    m, meta = market or datafeed.snapshot()
    if live:
        lots, all_notional, usdc, complete = refresh_live(s, p, today, now)
    else:
        lots, all_notional, usdc, complete = refresh_dry(s, p, today, now, m)
    last = s.get("last_action")
    since = (now - dt.datetime.fromisoformat(last)).total_seconds() / 60 if last else 1e9
    d = decide(hour, m, lots, p, stopped=s["stopped"], opened_today=s["opened_notional"],
               minutes_since_last=since, all_notional=all_notional, wallet_usdc=usdc)
    if d.action == "open" and not complete:
        d = Decision("hold", "链上仓位未扫全（balanceOf 与找到的数量不一致），不开新仓")
    st = stop_status(lots, p.stop_loss_margin_pct)
    dev = (m.mark / m.index - 1) * 100 if m.index else 0
    logging.info(f"[{'LIVE' if live else 'DRY_RUN'}] {now:%m-%d %H:%M} HKT 现价 {m.mark:.3f} 指数 {m.index:.3f} 偏离 {dev:+.1f}% "
                 f"funding {m.funding_per_day:+.3f}$/单位/天 | 仓位 {len(lots)} 批 名义 {sum(l.notional for l in lots):.1f} "
                 f"保证金 {st['margin']:.2f} 估值 {st['value']:.2f} 亏损 {st['loss_pct']:+.1f}% | 今日已开 {s['opened_notional']:.1f} "
                 f"钱包 USDC {usdc:.2f} → {d.action} {d.reason} {'；'.join(d.blocked)}")
    if d.action == "open":
        _do_open(s, p, m, d, now, today, live, all_notional, usdc)
    elif d.action == "close":
        if d.stop and d.reason.startswith("止损"):
            s["stopped"] = True
        _do_close(s, p, m, d, now, live)
    save_state(s)
    return d, m


def _do_open(s, p, m, d, now, today, live, all_notional, usdc):
    try:
        q = quote_open(m, d, p)
    except Exception as e:
        logging.warning(f"  开仓报价失败，这步不下：{e}"); return
    if q.get("exceedsLiquidity") or q["impactPct"] > d.max_impact_pct:
        logging.info(f"  报价价格冲击 {q['impactPct']:.2f}% > {d.max_impact_pct}%（或深度不足），这步不下，等下一步"); return
    # 用报价的实际名义再核一遍上限
    again = open_guards(m, p, notional=q["notional"], margin=d.margin, all_notional=all_notional,
                        opened_today=s["opened_notional"], wallet_usdc=usdc)
    if again:
        logging.info(f"  按报价名义复核被拦：{'；'.join(again)}"); return
    if live:
        try:
            r = live_call({"action": "open", "isLong": True, "margin": d.margin, "leverage": p.leverage,
                           "slippagePct": d.slippage_pct, "maxImpactPct": d.max_impact_pct,
                           "maxTotalNotional": p.max_notional_usd, "minUsdc": p.usdc_buffer_usd,
                           "perp": datafeed.PERP})
        except Exception as e:
            logging.warning(f"  开仓失败，这步不下：{e}"); return
        lot = dict(id=r["positionId"], tx=r["txHash"], notional=float(r["notional"]), size=float(r["size"]),
                   entry_price=float(r["rawFillPrice"]), impact_pct=float(r["impactPct"]))
    else:
        lot = dict(id=f"sim-{now:%m%d%H%M%S}", simulated=True, notional=float(q["notional"]), size=float(q["size"]),
                   entry_price=float(q["fillPrice"]), impact_pct=float(q["impactPct"]))
    lot.update(margin=d.margin, margin_net=d.margin - FEE_RATE * lot["notional"], day=today, opened=now.isoformat(),
               close_value=d.margin - 2 * FEE_RATE * lot["notional"])
    s["lots"].append(lot); s["opened_notional"] += lot["notional"]; s["last_action"] = now.isoformat()
    logging.info(f"  开多 #{lot['id']} 保证金 {d.margin:.2f} 名义 {lot['notional']:.2f} 均价 {lot['entry_price']:.4f} "
                 f"冲击 {lot['impact_pct']:.2f}% 今日累计 {s['opened_notional']:.1f}/{p.target_notional_usd:g} {lot.get('tx', '')}")


def _do_close(s, p, m, d, now, live):
    for pid in d.ids:
        lot = next((x for x in s["lots"] if str(x["id"]) == str(pid)), None)
        if lot is None:
            continue
        if str(pid).isdigit() and int(pid) in p.ignore_position_ids:
            logging.error(f"  {pid} 在忽略列表里，绝不平"); continue
        try:
            q = quote_close(m, lot, p)
        except Exception as e:
            logging.warning(f"  平仓报价失败 #{pid}：{e}"); continue
        if q["impactPct"] > d.max_impact_pct:
            logging.info(f"  #{pid} 平仓冲击 {q['impactPct']:.2f}% > {d.max_impact_pct}%，这步不平"); continue
        value = lot["close_value"]
        if live:
            try:
                r = live_call({"action": "close", "positionId": str(pid), "slippagePct": d.slippage_pct,
                               "maxImpactPct": d.max_impact_pct, "perp": datafeed.PERP})
            except Exception as e:
                logging.warning(f"  平仓失败 #{pid}：{e}"); continue
            extra = {"tx": r["txHash"]}
        else:
            extra = {"simulated": True}
        net = value - lot["margin"]
        s["sim_pnl_usd"] = s.get("sim_pnl_usd", 0.0) + net
        s["trades"].append(dict(id=pid, open=lot["opened"], close=now.isoformat(), reason=d.reason, margin=lot["margin"],
                                notional=round(lot["notional"], 4), entry=lot["entry_price"], close_impact_pct=round(q["impactPct"], 3),
                                est_value=round(value, 4), net=round(net, 4), **extra))
        s["lots"] = [x for x in s["lots"] if str(x["id"]) != str(pid)]
        s["last_action"] = now.isoformat()
        logging.info(f"  平多 #{pid} 名义 {lot['notional']:.2f} 冲击 {q['impactPct']:.2f}% 估算回收 {value:.2f} 净 {net:+.2f} "
                     f"累计 {s['sim_pnl_usd']:+.2f} {extra.get('tx', '')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true"); ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--params", default=None, help="JSON 参数文件")
    ap.add_argument("--at", default=None, help="注入香港时间（仅 DRY_RUN），如 2026-11-10T07:00")
    a = ap.parse_args()
    setup_log()
    p = Params.from_env(a.params)
    if os.environ.get("DRY_RUN", "1") == "0" and not is_live():
        logging.error("DRY_RUN=0 但未同时设置 LIVE_TRADING=1 与 CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY，拒绝实盘，退出。")
        sys.exit(2)
    if is_live() and not (os.environ.get("PRIVATE_KEY") and os.environ.get("RPC_URL")):
        logging.error("实盘需要环境变量 PRIVATE_KEY 与 RPC_URL"); sys.exit(2)
    if a.at and is_live():
        logging.error("--at 只能用于 DRY_RUN"); sys.exit(2)
    at = dt.datetime.fromisoformat(a.at).replace(tzinfo=HKT) if a.at else None
    lock = open(os.path.join(HERE, ".bot.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.warning("已有实例在运行，跳过"); return
    while True:
        try:
            step(p, at)
        except Exception as e:
            logging.exception(f"本步出错，不下单：{e}")
        if not a.loop:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()

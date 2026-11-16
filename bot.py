"""HORMUZ-SHIPS 日内多空机器人（分批开平 + 保证金 30% 止损）。默认 DRY_RUN=1：只读行情和链上报价、模拟成交，绝不发交易。
空单腿只在 short_enabled=true 时启用；关闭时行为与纯多头版本一致。

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
from strategy import (Params, Lot, Decision, decide, stop_status, open_guards, model_impact_pct, in_window,
                      sim_open, sim_close_value, close_proceeds, entry_usd_leg,
                      FEE_RATE, UTIL_FEE_PER_DAY, SHORT_UTIL_FEE_PER_DAY)
import datafeed

HERE = os.path.dirname(os.path.abspath(__file__))
HKT = ZoneInfo("Asia/Hong_Kong")
STATE = os.environ.get("HORMUZ_STATE", os.path.join(HERE, "state", "state.json"))
LOGDIR = os.environ.get("HORMUZ_LOGDIR", os.path.join(HERE, "logs"))
LAST_DECISIONS: dict = {}   # 最近一步每条腿的决策（测试/模拟用）
# 每条腿在 state 里用的键：多头沿用旧键名，保证旧 state 文件直接可用
KEYS = {"long": dict(day="day", opened="opened_notional", stopped="stopped", last="last_action"),
        "short": dict(day="short_day", opened="short_opened_notional", stopped="short_stopped", last="short_last_action")}


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


def trading_day(now: dt.datetime, p: Params, side: str = "long") -> str:
    """交易日 = 开仓时刻所在日期。空单 19:00 开、次日 06:00 前平，整段都算 19:00 那天。"""
    h = p.short_open_hour if side == "short" else p.long_open_hour
    return (now - dt.timedelta(hours=h)).date().isoformat()


def lot_side(d: dict) -> str:
    return d.get("side", "long")


def lot_from_state(d: dict, today: str, short_today: str | None = None) -> Lot:
    side = lot_side(d)
    ref = short_today if (side == "short" and short_today is not None) else today
    return Lot(id=str(d["id"]), margin=d["margin"], notional=d["notional"], size=d["size"],
               entry_price=d["entry_price"], close_value=d.get("close_value", d["margin"]),
               day=d.get("day", ref), overdue=d.get("day", ref) != ref, side=side)


def short_active(s, p) -> bool:
    """空单腿要不要跑：启用了，或者 state 里还有空单（关闭后也要把剩下的平掉）。"""
    return p.short_enabled or any(lot_side(d) == "short" for d in s["lots"])


def refresh_live(s, p, today, now, short_today=None):
    """实盘：以链上为准核对仓位（忽略 ignore 列表），返回 (lots, all_notional, wallet_usdc, complete)。
    all_notional = 钱包在该市场全部多空仓位名义绝对值（含 ignore 列表）。"""
    short_today = short_today or trading_day(now, p, "short")
    r = chain_read({"action": "positions", "perp": datafeed.PERP, "ignore": p.ignore_position_ids})
    by_id = {str(l["id"]): l for l in s["lots"]}
    chain = {x["positionId"]: x for x in r["positions"] if not x["ignored"]}
    for gone in set(by_id) - set(chain):
        logging.warning(f"  state 里的仓位 {gone} 链上已不存在（被平或清算），移出 state")
        s["trades"].append(dict(id=gone, close=now.isoformat(), reason="链上已不存在", margin=by_id[gone]["margin"]))
        by_id.pop(gone)
    for pid, x in chain.items():
        d = by_id.get(pid)
        if not x["isLong"]:
            if d is None and not p.short_enabled:
                logging.warning(f"  仓位 {pid} 是空单，空单腿未启用，bot 不管，请人工处理"); continue
            if d is not None and lot_side(d) != "short":
                logging.error(f"  仓位 {pid} 链上是空单但 state 记成多单，按链上改为空单")
        elif d is not None and lot_side(d) == "short":
            logging.error(f"  仓位 {pid} 链上是多单但 state 记成空单，按链上改为多单")
        if d is None:
            side_day = today if x["isLong"] else short_today
            logging.warning(f"  链上有 state 没记录的{'多' if x['isLong'] else '空'}单 {pid}，按 bot 仓位接管（保证金按链上值+开仓费估算）")
            d = dict(id=pid, margin=x["marginOnChain"] + FEE_RATE * x["entryNotional"], day=side_day, opened=now.isoformat(), adopted=True)
            by_id[pid] = d
        if x["isLong"]:
            d.pop("side", None)
        else:
            d["side"] = "short"
        d.update(notional=x["entryNotional"], size=abs(x["size"]), entry_price=x["entryPrice"],
                 margin_net=x["marginOnChain"], close_value=x["closeValue"],
                 funding_owed=x["fundingOwed"], util_owed=x["utilOwed"])
    s["lots"] = [by_id[k] for k in sorted(by_id, key=int)]
    lots = [lot_from_state(d, today, short_today) for d in s["lots"] if d.get("size", 0) > 0]
    return lots, r["totalNotionalAll"], r["usdc"], r["complete"]


def refresh_dry(s, p, today, now, m, short_today=None):
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
        side = lot_side(d)
        held = max((now - dt.datetime.fromisoformat(d["opened"])).total_seconds() / 86400, 0)
        signed = -d["size"] if side == "short" else d["size"]           # funding>0 多头付、空头收
        util = SHORT_UTIL_FEE_PER_DAY if side == "short" else UTIL_FEE_PER_DAY
        owed = util * d["notional"] * held + m.funding_per_day * signed * held
        imp = None
        if chain_enabled():
            try:
                q = chain_read(_close_quote_payload(d))
                d["close_value"] = d["margin_net"] - owed + q["proceeds"] + entry_usd_leg(d["notional"], side); imp = q["impactPct"]
            except Exception as e:
                logging.warning(f"  平仓报价失败，用模型：{e}")
        if imp is None:
            d["close_value"] = sim_close_value(d["margin_net"], d["notional"], d["size"], m.mark,
                                               model_impact_pct(d["notional"], p), owed, side=side)
    usdc -= sum(d["margin"] for d in s["lots"])
    lots = [lot_from_state(d, today, short_today) for d in s["lots"]]
    return lots, ignored_notional + sum(d["notional"] for d in s["lots"]), usdc, complete


def _close_quote_payload(lot: dict) -> dict:
    """平仓报价请求。perpDelta = 原仓位带符号的单位×1e6（多为正、空为负），与旧接口（正数=多）兼容。"""
    signed = -abs(lot["size"]) if lot_side(lot) == "short" else abs(lot["size"])
    pl = {"action": "quote_close", "perp": datafeed.PERP, "perpDelta": str(int(round(signed * 1e6)))}
    if lot_side(lot) == "short":
        pl["isLong"] = False
    return pl


def quote_open(m, d: Decision, p: Params) -> dict:
    if chain_enabled():
        pl = {"action": "quote_open", "perp": datafeed.PERP, "margin": d.margin, "leverage": p.leverage}
        if d.side == "short":
            pl["isLong"] = False
        q = chain_read(pl)
        q["size"] = abs(float(q["size"]))       # 空单链上 perpDelta 为负，state 里一律存正数
        return q
    imp = model_impact_pct(d.notional, p)
    o = sim_open(m.mark, d.margin, p.leverage, imp, side=d.side)
    return dict(impactPct=imp, notional=o["notional"], size=o["size"], fillPrice=o["entry_price"],
                exceedsLiquidity=False, model=True)


def quote_close(m, lot: dict, p: Params) -> dict:
    """proceeds 带符号：平多为卖出所得（正），平空为买回花费（负）。"""
    if chain_enabled():
        return chain_read(_close_quote_payload(lot))
    imp = model_impact_pct(lot["notional"], p)
    return dict(impactPct=imp, proceeds=close_proceeds(lot["size"], m.mark, imp, lot_side(lot)), model=True)


def _since(s, key, now):
    last = s.get(key)
    return (now - dt.datetime.fromisoformat(last)).total_seconds() / 60 if last else 1e9


def _is_stop_close(d: Decision) -> bool:
    """亏损达保证金 30% 的止损全平（含止损后继续平剩余）。普通到点平仓/强平不算。"""
    return d.action == "close" and d.stop and d.reason.startswith("止损")


def _step_stale(p: Params, wall_start: float) -> tuple[bool, float]:
    """墙钟距 step 起始是否已超过 stale_abort_seconds。返回 (是否过期, 已过秒数)。"""
    elapsed = time.time() - wall_start
    limit = float(getattr(p, "stale_abort_seconds", 120) or 0)
    return (limit > 0 and elapsed > limit), elapsed


def step(p: Params, now: dt.datetime | None = None, market=None):
    wall_start = time.time()          # 墙钟起点；与下面取死的 now 分开，冻结恢复后才能判断过期
    now = now or dt.datetime.now(HKT)
    hour = now.hour + now.minute / 60 + now.second / 3600
    live = is_live()
    s = load_state()
    today = trading_day(now, p)
    if s.get("day") != today:
        s.update(day=today, opened_notional=0.0, stopped=False)
    sides = ["long"]
    sday = trading_day(now, p, "short")
    if short_active(s, p):
        sides.append("short")
        if s.get("short_day") != sday:      # 空单交易日从 19:00 算，跨午夜不重置
            s.update(short_day=sday, short_opened_notional=0.0, short_stopped=False)
    m, meta = market or datafeed.snapshot()
    if live:
        lots, all_notional, usdc, complete = refresh_live(s, p, today, now, sday)
    else:
        lots, all_notional, usdc, complete = refresh_dry(s, p, today, now, m, sday)
    dec = {}
    for side in sides:
        k = KEYS[side]
        if side == "long":   # 同一日历日另一条腿已开（只给 daily_total_open_cap_usd 用）
            other = s.get("short_opened_notional", 0.0) if s.get("short_day") == today else 0.0
        else:
            other = s["opened_notional"] if s.get("day") == sday else 0.0
        d = decide(hour, m, lots, p, stopped=s[k["stopped"]], opened_today=s[k["opened"]],
                   minutes_since_last=_since(s, k["last"], now), all_notional=all_notional, wallet_usdc=usdc,
                   side=side, other_opened_today=other)
        if d.action == "open" and not complete:
            d = Decision("hold", "链上仓位未扫全（balanceOf 与找到的数量不一致），不开新仓", side=side)
        d._other = other
        dec[side] = d
    LAST_DECISIONS.clear(); LAST_DECISIONS.update(dec)
    # 读链完成后、下单前：墙钟已过期则放弃普通开/平，止损仍执行。报价在 _do_* 里，那里会再核一次。
    stale, elapsed = _step_stale(p, wall_start)
    if stale:
        would_trade = any(dec[x].action in ("open", "close") and not _is_stop_close(dec[x]) for x in sides)
        if would_trade:
            logging.warning(f"步骤过期 {elapsed:.0f} 秒，放弃下单，留给下一步")
            for side in sides:
                d = dec[side]
                if d.action in ("open", "close") and not _is_stop_close(d):
                    dec[side] = Decision("hold", f"stale（步骤过期 {elapsed:.0f} 秒）", side=side)
            LAST_DECISIONS.clear(); LAST_DECISIONS.update(dec)
    dev = (m.mark / m.index - 1) * 100 if m.index else 0
    for side in sides:
        d, k = dec[side], KEYS[side]
        sl = [l for l in lots if l.side == side]
        st = stop_status(sl, p.stop_loss_margin_pct)
        head = (f"[{'LIVE' if live else 'DRY_RUN'}] {now:%m-%d %H:%M} HKT 现价 {m.mark:.3f} 指数 {m.index:.3f} 偏离 {dev:+.1f}% "
                f"funding {m.funding_per_day:+.3f}$/单位/天 | ") if side == "long" else "  └ 空单腿 | "
        logging.info(f"{head}仓位 {len(sl)} 批 名义 {sum(l.notional for l in sl):.1f} "
                     f"保证金 {st['margin']:.2f} 估值 {st['value']:.2f} 亏损 {st['loss_pct']:+.1f}% | "
                     f"{'今日' if side == 'long' else '空单当期'}已开 {s[k['opened']]:.1f} "
                     f"钱包 USDC {usdc:.2f} → {d.action} {d.reason} {'；'.join(d.blocked)}")
    # 执行：先平（各条腿的平仓/止损都做），没有平仓才开（只开一条腿）
    closes = [dec[x] for x in sides if dec[x].action == "close"]
    opens = [dec[x] for x in sides if dec[x].action == "open"]
    for d in closes:
        if d.stop and d.reason.startswith("止损"):
            s[KEYS[d.side]["stopped"]] = True
        _do_close(s, p, m, d, now, live, wall_start=wall_start, allow_stale=_is_stop_close(d))
    if not closes and opens:
        d = opens[0]
        _do_open(s, p, m, d, now, today if d.side == "long" else sday, live, all_notional, usdc, wall_start=wall_start)
    save_state(s)
    acted = closes[0] if closes else (opens[0] if opens else None)
    if acted is None:
        acted = dec["long"]
        if "short" in dec and (any(l.side == "short" for l in lots) or
                               in_window(hour, p.short_open_hour, p.short_close_hour)):
            acted = dec["short"]
    return acted, m


def _do_open(s, p, m, d, now, today, live, all_notional, usdc, wall_start=None):
    short = d.side == "short"
    k = KEYS[d.side]
    other = getattr(d, "_other", 0.0)
    try:
        q = quote_open(m, d, p)
    except Exception as e:
        logging.warning(f"  开仓报价失败，这步不下：{e}"); return
    if q.get("exceedsLiquidity") or q["impactPct"] > d.max_impact_pct:
        logging.info(f"  报价价格冲击 {q['impactPct']:.2f}% > {d.max_impact_pct}%（或深度不足），这步不下，等下一步"); return
    # 用报价的实际名义再核一遍上限
    again = open_guards(m, p, notional=q["notional"], margin=d.margin, all_notional=all_notional,
                        opened_today=s[k["opened"]], wallet_usdc=usdc, side=d.side, other_opened_today=other)
    if again:
        logging.info(f"  按报价名义复核被拦：{'；'.join(again)}"); return
    if wall_start is not None:
        stale, elapsed = _step_stale(p, wall_start)
        if stale:
            logging.warning(f"步骤过期 {elapsed:.0f} 秒，放弃下单，留给下一步"); return
    if live:
        payload = {"action": "open", "isLong": not short, "margin": d.margin, "leverage": p.leverage,
                   "slippagePct": d.slippage_pct, "maxImpactPct": d.max_impact_pct,
                   "maxTotalNotional": p.max_notional_usd, "minUsdc": p.usdc_buffer_usd,
                   "perp": datafeed.PERP}
        if short:
            payload.update(allowShort=True, side="short")   # trade.mjs 只有显式放行才接受空单
        try:
            r = live_call(payload)
        except Exception as e:
            logging.warning(f"  开仓失败，这步不下：{e}"); return
        lot = dict(id=r["positionId"], tx=r["txHash"], notional=float(r["notional"]), size=abs(float(r["size"])),
                   entry_price=float(r["rawFillPrice"]), impact_pct=float(r["impactPct"]))
    else:
        lot = dict(id=f"sim-{now:%m%d%H%M%S}", simulated=True, notional=float(q["notional"]), size=abs(float(q["size"])),
                   entry_price=float(q["fillPrice"]), impact_pct=float(q["impactPct"]))
    if short:
        lot["side"] = "short"
    lot.update(margin=d.margin, margin_net=d.margin - FEE_RATE * lot["notional"], day=today, opened=now.isoformat(),
               close_value=d.margin - 2 * FEE_RATE * lot["notional"])
    s["lots"].append(lot); s[k["opened"]] += lot["notional"]; s[k["last"]] = now.isoformat()
    target = p.leg(d.side).target_notional_usd
    logging.info(f"  开{'空' if short else '多'} #{lot['id']} 保证金 {d.margin:.2f} 名义 {lot['notional']:.2f} 均价 {lot['entry_price']:.4f} "
                 f"冲击 {lot['impact_pct']:.2f}% {'空单当期' if short else '今日'}累计 {s[k['opened']]:.1f}/{target:g} {lot.get('tx', '')}")


def _do_close(s, p, m, d, now, live, wall_start=None, allow_stale=False):
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
        if wall_start is not None and not allow_stale:
            stale, elapsed = _step_stale(p, wall_start)
            if stale:
                logging.warning(f"步骤过期 {elapsed:.0f} 秒，放弃下单，留给下一步"); continue
        value = lot["close_value"]
        short = lot_side(lot) == "short"
        if live:
            payload = {"action": "close", "positionId": str(pid), "slippagePct": d.slippage_pct,
                       "maxImpactPct": d.max_impact_pct, "perp": datafeed.PERP}
            if short:
                payload["side"] = "short"     # trade.mjs 会核对链上方向
            try:
                r = live_call(payload)
            except Exception as e:
                logging.warning(f"  平仓失败 #{pid}：{e}"); continue
            extra = {"tx": r["txHash"]}
        else:
            extra = {"simulated": True}
        net = value - lot["margin"]
        s["sim_pnl_usd"] = s.get("sim_pnl_usd", 0.0) + net
        if short:
            extra["side"] = "short"
        s["trades"].append(dict(id=pid, open=lot["opened"], close=now.isoformat(), reason=d.reason, margin=lot["margin"],
                                notional=round(lot["notional"], 4), entry=lot["entry_price"], close_impact_pct=round(q["impactPct"], 3),
                                est_value=round(value, 4), net=round(net, 4), **extra))
        s["lots"] = [x for x in s["lots"] if str(x["id"]) != str(pid)]
        s[KEYS[lot_side(lot)]["last"]] = now.isoformat()
        logging.info(f"  平{'空' if short else '多'} #{pid} 名义 {lot['notional']:.2f} 冲击 {q['impactPct']:.2f}% 估算回收 {value:.2f} 净 {net:+.2f} "
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

"""空单腿测试：时段、跨日、上限合计、止损、追价、funding 方向、short_enabled=false 时与旧行为一致。"""
import os, sys, json, random, tempfile, subprocess, unittest, datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp(); os.environ.setdefault("HORMUZ_STATE", os.path.join(tmp, "s.json")); os.environ.setdefault("HORMUZ_LOGDIR", tmp)
import bot
from strategy import (Params, Market, Lot, decide, open_guards, stop_drop_pct, sim_open, sim_close_value,
                      close_proceeds, FEE_RATE)
HKT = ZoneInfo("Asia/Hong_Kong")
LIVE = {"DRY_RUN": "0", "LIVE_TRADING": "1", "CONFIRM_LIVE": "I_UNDERSTAND_REAL_MONEY"}
PS = dict(long_close_hour=16, short_enabled=True)          # 用户方案：多 7→16，空 19→次日 5
M = Market(mark=7.0, index=7.0, funding_per_day=0.0)


def T(h, m=0, day=10):
    return dt.datetime(2026, 11, day, h, m, tzinfo=HKT)


def SP(**kw):
    p = Params(**dict(PS, **kw)); p.validate(); return p


def slot(i, margin=25.0, notional=50.0, value=None, overdue=False):
    return Lot(id=f"s{i}", margin=margin, notional=notional, size=notional / 7, entry_price=7.0,
               close_value=margin - 1 if value is None else value, overdue=overdue, side="short")


def DS(hour, lots=(), p=None, m=M, **kw):
    a = dict(stopped=False, opened_today=0.0, minutes_since_last=1e9, all_notional=sum(l.notional for l in lots),
             wallet_usdc=1000.0, side="short")
    a.update(kw)
    return decide(hour, m, list(lots), p or SP(), **a)


class ShortParams(unittest.TestCase):
    def test_defaults_off(self):
        p = Params(); self.assertFalse(p.short_enabled)
        self.assertEqual((p.short_open_hour, p.short_close_hour, p.short_target_notional_usd,
                          p.short_entry_grace_hours, p.short_close_force_after_hours, p.daily_total_open_cap_usd),
                         (19, 5, 200, 1, 1, 0))

    def test_validation(self):
        for kw in (dict(short_target_notional_usd=201), dict(short_open_hour=5), dict(short_open_hour=24),
                   dict(long_close_hour=20),                       # 多单 20:00 平 → 与 19:00 开空重叠
                   dict(daily_total_open_cap_usd=401), dict(daily_total_open_cap_usd=-1)):
            with self.assertRaises(ValueError, msg=kw): SP(**kw)
        Params(long_close_hour=20).validate()                       # 不开空时旧参数照常通过

    def test_example_files(self):
        p = Params.from_env(os.path.join(HERE, "params.live_short.example.json"))
        self.assertTrue(p.short_enabled); self.assertEqual(p.long_close_hour, 16)


class ShortSchedule(unittest.TestCase):
    def test_full_cycle_crosses_midnight(self):
        """每 5 分钟一步：19:00 起 4 批开满 200，持仓过夜，05:00 起每 10 分钟平一批。"""
        p = SP(); lots = []; opened = 0.0; last = None; log = []
        t = 18.0
        while t < 18.0 + 13:
            h = t % 24
            since = 1e9 if last is None else (t - last) * 60
            d = DS(h, lots, p, opened_today=opened, minutes_since_last=since)
            self.assertEqual(d.side, "short")
            if d.action == "open":
                lots.append(slot(len(log), d.margin, d.notional)); opened += d.notional; last = t; log.append(("open", h))
            elif d.action == "close":
                lots = [l for l in lots if l.id not in d.ids]; last = t; log.append(("close", h))
            t = round(t + 5 / 60, 6)
        opens = [x for a, x in log if a == "open"]; closes = [x for a, x in log if a == "close"]
        self.assertEqual(len(opens), 4); self.assertAlmostEqual(opened, 200)
        self.assertTrue(all(19 <= x < 20 for x in opens), opens)
        self.assertEqual(len(closes), 4); self.assertTrue(all(5 <= x < 6 for x in closes), closes)
        self.assertEqual(lots, [])

    def test_no_open_outside_window_or_after_grace(self):
        self.assertEqual(DS(18.9).action, "hold")
        self.assertEqual(DS(19.0).action, "open")
        self.assertEqual(DS(20.0, [slot(1)], opened_today=50).action, "open")      # 宽限 1 小时（含边界，与多单 9:00 一致）
        self.assertEqual(DS(20.1, [slot(1)], opened_today=50).action, "hold")
        self.assertEqual(DS(23.5, [slot(1)], opened_today=50).action, "hold")      # 过夜持有，不平
        self.assertEqual(DS(2.0, [slot(1)], opened_today=50).action, "hold")

    def test_close_then_force_at_6(self):
        lots = [slot(1, notional=50), slot(2, notional=40)]
        d = DS(5.0, lots); self.assertEqual((d.action, d.ids, d.stop), ("close", ["s1"], False))
        self.assertEqual(DS(5.05, lots, minutes_since_last=3).action, "hold")
        d = DS(6.0, lots, minutes_since_last=3)
        self.assertEqual(d.action, "close"); self.assertTrue(d.stop); self.assertEqual(d.max_impact_pct, Params().stop_max_impact_pct)

    def test_disabled_never_opens_but_still_closes_leftover(self):
        p = Params(long_close_hour=16)
        self.assertEqual(DS(19.0, p=p).action, "hold")
        self.assertEqual(DS(5.0, [slot(1)], p=p).action, "close")

    def test_legs_ignore_each_others_lots(self):
        long_lot = Lot(id="L", margin=25, notional=50, size=7, entry_price=7, close_value=5)    # 多单亏损严重
        d = DS(19.0, [long_lot], all_notional=50)
        self.assertEqual(d.action, "open")                                                    # 空单腿不因多单止损而平/停
        d = decide(19.0, M, [slot(1, value=5)], SP(), stopped=False, opened_today=0, minutes_since_last=1e9,
                   all_notional=50, wallet_usdc=1000)                                         # 多单腿看不到空单
        self.assertEqual(d.action, "hold")


class ShortGuards(unittest.TestCase):
    def g(self, m, **kw):
        a = dict(notional=50, margin=25, all_notional=0, opened_today=0, wallet_usdc=1000, side="short"); a.update(kw)
        return open_guards(m, SP(), **a)

    def test_chase_symmetric(self):
        self.assertTrue(any("不追空" in x for x in self.g(Market(6.6, 7.0))))      # mark 低于指数 5.7%
        self.assertEqual(self.g(Market(6.7, 7.0)), [])                              # 低 4.3%：可以
        self.assertEqual(self.g(Market(7.5, 7.0)), [])                              # mark 高于指数：空单不受追多限制
        self.assertTrue(any("不追多" in x for x in self.g(Market(7.5, 7.0), side="long")))

    def test_funding_direction(self):
        # funding_per_day < 0：空头付钱
        self.assertTrue(any("空头需付" in x for x in self.g(Market(7, 7, funding_per_day=-0.5))))   # 7.1%/天
        self.assertEqual(self.g(Market(7, 7, funding_per_day=-0.35)), [])                          # 5%/天
        self.assertEqual(self.g(Market(7, 7, funding_per_day=+0.8)), [])                           # 多头付：空单不拦
        self.assertTrue(any("多头需付" in x for x in self.g(Market(7, 7, funding_per_day=+0.8), side="long")))

    def test_caps(self):
        self.assertTrue(any("总仓位名义" in x for x in self.g(M, all_notional=260)))           # 多空 + 117 合计 >300
        self.assertTrue(any("超过目标" in x for x in self.g(M, opened_today=160)))
        self.assertTrue(any("池子剩余可开空" in x for x in self.g(Market(7, 7, capacity_long_usd=1e9, capacity_short_usd=30))))
        self.assertTrue(any("钱包" in x for x in self.g(M, wallet_usdc=25)))

    def test_daily_total_cap_configurable(self):
        p = SP(daily_total_open_cap_usd=300)
        a = dict(notional=50, margin=25, all_notional=0, wallet_usdc=1000, side="short")
        self.assertEqual(open_guards(M, p, opened_today=50, other_opened_today=200, **a), [])
        self.assertTrue(any("合计" in x for x in open_guards(M, p, opened_today=100, other_opened_today=200, **a)))
        self.assertEqual(open_guards(M, SP(), opened_today=150, other_opened_today=200, **a), [])   # 默认 0 = 各自 ≤200

    def test_property_short_open_respects_caps(self):
        rnd = random.Random(11)
        for _ in range(4000):
            p = SP(leverage=rnd.choice([2, 3, 4]), daily_total_open_cap_usd=rnd.choice([0, 250, 400]))
            lots = [slot(i, 25, rnd.uniform(5, 80)) for i in range(rnd.randint(0, 5))]
            all_n = sum(l.notional for l in lots) + rnd.choice([0, 30, 150]); opened = rnd.uniform(0, 220)
            other = rnd.uniform(0, 200)
            m = Market(rnd.uniform(5, 10), rnd.uniform(5, 10), rnd.uniform(-1, 1), float("inf"), rnd.choice([float("inf"), 30]))
            d = DS(rnd.uniform(0, 24), lots, p, m=m, stopped=rnd.random() < .2, opened_today=opened,
                   minutes_since_last=rnd.uniform(0, 30), all_notional=all_n, wallet_usdc=rnd.uniform(0, 150),
                   other_opened_today=other)
            if d.action == "open":
                self.assertLessEqual(d.margin, 100 + 1e-9); self.assertLessEqual(all_n + d.notional, 300 + 1e-9)
                self.assertLessEqual(opened + d.notional, 200 * 1.01 + 1e-9)
                if p.daily_total_open_cap_usd:
                    self.assertLessEqual(opened + other + d.notional, p.daily_total_open_cap_usd * 1.01 + 1e-9)
                self.assertGreaterEqual(-(m.mark / m.index - 1) * 100, -1e9)
                self.assertLessEqual(-(m.mark / m.index - 1) * 100, p.max_chase_pct + 1e-9)


class ShortStopMath(unittest.TestCase):
    def test_stop_aggregate_and_leg_only(self):
        lots = [slot(1, value=17.5), slot(2, value=17.5)]                    # 亏 30%
        d = DS(23, lots); self.assertEqual(d.action, "close"); self.assertTrue(d.stop); self.assertEqual(set(d.ids), {"s1", "s2"})
        self.assertEqual(DS(23, [slot(1, value=17.6), slot(2, value=17.5)]).action, "hold")   # 29.8%
        self.assertEqual(DS(19.2, [], stopped=True).action, "hold")           # 本期已止损不再开

    def test_close_value_short_sign(self):
        o = sim_open(7.0, 25, 2, 0.0, side="short")
        self.assertAlmostEqual(25 - sim_close_value(o["margin_net"], o["notional"], o["size"], 7.0, 0.0, side="short"),
                               2 * FEE_RATE * 50, places=6)                  # 价格不动：亏来回手续费
        up = sim_close_value(o["margin_net"], o["notional"], o["size"], 7.7, 0.0, side="short")
        dn = sim_close_value(o["margin_net"], o["notional"], o["size"], 6.3, 0.0, side="short")
        self.assertLess(up, 25 - 2 * FEE_RATE * 50); self.assertGreater(dn, 25)   # 涨了空头亏，跌了空头赚
        # funding 收到（owed<0）让估值变好
        self.assertGreater(sim_close_value(o["margin_net"], 50, o["size"], 7, 0, owed=-1, side="short"),
                           sim_close_value(o["margin_net"], 50, o["size"], 7, 0, owed=0, side="short"))
        self.assertLess(close_proceeds(1, 7, 0.4, "short"), 0); self.assertGreater(close_proceeds(1, 7, 0.4, "long"), 0)

    def test_stop_rise_matches_formula(self):
        r2, r3, r4 = (stop_drop_pct(l, side="short") for l in (2, 3, 4))
        self.assertTrue(r2 > r3 > r4 > 0)
        for lev, r in ((2, r2), (3, r3), (4, r4)):
            o = sim_open(7.0, 25, lev, 0.41, side="short")
            v = sim_close_value(o["margin_net"], o["notional"], o["size"], 7.0 * (1 + r / 100), 0.40, side="short")
            self.assertAlmostEqual((25 - v) / 25, 0.30, places=6)


class ShortBotDry(unittest.TestCase):
    """bot.step 全流程（HZ_NO_CHAIN，mock 行情）。"""
    def setUp(self):
        if os.path.exists(bot.STATE): os.remove(bot.STATE)
        bot.setup_log()

    def run_at(self, t, mark=7.0, index=7.0, p=None, funding=0.0, usdc="300"):
        mk = (Market(mark=mark, index=index, funding_per_day=funding), {"source": "test"})
        env = {"DRY_RUN": "1", "HZ_NO_CHAIN": "1", "SIM_WALLET_USDC": usdc}
        with mock.patch.object(bot, "_node", side_effect=AssertionError("DRY_RUN 不应调用 node")), mock.patch.dict(os.environ, env):
            return bot.step(p or SP(), t, market=mk)[0]

    def st(self): return json.load(open(bot.STATE))

    def test_cross_day_state(self):
        for mi in (0, 10, 20, 30):
            self.assertEqual(self.run_at(T(19, mi)).action, "open")
        s = self.st(); self.assertEqual(s["short_day"], "2026-11-10"); self.assertAlmostEqual(s["short_opened_notional"], 200)
        self.assertTrue(all(l["side"] == "short" and l["size"] > 0 for l in s["lots"]))
        for t in (T(23, 55), T(0, 30, 11), T(3, 0, 11), T(4, 55, 11)):         # 过午夜：不重置、不当成过期仓
            self.assertEqual(self.run_at(t).action, "hold", t)
        s = self.st(); self.assertEqual(len(s["lots"]), 4); self.assertEqual(s["short_day"], "2026-11-10")
        self.assertAlmostEqual(s["short_opened_notional"], 200)
        closes = [mi for mi in range(0, 60, 5) if self.run_at(T(5, mi, 11)).action == "close"]
        self.assertEqual(closes, [0, 10, 20, 30]); self.assertEqual(self.st()["lots"], [])
        self.assertTrue(all(t.get("side") == "short" for t in self.st()["trades"]))
        self.assertEqual(self.run_at(T(7, 0, 11)).action, "open")              # 第二天多单照常

    def test_long_day_reset_does_not_touch_shorts(self):
        self.run_at(T(19, 0))
        # 假设 05:00–06:59 一直平不掉（这里直接跳到 07:00）：空单不能被多单腿的「前一交易日遗留」逻辑接走
        d = self.run_at(T(7, 0, 11))
        self.assertEqual((d.action, d.side, d.reason), ("close", "short", "平仓超时，放宽限制"))
        self.assertEqual(self.st()["opened_notional"], 0.0)                     # 这一步只平空，不开多

    def test_leftover_short_is_overdue_next_evening(self):
        self.run_at(T(19, 0))
        s = self.st(); s["lots"][0]["day"] = "2026-11-09"; json.dump(s, open(bot.STATE, "w"))   # 前一期遗留
        d = self.run_at(T(19, 5))
        self.assertEqual((d.action, d.reason), ("close", "平掉之前交易日没平完的仓位"))

    def test_short_stop_only_stops_short_leg(self):
        for mi in (0, 10): self.run_at(T(19, mi))
        d = self.run_at(T(19, 15), mark=8.1, index=8.1)                         # 涨 15.7% → 2x 空单亏 >30%
        self.assertEqual((d.action, d.side), ("close", "short")); self.assertTrue(d.stop)
        s = self.st(); self.assertEqual(s["lots"], []); self.assertTrue(s["short_stopped"]); self.assertFalse(s["stopped"])
        self.assertEqual(self.run_at(T(19, 30)).action, "hold")                # 本期不再开空
        self.assertEqual(self.run_at(T(7, 0, 11)).action, "open")              # 多单不受影响
        d = self.run_at(T(19, 0, 11))                                           # 07:00 那批多单 16:00 没平（这里没跑 16:00 的步），
        self.assertEqual((d.action, d.side, d.reason), ("close", "long", "平仓超时，放宽限制"))   # 先按超时平多，这一步不开空

    def test_short_resumes_next_period(self):
        self.run_at(T(19, 0)); self.run_at(T(19, 15), mark=8.1, index=8.1)
        self.assertTrue(self.st()["short_stopped"])
        d = self.run_at(T(19, 0, 11)); self.assertEqual((d.action, d.side), ("open", "short"))
        self.assertFalse(self.st()["short_stopped"])

    def test_funding_accrual_direction_in_valuation(self):
        self.run_at(T(19, 0), funding=0.0)
        self.run_at(T(4, 0, 11), funding=+0.5)        # 多头付 → 空头收：估值应高于 funding=0
        v_recv = self.st()["lots"][0]["close_value"]
        self.run_at(T(4, 0, 11), funding=-0.5)
        v_pay = self.st()["lots"][0]["close_value"]
        self.assertGreater(v_recv, v_pay)


class ShortDisabledEqualsLegacy(unittest.TestCase):
    """short_enabled=false：新代码和改动前的代码（tests/legacy）逐步决策、最终 state 完全一致。"""
    def run_dir(self, code, params=None):
        d = tempfile.mkdtemp(); args = [sys.executable, os.path.join(HERE, "tests", "_equiv_harness.py"), code, os.path.join(d, "s.json")]
        if params: args.append(params)
        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_same_as_legacy(self):
        for params in (None, os.path.join(HERE, "params.live.json")):
            old = self.run_dir(os.path.join(HERE, "tests", "legacy"), params)
            new = self.run_dir(HERE, params)
            self.assertEqual(old["steps"], new["steps"])
            self.assertEqual(old["state"], new["state"])
            acts = {a for _, a, *_ in new["steps"]}
            self.assertEqual(acts, {"open", "close", "hold"})
            self.assertTrue(any("止损" in s[2] for s in new["steps"]))            # 序列里确实跑到了止损

    def test_decide_long_matches_legacy_randomised(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("legacy_strategy", os.path.join(HERE, "tests", "legacy", "strategy.py"))
        L = importlib.util.module_from_spec(spec); sys.modules["legacy_strategy"] = L; spec.loader.exec_module(L)
        rnd = random.Random(5)
        for _ in range(5000):
            kw = dict(leverage=rnd.choice([2, 3, 4]), batch_notional_usd=rnd.choice([40, 50, 100]),
                      long_close_hour=rnd.choice([16, 20]))
            n = rnd.randint(0, 5)
            spec_lots = [(str(i), 25, rnd.uniform(5, 80), rnd.uniform(5, 30), rnd.random() < .1) for i in range(n)]
            m = (rnd.uniform(5, 10), rnd.uniform(5, 10), rnd.uniform(-1, 1), rnd.choice([float("inf"), 30]))
            a = dict(stopped=rnd.random() < .2, opened_today=rnd.uniform(0, 220), minutes_since_last=rnd.uniform(0, 30),
                     all_notional=rnd.uniform(0, 320), wallet_usdc=rnd.uniform(0, 150))
            h = rnd.uniform(0, 24)
            mk = lambda mod: [mod.Lot(id=i, margin=mg, notional=nt, size=nt / 7, entry_price=7, close_value=v, overdue=o)
                              for i, mg, nt, v, o in spec_lots]
            old = L.decide(h, L.Market(*m), mk(L), L.Params(**kw), **a)
            new = decide(h, Market(*m), mk(sys.modules["strategy"]), Params(**kw), **a)
            self.assertEqual((old.action, old.reason, old.ids, old.margin, old.stop, old.blocked),
                             (new.action, new.reason, new.ids, new.margin, new.stop, new.blocked))


class ShortLiveWiring(unittest.TestCase):
    def setUp(self):
        if os.path.exists(bot.STATE): os.remove(bot.STATE)
        bot.setup_log()

    def chain(self, extra=()):
        pos = [dict(positionId="117", ignored=True, isLong=True, size=3.93, entryNotional=29.6, entryPrice=7.53,
                    marginOnChain=29.67, closeValue=10.0, fundingOwed=0, utilOwed=0)] + list(extra)
        return {"ok": True, "usdc": 144.0, "complete": True, "positions": pos,
                "totalNotionalAll": sum(x["entryNotional"] for x in pos)}

    def run_live(self, t, chain, p=None):
        calls, reads = [], []
        def lc(payload):
            calls.append(payload)
            return {"positionId": "300", "txHash": "0xabc", "notional": 49.6, "size": -6.98, "rawFillPrice": 7.10, "impactPct": 0.44}
        def cr(payload):
            reads.append(payload)
            if payload["action"] == "positions": return chain
            if payload["action"] == "quote_open": return {"impactPct": 0.44, "notional": 49.6, "size": -6.98, "fillPrice": 7.10, "exceedsLiquidity": False}
            if payload["action"] == "quote_close": return {"impactPct": 0.45, "proceeds": -50.6}
        with mock.patch.dict(os.environ, dict(LIVE, RPC_URL="x", PRIVATE_KEY="y")), \
             mock.patch.object(bot, "live_call", side_effect=lc), mock.patch.object(bot, "chain_read", side_effect=cr):
            d, _ = bot.step(p or SP(), t, market=(Market(7.1, 7.0, 0.2), {"source": "t"}))
        return d, calls, reads

    def test_short_open_payload(self):
        d, calls, reads = self.run_live(T(19, 0), self.chain())
        self.assertEqual((d.action, d.side), ("open", "short"))
        c = calls[0]
        self.assertEqual((c["isLong"], c["allowShort"], c["side"], c["margin"], c["leverage"], c["maxTotalNotional"]),
                         (False, True, "short", 25.0, 2.0, 300.0))
        self.assertIn({"action": "quote_open", "perp": bot.datafeed.PERP, "margin": 25.0, "leverage": 2.0, "isLong": False}, reads)
        lot = json.load(open(bot.STATE))["lots"][0]
        self.assertEqual((lot["side"], lot["size"]), ("short", 6.98))

    def test_long_payload_unchanged(self):
        d, calls, _ = self.run_live(T(7, 0), self.chain())
        self.assertEqual(set(calls[0]), {"action", "isLong", "margin", "leverage", "slippagePct", "maxImpactPct",
                                         "maxTotalNotional", "minUsdc", "perp"})
        self.assertIs(calls[0]["isLong"], True)

    def test_chain_short_adopted_and_closed_with_side(self):
        sh = dict(positionId="301", ignored=False, isLong=False, size=-6.98, entryNotional=49.6, entryPrice=7.1,
                  marginOnChain=24.45, closeValue=23.5, fundingOwed=-0.1, utilOwed=0.05)
        d, calls, _ = self.run_live(T(5, 0, 11), self.chain([sh]))
        self.assertEqual((d.action, d.side), ("close", "short"))
        self.assertEqual(calls, [{"action": "close", "positionId": "301", "slippagePct": 1.5, "maxImpactPct": 1.5,
                                  "perp": bot.datafeed.PERP, "side": "short"}])

    def test_chain_short_ignored_when_disabled(self):
        sh = dict(positionId="301", ignored=False, isLong=False, size=-6.98, entryNotional=49.6, entryPrice=7.1,
                  marginOnChain=24.45, closeValue=5.0, fundingOwed=0, utilOwed=0)
        d, calls, _ = self.run_live(T(5, 0, 11), self.chain([sh]), p=Params(long_close_hour=16))
        self.assertEqual(d.action, "hold"); self.assertEqual(calls, [])
        self.assertEqual(json.load(open(bot.STATE))["lots"], [])

    def test_short_counts_toward_300_with_117(self):
        own = [dict(positionId=str(310 + i), ignored=False, isLong=bool(i % 2), size=6.5 * (1 if i % 2 else -1), entryNotional=60,
                    entryPrice=7.6, marginOnChain=29, closeValue=29.5, fundingOwed=0, utilOwed=0) for i in range(4)]
        d, calls, _ = self.run_live(T(19, 0), self.chain(own))   # 240 + 117 的 29.6 + 50 > 300
        self.assertNotEqual(d.action, "open"); self.assertFalse(any(c["action"] == "open" for c in calls))


if __name__ == "__main__":
    unittest.main()

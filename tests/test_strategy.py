import os, sys, unittest, random, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy import (Params, Market, Lot, in_window, decide, open_guards, stop_status, stop_drop_pct,
                      sim_open, sim_close_value, FEE_RATE, HARD_MAX_ORDER_MARGIN_USD, HARD_MAX_MARKET_NOTIONAL_USD)

M = Market(mark=7.0, index=7.2, funding_per_day=-0.1)


def D(hour, lots=(), p=None, m=M, **kw):
    a = dict(stopped=False, opened_today=0.0, minutes_since_last=1e9, all_notional=sum(l.notional for l in lots),
             wallet_usdc=1000.0)
    a.update(kw)
    return decide(hour, m, list(lots), p or Params(), **a)


def lot(i, margin=25.0, notional=50.0, value=None, overdue=False):
    return Lot(id=str(i), margin=margin, notional=notional, size=notional / 7, entry_price=7.0,
               close_value=margin - 1 if value is None else value, overdue=overdue)


class Windows(unittest.TestCase):
    def test_simple(self):
        self.assertTrue(in_window(7, 7, 20)); self.assertFalse(in_window(20, 7, 20)); self.assertFalse(in_window(6.9, 7, 20))

    def test_property_complement(self):
        rnd = random.Random(0)
        for _ in range(2000):
            a, b, h = rnd.uniform(0, 24), rnd.uniform(0, 24), rnd.uniform(0, 24)
            if abs(a - b) < 1e-9: continue
            self.assertNotEqual(in_window(h, a, b), in_window(h, b, a))


class ParamRules(unittest.TestCase):
    def test_defaults(self):
        p = Params(); p.validate()
        self.assertEqual((p.target_notional_usd, p.leverage, p.batch_notional_usd), (200, 2, 50))
        self.assertEqual(p.stop_loss_margin_pct, 30); self.assertIn(117, p.ignore_position_ids)

    def test_leverage_range(self):
        for lev in (1, 1.99, 4.01, 5, 10):
            with self.assertRaises(ValueError): Params(leverage=lev).validate()
        for lev in (2, 2.5, 3, 4):
            Params(leverage=lev).validate()

    def test_caps(self):
        bad = [dict(max_notional_usd=301), dict(target_notional_usd=201), dict(batch_notional_usd=250, target_notional_usd=200),
               dict(stop_loss_margin_pct=31), dict(stop_loss_margin_pct=0), dict(batch_notional_usd=8),
               dict(long_open_hour=5, long_close_hour=5), dict(max_impact_pct=5, stop_max_impact_pct=4)]
        for kw in bad:
            with self.assertRaises(ValueError, msg=kw): Params(**kw).validate()

    def test_order_margin_cap_property(self):
        # 性质：任意通过校验的参数，每批保证金 ≤100、杠杆在 [2,4]
        rnd = random.Random(3)
        for _ in range(3000):
            p = Params(leverage=rnd.uniform(1, 5), batch_notional_usd=rnd.uniform(1, 400), target_notional_usd=rnd.uniform(1, 300))
            try: p.validate()
            except ValueError: continue
            self.assertLessEqual(p.batch_margin_usd, HARD_MAX_ORDER_MARGIN_USD)
            self.assertTrue(2 <= p.leverage <= 4)

    def test_env_override_and_list(self):
        os.environ.update(HZ_LEVERAGE="3", HZ_IGNORE_POSITION_IDS="117,120")
        try:
            p = Params.from_env(); self.assertEqual(p.leverage, 3.0); self.assertEqual(p.ignore_position_ids, [117, 120])
        finally:
            del os.environ["HZ_LEVERAGE"]; del os.environ["HZ_IGNORE_POSITION_IDS"]

    def test_env_bad_leverage_rejected(self):
        os.environ["HZ_LEVERAGE"] = "5"
        try: self.assertRaises(ValueError, Params.from_env)
        finally: del os.environ["HZ_LEVERAGE"]

    def test_example_params_file_valid(self):
        p = Params.from_env(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "params.example.json"))
        self.assertEqual(p.target_notional_usd, 200)


class Batching(unittest.TestCase):
    def test_first_batch_at_open(self):
        d = D(7.0)
        self.assertEqual(d.action, "open"); self.assertAlmostEqual(d.notional, 50); self.assertAlmostEqual(d.margin, 25)

    def test_interval_respected(self):
        self.assertEqual(D(7.1, [lot(1)], opened_today=50, minutes_since_last=5).action, "hold")
        self.assertEqual(D(7.2, [lot(1)], opened_today=50, minutes_since_last=10).action, "open")

    def test_last_batch_is_remainder(self):
        d = D(7.5, [lot(i) for i in range(3)], opened_today=170)
        self.assertEqual(d.action, "open"); self.assertAlmostEqual(d.notional, 30)

    def test_target_filled_holds(self):
        self.assertEqual(D(7.5, [lot(i) for i in range(4)], opened_today=199).action, "hold")

    def test_grace_window(self):
        self.assertEqual(D(9.1, [lot(1)], opened_today=50).action, "hold")   # 凑不满按已开部分持有
        self.assertEqual(D(9.0, [lot(1)], opened_today=50).action, "open")

    def test_full_day_sequence(self):
        """模拟一天：每 5 分钟一步，间隔 10 分钟 → 4 批开满 200，20:00 起每 10 分钟平一批。"""
        p = Params(); lots = []; opened = 0.0; last = None; log = []
        t = 6.0
        while t < 22:
            since = 1e9 if last is None else (t - last) * 60
            d = D(t, lots, p, opened_today=opened, minutes_since_last=since)
            if d.action == "open":
                lots.append(lot(len(log), d.margin, d.notional)); opened += d.notional; last = t; log.append(("open", t))
            elif d.action == "close":
                lots = [l for l in lots if l.id not in d.ids]; last = t; log.append(("close", t))
            t = round(t + 5 / 60, 6)
        opens = [x for a, x in log if a == "open"]; closes = [x for a, x in log if a == "close"]
        self.assertEqual(len(opens), 4); self.assertAlmostEqual(opened, 200)
        self.assertTrue(all(7 <= x < 8 for x in opens)); self.assertEqual(len(closes), 4)
        self.assertTrue(all(20 <= x < 21 for x in closes)); self.assertEqual(lots, [])

    def test_close_one_at_a_time_then_force(self):
        lots = [lot(1, notional=50), lot(2, notional=40)]
        d = D(20.0, lots)
        self.assertEqual((d.action, d.ids, d.stop), ("close", ["1"], False))   # 先平大的
        self.assertEqual(D(20.05, lots, minutes_since_last=3).action, "hold")
        d = D(21.0, lots, minutes_since_last=3)                                 # 超时 1 小时：放宽、不等间隔
        self.assertEqual(d.action, "close"); self.assertTrue(d.stop); self.assertEqual(d.max_impact_pct, Params().stop_max_impact_pct)

    def test_overdue_closed_first(self):
        d = D(7.0, [lot(1, overdue=True)])
        self.assertEqual((d.action, d.ids), ("close", ["1"]))

    def test_property_open_respects_all_caps(self):
        rnd = random.Random(7)
        for _ in range(5000):
            lev = rnd.choice([2, 3, 4]); p = Params(leverage=lev, batch_notional_usd=rnd.choice([40, 50, 100, 200]))
            n = rnd.randint(0, 6); lots = [lot(i, 25, rnd.uniform(5, 80)) for i in range(n)]
            all_n = sum(l.notional for l in lots) + rnd.choice([0, 30, 150]); opened = rnd.uniform(0, 220)
            usdc = rnd.uniform(0, 150)
            m = Market(rnd.uniform(5, 10), rnd.uniform(5, 10), rnd.uniform(-1, 1), rnd.choice([float("inf"), 30]))
            d = decide(rnd.uniform(0, 24), m, lots, p, stopped=rnd.random() < .2, opened_today=opened,
                       minutes_since_last=rnd.uniform(0, 30), all_notional=all_n, wallet_usdc=usdc)
            if d.action == "open":
                self.assertLessEqual(d.margin, 100 + 1e-9)
                self.assertLessEqual(all_n + d.notional, 300 + 1e-9)
                self.assertLessEqual(opened + d.notional, 200 * 1.01 + 1e-9)
                self.assertGreaterEqual(usdc, d.margin)
                self.assertAlmostEqual(d.margin * lev, d.notional)
                self.assertFalse(stop_status(lots, p.stop_loss_margin_pct)["triggered"])


class Guards(unittest.TestCase):
    def kw(self, **o):
        a = dict(notional=50, margin=25, all_notional=0, opened_today=0, wallet_usdc=100); a.update(o); return a

    def test_ok(self):
        self.assertEqual(open_guards(M, Params(), **self.kw()), [])

    def test_each_guard(self):
        p = Params()
        self.assertTrue(open_guards(Market(8, 7), p, **self.kw()))                       # 追多
        self.assertTrue(open_guards(Market(7, 7, 0.7), p, **self.kw()))                  # funding 10%/天
        self.assertTrue(open_guards(M, p, **self.kw(all_notional=260)))                  # 300 总上限
        self.assertTrue(open_guards(M, p, **self.kw(opened_today=160)))                  # 当日 200
        self.assertTrue(open_guards(M, p, **self.kw(wallet_usdc=25)))                    # 余额（含缓冲）
        self.assertTrue(open_guards(M, p, **self.kw(margin=101, notional=202)))          # 单笔保证金
        self.assertTrue(open_guards(Market(7, 7.2, 0, 40), p, **self.kw()))              # 池子容量

    def test_ignored_position_counts_toward_300(self):
        # 钱包里不归 bot 管的仓位（如 117）也计入 300u 总上限
        self.assertTrue(open_guards(M, Params(), **self.kw(all_notional=30 + 230)))


class StopLoss(unittest.TestCase):
    def test_threshold_exact(self):
        p = Params()
        lots = [lot(1, 25, value=17.6), lot(2, 25, value=17.5)]   # 亏 14.9/50 = 29.8%
        self.assertEqual(D(10, lots, p).action, "hold")
        lots = [lot(1, 25, value=17.5), lot(2, 25, value=17.5)]   # 亏 15/50 = 30%
        d = D(10, lots, p)
        self.assertEqual(d.action, "close"); self.assertTrue(d.stop); self.assertEqual(set(d.ids), {"1", "2"})
        self.assertEqual(d.max_impact_pct, p.stop_max_impact_pct); self.assertIn("止损", d.reason)

    def test_aggregate_not_per_lot(self):
        lots = [lot(1, 25, value=10), lot(2, 25, value=26)]       # 单批亏 60%，合计亏 28%
        self.assertEqual(D(10, lots).action, "hold")

    def test_stop_overrides_interval_and_window(self):
        lots = [lot(1, 25, value=10)]
        for h in (7.1, 12, 20.01, 23):
            d = D(h, lots, minutes_since_last=0)
            self.assertEqual(d.action, "close"); self.assertTrue(d.stop)

    def test_no_reopen_after_stop(self):
        self.assertEqual(D(7.2, [], stopped=True).action, "hold")
        d = D(7.2, [lot(1)], stopped=True)                        # 止损后还有没平掉的 → 继续全平
        self.assertEqual(d.action, "close"); self.assertTrue(d.stop)

    def test_drop_conversion(self):
        # 杠杆越高，触发止损的价格跌幅越小；数值与公式反解一致
        d2, d3, d4 = (stop_drop_pct(l) for l in (2, 3, 4))
        self.assertTrue(d2 > d3 > d4 > 0)
        for lev, d in ((2, d2), (3, d3), (4, d4)):
            margin = 25.0; o = sim_open(7.0, margin, lev, 0.41)
            v = sim_close_value(o["margin_net"], o["notional"], o["size"], 7.0 * (1 - d / 100), 0.40)
            self.assertAlmostEqual((margin - v) / margin, 0.30, places=6)

    def test_flat_price_round_trip_cost(self):
        o = sim_open(7.0, 25, 2, 0.0)
        v = sim_close_value(o["margin_net"], o["notional"], o["size"], 7.0, 0.0)
        self.assertAlmostEqual(25 - v, FEE_RATE * 50 + FEE_RATE * 50, places=6)


if __name__ == "__main__":
    unittest.main()

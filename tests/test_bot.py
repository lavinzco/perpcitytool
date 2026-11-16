import os, sys, json, logging, tempfile, unittest, datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp(); os.environ["HORMUZ_STATE"] = os.path.join(tmp, "s.json"); os.environ["HORMUZ_LOGDIR"] = tmp
import bot
from strategy import Params, Market
HKT = ZoneInfo("Asia/Hong_Kong")
LIVE = {"DRY_RUN": "0", "LIVE_TRADING": "1", "CONFIRM_LIVE": "I_UNDERSTAND_REAL_MONEY"}


def T(h, m=0, day=10):
    return dt.datetime(2026, 11, day, h, m, tzinfo=HKT)


class Gate(unittest.TestCase):
    def test_default_is_dry(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bot.is_live())

    def test_needs_all_three(self):
        for drop in LIVE:
            with mock.patch.dict(os.environ, {k: v for k, v in LIVE.items() if k != drop}, clear=True):
                self.assertFalse(bot.is_live())
        with mock.patch.dict(os.environ, LIVE, clear=True):
            self.assertTrue(bot.is_live())

    def test_live_call_refuses_in_dry(self):
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}), mock.patch.object(bot, "_node", side_effect=AssertionError):
            self.assertRaises(AssertionError, bot.live_call, {"action": "open"})

    def test_chain_read_only_readonly_actions(self):
        with mock.patch.object(bot, "_node", return_value={}):
            for a in ("open", "close"):
                self.assertRaises(AssertionError, bot.chain_read, {"action": a})


class DatafeedContract(unittest.TestCase):
    def test_from_public_builds_market(self):
        sub = {"data": {"perp": {"sqrtPriceX96": str(int((7.5 ** 0.5) * 2 ** 96)), "fundingPerDay": str(-5 * 10 ** 17),
               "openInterestLong": "1000000", "openInterestShort": "0", "capacityLong": "60000000", "capacityShort": "60000000",
               "emaAmmPriceX96": str(7 * 2 ** 96), "emaIndexPriceX96": str(7 * 2 ** 96), "lastTouchedTimestamp": "0"}}}
        with mock.patch.object(bot.datafeed, "_get", side_effect=[sub, {"index_price": 7.6, "timestamp": 1}]):
            m, meta = bot.datafeed.from_public()
        self.assertAlmostEqual(m.mark, 7.5, places=6); self.assertAlmostEqual(m.capacity_long_usd, 59 * 7.5, places=3)


class DryRun(unittest.TestCase):
    """HZ_NO_CHAIN=1：只用模型，不碰链。"""
    def setUp(self):
        if os.path.exists(bot.STATE): os.remove(bot.STATE)
        bot.setup_log()

    def run_at(self, t, mark=7.0, index=7.1):
        mk = (Market(mark=mark, index=index, funding_per_day=-0.05), {"source": "test"})
        env = {"DRY_RUN": "1", "HZ_NO_CHAIN": "1", "SIM_WALLET_USDC": "110"}
        with mock.patch.object(bot, "_node", side_effect=AssertionError("DRY_RUN 不应调用 node")), \
             mock.patch.dict(os.environ, env):
            return bot.step(Params(), t, market=mk)[0]

    def st(self): return json.load(open(bot.STATE))

    def test_open_batches_then_close_batches(self):
        acts = [(t, self.run_at(T(7, mi)).action) for mi in range(0, 60, 5) for t in [mi]]
        opens = [t for t, a in acts if a == "open"]
        self.assertEqual(opens, [0, 10, 20, 30])                     # 间隔 10 分钟、4 批
        s = self.st(); self.assertEqual(len(s["lots"]), 4); self.assertAlmostEqual(s["opened_notional"], 200)
        self.assertTrue(all(abs(l["margin"] - 25) < 1e-9 for l in s["lots"]))
        self.assertEqual(self.run_at(T(12)).action, "hold")
        closes = [mi for mi in range(0, 60, 5) if self.run_at(T(20, mi)).action == "close"]
        self.assertEqual(closes, [0, 10, 20, 30])
        s = self.st(); self.assertEqual(s["lots"], []); self.assertEqual(len(s["trades"]), 4)
        self.assertTrue(all(t["net"] < 0 for t in s["trades"]))     # 价格不动 → 手续费让每批都亏

    def test_wallet_limits_batches(self):
        with mock.patch.dict(os.environ, {"SIM_WALLET_USDC": "60"}):
            pass
        env = {"DRY_RUN": "1", "HZ_NO_CHAIN": "1", "SIM_WALLET_USDC": "60"}
        mk = (Market(7.0, 7.1), {"source": "t"})
        with mock.patch.object(bot, "_node", side_effect=AssertionError), mock.patch.dict(os.environ, env):
            n = sum(bot.step(Params(), T(7, mi), market=mk)[0].action == "open" for mi in range(0, 60, 5))
        self.assertEqual(n, 2)                                         # 60u 只够 2 批 25u 保证金

    def test_stop_loss_closes_all_and_blocks_day(self):
        for mi in (0, 10):
            self.run_at(T(7, mi))
        self.assertEqual(len(self.st()["lots"]), 2)
        d = self.run_at(T(7, 15), mark=6.0, index=7.1)                 # 2x 下跌 14% → 亏损 >30%
        self.assertEqual(d.action, "close"); self.assertTrue(d.stop)
        s = self.st(); self.assertEqual(s["lots"], []); self.assertTrue(s["stopped"])
        self.assertEqual(self.run_at(T(7, 30)).action, "hold")       # 当天不再开
        self.assertEqual(self.run_at(T(7, 0, day=11)).action, "open")  # 第二天恢复

    def test_impact_too_high_skips_step(self):
        p = Params(max_impact_pct=0.3)                                 # 模型 50u ≈ 0.4%
        with mock.patch.object(bot, "_node", side_effect=AssertionError), \
             mock.patch.dict(os.environ, {"DRY_RUN": "1", "HZ_NO_CHAIN": "1"}):
            d, _ = bot.step(p, T(7, 0), market=(Market(7.0, 7.1), {"source": "t"}))
        self.assertEqual(d.action, "open"); self.assertEqual(self.st()["lots"], [])
        self.assertIsNone(self.st()["last_action"])                   # 没下 → 下一步还能马上再试


class LiveWiring(unittest.TestCase):
    """实盘接线：chain_read/live_call 全部 mock，验证不碰 117、上限参数传给 trade.mjs、以链上为准。"""
    def setUp(self):
        if os.path.exists(bot.STATE): os.remove(bot.STATE)
        bot.setup_log()

    def chain(self, extra=()):
        pos = [dict(positionId="117", ignored=True, isLong=True, size=3.93, entryNotional=29.6, entryPrice=7.53,
                    marginOnChain=29.67, closeValue=10.0, fundingOwed=0, utilOwed=0)]   # 117 亏很多也不能触发 bot 止损
        pos += list(extra)
        return {"ok": True, "usdc": 100.0, "complete": True, "positions": pos,
                "totalNotionalAll": sum(x["entryNotional"] for x in pos)}

    def run_live(self, t, chain, live_ret=None, p=None):
        calls = []
        def lc(payload):
            calls.append(payload)
            return live_ret or {"positionId": "200", "txHash": "0xabc", "notional": 49.6, "size": 6.5,
                                "rawFillPrice": 7.6, "impactPct": 0.4}
        def cr(payload):
            if payload["action"] == "positions": return chain
            if payload["action"] == "quote_open": return {"impactPct": 0.4, "notional": 49.6, "size": 6.5, "fillPrice": 7.6, "exceedsLiquidity": False}
            if payload["action"] == "quote_close": return {"impactPct": 0.4, "proceeds": 48.5}
        with mock.patch.dict(os.environ, dict(LIVE, RPC_URL="x", PRIVATE_KEY="y")), \
             mock.patch.object(bot, "live_call", side_effect=lc), mock.patch.object(bot, "chain_read", side_effect=cr):
            d, _ = bot.step(p or Params(), t, market=(Market(7.5, 7.6), {"source": "t"}))
        return d, calls

    def test_open_payload_has_caps_and_ignores_117(self):
        d, calls = self.run_live(T(7, 0), self.chain())
        self.assertEqual(d.action, "open"); self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual((c["action"], c["isLong"], c["leverage"], c["margin"]), ("open", True, 2.0, 25.0))
        self.assertEqual(c["maxTotalNotional"], 300.0); self.assertEqual(c["maxImpactPct"], 1.5)
        s = json.load(open(bot.STATE)); self.assertEqual([l["id"] for l in s["lots"]], ["200"])

    def test_never_closes_117_even_at_close_time_or_stop(self):
        for t in (T(20, 0), T(21, 30)):
            d, calls = self.run_live(t, self.chain())
            self.assertEqual(d.action, "hold"); self.assertEqual(calls, [])

    def test_chain_is_truth_and_stop_uses_chain_value(self):
        own = [dict(positionId="201", ignored=False, isLong=True, size=6.5, entryNotional=49.6, entryPrice=7.6,
                    marginOnChain=24.45, closeValue=17.0, fundingOwed=0, utilOwed=0)]   # 投入≈25，估值 17 → 亏 32%
        d, calls = self.run_live(T(10, 0), self.chain(own))
        self.assertEqual(d.action, "close"); self.assertTrue(d.stop)
        self.assertEqual([c["positionId"] for c in calls], ["201"])
        self.assertEqual(calls[0]["maxImpactPct"], Params().stop_max_impact_pct)
        self.assertTrue(json.load(open(bot.STATE))["stopped"])

    def test_counts_117_toward_300(self):
        own = [dict(positionId=str(300 + i), ignored=False, isLong=True, size=6.5, entryNotional=60, entryPrice=7.6,
                    marginOnChain=29, closeValue=29.5, fundingOwed=0, utilOwed=0) for i in range(4)]  # 240 + 117 的 29.6
        d, calls = self.run_live(T(7, 0), self.chain(own))
        self.assertEqual(d.action, "hold"); self.assertEqual(calls, [])

    def test_incomplete_scan_blocks_open(self):
        ch = self.chain(); ch["complete"] = False
        d, calls = self.run_live(T(7, 0), ch)
        self.assertEqual(d.action, "hold"); self.assertEqual(calls, [])


class StaleAbort(unittest.TestCase):
    """墙钟超过 stale_abort_seconds（默认 120）则放弃普通开/平；止损不受拦。"""
    def setUp(self):
        if os.path.exists(bot.STATE): os.remove(bot.STATE)
        bot.setup_log()

    def run_at(self, t, mark=7.0, index=7.1, elapsed=0, p=None, capture_logs=False):
        mk = (Market(mark=mark, index=index, funding_per_day=-0.05), {"source": "test"})
        env = {"DRY_RUN": "1", "HZ_NO_CHAIN": "1", "SIM_WALLET_USDC": "110"}
        t0 = 1_000_000.0
        n = {"i": 0}
        def wall():
            n["i"] += 1
            return t0 if n["i"] == 1 else t0 + elapsed
        buf = []
        class H(logging.Handler):
            def emit(self, record):
                buf.append(record.getMessage())
        handler = H(); handler.setLevel(logging.WARNING)
        if capture_logs:
            logging.getLogger().addHandler(handler)
        try:
            with mock.patch.object(bot, "_node", side_effect=AssertionError("DRY_RUN 不应调用 node")), \
                 mock.patch.dict(os.environ, env), \
                 mock.patch.object(bot.time, "time", side_effect=wall):
                d = bot.step(p or Params(), t, market=mk)[0]
        finally:
            logging.getLogger().removeHandler(handler)
        return (d, buf) if capture_logs else d

    def st(self):
        return json.load(open(bot.STATE))

    def test_default_is_120_and_optional(self):
        self.assertEqual(Params().stale_abort_seconds, 120)
        # 旧 params JSON 没有该字段也应能加载
        path = os.path.join(tmp, "params_no_stale.json")
        json.dump({"leverage": 2, "target_notional_usd": 200}, open(path, "w"))
        p = Params.from_env(path)
        self.assertEqual(p.stale_abort_seconds, 120)

    def test_stale_skips_open(self):
        d, logs = self.run_at(T(7, 0), elapsed=121, capture_logs=True)
        self.assertEqual(d.action, "hold")
        self.assertIn("stale", d.reason)
        self.assertTrue(any("步骤过期 121 秒，放弃下单，留给下一步" in x for x in logs), logs)
        s = self.st()
        self.assertEqual(s["lots"], [])
        self.assertIsNone(s.get("last_action"))

    def test_fresh_still_opens(self):
        d = self.run_at(T(7, 0), elapsed=10)
        self.assertEqual(d.action, "open")
        self.assertEqual(len(self.st()["lots"]), 1)

    def test_stale_skips_regular_close(self):
        for mi in (0, 10, 20, 30):
            self.run_at(T(7, mi), elapsed=0)
        self.assertEqual(len(self.st()["lots"]), 4)
        d, logs = self.run_at(T(20, 0), elapsed=121, capture_logs=True)
        self.assertEqual(d.action, "hold")
        self.assertIn("stale", d.reason)
        self.assertTrue(any("步骤过期 121 秒，放弃下单，留给下一步" in x for x in logs), logs)
        self.assertEqual(len(self.st()["lots"]), 4)

    def test_stale_does_not_block_stop(self):
        self.run_at(T(7, 0), elapsed=0)
        self.run_at(T(7, 10), elapsed=0)
        self.assertEqual(len(self.st()["lots"]), 2)
        d = self.run_at(T(7, 15), mark=6.0, index=7.1, elapsed=200)
        self.assertEqual(d.action, "close")
        self.assertTrue(d.stop)
        self.assertTrue(d.reason.startswith("止损"))
        s = self.st()
        self.assertEqual(s["lots"], [])
        self.assertTrue(s["stopped"])


if __name__ == "__main__":
    unittest.main()

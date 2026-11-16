"""trade.mjs 静态硬性检查（--validate-only，不连链、不需要实盘开关）+ close 接口与 close_117.sh 兼容。"""
import json, os, subprocess, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERP = "0xb9F540CE4315e7F4382C96532f8eC3903899D86d"
SCRIPT = os.environ.get("TRADE_MJS", os.path.join(HERE, "live", "trade.mjs"))


def run(req):
    env = {k: v for k, v in os.environ.items() if k not in ("DRY_RUN", "LIVE_TRADING", "CONFIRM_LIVE")}
    return subprocess.run(["node", SCRIPT, "--validate-only"], input=json.dumps(req), capture_output=True, text=True, env=env, timeout=60)


class TradeMjs(unittest.TestCase):
    def ok(self, **kw): return run(dict(action="open", perp=PERP, isLong=True, slippagePct=1.5, **kw)).returncode == 0

    def test_open_caps(self):
        self.assertTrue(self.ok(margin=25, leverage=2)); self.assertTrue(self.ok(margin=50, leverage=4))
        self.assertTrue(self.ok(margin=100, leverage=3))
        self.assertFalse(self.ok(margin=101, leverage=2))       # 单笔保证金
        self.assertFalse(self.ok(margin=25, leverage=1))        # 杠杆 <2
        self.assertFalse(self.ok(margin=25, leverage=4.5))      # 杠杆 >4
        self.assertFalse(self.ok(margin=4, leverage=2))         # 交易所最小
        r = run(dict(action="open", perp=PERP, isLong=False, margin=25, leverage=2, slippagePct=1.5))
        self.assertNotEqual(r.returncode, 0)                    # 只做多

    def test_short_needs_explicit_allow(self):
        base = dict(action="open", perp=PERP, isLong=False, margin=25, leverage=2, slippagePct=1.5)
        self.assertNotEqual(run(base).returncode, 0)                                   # 没放行
        self.assertNotEqual(run(dict(base, allowShort=True)).returncode, 0)            # 缺 side
        self.assertNotEqual(run(dict(base, side="short")).returncode, 0)               # 缺 allowShort
        self.assertNotEqual(run(dict(base, allowShort="true", side="short")).returncode, 0)   # 必须是布尔 true
        self.assertEqual(run(dict(base, allowShort=True, side="short")).returncode, 0)
        for bad in (dict(margin=101), dict(leverage=1), dict(leverage=4.5), dict(margin=4)):  # 空单同样受硬上限
            self.assertNotEqual(run(dict(base, allowShort=True, side="short", **bad)).returncode, 0, bad)
        self.assertNotEqual(run(dict(base, isLong="false", allowShort=True, side="short")).returncode, 0)
        self.assertNotEqual(run(dict(action="open", perp=PERP, isLong=True, side="short", margin=25, leverage=2,
                                     slippagePct=1.5)).returncode, 0)                  # 方向自相矛盾

    def test_short_open_refused_without_switches(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DRY_RUN", "LIVE_TRADING", "CONFIRM_LIVE")}
        req = {"action": "open", "isLong": False, "allowShort": True, "side": "short", "margin": 25, "leverage": 2,
               "slippagePct": 1.5, "perp": PERP}
        r = subprocess.run(["node", SCRIPT], input=json.dumps(req), capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(r.returncode, 0); self.assertIn("实盘开关", r.stderr)

    def test_close_117_payload_still_accepted(self):
        r = run({"action": "close", "positionId": "117", "slippagePct": 2.5, "perp": PERP})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_live_actions_refused_without_switches(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DRY_RUN", "LIVE_TRADING", "CONFIRM_LIVE")}
        for req in ({"action": "close", "positionId": "999999", "slippagePct": 2.5, "perp": PERP},
                    {"action": "open", "isLong": True, "margin": 25, "leverage": 2, "slippagePct": 1.5, "perp": PERP},
                    {"action": "reduce", "positionId": "187", "side": "long", "size": "1", "slippagePct": 1.5, "perp": PERP}):
            r = subprocess.run(["node", SCRIPT], input=json.dumps(req), capture_output=True, text=True, env=env, timeout=60)
            self.assertNotEqual(r.returncode, 0); self.assertIn("实盘开关", r.stderr)

    def test_reduce_static_fail_closed(self):
        good = {"action": "reduce", "positionId": "187", "side": "long", "size": "1.234567", "slippagePct": 1.5, "perp": PERP}
        self.assertEqual(run(good).returncode, 0)
        self.assertEqual(run(dict(good, size=None, full=True)).returncode, 0)
        bad = [
            dict(good, side="short"),
            dict(good, isLong=False),
            dict(good, size="0"),
            dict(good, size="-1"),
            dict(good, size="NaN"),
            dict(good, size="Infinity"),
            dict(good, size="1e-3"),
            dict(good, size="1.2345678"),
            dict(good, size=""),
            dict(good, slippagePct=0),
            dict(good, slippagePct=-1),
            dict(good, perpDelta="1"),
            dict(good, marginDelta="0"),
            dict(good, full="true"),
            dict(good, positionId=""),
            dict(good, perp="0xnot-an-address"),
            {"action": "reduce", "positionId": "187", "side": "long", "slippagePct": 1.5, "perp": PERP},
        ]
        for req in bad:
            r = run(req)
            self.assertNotEqual(r.returncode, 0, req)
            self.assertNotIn("实盘开关", r.stderr)


if __name__ == "__main__":
    unittest.main()

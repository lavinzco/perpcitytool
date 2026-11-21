"""Deterministic tests for the one-off stop guardian. No chain, no secrets, no sends."""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oneoff.guardian import (
    ALLOWED_ACTIONS, MAX_INVESTED_MARGIN, MAX_OPEN_NOTIONAL, STOP_IMPACT, STOP_PCT, STOP_SLIPPAGE,
    GuardianError, apply_latch, arm_inflight, assess_health, attach, book_cash, live_flags,
    load_state, loss_status, new_state, note_send, plan, reconcile, run_once, save_state,
    size_str, trusted,
)

PERP = "0xb9F540CE4315e7F4382C96532f8eC3903899D86d"
WALLET = "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"
NOW = 1_000_000.0


def pos(pid="200", close=20.0, micro=2_000_000, is_long=True, owner=None, margin_on_chain=24.0):
    return {"positionId": str(pid), "isLong": is_long, "closeValue": close, "perpDelta": str(micro),
            "marginOnChain": margin_on_chain, "entryNotional": 48.0, "fundingOwed": 0.2, "utilOwed": 0.1,
            "owner": WALLET if owner is None else owner, "markValue": close + 5}


def obs(positions, *, asof=NOW, ok=True, complete=True, wallet=WALLET, usdc=100.0, error=None, perp=PERP):
    return {"ok": ok, "complete": complete, "asof": asof, "wallet": wallet, "usdc": usdc,
            "error": error, "perp": perp, "positions": positions}


def attached(invested=25.0, notional=48.0, cash=0.0, latched=False):
    s = new_state()
    s["cash_out"] = cash
    s["latched"] = latched
    s["position"] = {"id": "200", "invested_margin": invested, "open_notional": notional,
                     "perp": PERP, "expected_wallet": WALLET, "attached_at": NOW - 10}
    return s


def low(_micro):
    return {"impactPct": 1.0, "exceedsLiquidity": False}


class Boundary(unittest.TestCase):
    def test_exact_30_triggers_and_just_under_holds(self):
        # 25 - 17.5 = 7.5 = 30% of the original 25, not of marginOnChain and not of notional.
        hit = loss_status(25, 17.5, 0)
        self.assertTrue(hit["triggered"])
        self.assertAlmostEqual(hit["loss_pct"], 30.0)
        miss = loss_status(25, 17.5 + 1e-4, 0)
        self.assertFalse(miss["triggered"])
        self.assertLess(miss["loss_pct"], STOP_PCT)

        armed = attached()
        over = plan(armed, obs([pos(close=17.5)]), low, NOW)
        self.assertTrue(over["latched"])
        self.assertTrue(over["would_send"])
        self.assertEqual(over["payload"]["action"], "close")
        self.assertEqual(over["payload"]["maxImpactPct"], STOP_IMPACT)
        self.assertEqual(over["payload"]["slippagePct"], STOP_SLIPPAGE)
        self.assertEqual(over["payload"]["side"], "long")

        under = plan(armed, obs([pos(close=17.5 + 1e-4, margin_on_chain=1.0)]), low, NOW)
        self.assertEqual(under["action"], "hold")
        self.assertFalse(under["would_send"])
        self.assertFalse(under["latched"])

    def test_pre_open_quote_does_not_latch(self):
        # A 31% pre-open quote must not stop a position whose post-open closeValue is fine.
        fresh = obs([pos(close=23.775)])  # 25 - 23.775 = 1.225 = 4.9% of 25
        fresh["preOpenQuoteCloseLossPct"] = 31.10
        held = plan(attached(), fresh, low, NOW)
        self.assertEqual(held["action"], "hold")
        self.assertFalse(held["latched"])
        # The opposite mistake must not hide a real post-open 31% closeValue.
        bad = obs([pos(close=17.225)])
        bad["preOpenQuoteCloseLossPct"] = 4.9
        fired = plan(attached(), bad, low, NOW)
        self.assertTrue(fired["latched"])
        self.assertGreaterEqual(fired["loss_pct"], 30)

    def test_uses_close_value_not_mark_or_chain_margin(self):
        # markValue is 5 above closeValue. Fees/funding live in closeValue.
        # marginOnChain 100 must not dilute a loss against the original 25.
        p = plan(attached(), obs([pos(close=17.0, margin_on_chain=100.0)]), low, NOW)
        self.assertTrue(p["latched"])
        self.assertAlmostEqual(p["loss"], 8.0)
        held = plan(attached(), obs([pos(close=20.0, margin_on_chain=5.0)]), low, NOW)
        self.assertEqual(held["action"], "hold")
        self.assertAlmostEqual(held["loss_pct"], 20.0)

    def test_cash_already_received_counts_quote_does_not(self):
        # Equity = close value + measured cash. A quote of 10 is not passed in.
        st = loss_status(25, 10, 8)
        self.assertAlmostEqual(st["loss_pct"], 28.0)
        self.assertFalse(st["triggered"])
        st2 = loss_status(25, 10, 0)
        self.assertTrue(st2["triggered"])


class NoOpenNoSchedule(unittest.TestCase):
    def test_no_open_action_and_no_clock_close(self):
        self.assertNotIn("open", ALLOWED_ACTIONS)
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "oneoff", "guardian.py")
        with open(src_path) as fh:
            src = fh.read()
        self.assertNotIn('"action": "open"', src)
        self.assertNotIn("long_close_hour", src)
        fresh = obs([pos(close=24.0)])
        for now in (NOW, NOW + 5, NOW + 60 * 60 * 20):
            p = plan(attached(), fresh, low, now)
            self.assertEqual(p["action"], "hold", now)
            self.assertFalse(p["would_send"])

    def test_does_not_adopt_other_ids(self):
        bare = new_state()
        seen = obs([pos(pid="999", close=1.0)])
        p = plan(bare, seen, low, NOW)
        self.assertEqual(p["action"], "await")
        self.assertFalse(p["would_send"])
        self.assertIsNone(bare["position"])
        mixed = plan(attached(), obs([pos(close=24.0), pos(pid="999", close=1.0)]), low, NOW)
        self.assertEqual(mixed["position_id"], "200")
        self.assertEqual(mixed["ignored_ids"], ["999"])
        self.assertEqual(mixed["action"], "hold")


class Accounting(unittest.TestCase):
    def test_partial_reduce_keeps_original_margin_and_ignores_quote(self):
        s = attached(invested=25, notional=48)
        high_full = lambda m: {"impactPct": 6.0 if m >= 2_000_000 else 1.2, "exceedsLiquidity": False}
        calls = []

        def sender(payload):
            calls.append(payload)
            return {"txHash": "0xabc", "walletUsdcCredit": "none", "quoteEffectiveUsdMicro": "10000000",
                    "quoteProceedsAreWalletCash": False}

        s, planned = run_once(s, obs([pos(close=10.0)]), high_full, now=NOW, live=True, sender=sender)
        self.assertTrue(planned["sent"])
        self.assertEqual(planned["payload"]["action"], "reduce")
        self.assertEqual(planned["payload"]["maxImpactPct"], 4)
        self.assertEqual(planned["payload"]["slippagePct"], 3)
        self.assertEqual(s["position"]["invested_margin"], 25)
        self.assertEqual(s["position"]["open_notional"], 48)
        self.assertEqual(s["cash_out"], 0.0)
        self.assertEqual(s["inflight"]["ignored_quote_micro"], "10000000")
        # Quote micro must not have been added to cash.
        self.assertNotEqual(s["cash_out"], 10.0)
        later = obs([pos(close=12.0, micro=1_000_000)], asof=NOW + 5, usdc=100.0)
        s = reconcile(s, later, NOW + 5)
        self.assertIsNone(s["inflight"])
        self.assertEqual(s["position"]["invested_margin"], 25)
        self.assertEqual(s["cash_out"], 0.0)
        self.assertFalse(s["closed"])
        # Still latched on the original 25, using the new close value, not the quote.
        again = plan(s, later, low, NOW + 5)
        self.assertTrue(again["latched"])
        self.assertAlmostEqual(again["loss"], 13.0)

    def test_full_close_books_measured_usdc_only(self):
        s = attached()
        s["latched"] = True
        s["inflight"] = {"kind": "close", "micro": 2_000_000, "started": NOW, "size_micro_before": 2_000_000,
                         "usdc_before": 100.0, "error": None, "ignored_quote_micro": "999"}
        done = obs([], asof=NOW + 3, usdc=118.2)
        s = reconcile(s, done, NOW + 3)
        self.assertTrue(s["closed"])
        self.assertEqual(s["cash_out"], 18.2)
        self.assertEqual(s["position"]["invested_margin"], 25)
        self.assertAlmostEqual(s["net"], 18.2 - 25)
        self.assertIsNone(s["inflight"])


class UnsafeReads(unittest.TestCase):
    def test_stale_failed_and_incomplete_are_not_safe(self):
        s = attached(latched=True)
        for bad in (obs([pos(close=1)], asof=NOW - 21),
                    obs([pos(close=1)], ok=False),
                    obs([pos(close=1)], complete=False),
                    obs([pos(close=1)], error="timeout"),
                    None):
            p = plan(s, bad, low, NOW)
            self.assertFalse(p["chain_trusted"], bad)
            self.assertFalse(p["send"])
            self.assertFalse(p["would_send"])
            self.assertTrue(p["latched"])
        # A stale terrible print must not create a new latch either.
        fresh = attached(latched=False)
        p = plan(fresh, obs([pos(close=0.0)], asof=NOW - 30), low, NOW)
        self.assertFalse(p["latched"])
        self.assertFalse(p["chain_trusted"])

    def test_missing_close_value_is_not_safe(self):
        bad = pos(close=17.0)
        bad["closeValue"] = None
        p = plan(attached(), obs([bad]), low, NOW)
        self.assertFalse(p["chain_trusted"])
        self.assertFalse(p["would_send"])


class RestartAndIdentity(unittest.TestCase):
    def test_restart_stays_latched_even_if_price_recovers(self):
        s = attached(latched=True)
        s["latch_reason"] = "loss >= 30%"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "guardian.json")
            save_state(s, path)
            loaded = load_state(path)
        self.assertTrue(loaded["latched"])
        self.assertEqual(loaded["position"]["invested_margin"], 25)
        p = plan(loaded, obs([pos(close=25.0)]), low, NOW)
        self.assertTrue(p["would_send"])
        self.assertEqual(p["payload"]["action"], "close")
        self.assertIn("latched", p["reason"])

    def test_wrong_owner_and_wrong_side_do_not_send(self):
        own = plan(attached(), obs([pos(owner="0x" + "11" * 20)], wallet="0x" + "11" * 20), low, NOW)
        self.assertEqual(own["action"], "refuse")
        self.assertFalse(own["would_send"])
        mismatch = plan(attached(), obs([pos()], wallet="0x" + "22" * 20), low, NOW)
        self.assertEqual(mismatch["action"], "refuse")
        side = plan(attached(), obs([pos(is_long=False, micro=-2_000_000)]), low, NOW)
        self.assertEqual(side["action"], "refuse")
        self.assertFalse(side["would_send"])
        self.assertIsNone(side["payload"])

    def test_no_duplicate_tx_while_inflight_or_before_a_later_read(self):
        calls = []

        def sender(payload):
            calls.append(payload)
            return {"txHash": "0x1", "walletUsdcCredit": "none", "quoteEffectiveUsdMicro": "1"}

        s, first = run_once(attached(), obs([pos(close=10)]), low, now=NOW, live=True, sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertTrue(first["sent"])
        # Same read cannot clear inflight or send again.
        s, second = run_once(s, obs([pos(close=10)], asof=NOW), low, now=NOW + 1, live=True, sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertFalse(second.get("sent", False))
        self.assertIsNotNone(s["inflight"])
        # A later read that still shows the old size reconciles and still does not send in that step.
        s, third = run_once(s, obs([pos(close=10)], asof=NOW + 5), low, now=NOW + 6, live=True, sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(s["inflight"])
        self.assertFalse(third.get("sent", False))


class AttachAndHealth(unittest.TestCase):
    def test_attach_requires_fresh_long_and_does_not_infer_notional(self):
        base = obs([pos(close=24)])
        s = attach(new_state(), base, pid="200", invested_margin=25, open_notional=None, now=NOW, perp=PERP)
        self.assertEqual(s["position"]["invested_margin"], 25)
        self.assertIsNone(s["position"]["open_notional"])
        with self.assertRaises(GuardianError):
            attach(new_state(), base, pid="200", invested_margin=25, open_notional=55.13, now=NOW)
        with self.assertRaises(GuardianError):
            attach(new_state(), base, pid="200", invested_margin=25.01, open_notional=50, now=NOW)
        ok = attach(new_state(), base, pid="200", invested_margin=MAX_INVESTED_MARGIN,
                    open_notional=MAX_OPEN_NOTIONAL, now=NOW)
        self.assertEqual(ok["position"]["open_notional"], 50)
        with self.assertRaises(GuardianError):
            attach(new_state(), obs([pos()], asof=NOW - 60), pid="200", invested_margin=25,
                   open_notional=40, now=NOW)
        with self.assertRaises(GuardianError):
            attach(new_state(), obs([pos(is_long=False)]), pid="200", invested_margin=25,
                   open_notional=40, now=NOW)
        with self.assertRaises(GuardianError):
            attach(s, obs([pos(pid="201")]), pid="201", invested_margin=25, open_notional=40, now=NOW)
        # Idempotent same basis. A different basis is refused.
        again = attach(s, base, pid="200", invested_margin=25, open_notional=None, now=NOW + 1)
        self.assertEqual(again["position"]["invested_margin"], 25)
        with self.assertRaises(GuardianError):
            attach(s, base, pid="200", invested_margin=20, open_notional=None, now=NOW + 1)

    def test_health_before_and_after_attach(self):
        pre = assess_health(new_state(), obs([]), now=NOW, flags_ok=False)
        self.assertTrue(pre["ready_dry_run"])
        self.assertFalse(pre["ready_live"])
        self.assertFalse(pre["config"]["opens"])
        self.assertFalse(pre["config"]["scheduled_close"])
        self.assertFalse(pre["config"]["fill_guaranteed"])
        flagged = assess_health(new_state(), obs([]), now=NOW, flags_ok=True)
        self.assertTrue(flagged["ready_live"])
        s = attach(new_state(), obs([pos(close=24)]), pid="200", invested_margin=24.5,
                   open_notional=49.0, now=NOW)
        post = assess_health(s, obs([pos(close=24)]), now=NOW, flags_ok=True)
        self.assertTrue(post["position_verified"])
        self.assertTrue(post["ready_live"])
        stale = assess_health(s, obs([pos(close=24)], asof=NOW - 25), now=NOW, flags_ok=True)
        self.assertFalse(stale["chain_trusted"])
        self.assertFalse(stale["ready_live"])
        self.assertIn("chain_unusable", stale["problems"])
        off = assess_health(s, obs([pos(close=24)]), now=NOW, flags_ok=False)
        self.assertFalse(off["ready_live"])

    def test_live_flags_default_closed(self):
        self.assertFalse(live_flags({}))
        self.assertFalse(live_flags({"DRY_RUN": "0", "LIVE_TRADING": "1"}))
        self.assertTrue(live_flags({"DRY_RUN": "0", "LIVE_TRADING": "1", "CONFIRM_LIVE": "I_UNDERSTAND_REAL_MONEY"}))

    def test_dry_run_does_not_call_sender(self):
        def sender(_p):
            raise AssertionError("dry-run sent")
        s, p = run_once(attached(), obs([pos(close=10)]), low, now=NOW, live=False, sender=sender)
        self.assertTrue(p["would_send"])
        self.assertFalse(p["send"])
        self.assertTrue(s["latched"])
        self.assertIsNone(s["inflight"])

    def test_refuses_bot_state_path(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "params.live.json")
            with self.assertRaises(GuardianError):
                save_state(new_state(), bad)

    def test_size_string_and_impact_cap_slice(self):
        self.assertEqual(size_str(1_250_000), "1.250000")
        self.assertEqual(size_str(10), "0.000010")
        s = attached(latched=True)
        s["last_failed_micro"] = 2_000_000
        def quotes(m):
            return {"impactPct": 9.0 if m >= 2_000_000 else 3.5, "exceedsLiquidity": False}
        p = plan(s, obs([pos(close=10, micro=2_000_000)]), quotes, NOW)
        self.assertEqual(p["action"], "reduce")
        self.assertEqual(p["payload"]["size"], size_str(1_000_000))
        self.assertLessEqual(p["impactPct"], STOP_IMPACT)


class ProtocolReject(unittest.TestCase):
    def test_price_impact_reject_retries_smaller_without_a_second_send(self):
        calls = []

        def sender(payload):
            calls.append(payload)
            raise RuntimeError("PriceImpactTooHigh")

        s, first = run_once(attached(), obs([pos(close=10)]), low, now=NOW, live=True, sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["action"], "close")
        self.assertIn("PriceImpactTooHigh", s["inflight"]["error"])
        s, mid = run_once(s, obs([pos(close=10)], asof=NOW + 2), low, now=NOW + 3, live=True, sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertEqual(s["last_failed_micro"], 2_000_000)
        self.assertIsNone(s["inflight"])
        def quotes(m):
            return {"impactPct": 8.0 if m >= 2_000_000 else 2.0, "exceedsLiquidity": False}
        s, nxt = run_once(s, obs([pos(close=10)], asof=NOW + 4), quotes, now=NOW + 4, live=True, sender=sender)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["action"], "reduce")
        self.assertTrue(s["latched"])
        self.assertEqual(s["position"]["invested_margin"], 25)


if __name__ == "__main__":
    unittest.main()

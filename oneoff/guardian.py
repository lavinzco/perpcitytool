#!/usr/bin/env python3
"""One-off LONG stop guardian.

Watches only an explicitly attached position id. It never opens, never closes on a
clock, and never adopts any other position. A loss of at least 30% of the original
invested margin latches a stop. The loss uses the fresh chain closeValue after the
position exists (fees and funding included). A pre-open quote_close is not that
number and cannot latch a stop. The latch stays until a fresh read shows that id is
gone. Full close uses the existing stop limits (impact 4%, slippage 3%). If that
size cannot be sent, a smaller reduce is used. Those caps are not lifted. Quote
proceeds are not wallet cash. A sent order is not a guaranteed fill.

No live send unless both --live and the three trade.mjs switches are set. This
process does not read PRIVATE_KEY. It does not touch params.live.json or the
ordinary bot state.
"""
from __future__ import annotations

import argparse, json, math, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategy import Lot, stop_status

PERP_DEFAULT = "0xb9F540CE4315e7F4382C96532f8eC3903899D86d"
STOP_PCT = 30.0
STOP_IMPACT = 4.0
STOP_SLIPPAGE = 3.0
MAX_INVESTED_MARGIN = 25.0
MAX_OPEN_NOTIONAL = 50.0
STALE_SECONDS = 20.0
SLICE_DENOMS = (1, 2, 4, 10)
ALLOWED_ACTIONS = frozenset({"positions", "quote_close", "close", "reduce"})


class GuardianError(RuntimeError):
    pass


def home_dir() -> str:
    return os.environ.get("ONEOFF_HOME") or os.path.join(ROOT, "oneoff", "runtime")


def state_path() -> str:
    return os.environ.get("ONEOFF_STATE") or os.path.join(home_dir(), "state", "guardian.json")


def lock_path() -> str:
    return os.environ.get("ONEOFF_LOCK") or os.path.join(home_dir(), "guardian.lock")


def trade_mjs() -> str:
    return os.environ.get("TRADE_MJS") or os.path.join(ROOT, "live", "trade.mjs")


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def forbidden_targets() -> set[str]:
    names = ("params.live.json", "params.example.json", "params.live_short.example.json",
             os.path.join("state", "state.json"), os.path.join("state", "live_state.json"))
    return {_real(os.path.join(ROOT, n)) for n in names}


def assert_isolated(path: str) -> str:
    """Refuse the ordinary bot parameter and state files."""
    rp = _real(path)
    if rp in forbidden_targets() or os.path.basename(rp) == "params.live.json":
        raise GuardianError(f"refusing bot path {rp}")
    return rp


def finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def live_flags(env: dict | None = None) -> bool:
    e = os.environ if env is None else env
    return (e.get("DRY_RUN") == "0" and e.get("LIVE_TRADING") == "1"
            and e.get("CONFIRM_LIVE") == "I_UNDERSTAND_REAL_MONEY")


def new_state() -> dict:
    return {"version": 1, "position": None, "latched": False, "latch_reason": None,
            "closed": False, "inflight": None, "cash_out": 0.0, "cash_unknown": False,
            "last_failed_micro": None, "events": [], "fill_guaranteed": False}


def load_state(path: str | None = None) -> dict:
    path = assert_isolated(path or state_path())
    if not os.path.exists(path):
        return new_state()
    with open(path) as f:
        s = json.load(f)
    base = new_state()
    base.update(s)
    base["fill_guaranteed"] = False
    if "cash_out" not in s:
        base["cash_out"] = 0.0
    return base


def save_state(state: dict, path: str | None = None) -> None:
    path = assert_isolated(path or state_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = dict(state)
    state["fill_guaranteed"] = False
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Lock:
    def __init__(self, path: str | None = None):
        self.path = assert_isolated(path or lock_path())
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            raise GuardianError("guardian already running") from None
        return self

    def __exit__(self, *a):
        if self.fh:
            try:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()


def size_str(micro: int) -> str:
    if not isinstance(micro, int) or isinstance(micro, bool) or micro <= 0:
        raise GuardianError("reduce size must be a positive integer micro amount")
    whole, frac = divmod(micro, 1_000_000)
    return f"{whole}.{frac:06d}"


def slice_micros(remaining: int, last_failed: int | None) -> list[int]:
    if remaining <= 0:
        return []
    sizes = []
    for d in SLICE_DENOMS:
        m = remaining // d
        if m >= 1 and m not in sizes:
            sizes.append(m)
    if not sizes:
        return []
    if last_failed is None:
        return sizes
    smaller = [m for m in sizes if m < int(last_failed)]
    return smaller or [sizes[-1]]


def loss_status(invested: float, close_value: float, cash_out: float, stop_pct: float = STOP_PCT) -> dict:
    """Loss versus the original invested margin. close_value is the chain estimate
    of what closing the remainder would return (fees and funding included).
    cash_out is measured wallet USDC already received, never a quote."""
    if not finite(invested) or invested <= 0 or not finite(close_value) or not finite(cash_out):
        raise GuardianError("stop basis is not a finite positive invested margin and finite values")
    equity = float(close_value) + float(cash_out)
    st = stop_status([Lot(id="oneoff", margin=float(invested), notional=0.0, size=0.0,
                          entry_price=0.0, close_value=equity)], stop_pct)
    st["equity"] = equity
    st["cash_out"] = float(cash_out)
    st["invested_margin"] = float(invested)
    return st


def trusted(obs: dict | None, now: float) -> bool:
    if not isinstance(obs, dict) or obs.get("ok") is not True or obs.get("complete") is not True:
        return False
    if obs.get("error"):
        return False
    asof = obs.get("asof")
    if not finite(asof) or not finite(now):
        return False
    if float(now) + 1 < float(asof):
        return False
    if float(now) - float(asof) > STALE_SECONDS:
        return False
    return True


def _wallet(obs: dict) -> str:
    return str(obs.get("wallet") or "").lower()


def find_position(obs: dict, pid: str) -> dict | None:
    want = str(pid)
    for p in obs.get("positions") or []:
        if str(p.get("positionId")) == want:
            return p
    return None


def owner_ok(pos: dict, obs: dict, expected: str) -> bool:
    exp = str(expected or "").lower()
    if not exp or _wallet(obs) != exp:
        return False
    own = pos.get("owner")
    if own is not None and str(own).lower() != exp:
        return False
    return True


def close_payload(perp: str, pid: str) -> dict:
    return {"action": "close", "perp": perp, "positionId": str(pid), "side": "long",
            "slippagePct": STOP_SLIPPAGE, "maxImpactPct": STOP_IMPACT}


def reduce_payload(perp: str, pid: str, micro: int) -> dict:
    return {"action": "reduce", "perp": perp, "positionId": str(pid), "side": "long",
            "size": size_str(micro), "slippagePct": STOP_SLIPPAGE, "maxImpactPct": STOP_IMPACT}


def _quote_ok(q: dict | None) -> bool:
    return isinstance(q, dict) and finite(q.get("impactPct")) and q.get("exceedsLiquidity") is not True


def choose_order(remaining: int, last_failed, quotes) -> tuple[dict | None, int | None, str]:
    """Pick a full close or a smaller reduce whose quoted impact is within 4%.
    quotes(micro) is read-only. A quote failure fails closed (no send)."""
    sizes = slice_micros(remaining, None if last_failed in (None, "") else int(last_failed))
    if not sizes:
        return None, None, "no_size"
    saw_quote = False
    for i, micro in enumerate(sizes):
        try:
            q = quotes(micro)
        except Exception as e:
            return None, None, f"quote_failed:{e}"[:240]
        if not isinstance(q, dict) or "impactPct" not in q:
            return None, None, "quote_failed"
        saw_quote = True
        if q.get("exceedsLiquidity") is True:
            continue
        if not finite(q.get("impactPct")):
            return None, None, "quote_failed"
        if float(q["impactPct"]) <= STOP_IMPACT + 1e-9:
            return ({"micro": micro, "full": micro == remaining, "impactPct": float(q["impactPct"])},
                    micro, "within_cap")
        if i == 0 and micro == remaining:
            continue
    if not saw_quote:
        return None, None, "quote_failed"
    return None, None, "impact_above_cap"


def plan(state: dict, obs: dict | None, quotes, now: float) -> dict:
    """Pure step. Does not send, does not adopt other ids, does not open."""
    out = {"action": "hold", "send": False, "would_send": False, "latched": bool(state.get("latched")),
           "closed": bool(state.get("closed")), "chain_trusted": False, "reason": "",
           "payload": None, "loss_pct": None, "fill_guaranteed": False, "position_id": None}
    posrec = state.get("position")
    if not posrec:
        out["action"] = "await"
        out["reason"] = "no explicit position attached"
        out["chain_trusted"] = trusted(obs, now)
        return out
    out["position_id"] = str(posrec["id"])
    if state.get("closed"):
        out["action"] = "closed"
        out["reason"] = "attached position already confirmed gone; no new position will be adopted"
        out["chain_trusted"] = trusted(obs, now)
        return out
    if state.get("inflight"):
        out["action"] = "reconcile"
        out["reason"] = "inflight tx not reconciled; not sending another"
        out["chain_trusted"] = trusted(obs, now)
        return out
    if not trusted(obs, now):
        out["reason"] = "chain read missing, incomplete, or stale; not treated as safe"
        return out
    out["chain_trusted"] = True
    perp = posrec["perp"]
    if str(obs.get("perp") or perp).lower() != str(perp).lower():
        out["action"] = "refuse"
        out["reason"] = "chain read is for a different market"
        return out
    hit = find_position(obs, posrec["id"])
    others = [str(p.get("positionId")) for p in (obs.get("positions") or [])
              if str(p.get("positionId")) != str(posrec["id"])]
    out["ignored_ids"] = others
    if hit is None:
        out["action"] = "flat"
        out["reason"] = "attached id is not in this wallet's complete position set"
        out["mark_closed"] = True
        return out
    if not owner_ok(hit, obs, posrec.get("expected_wallet")):
        out["action"] = "refuse"
        out["reason"] = "owner does not match the wallet recorded at attach"
        return out
    if hit.get("isLong") is not True:
        out["action"] = "refuse"
        out["reason"] = "position is not a long; refusing close"
        return out
    if not finite(hit.get("closeValue")):
        out["chain_trusted"] = False
        out["reason"] = "close value missing; fee/funding estimate unavailable"
        return out
    try:
        micro = int(hit["perpDelta"])
    except (KeyError, TypeError, ValueError):
        out["chain_trusted"] = False
        out["reason"] = "perpDelta missing"
        return out
    if micro <= 0:
        out["action"] = "refuse"
        out["reason"] = "remaining size is not a positive long"
        return out
    st = loss_status(posrec["invested_margin"], hit["closeValue"], state.get("cash_out") or 0.0, STOP_PCT)
    out["loss_pct"] = st["loss_pct"]
    out["loss"] = st["loss"]
    out["equity"] = st["equity"]
    triggered = bool(st["triggered"])
    if triggered or state.get("latched"):
        out["latched"] = True
        if not state.get("latched"):
            out["latch_now"] = True
            out["reason"] = f"loss {st['loss_pct']:.4f}% of original margin >= {STOP_PCT:g}%"
        else:
            out["reason"] = "stop latched; retrying close"
    else:
        out["action"] = "hold"
        out["reason"] = f"loss {st['loss_pct']:.4f}% < {STOP_PCT:g}% of original invested margin; holding"
        return out
    picked, micro_send, why = choose_order(micro, state.get("last_failed_micro"), quotes)
    if picked is None:
        out["action"] = "hold"
        out["reason"] = f"latched but not sending: {why}"
        return out
    if picked["full"]:
        payload = close_payload(perp, posrec["id"])
        action = "close"
    else:
        payload = reduce_payload(perp, posrec["id"], picked["micro"])
        action = "reduce"
    if payload["action"] not in ALLOWED_ACTIONS:
        raise GuardianError("refusing unexpected action")
    out.update(action=action, send=True, would_send=True, payload=payload, micro=picked["micro"],
               impactPct=picked["impactPct"], reason=out["reason"] + f"; {action} impact {picked['impactPct']:.2f}%")
    return out


def book_cash(state: dict, usdc_before, usdc_after) -> None:
    """Book the measured USDC balance change only. Caller must not pass a quote."""
    if not finite(usdc_before) or not finite(usdc_after):
        state["cash_unknown"] = True
        return
    state["cash_out"] = round(float(state.get("cash_out") or 0.0) + (float(usdc_after) - float(usdc_before)), 6)


def reconcile(state: dict, obs: dict, now: float) -> dict:
    """Clear inflight only from a fresh read taken after the send. Does not send."""
    inf = state.get("inflight")
    if not inf:
        return state
    if not trusted(obs, now) or not finite(obs.get("asof")) or float(obs["asof"]) <= float(inf["started"]):
        return state
    state = json.loads(json.dumps(state))
    pos = state.get("position") or {}
    hit = find_position(obs, pos.get("id"))
    before = int(inf["size_micro_before"])
    book_cash(state, inf.get("usdc_before"), obs.get("usdc"))
    if hit is None and owner_ok({"owner": obs.get("wallet")}, obs, pos.get("expected_wallet")):
        state["inflight"] = None
        state["closed"] = True
        state["last_failed_micro"] = None
        if not state.get("cash_unknown"):
            state["net"] = round(float(state["cash_out"]) - float(pos["invested_margin"]), 6)
        else:
            state["net"] = None
        _event(state, "closed", "position absent after inflight")
        return state
    if hit is None or hit.get("isLong") is not True:
        return state
    try:
        now_micro = int(hit["perpDelta"])
    except (KeyError, TypeError, ValueError):
        return state
    if now_micro == before:
        state["inflight"] = None
        if inf.get("error") and "PriceImpactTooHigh" in str(inf.get("error")):
            state["last_failed_micro"] = int(inf["micro"])
        _event(state, "unfilled", "size unchanged after inflight")
        return state
    if now_micro < before:
        state["inflight"] = None
        state["last_failed_micro"] = None
        _event(state, "reduced", f"{before}->{now_micro}")
        return state
    state["anomaly"] = "size_increased_or_flipped"
    return state


def _event(state: dict, kind: str, detail: str) -> None:
    ev = state.setdefault("events", [])
    ev.append({"kind": kind, "detail": detail})
    del ev[:-50]


def apply_latch(state: dict, planned: dict) -> dict:
    if planned.get("latch_now"):
        state["latched"] = True
        state["latch_reason"] = planned.get("reason")
    if planned.get("mark_closed") and planned.get("chain_trusted") and not state.get("inflight"):
        state["closed"] = True
        state["latched"] = True
        # No paired wallet measurement for an external disappearance. Do not invent net.
        if state.get("net") is None:
            state["cash_unknown"] = True
    return state


def arm_inflight(state: dict, planned: dict, obs: dict, now: float) -> dict:
    hit = find_position(obs, state["position"]["id"])
    state["inflight"] = {
        "kind": planned["action"], "micro": int(planned["micro"]), "started": float(now),
        "size_micro_before": int(hit["perpDelta"]), "usdc_before": obs.get("usdc"),
        "txHash": None, "error": None,
    }
    return state


def note_send(state: dict, result: dict | None, error: str | None) -> dict:
    inf = state.get("inflight") or {}
    if error:
        inf["error"] = error[:400]
    elif isinstance(result, dict):
        inf["txHash"] = result.get("txHash")
        inf["wallet_credit"] = result.get("walletUsdcCredit")
        # Retained only so a later audit can see the quote was ignored.
        inf["ignored_quote_micro"] = result.get("quoteEffectiveUsdMicro")
    state["inflight"] = inf
    return state


def attach(state: dict, obs: dict, *, pid: str, invested_margin: float, open_notional, now: float,
           perp: str = PERP_DEFAULT) -> dict:
    """Record the explicit id and the original invested margin. Requires a fresh long owned by this wallet."""
    if state.get("position") and not state.get("closed"):
        if str(state["position"]["id"]) != str(pid):
            raise GuardianError("a different one-off id is already attached")
        if abs(float(state["position"]["invested_margin"]) - float(invested_margin)) > 1e-9:
            raise GuardianError("refusing to rewrite original invested margin")
        return state
    state = json.loads(json.dumps(state))
    if state.get("closed"):
        raise GuardianError("one-off already closed; not attaching another position")
    if not trusted(obs, now):
        raise GuardianError("attach refused: chain read missing, incomplete, or stale")
    if not finite(invested_margin) or invested_margin <= 0 or invested_margin > MAX_INVESTED_MARGIN + 1e-9:
        raise GuardianError(f"invested margin must be within (0, {MAX_INVESTED_MARGIN:g}] USDC")
    if open_notional is not None:
        if not finite(open_notional) or open_notional <= 0 or open_notional > MAX_OPEN_NOTIONAL + 1e-9:
            raise GuardianError(f"actual open notional must be within (0, {MAX_OPEN_NOTIONAL:g}] USDC")
    if str(obs.get("perp") or perp).lower() != perp.lower():
        raise GuardianError("chain read market does not match perp")
    hit = find_position(obs, pid)
    if hit is None:
        raise GuardianError("position id is not owned in this complete read; not adopting another id")
    wallet = _wallet(obs)
    if not wallet or not owner_ok(hit, obs, wallet):
        raise GuardianError("owner check failed")
    if hit.get("isLong") is not True:
        raise GuardianError("refusing to attach a short")
    if not finite(hit.get("closeValue")):
        raise GuardianError("close value missing on attach")
    state = dict(state)
    state["position"] = {
        "id": str(pid), "invested_margin": float(invested_margin),
        "open_notional": None if open_notional is None else float(open_notional),
        "perp": perp, "expected_wallet": wallet, "attached_at": float(now),
    }
    state["closed"] = False
    _event(state, "attach", str(pid))
    return state


def assess_health(state: dict, obs: dict | None, *, now: float, flags_ok: bool) -> dict:
    cfg = {"stop_pct": STOP_PCT, "stop_impact": STOP_IMPACT, "stop_slippage": STOP_SLIPPAGE,
           "max_invested_margin": MAX_INVESTED_MARGIN, "max_open_notional": MAX_OPEN_NOTIONAL,
           "opens": False, "scheduled_close": False, "fill_guaranteed": False}
    chain_ok = trusted(obs, now)
    pos = state.get("position")
    verified = None
    problems = []
    if not chain_ok:
        problems.append("chain_unusable")
    if state.get("inflight") and (not chain_ok or float(obs.get("asof") or 0) <= float(state["inflight"]["started"])):
        problems.append("inflight_unreconciled")
    if pos and not state.get("closed"):
        if not chain_ok:
            verified = False
        else:
            hit = find_position(obs, pos["id"])
            verified = bool(hit and hit.get("isLong") is True and owner_ok(hit, obs, pos.get("expected_wallet"))
                            and finite(hit.get("closeValue")) and str(obs.get("perp") or pos["perp"]).lower() == pos["perp"].lower())
            if not verified:
                problems.append("attached_position_not_verified")
    ready_live = bool(flags_ok and chain_ok and "inflight_unreconciled" not in problems and verified is not False)
    ready_dry = "config"  # config is hardcoded; dry-run does not require chain
    return {"ok": True, "ready_dry_run": True, "ready_live": ready_live, "flags_ok": bool(flags_ok),
            "chain_trusted": chain_ok, "attached": bool(pos), "position_verified": verified,
            "latched": bool(state.get("latched")), "closed": bool(state.get("closed")),
            "problems": problems, "config": cfg, "fill_guaranteed": False,
            "ignored_other_ids": [str(p.get("positionId")) for p in ((obs or {}).get("positions") or [])
                                  if not pos or str(p.get("positionId")) != str(pos.get("id"))]}


def run_once(state: dict, obs, quotes, *, now: float, live: bool, sender=None) -> tuple[dict, dict]:
    """One locked step worth of logic, without taking the lock itself.
    If an inflight record exists, this only reconciles. It never sends a second tx
    in that step."""
    state = json.loads(json.dumps(state))
    if state.get("inflight"):
        before = json.dumps(state.get("inflight"))
        state = reconcile(state, obs or {}, now)
        changed = json.dumps(state.get("inflight")) != before
        planned = plan(state, obs, quotes, now)
        planned["action"] = "reconcile"
        planned["send"] = False
        planned["would_send"] = False
        planned["reconciled"] = changed and not state.get("inflight")
        planned["fill_guaranteed"] = False
        return state, planned
    planned = plan(state, obs, quotes or (lambda _m: {"impactPct": 99.0, "exceedsLiquidity": False}), now)
    state = apply_latch(state, planned)
    if not planned.get("send"):
        return state, planned
    if not live:
        planned["send"] = False
        planned["dry_run"] = True
        return state, planned
    if sender is None:
        raise GuardianError("live send requested without a sender")
    state = arm_inflight(state, planned, obs, now)
    try:
        result = sender(planned["payload"])
    except Exception as e:
        state = note_send(state, None, str(e))
        planned["sent"] = False
        planned["error"] = str(e)[:400]
        planned["send"] = False
        return state, planned
    state = note_send(state, result if isinstance(result, dict) else {}, None)
    planned["sent"] = True
    planned["txHash"] = (result or {}).get("txHash") if isinstance(result, dict) else None
    planned["send"] = False
    planned["fill_guaranteed"] = False
    return state, planned


def node_call(payload: dict, timeout: int = 120) -> dict:
    if payload.get("action") not in ALLOWED_ACTIONS:
        raise GuardianError(f"refusing action {payload.get('action')}")
    import subprocess
    r = subprocess.run(["node", trade_mjs()], input=json.dumps(payload), capture_output=True,
                       text=True, timeout=timeout, env=os.environ.copy())
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise GuardianError(err[-800:] or "trade.mjs failed")
    line = (r.stdout or "").strip().splitlines()
    if not line:
        raise GuardianError("trade.mjs returned no JSON")
    return json.loads(line[-1])


def read_chain(perp: str) -> dict:
    now = time.time()
    try:
        raw = node_call({"action": "positions", "perp": perp, "ignore": []})
    except Exception as e:
        return {"ok": False, "complete": False, "error": str(e)[:400], "asof": now, "perp": perp,
                "positions": [], "wallet": None, "usdc": None}
    return {"ok": bool(raw.get("ok", True)), "complete": raw.get("complete") is True, "error": None,
            "asof": now, "perp": perp, "wallet": raw.get("wallet"), "usdc": raw.get("usdc"),
            "positions": raw.get("positions") or []}


def quote_fn(perp: str):
    def quotes(micro: int) -> dict:
        return node_call({"action": "quote_close", "perp": perp, "perpDelta": str(int(micro)), "isLong": True})
    return quotes


def emit(obj: dict, code: int = 0) -> int:
    obj = dict(obj)
    obj["fill_guaranteed"] = False
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return code


def cmd_health(args) -> int:
    perp = args.perp
    with Lock():
        state = load_state()
        obs = read_chain(perp)
        report = assess_health(state, obs, now=time.time(), flags_ok=live_flags())
        report["state_path"] = assert_isolated(state_path())
        report["lock_path"] = assert_isolated(lock_path())
        if not report["chain_trusted"] or (args.require_live and not report["ready_live"]):
            return emit(report, 2)
        return emit(report, 0)


def cmd_attach(args) -> int:
    perp = args.perp
    with Lock():
        state = load_state()
        obs = read_chain(perp)
        try:
            state = attach(state, obs, pid=str(args.id), invested_margin=float(args.invested_margin),
                           open_notional=None if args.notional is None else float(args.notional),
                           now=time.time(), perp=perp)
        except GuardianError as e:
            return emit({"ok": False, "error": str(e), "attached": False}, 2)
        save_state(state)
        report = assess_health(state, obs, now=time.time(), flags_ok=live_flags())
        report["ok"] = report.get("position_verified") is True
        return emit(report, 0 if report["ok"] else 2)


def cmd_once(args) -> int:
    perp = args.perp
    live = bool(args.live)
    if live and not live_flags():
        return emit({"ok": False, "error": "live refused: DRY_RUN=0 LIVE_TRADING=1 CONFIRM_LIVE missing",
                     "sent": False}, 2)
    with Lock():
        state = load_state()
        obs = read_chain(perp)
        state, planned = run_once(state, obs, quote_fn(perp), now=time.time(), live=live,
                                  sender=node_call if live else None)
        save_state(state)
        if planned.get("sent"):
            obs2 = read_chain(perp)
            state = reconcile(state, obs2, time.time())
            save_state(state)
            planned["post_read_trusted"] = trusted(obs2, time.time())
            planned["closed"] = bool(state.get("closed"))
            planned["cash_out"] = state.get("cash_out")
        planned["ok"] = True
        planned["live"] = live
        code = 0 if planned.get("chain_trusted") else 4
        return emit(planned, code)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="One-off long stop guardian (does not open)")
    p.add_argument("--perp", default=os.environ.get("PERP_ADDRESS", PERP_DEFAULT))
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health")
    h.add_argument("--require-live", action="store_true")
    a = sub.add_parser("attach")
    a.add_argument("--id", required=True)
    a.add_argument("--invested-margin", type=float, required=True)
    a.add_argument("--notional", type=float, default=None,
                   help="Actual opening notional. Do not pass margin times leverage.")
    o = sub.add_parser("once")
    o.add_argument("--live", action="store_true", help="Send only if the three live switches are also set")
    args = p.parse_args(argv)
    try:
        if args.cmd == "health":
            return cmd_health(args)
        if args.cmd == "attach":
            return cmd_attach(args)
        if args.cmd == "once":
            return cmd_once(args)
    except GuardianError as e:
        return emit({"ok": False, "error": str(e), "fill_guaranteed": False}, 2)
    return emit({"ok": False, "error": "unknown command"}, 2)


if __name__ == "__main__":
    sys.exit(main())

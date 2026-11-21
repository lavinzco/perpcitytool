# One-off stop guardian

This process only stop-manages an explicit long position id. It does not open, does not
close on a clock, and does not read or write `params.live.json` or the ordinary bot
state. Do not start `run_live.sh` or `watchdog.sh`. Leave the scheduled strategy paused
(`daily_total_open_cap_usd=1`, `short_enabled=false`) in whatever file the opener uses.
This guardian never loads that file.

Stop rule: unrealized loss `>= 30%` of the **original invested margin** (the USDC
actually spent to open, recorded at attach). The close value is the fresh chain
`closeValue` (fees, funding, and utilization included) plus measured wallet USDC
already received. A quote is not cash. Margin on chain is not the basis. Margin 25
does not imply notional 50.

Limits hardcoded here: invested margin `<= 25`, actual open notional `<= 50` if you
record it, stop impact `4%`, stop slippage `3%`. A full close that cannot fit under
4% uses `reduce` slices. A stop latches and retries. Fills are not guaranteed.

## Install the runtime directory

State and the lock live only under `ONEOFF_HOME`.

```bash
mkdir -p /workspace/one-off/state /workspace/one-off/logs
export ONEOFF_HOME=/workspace/one-off
export ONEOFF_STATE=/workspace/one-off/state/guardian.json
export ONEOFF_LOCK=/workspace/one-off/guardian.lock
export TRADE_MJS=/absolute/path/to/repo/live/trade.mjs
export PERP_ADDRESS=0xb9F540CE4315e7F4382C96532f8eC3903899D86d
# PRIVATE_KEY and RPC_URL are read only by trade.mjs. Do not put them in a file here.
```

From this repo the program is `oneoff/guardian.py`. Run it from anywhere:

```bash
python3 /absolute/path/to/repo/oneoff/guardian.py health
```

`health` exits 2 until a fresh complete `positions` read succeeds. It still does
not send, and it does not open. Running it is not a precondition this program
can place on a separate opener.

`--live` does nothing unless all three switches are set in this process only:

```bash
export DRY_RUN=0 LIVE_TRADING=1 CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY
python3 /absolute/path/to/repo/oneoff/guardian.py health --require-live
```

`ready_live` is this process's own check. It does not open and it does not attach a position.

## After the separate opener fills

Pass the position id, the actual USDC spent (`invested-margin`), and the actual
opening notional if you know it. Do not pass `margin * leverage`. A notional above
50 or a margin above 25 is rejected. The chain read must show that id, owned by
this wallet, long, with a finite `closeValue`.

```bash
python3 /absolute/path/to/repo/oneoff/guardian.py attach \
  --id POSITION_ID --invested-margin ACTUAL_USDC_SPENT --notional ACTUAL_OPEN_NOTIONAL
python3 /absolute/path/to/repo/oneoff/guardian.py health --require-live
```

Attach is refused when the read is stale, incomplete, the wrong side, or the wrong
wallet. Health after attach must report `position_verified: true`.

## Watch

Dry-run (no send) even if the live switches are set:

```bash
python3 /absolute/path/to/repo/oneoff/guardian.py once
```

One live step (send at most one close or reduce, then re-read; no second send in that step):

```bash
python3 /absolute/path/to/repo/oneoff/guardian.py once --live
```

Repeat `once --live` on your own timer (for example once a minute) under the same
`ONEOFF_HOME`. A second process exits because of the lock. If a transaction outcome
is unknown, the next step only reconciles the fresh position size and USDC balance.
It does not assume the quote filled.

Stdout is one JSON object. `fill_guaranteed` is always false. Exit 4 means the chain
read was not trusted; the latch is left unchanged and nothing is sent.

There is no expiry and no time-of-day close. While the loss stays under 30% the
guardian holds. After it latches, it keeps retrying until a complete read no longer
contains that id. It will not attach a replacement. Stop impact stays 4% and stop
slippage stays 3% on every close and every reduce slice. If the pool will not take
the size inside those caps, the latch remains and the next step retries. Nothing
in that loop guarantees a fill.

## What is not a stop signal

A `quote_close` of the intended size against the pool **before** the open is not
the post-open close value. Opening buys first and moves the price; the close then
sells from that new price. Quoting the sell on the untouched pool double-counts
impact in the wrong direction and can print a loss near 30% when the instant
round trip is mostly fees. The reverse approximation can look near 5%. Neither
number is the guardian's trigger.

`oneoff/sequential_sim.mjs` runs the SDK's `simulateTakerSwapExact` twice on a
caller-supplied book: open, then close from the resulting sqrt price and tick-map
liquidity. It does not write, does not fork the chain, and does not latch a stop.
It warns when the caller says the index is at least 15 minutes old or the pool
was last touched at least 9.5 hours ago, and when the swap crosses ticks or runs
out of liquidity. Instant funding and utilization are zero in that sim. The only
stop input after a real open is a fresh `positions` `closeValue`.

```bash
cd /absolute/path/to/repo && node --test oneoff/sequential.test.mjs
```

That test needs `live/node_modules` (`npm ci --ignore-scripts` in `live/`). It uses
a synthetic book, not the live Hormuz pool. Do not treat its percentages as this
market's loss.

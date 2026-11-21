// Sequential open-then-close on a synthetic book. No RPC and no transaction.
import test from 'node:test';
import assert from 'node:assert/strict';
import { fixturePool, sequentialRoundTrip } from './sequential_sim.mjs';

test('pre-open close is not the post-open sequential loss', () => {
  // Thin synthetic book, not the live pool. Sized so both swaps still fill.
  const pool = fixturePool({ price: 10, liquidity: 250_000_000n, feeRate: 0.0111 });
  const r = sequentialRoundTrip(pool, { margin: 22, leverage: 2 }, {
    indexAgeSec: 15 * 60, lastTouchedAgeSec: 9.5 * 3600,
  });
  assert.equal(r.stop_trigger, false);
  assert.equal(r.uses_pre_open_quote_as_stop, false);
  assert.equal(r.chain_fork, false);
  assert.equal(r.fill_guaranteed, false);
  assert.equal(r.sequentialError, null);
  assert.ok(r.actualOpenNotionalUsd > 44);
  assert.ok(r.sequentialInstantLossPct < 10);
  assert.ok(r.preOpenIndependentCloseLossPct > 20);
  // Selling the same size into the untouched pool is a worse exit than selling
  // back from the post-open price. That gap is not a measured live-pool loss.
  assert.ok(r.preOpenIndependentCloseLossPct > r.sequentialInstantLossPct + 15);
  assert.ok(r.warnings.includes('stale_index'));
  assert.ok(r.warnings.includes('stale_last_touch'));
});

test('a deep book round trip stays near fees and does not invent a stop', () => {
  const pool = fixturePool({ price: 10, liquidity: 50_000_000_000_000n, feeRate: 0.0111 });
  const r = sequentialRoundTrip(pool, { margin: 22, leverage: 2 });
  assert.equal(r.sequentialError, null);
  assert.ok(r.sequentialInstantLossPct > 2);
  assert.ok(r.sequentialInstantLossPct < 12);
  assert.equal(r.stop_trigger, false);
  assert.equal(r.warnings.includes('stale_index'), false);
});

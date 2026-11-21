// Client-side sequential open-then-close. No RPC, no transaction.
// Uses SDK 0.19 simulateTakerSwapExact: the close starts at the open's end
// sqrt price and the tick-map liquidity there. An independent quote_close on
// the pre-open pool is reported only as a contrast. It is not a stop trigger.
import {
  activeLiquidityAt, derivePerpDelta, priceToSqrtPriceX96, priceToTick,
  simulateTakerSwapExact,
} from '../live/node_modules/@strobelabs/perpcity-sdk/dist/index.js';

const STALE_INDEX_SEC = 15 * 60;
const STALE_TOUCH_SEC = 9.5 * 3600;

function num(micro) {
  return Number(micro) / 1e6;
}

function openFeeMicro(open) {
  // Long: both legs are negative. The fee is the extra USDC beyond the pool delta.
  return (-open.effectiveUsdDelta) - (-open.usdDelta);
}

function closeValueUsd(margin, open, closeSwap) {
  const marginMicro = BigInt(Math.round(margin * 1e6));
  const marginAfterOpenFee = marginMicro - openFeeMicro(open);
  // Same split as trade.mjs positions(): on-chain margin + fee-inclusive close
  // proceeds + fee-exclusive entry usd leg. Funding and utilization are zero
  // on an instant round trip; this function does not add them.
  return num(marginAfterOpenFee + closeSwap.effectiveUsdDelta + open.usdDelta);
}

function lossPct(margin, value) {
  return (margin - value) / margin * 100;
}

function warningsOf(meta, open, close) {
  const warnings = [];
  if (meta.indexAgeSec != null && meta.indexAgeSec >= STALE_INDEX_SEC)
    warnings.push('stale_index');
  if (meta.lastTouchedAgeSec != null && meta.lastTouchedAgeSec >= STALE_TOUCH_SEC)
    warnings.push('stale_last_touch');
  if (open.exceedsLiquidity || close?.exceedsLiquidity)
    warnings.push('exceeds_liquidity');
  if ((open.ticksCrossed || 0) > 0 || (close?.ticksCrossed || 0) > 0)
    warnings.push('crossed_ticks');
  if (open.liquidityLimited || close?.liquidityLimited)
    warnings.push('liquidity_limited');
  return warnings;
}

/**
 * @param {object} pool sqrtPriceX96, liquidity, tickMap, markPrice, feeRate
 * @param {object} order margin, leverage
 * @param {object} [meta] indexAgeSec, lastTouchedAgeSec
 */
export function sequentialRoundTrip(pool, order, meta = {}) {
  const perpDelta = derivePerpDelta({
    margin: order.margin, leverage: order.leverage, price: pool.markPrice, isLong: true,
  });
  const common = {
    sqrtPriceX96: pool.sqrtPriceX96, liquidity: pool.liquidity, markPrice: pool.markPrice,
    tickMap: pool.tickMap, feeRate: pool.feeRate,
  };
  const open = simulateTakerSwapExact({ ...common, perpDelta });
  let preOpenClose = null;
  let preOpenError = null;
  try {
    preOpenClose = simulateTakerSwapExact({ ...common, perpDelta: -perpDelta });
  } catch (e) {
    preOpenError = String(e?.message || e);
  }
  let close = null;
  let sequentialError = null;
  if (!open.exceedsLiquidity && open.endSqrtPriceX96 > 0n) {
    try {
      const endLiquidity = activeLiquidityAt(pool.tickMap, open.endSqrtPriceX96);
      close = simulateTakerSwapExact({
        sqrtPriceX96: open.endSqrtPriceX96, liquidity: endLiquidity, perpDelta: -perpDelta,
        markPrice: pool.markPrice, tickMap: pool.tickMap, feeRate: pool.feeRate,
      });
    } catch (e) {
      sequentialError = String(e?.message || e);
    }
  } else {
    sequentialError = 'open exceeds liquidity; no post-open state to close from';
  }
  const sequentialValue = close && !close.exceedsLiquidity ? closeValueUsd(order.margin, open, close) : null;
  const preValue = preOpenClose && !preOpenClose.exceedsLiquidity
    ? closeValueUsd(order.margin, open, preOpenClose) : null;
  return {
    kind: 'hypothetical_client_sim',
    chain_fork: false,
    stop_trigger: false,
    uses_pre_open_quote_as_stop: false,
    fill_guaranteed: false,
    perpDelta: perpDelta.toString(),
    actualOpenNotionalUsd: open.exceedsLiquidity ? null : num(-open.effectiveUsdDelta),
    feeExclusiveOpenUsd: open.exceedsLiquidity ? null : num(-open.usdDelta),
    openExceedsLiquidity: open.exceedsLiquidity,
    openTicksCrossed: open.ticksCrossed,
    sequentialInstantLossPct: sequentialValue == null ? null : lossPct(order.margin, sequentialValue),
    preOpenIndependentCloseLossPct: preValue == null ? null : lossPct(order.margin, preValue),
    sequentialError,
    preOpenError,
    warnings: warningsOf(meta, open, close),
    note: 'sequentialInstantLossPct is a zero-time client sim of open then close. It is not the guardian stop, not a fill, and not a substitute for the post-open chain closeValue.',
  };
}

/** Wide one-range book around `price`, for tests. Not a live pool. */
export function fixturePool({ price = 10, liquidity = 5_000_000_000_000n, feeRate = 0.0111, spacing = 60 } = {}) {
  const tick = priceToTick(price);
  const lower = Math.floor(tick / spacing) * spacing - spacing * 20;
  const upper = lower + spacing * 40;
  const sqrtPriceX96 = priceToSqrtPriceX96(price);
  const tickMap = {
    tickSpacing: spacing,
    ticks: [{ tick: lower, liquidityNet: liquidity }, { tick: upper, liquidityNet: -liquidity }],
  };
  const at = activeLiquidityAt(tickMap, sqrtPriceX96);
  if (at !== liquidity) throw new Error(`fixture liquidity ${at} != ${liquidity}`);
  return { sqrtPriceX96, liquidity, tickMap, markPrice: price, feeRate };
}

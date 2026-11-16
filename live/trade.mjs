// Perp City HORMUZ-SHIPS 链上接口（@strobelabs/perpcity-sdk 0.19，Arbitrum One）。
// 从 stdin 读一个 JSON 请求，向 stdout 最后一行输出 JSON 结果；出错时 stderr 写原因、exit 1。
// 私钥只从环境变量 PRIVATE_KEY 读取（仅用于推导钱包地址/签名），不落盘、不打印。
//
// 只读（不需要实盘开关，不发交易）：
//   {action:"positions", perp, ignore:[ids], scanBack}   钱包在该 perp 的所有仓位 + 估算平仓价值 + USDC 余额
//   {action:"quote_open", perp, margin, leverage, isLong?}  开仓报价（价格冲击、名义）；isLong 默认 true，false = 开空（卖出 base）
//   {action:"quote_close", perp, perpDelta, isLong?}        平仓报价。perpDelta = 原仓位带符号单位×1e6（正=多，负=空）；
//        旧调用传正数仍表示多单；传 isLong:false 时按空单处理（正数会被取负）。proceeds 带符号：平多为卖出所得（正），平空为买回花费（负）
// 实盘（必须 DRY_RUN=0 LIVE_TRADING=1 CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY）：
//   {action:"open", perp, margin, leverage, isLong:true, slippagePct, maxImpactPct?, maxTotalNotional?, minUsdc?}
//   {action:"open", perp, margin, leverage, isLong:false, allowShort:true, side:"short", ...}   开空：必须显式放行
//   {action:"close", perp, positionId, slippagePct, maxImpactPct?, side?}   （与旧接口兼容：不给 maxImpactPct 时行为不变；
//        给 side 时先核对链上方向一致）
// 方向约定（SDK 0.19 / llms-full.txt「Close a position」「Open Positions」）：
//   开多 perpDelta>0 买入 base，amt1Limit = 最多付多少 USDC；开空 perpDelta<0 卖出 base，amt1Limit = 最少收多少 USDC
//   平多卖出（amt1Limit=最少收），平空买回（amt1Limit=最多付，不能传 0）。都用 calculateTakerSlippageLimit(quote, swap 是否买入, %)
import {
  PerpCityContext, estimateTakerPosition, calculateTakerSlippageLimit,
  openTakerPosition, estimateTakerAdjust, closePosition, fetchPoolTickMap,
} from '@strobelabs/perpcity-sdk';
import { PERP_ABI } from '@strobelabs/perpcity-sdk/abis';
import { fundingPaymentUsd, takerUtilOwed } from '@strobelabs/perpcity-sdk/math';
import { createWalletClient, createPublicClient, http, erc20Abi } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { arbitrum } from 'viem/chains';

const USDC = process.env.USDC_ADDRESS || '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'; // Arbitrum One 原生 USDC
// 用户硬性上限（2026-11-09）：单笔保证金 ≤100u；杠杆 2~4x；单一市场总仓位名义 ≤300u
const HARD_MAX_MARGIN = 100, HARD_MIN_LEV = 2, HARD_MAX_LEV = 4, HARD_MAX_NOTIONAL = 300;
const j = (o) => JSON.stringify(o, (k, v) => (typeof v === 'bigint' ? v.toString() : v));

function guard() {
  const ok = process.env.DRY_RUN === '0' && process.env.LIVE_TRADING === '1'
    && process.env.CONFIRM_LIVE === 'I_UNDERSTAND_REAL_MONEY';
  if (!ok) throw new Error('实盘开关未全部打开（DRY_RUN=0, LIVE_TRADING=1, CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY），拒绝下单');
  if (!process.env.PRIVATE_KEY || !process.env.RPC_URL) throw new Error('缺少 PRIVATE_KEY 或 RPC_URL');
}

let _acct;
const account = () => (_acct ??= privateKeyToAccount(process.env.PRIVATE_KEY));

async function ctx() {
  if (!process.env.PRIVATE_KEY || !process.env.RPC_URL) throw new Error('缺少 PRIVATE_KEY 或 RPC_URL');
  const walletClient = createWalletClient({ account: account(), chain: arbitrum, transport: http(process.env.RPC_URL) });
  const c = new PerpCityContext({ rpcUrl: process.env.RPC_URL, walletClient, deployments: { usdc: USDC } });
  await c.validateChainId();
  return c;
}

const pub = () => createPublicClient({ chain: arbitrum, transport: http(process.env.RPC_URL) });

async function tickMap(pc, perp, pd) {
  try {
    const poolId = await pc.readContract({ address: perp, abi: PERP_ABI, functionName: 'POOL_ID' });
    return await fetchPoolTickMap(pc, { poolId, tickSpacing: pd.tickSpacing });
  } catch { return undefined; } // 拿不到就用单区间模型
}

async function quoteOpen(c, pc, perp, margin, leverage, pd, isLong = true) {
  pd ??= await c.getPerpData(perp);
  const price = pd.spotMark ?? pd.mark;
  const tm = await tickMap(pc, perp, pd);
  const q = await estimateTakerPosition(c, perp, { isLong, margin, leverage, tickMap: tm });
  return { q, price, impactPct: Math.abs(q.fillPrice / price - 1) * 100, notional: Math.abs(Number(q.usdDelta)) / 1e6,
    size: Number(q.perpDelta) / 1e6, exact: !!tm };
}

// perpDeltaOpen：原仓位带符号的 perpDelta（多为正、空为负）；平仓 swap = −perpDeltaOpen（平多卖出、平空买回）
async function quoteClose(c, pc, perp, perpDeltaOpen, pd) {
  pd ??= await c.getPerpData(perp);
  const price = pd.spotMark ?? pd.mark;
  const tm = await tickMap(pc, perp, pd);
  const q = await estimateTakerAdjust(c, perp, { perpDelta: -perpDeltaOpen, tickMap: tm });
  return { q, price, impactPct: Math.abs(q.fillPrice / price - 1) * 100, exact: !!tm };
}

// 钱包在该 perp 的所有仓位（Perp 是 ERC721，但不可枚举：扫最近 scanBack 个 posId 的 ownerOf，再用 balanceOf 核对数量）
async function positions(c, pc, perp, ignore = [], scanBack = 400) {
  const me = account().address;
  const R = (functionName, args = []) => pc.readContract({ address: perp, abi: PERP_ABI, functionName, args });
  const [next, bal, pd, cuml, rates, block] = await Promise.all([
    R('nextPosId'), R('balanceOf', [me]), c.getPerpData(perp), R('cumulatives'), R('rates'), pc.getBlock()]);
  const from = next > BigInt(scanBack) ? next - BigInt(scanBack) : 1n;
  const ids = []; for (let i = from; i < next; i++) ids.push(i);
  const owners = await pc.multicall({ contracts: ids.map((id) => ({ address: perp, abi: PERP_ABI, functionName: 'ownerOf', args: [id] })), allowFailure: true });
  const mine = ids.filter((id, k) => owners[k].status === 'success' && owners[k].result.toLowerCase() === me.toLowerCase());
  const usdc = Number(await pc.readContract({ address: USDC, abi: erc20Abi, functionName: 'balanceOf', args: [me] })) / 1e6;
  const tm = await tickMap(pc, perp, pd);
  const price = pd.spotMark ?? pd.mark;
  const now = Number(block.timestamp);
  const lastTouch = Number(rates[3]);
  const dtDays = Math.max(0, now - lastTouch) / 86400;
  const out = [];
  for (const id of mine) {
    const [p, td] = await Promise.all([R('positions', [id]), R('takerDetails', [id])]);
    const [delta, marginRaw] = p;
    // BalanceDelta: 高 128 位 amount0(perp)，低 128 位 amount1(usd)，均为有符号
    const toI128 = (x) => (x >= (1n << 127n) ? x - (1n << 128n) : x);
    const d = BigInt.asUintN(256, delta);
    const perpDelta = toI128(d >> 128n), usdDelta = toI128(d & ((1n << 128n) - 1n));
    if (perpDelta === 0n) continue; // maker 或空仓
    const fundingOwed = fundingPaymentUsd(perpDelta, cuml[0], p[4], pd.build);   // 截至上次 touch
    const utilOwed = takerUtilOwed({ perpDelta, longUtilPaymentsX96: cuml[2], shortUtilPaymentsX96: cuml[3],
      lastLongUtilPaymentsX96: td[0], lastShortUtilPaymentsX96: td[1] });
    // 上次 touch 之后到现在的部分按当前费率外推（rates: fundingPerDay 为每单位每天 USD×1e18，>0 多头付；utilFee 为名义/天×1e18）
    const size = Number(perpDelta) / 1e6;
    const fundingExtra = size * (Number(rates[0]) / 1e18) * dtDays;
    const utilExtra = Math.abs(size) * pd.fairMark * (Number(perpDelta > 0n ? rates[1] : rates[2]) / 1e18) * dtDays;
    const cq = await estimateTakerAdjust(c, perp, { perpDelta: -perpDelta, tickMap: tm });
    const margin = Number(marginRaw) / 1e6;
    const owed = Number(fundingOwed) / 1e6 + Number(utilOwed) / 1e6 + fundingExtra + utilExtra;
    // 平仓估算价值 = 链上保证金 − 应付 funding/利用率费 + 平仓卖出所得（含 1.11% 手续费和价格冲击）+ 开仓 usd 腿（多头为负）
    const closeValue = margin - owed + Number(cq.effectiveUsdDelta) / 1e6 + Number(usdDelta) / 1e6;
    out.push({ positionId: id.toString(), ignored: ignore.map(String).includes(id.toString()),
      isLong: perpDelta > 0n, size, perpDelta: perpDelta.toString(), entryNotional: Math.abs(Number(usdDelta)) / 1e6,
      entryPrice: Math.abs(Number(usdDelta) / Number(perpDelta)), marginOnChain: margin,
      fundingOwed: Number(fundingOwed) / 1e6 + fundingExtra, utilOwed: Number(utilOwed) / 1e6 + utilExtra,
      closeFillPrice: cq.fillPrice, closeImpactPct: Math.abs(cq.fillPrice / price - 1) * 100,
      closeValue, markValue: margin - owed + size * pd.fairMark + Number(usdDelta) / 1e6 });
  }
  return { ok: true, wallet: me, usdc, balanceOf: Number(bal), found: out.length, complete: Number(bal) === mine.length,
    nextPosId: next.toString(), spotMark: price, fairMark: pd.fairMark, positions: out,
    totalNotionalAll: out.reduce((s, x) => s + x.entryNotional, 0) };
}

// 开仓请求的静态硬性检查（不需要链）：--validate-only 也会跑，便于单测
export function checkOpenRequest(req) {
  const { margin, leverage, isLong } = req;
  if (isLong === false) {
    // 空单只有在调用方显式放行时才接受（bot.py 的空单腿会同时传 allowShort:true 与 side:"short"）
    if (!(req.allowShort === true && req.side === 'short')) throw new Error('空单需要显式 allowShort:true 且 side:"short"，拒绝');
  } else if (isLong !== true) {
    throw new Error('isLong 必须是 true 或 false');
  } else if (req.side != null && req.side !== 'long') {
    throw new Error('isLong:true 与 side 不一致');
  }
  if (!(margin >= 5)) throw new Error('保证金须 ≥$5');
  if (margin > HARD_MAX_MARGIN) throw new Error('单笔保证金超过 100u 上限');
  if (!(leverage >= HARD_MIN_LEV && leverage <= HARD_MAX_LEV)) throw new Error('杠杆须在 2~4x');
  if (margin * leverage > HARD_MAX_NOTIONAL) throw new Error('单笔名义超过 300u 总仓位上限');
  if (!(Number(req.slippagePct) > 0)) throw new Error('slippagePct 须 >0');
}

const readStdin = async () => { let s = ''; for await (const ch of process.stdin) s += ch; return JSON.parse(s); };

async function main() {
  const req = await readStdin();
  if (!/^0x[0-9a-fA-F]{40}$/.test(req.perp || '')) throw new Error('perp 地址无效');
  if (process.argv.includes('--validate-only')) {
    if (req.action === 'open') checkOpenRequest(req);
    console.log(JSON.stringify({ ok: true, action: req.action })); return;
  }
  const perp = req.perp;

  // ---------- 只读 ----------
  if (req.action === 'positions') {
    const c = await ctx(); console.log(j(await positions(c, pub(), perp, req.ignore || [], req.scanBack || 400))); return;
  }
  if (req.action === 'quote_open') {
    const c = await ctx();
    const r = await quoteOpen(c, pub(), perp, Number(req.margin), Number(req.leverage), undefined, req.isLong !== false);
    console.log(j({ ok: true, price: r.price, fillPrice: r.q.fillPrice, effectiveFillPrice: r.q.effectiveFillPrice,
      impactPct: r.impactPct, notional: r.notional, size: r.size, exceedsLiquidity: r.q.exceedsLiquidity,
      liquidityLimited: r.q.liquidityLimited, exact: r.exact }));
    return;
  }
  if (req.action === 'quote_close') {
    const c = await ctx();
    let od = BigInt(req.perpDelta);
    if (od === 0n) throw new Error('perpDelta 不能为 0');
    if (req.isLong === false && od > 0n) od = -od;          // 显式空单：原仓位为负
    if (req.isLong === true && od < 0n) throw new Error('isLong:true 但 perpDelta 为负');
    const r = await quoteClose(c, pub(), perp, od);
    console.log(j({ ok: true, isLong: od > 0n, price: r.price, fillPrice: r.q.fillPrice, effectiveFillPrice: r.q.effectiveFillPrice,
      impactPct: r.impactPct, proceeds: Number(r.q.effectiveUsdDelta) / 1e6, grossUsd: Number(r.q.usdDelta) / 1e6,
      exceedsLiquidity: r.q.exceedsLiquidity, exact: r.exact }));
    return;
  }

  // ---------- 实盘 ----------
  guard();
  const c = await ctx();
  if (req.action === 'open') {
    checkOpenRequest(req);
    const { margin, leverage, slippagePct } = req;
    const isLong = req.isLong === true;
    const pc = pub();
    // 链上核对：钱包在该 perp 的全部仓位（含被 bot 忽略的）+ 本笔 ≤ 上限
    const cap = Math.min(Number(req.maxTotalNotional ?? HARD_MAX_NOTIONAL), HARD_MAX_NOTIONAL);
    const ps = await positions(c, pc, perp, [], req.scanBack || 400);
    if (!ps.complete) throw new Error(`链上仓位未扫全（balanceOf=${ps.balanceOf} 找到 ${ps.found}），拒绝开仓`);
    if (ps.usdc < margin + Number(req.minUsdc ?? 0)) throw new Error(`USDC 余额 ${ps.usdc.toFixed(2)} 不足（需 ${margin}+缓冲）`);
    const pd = await c.getPerpData(perp);
    const r = await quoteOpen(c, pc, perp, margin, leverage, pd, isLong);
    if (r.q.exceedsLiquidity) throw new Error('池子深度不够，放弃');
    if (ps.totalNotionalAll + r.notional > cap + 1e-6) throw new Error(`总仓位名义 ${ps.totalNotionalAll.toFixed(2)}+${r.notional.toFixed(2)} 超过 ${cap}u 上限`);
    const maxImpact = Number(req.maxImpactPct ?? slippagePct);
    if (r.impactPct > maxImpact) throw new Error(`报价价格冲击 ${r.impactPct.toFixed(2)}% 超过上限 ${maxImpact}%，放弃`);
    if ((r.q.perpDelta > 0n) !== isLong) throw new Error('报价方向与请求不一致，放弃');
    // 多：最多付 |usd|×(1+s)；空：最少收 |usd|×(1−s)
    const amt1Limit = calculateTakerSlippageLimit(r.q, isLong, slippagePct);
    // 用报价里的 perpDelta（已计入手续费），避免与 derivePerpDelta 不一致导致超出滑点上限
    const pos = await openTakerPosition(c, perp, { margin, perpDelta: r.q.perpDelta, amt1Limit });
    console.log(j({ ok: true, isLong, positionId: pos.positionId.toString(), txHash: pos.txHash,
      fillPrice: r.q.effectiveFillPrice ?? r.q.fillPrice, rawFillPrice: r.q.fillPrice, impactPct: r.impactPct,
      size: Number(r.q.perpDelta) / 1e6, notional: r.notional }));
  } else if (req.action === 'close') {
    const id = BigInt(req.positionId);
    const raw = await c.getPositionRawData(perp, id);
    if (raw.entryPerpDelta === 0n) throw new Error('仓位为空');
    if (req.side != null && (raw.entryPerpDelta > 0n ? 'long' : 'short') !== req.side)
      throw new Error(`仓位 ${req.positionId} 链上方向与 side=${req.side} 不一致，拒绝平仓`);
    const closeDelta = -raw.entryPerpDelta;   // 平多卖出（<0），平空买回（>0）
    const quote = await estimateTakerAdjust(c, perp, { perpDelta: closeDelta });
    if (req.maxImpactPct != null) {
      const pd = await c.getPerpData(perp); const price = pd.spotMark ?? pd.mark;
      const impact = Math.abs(quote.fillPrice / price - 1) * 100;
      if (impact > Number(req.maxImpactPct)) throw new Error(`平仓价格冲击 ${impact.toFixed(2)}% 超过上限 ${req.maxImpactPct}%，放弃`);
    }
    // 平空是买入：amt1Limit = 最多付；平多是卖出：amt1Limit = 最少收
    const amt1Limit = calculateTakerSlippageLimit(quote, closeDelta > 0n, req.slippagePct);
    const { txHash } = await closePosition(c, perp, id, { amt1Limit });
    console.log(JSON.stringify({ ok: true, txHash, fillPrice: quote.effectiveFillPrice ?? quote.fillPrice,
      proceeds: Number(quote.effectiveUsdDelta) / 1e6 }));
  } else {
    throw new Error('未知 action');
  }
}

main().catch((e) => { console.error(e?.message || String(e)); process.exit(1); });

// 减仓单测：官方 calculateTakerSlippageLimit + mock 的链上 SDK。不连 RPC、不签名、不发交易。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { calculateTakerSlippageLimit } from '@strobelabs/perpcity-sdk';
import { executeLongReduce, parseReduceMicro, planLongReduce } from './trade.mjs';

const PERP = '0xb9F540CE4315e7F4382C96532f8eC3903899D86d';
const OTHER = '0x1111111111111111111111111111111111111111';

function baseReq(over = {}) {
  return { action: 'reduce', perp: PERP, positionId: '187', side: 'long', size: '1.234567', slippagePct: 1.5, ...over };
}

function harness(entryPerpDelta, opt = {}) {
  const calls = [];
  const quote = {
    usdDelta: 123456789n,              // 费前，滑点界限用这个
    effectiveUsdDelta: 120000000n,     // 费后，不得拿来算 amt1Limit，也不是钱包入账
    fillPrice: opt.fillPrice ?? 8,
    effectiveFillPrice: 7.91,
    exceedsLiquidity: false,
  };
  const sdk = {
    async getPositionRawData(perp, id) {
      calls.push(['getPositionRawData', perp, id]);
      return {
        perpId: opt.perpId === undefined ? perp : opt.perpId,
        positionId: opt.positionId === undefined ? id : opt.positionId,
        entryPerpDelta: opt.entry === undefined ? entryPerpDelta : opt.entry,
        entryUsdDelta: -50_000_000n,
        margin: 25,
        makerDetails: opt.makerDetails ?? null,
      };
    },
    async estimateTakerAdjust(perp, params) {
      calls.push(['estimateTakerAdjust', perp, params]);
      return quote;
    },
    async getPerpData() {
      calls.push(['getPerpData']);
      return { spotMark: opt.spotMark ?? 100, mark: opt.spotMark ?? 100 };
    },
    async adjustTaker(perp, params) {
      calls.push(['adjustTaker', perp, params]);
      return { txHash: '0xadjust' };
    },
    async closePosition(perp, id, params) {
      calls.push(['closePosition', perp, id, params]);
      return { txHash: '0xclose' };
    },
    async adjustMargin() {
      calls.push(['adjustMargin']);
      throw new Error('不得单独提取保证金');
    },
  };
  return { sdk, calls, quote };
}

const names = (calls) => calls.map((c) => c[0]);

test('exact decimal size and 1.5% sell limit use fee-exclusive usdDelta', async () => {
  assert.equal(parseReduceMicro('1.234567'), 1234567n);
  assert.equal(parseReduceMicro('1.230000'), 1230000n);
  assert.equal(parseReduceMicro(1.25), 1250000n);
  const { sdk, calls, quote } = harness(5_000_000n);
  const out = await executeLongReduce(baseReq(), sdk);
  const amt1Limit = calculateTakerSlippageLimit(quote, false, 1.5);
  assert.equal(amt1Limit, 123456789n * 9850n / 10000n);
  assert.notEqual(amt1Limit, 120000000n * 9850n / 10000n);
  assert.deepEqual(calls.filter((c) => c[0] === 'adjustTaker').map((c) => c[2]), [{
    posId: 187n, marginDelta: 0n, perpDelta: -1234567n, amt1Limit,
  }]);
  assert.equal(names(calls).includes('closePosition'), false);
  assert.equal(names(calls).includes('adjustMargin'), false);
  assert.equal(names(calls).includes('getPerpData'), false);
  assert.equal(out.mode, 'adjust');
  assert.equal(out.closed, false);
  assert.equal(out.marginDelta, '0');
  assert.equal(out.perpDelta, '-1234567');
  assert.equal(out.reducedPerp, '1234567');
  assert.equal(out.expectedRemainingPerp, String(5_000_000n - 1234567n));
  assert.equal(out.amt1Limit, amt1Limit.toString());
  assert.equal(out.slippagePct, 1.5);
  assert.equal(out.impactChecked, false);
  assert.equal(out.impactPct, null);
  assert.equal(out.quoteUsdMicro, '123456789');
  assert.equal(out.quoteEffectiveUsdMicro, '120000000');
  assert.equal(out.quoteProceedsAreWalletCash, false);
  assert.equal(out.walletUsdcCredit, 'none');
  assert.equal(out.txHash, '0xadjust');
});

test('omitted maxImpactPct does not cap a large quote move', async () => {
  const { sdk, calls } = harness(5_000_000n, { fillPrice: 1, spotMark: 100 });
  const out = await executeLongReduce(baseReq(), sdk);
  assert.equal(out.impactChecked, false);
  assert.equal(names(calls).includes('getPerpData'), false);
  assert.equal(names(calls).includes('adjustTaker'), true);
});

test('explicit impact cap still rejects and does not send', async () => {
  const { sdk, calls } = harness(5_000_000n, { fillPrice: 110, spotMark: 100 });
  await assert.rejects(() => executeLongReduce(baseReq({ maxImpactPct: 1.5 }), sdk), /价格冲击/);
  assert.deepEqual(names(calls), ['getPositionRawData', 'estimateTakerAdjust', 'getPerpData']);
});

test('wrong side, empty, maker, and wrong market fail before a swap', async () => {
  for (const opt of [
    { entry: -5_000_000n },
    { entry: 0n },
    { makerDetails: { liquidity: 1n } },
    { perpId: OTHER },
    { positionId: 188n },
  ]) {
    const { sdk, calls } = harness(5_000_000n, opt);
    await assert.rejects(() => executeLongReduce(baseReq(), sdk));
    assert.deepEqual(names(calls), ['getPositionRawData']);
  }
});

test('full close of a short is refused and does not call closePosition', async () => {
  const { sdk, calls } = harness(-5_000_000n);
  await assert.rejects(
    () => executeLongReduce(baseReq({ size: undefined, full: true }), sdk),
    /空头/,
  );
  assert.deepEqual(names(calls), ['getPositionRawData']);
});

test('increase, overshoot, zero, and nonfinite fail with no adjust', async () => {
  const cases = [
    baseReq({ perpDelta: '1000000' }),
    baseReq({ marginDelta: 0 }),
    baseReq({ size: '-0.5' }),
    baseReq({ size: '0' }),
    baseReq({ size: '0.0' }),
    baseReq({ size: 'NaN' }),
    baseReq({ size: 'Infinity' }),
    baseReq({ size: '-1' }),
  ];
  for (const req of cases) {
    const { sdk, calls } = harness(5_000_000n);
    await assert.rejects(() => executeLongReduce(req, sdk));
    assert.deepEqual(calls, []);
  }
  assert.throws(() => parseReduceMicro(NaN), /非有限/);
  assert.throws(() => parseReduceMicro(Infinity), /非有限/);
  assert.throws(() => parseReduceMicro(0), /须为正/);
  assert.throws(() => parseReduceMicro(-1), /须为正/);
  const over = harness(1_000_000n);
  await assert.rejects(() => executeLongReduce(baseReq({ size: '1.000001' }), over.sdk), /超过剩余/);
  assert.deepEqual(names(over.calls), ['getPositionRawData']);
});

test('exact remaining size and full:true close, and do not withdraw margin', async () => {
  const entry = 1_234_567n;
  const partial = harness(entry);
  assert.throws(() => planLongReduce({
    entryPerpDelta: entry, reduceMicro: entry + 1n,
    quote: partial.quote, slippagePct: 1.5, maxImpactPct: null, spotPrice: 100,
  }), /超过剩余/);
  const bySize = await executeLongReduce(baseReq({ size: '1.234567' }), partial.sdk);
  const amt = calculateTakerSlippageLimit(partial.quote, false, 1.5);
  assert.equal(bySize.mode, 'close');
  assert.equal(bySize.closed, true);
  assert.equal(bySize.walletUsdcCredit, 'collateral');
  assert.equal(bySize.quoteProceedsAreWalletCash, false);
  assert.equal(bySize.expectedRemainingPerp, '0');
  assert.equal(bySize.marginDelta, '0');
  assert.deepEqual(partial.calls.filter((c) => c[0] === 'closePosition').map((c) => c[3]), [{ amt1Limit: amt }]);
  assert.equal(names(partial.calls).includes('adjustTaker'), false);
  assert.equal(names(partial.calls).includes('adjustMargin'), false);

  const byFull = harness(entry);
  const out = await executeLongReduce(baseReq({ size: undefined, full: true }), byFull.sdk);
  assert.equal(out.mode, 'close');
  assert.equal(out.perpDelta, (-entry).toString());
  assert.deepEqual(byFull.calls.find((c) => c[0] === 'estimateTakerAdjust')[2], { perpDelta: -entry });
  assert.equal(names(byFull.calls).includes('adjustTaker'), false);
  await assert.rejects(
    () => executeLongReduce(baseReq({ size: '1', full: true }), harness(entry).sdk),
    /不一致/,
  );
});

test('live reduce path calls the imported SDK adjustTaker and does not send', () => {
  const probe = `
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const file = process.env.TRADE_MJS;
const raw = readFileSync(file, 'utf8');
const block = raw.match(/executeLongReduce\\(req,\\s*\\{([\\s\\S]*?)\\}\\);/);
if (!block) throw new Error('live reduce wiring missing');
if (!/adjustTaker:\\s*\\(p,\\s*params\\)\\s*=>\\s*adjustTaker\\(c,\\s*p,\\s*params\\)/.test(block[1])) {
  throw new Error('live wiring does not call imported adjustTaker');
}
const imp = raw.match(/import\\s*\\{([^}]+)\\}\\s*from\\s*'@strobelabs\\/perpcity-sdk'/);
const imported = imp[1].split(',').map((s) => s.trim()).filter(Boolean);
if (!imported.includes('adjustTaker')) throw new Error('SDK import missing adjustTaker');
async function load(src) {
  const mod = new vm.SourceTextModule(src + '\\nexport async function __probeLiveAdjustTaker(c, perp, params) {\\n  const wiring = {' + block[1] + '};\\n  return wiring.adjustTaker(perp, params);\\n}\\n', {
    identifier: pathToFileURL(file).href,
  });
  await mod.link(async (specifier) => {
    const ns = await import(specifier);
    const names = Object.keys(ns);
    return new vm.SyntheticModule(names, function () {
      for (const k of names) this.setExport(k, ns[k]);
    }, { identifier: specifier });
  });
  await mod.evaluate();
  return mod.namespace.__probeLiveAdjustTaker;
}
const params = { posId: 187n, marginDelta: 0n, perpDelta: -1n, amt1Limit: 1n };
async function once(src) {
  const call = await load(src);
  let simulated = null;
  let wrote = false;
  const c = {
    publicClient: {
      simulateContract(arg) { simulated = arg; throw new Error('STOP_BEFORE_SEND'); },
    },
    walletClient: { account: {}, writeContract() { wrote = true; throw new Error('WRITE'); } },
  };
  try {
    await call(c, '0xb9F540CE4315e7F4382C96532f8eC3903899D86d', params);
    return { resolved: true, wrote, simulated };
  } catch (e) {
    return {
      name: e?.name, message: String(e?.message || e), wrote,
      functionName: simulated?.functionName || null,
      marginDelta: simulated?.args?.[0]?.marginDelta?.toString?.() ?? null,
      perpDelta: simulated?.args?.[0]?.perpDelta?.toString?.() ?? null,
    };
  }
}
const hit = await once(raw);
const stripped = raw.replace(/import\\s*\\{([^}]+)\\}\\s*from\\s*'@strobelabs\\/perpcity-sdk'/, (full, inner) => {
  const names = inner.split(',').map((s) => s.trim()).filter((n) => n && n !== 'adjustTaker');
  return "import {\\n  " + names.join(', ') + "\\n} from '@strobelabs/perpcity-sdk'";
});
const miss = await once(stripped);
console.log(JSON.stringify({ hit, miss }));
`;
  const r = spawnSync(process.execPath, ['--experimental-vm-modules', '--input-type=module'], {
    input: probe, encoding: 'utf8', cwd: new URL('.', import.meta.url),
    env: { ...process.env, TRADE_MJS: new URL('./trade.mjs', import.meta.url).pathname },
  });
  assert.equal(r.status, 0, r.stderr || r.stdout);
  const line = r.stdout.trim().split('\n').filter((s) => s.startsWith('{')).pop();
  const { hit, miss } = JSON.parse(line);
  assert.equal(hit.functionName, 'adjustTaker');
  assert.equal(hit.marginDelta, '0');
  assert.equal(hit.perpDelta, '-1');
  assert.equal(hit.wrote, false);
  assert.match(hit.message, /STOP_BEFORE_SEND/);
  assert.notEqual(hit.name, 'ReferenceError');
  assert.equal(miss.functionName, null);
  assert.equal(miss.name, 'ReferenceError');
  assert.match(miss.message, /adjustTaker/);
});

test('each call reads the fresh remaining size', async () => {
  const first = harness(5_000_000n);
  const a = await executeLongReduce(baseReq({ size: '1' }), first.sdk);
  assert.equal(a.perpDelta, '-1000000');
  assert.equal(a.expectedRemainingPerp, '4000000');
  const second = harness(4_000_000n);
  const b = await executeLongReduce(baseReq({ size: '1' }), second.sdk);
  assert.equal(b.perpDelta, '-1000000');
  assert.equal(b.expectedRemainingPerp, '3000000');
  const third = harness(500_000n);
  await assert.rejects(() => executeLongReduce(baseReq({ size: '1' }), third.sdk), /超过剩余/);
});

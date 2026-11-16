"""简单回测：每天 19:00 开空、次日 05:00 平空（香港时间），对照 7:00→16:00 多单。
口径：用 backtest.load_grid() 的 15 分钟网格（链上 AMM 成交后价格 mark、指数、funding）；
名义 200u、2x；开平各按 50u/批的实测冲击 0.45%（一次性近似，不模拟分批间价格变化）；手续费 1.11%×2；
利用率费 1%/天；funding 按网格逐根累计（fundingPerDay>0 多头付、空头收）；
止损：任一 15 分钟收盘按 mark 估算亏损 ≥ 保证金 30% 就按当时 mark 平（冲击放宽按 1.6% 近似）。
运行：python3 backtest_short.py
"""
import json, sys
import numpy as np, pandas as pd
from backtest import load_grid

FEE, UTIL, IMP, STOP_IMP, NOTIONAL, LEV, STOP = 0.0111, 0.01, 0.0045, 0.016, 200.0, 2.0, 0.30


def one(g, t0, t1, side):
    seg = g.loc[t0:t1]
    if len(seg) < 2 or seg.index[0] != t0 or seg.index[-1] != t1:
        return None
    m0 = seg.mark.iloc[0]
    fill = m0 * (1 + side * IMP)
    size = NOTIONAL / fill
    margin = NOTIONAL / LEV
    dt = (seg.index[1] - seg.index[0]).total_seconds() / 86400
    acc = 0.0
    for k, (ts, r) in enumerate(seg.iterrows()):
        if k:
            acc += side * size * seg.funding.iloc[k - 1] * dt + UTIL * NOTIONAL * dt   # 正 = 付出
        last = k == len(seg) - 1
        imp = IMP if last else STOP_IMP
        exitp = r.mark * (1 - side * imp)
        gross = side * size * (exitp - fill)
        fees = FEE * NOTIONAL + FEE * size * exitp
        net = gross - fees - acc
        if last or -net >= STOP * margin:
            return dict(open=t0, close=ts, side="short" if side < 0 else "long", entry=fill, exit=exitp,
                        price_chg_pct=(r.mark / m0 - 1) * 100, gross=gross, fees=fees, carry=acc, net=net,
                        net_pct_margin=net / margin * 100, stop=not last)


def run(g, oh, ch, side):
    out = []
    for day in pd.Series(g.index.normalize().unique()):
        t0 = day + pd.Timedelta(hours=oh)
        t1 = day + pd.Timedelta(hours=ch) + (pd.Timedelta(days=1) if ch <= oh else pd.Timedelta(0))
        r = one(g, t0, t1, side)
        if r: out.append(r)
    return pd.DataFrame(out)


def stats(tr):
    if tr.empty: return dict(n=0)
    return dict(n=len(tr), win_pct=round((tr.net > 0).mean() * 100, 1), mean_net_usd=round(tr.net.mean(), 2),
                median_net_usd=round(tr.net.median(), 2), total_net_usd=round(tr.net.sum(), 2),
                mean_net_pct_margin=round(tr.net_pct_margin.mean(), 2), mean_price_chg_pct=round(tr.price_chg_pct.mean(), 2),
                mean_fees_usd=round(tr.fees.mean(), 2), mean_carry_usd=round(tr.carry.mean(), 2), stops=int(tr.stop.sum()),
                worst_usd=round(tr.net.min(), 2), best_usd=round(tr.net.max(), 2),
                t_stat=round(tr.net.mean() / (tr.net.std(ddof=1) / np.sqrt(len(tr))), 2) if len(tr) > 2 else None)


if __name__ == "__main__":
    g = load_grid()
    print(f"数据：{g.index[0]} → {g.index[-1]}（香港时间）")
    cut = pd.Timestamp("2026-10-31", tz="Asia/Hong_Kong")
    res = {}
    for lab, oh, ch, side in (("空 19:00→次日05:00", 19, 5, -1), ("多 07:00→16:00", 7, 16, +1)):
        tr = run(g, oh, ch, side)
        res[lab] = {"全部": stats(tr), "10-31前": stats(tr[tr.open < cut]), "10-31后": stats(tr[tr.open >= cut])}
        print(f"\n== {lab}")
        for k, v in res[lab].items(): print(f"  {k}: {v}")
        if side < 0: tr.to_csv("backtest_trades_short.csv", index=False)
    json.dump(res, open("backtest_short_summary.json", "w"), ensure_ascii=False, indent=1, default=str)

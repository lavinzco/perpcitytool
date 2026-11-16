"""用链上真实 mark（AMM 成交后价格）回测日内策略，扣手续费、滑点、funding、利用率费。

数据：../sg_taker_actions.csv、../sg_rates.csv（Goldsky 子图）、../perp_index.csv（perp.city 指数导出）。
运行：python3 backtest.py [--refresh]
"""
from __future__ import annotations
import argparse, itertools, json, sys
import numpy as np, pandas as pd
from strategy_v1 import Params, Market, Position, decide, fill_price, trade_pnl

D = "/workspace/hormuz/"
TZ = "Asia/Hong_Kong"
Q96 = 2 ** 96


def load_grid(freq="15min") -> pd.DataFrame:
    t = pd.read_csv(D + "sg_taker_actions.csv")
    t["ts"] = pd.to_datetime(t.timestamp, unit="us", utc=True).dt.tz_convert(TZ)
    t["mark"] = t.newAmmPriceX96.astype(float) / Q96
    mk = t[t.mark > 0].set_index("ts").mark
    mk = mk[~mk.index.duplicated(keep="last")].sort_index()
    r = pd.read_csv(D + "sg_rates.csv")
    r["ts"] = pd.to_datetime(r.timestamp, unit="us", utc=True).dt.tz_convert(TZ)
    f = (r.set_index("ts").fundingPerDay.astype(float) / 1e18)
    f = f[~f.index.duplicated(keep="last")].sort_index()
    i = pd.read_csv(D + "perp_index.csv")
    i["ts"] = pd.to_datetime(i.time, unit="s", utc=True).dt.tz_convert(TZ)
    ix = i.set_index("ts").index_price
    ix = ix[~ix.index.duplicated(keep="last")].sort_index()
    start = mk.index[0].ceil("1D")  # 市场 10-06 晚上线，从次日 00:00 起
    end = min(mk.index[-1], ix.index[-1]).floor(freq)
    g = pd.date_range(start, end, freq=freq)
    step = lambda s: s.reindex(s.index.union(g)).ffill().reindex(g)
    return pd.DataFrame({"mark": step(mk), "index": step(ix), "funding": step(f)}).dropna()


def run(grid: pd.DataFrame, p: Params) -> pd.DataFrame:
    dt_days = (grid.index[1] - grid.index[0]).total_seconds() / 86400
    pos = None; acc_f = 0.0; trades = []; stopped = False; last_want = 0
    from strategy_v1 import target_side
    for ts, row in grid.iterrows():
        hour = ts.hour + ts.minute / 60
        want = target_side(hour, p)
        if want != last_want:
            stopped = False; last_want = want
        if pos is not None:
            acc_f += row.funding * dt_days  # 上一根到这一根之间累计（以当前值近似）
        m = Market(row.mark, row["index"], row.funding)
        d = decide(hour, m, pos, p, stopped)
        if d.action == "close":
            exit_px = fill_price(row.mark, -pos.side, pos.size * row.mark, p)
            hold = (ts - pos.opened_ts).total_seconds() / 86400
            pnl = trade_pnl(pos.side, pos.size, pos.entry_price, exit_px, acc_f, hold)
            notional = pos.size * pos.entry_price
            trades.append(dict(open=pos.opened_ts, close=ts, side=pos.side, entry=pos.entry_price,
                               exit=exit_px, reason=d.reason, notional=notional, **pnl,
                               net_pct=pnl["net"] / notional * 100, gross_pct=pnl["gross"] / notional * 100))
            if d.reason.startswith("止损"):
                stopped = True
            pos = None
        elif d.action == "open":
            px = fill_price(row.mark, d.side, d.notional, p)
            pos = Position(d.side, d.notional / px, px, hour); pos.opened_ts = ts; acc_f = 0.0
    return pd.DataFrame(trades)


def stats(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return dict(n=0)
    eq = tr.net.cumsum(); dd = (eq - eq.cummax().clip(lower=0)).min()
    return dict(n=len(tr), win=round((tr.net > 0).mean() * 100, 1),
                mean_net_pct=round(tr.net_pct.mean(), 2), median_net_pct=round(tr.net_pct.median(), 2),
                mean_gross_pct=round(tr.gross_pct.mean(), 2),
                fees_pct=round((tr.fees / tr.notional).mean() * 100, 2),
                funding_pct=round((tr.funding / tr.notional).mean() * 100, 2),
                util_pct=round((tr.util / tr.notional).mean() * 100, 2),
                total_net_usd=round(tr.net.sum(), 2), max_dd_usd=round(dd, 2),
                t_stat=round(tr.net_pct.mean() / (tr.net_pct.std(ddof=1) / np.sqrt(len(tr))), 2) if len(tr) > 2 else None,
                stops=int(tr.reason.str.startswith("止损").sum()))


def report(grid, p, label):
    out = {}
    tr = run(grid, p)
    cut = pd.Timestamp("2026-10-31", tz=TZ)
    for per in ("全部", "10-31前", "10-31后"):
        sub = tr if per == "全部" or tr.empty else (tr[tr.open < cut] if per == "10-31前" else tr[tr.open >= cut])
        out[per] = stats(sub)
    print(f"\n== {label}")
    for k, v in out.items():
        print(f"  {k}: {v}")
    return tr, out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--grid-search", action="store_true"); a = ap.parse_args()
    g = load_grid()
    print(f"数据：{g.index[0]} → {g.index[-1]}（香港时间），{g.index.normalize().nunique()} 天")
    res = {}
    base = dict(notional_usd=50, leverage=2, stop_loss_pct=8, max_chase_pct=5)
    cases = {
        "A 4点开多→17点平多": dict(long_open_hour=4, long_close_hour=17),
        "B 6点开多→18点平多": dict(long_open_hour=6, long_close_hour=18),
        "C 7点开多→20点平多（默认）": dict(long_open_hour=7, long_close_hour=20),
        "D 6→18 多 + 18→3 空": dict(long_open_hour=6, long_close_hour=18, short_enabled=True, short_open_hour=18, short_close_hour=3),
        "E 7→20 多 + 21→4 空": dict(long_open_hour=7, long_close_hour=20, short_enabled=True, short_open_hour=21, short_close_hour=4),
        "F 只做空 21→4": dict(long_open_hour=7, long_close_hour=7.25, short_enabled=True, short_open_hour=21, short_close_hour=4, max_notional_usd=200),
        "C 无风控（不止损、不限追价）": dict(long_open_hour=7, long_close_hour=20, stop_loss_pct=99, max_chase_pct=99, max_adverse_funding_pct_day=99),
        "C 压力：滑点翻倍 1.8%/每$100": dict(long_open_hour=7, long_close_hour=20, slip_pct_per_100usd=1.8, max_slippage_pct=5),
    }
    for lab, kw in cases.items():
        if kw is None:
            continue
        p = Params(**{**base, **kw}); p.validate()
        tr, res[lab] = report(g, p, lab)
        if lab.startswith("C 7"):
            tr.to_csv("backtest_trades_C.csv", index=False)
    print("\n== 规模敏感性（C 方案，滑点按 0.9%/每$100 线性放大）")
    for n in [10, 25, 50, 100, 200]:
        p = Params(**{**base, "long_open_hour": 7, "long_close_hour": 20, "notional_usd": n, "max_notional_usd": max(200, n),
                      "leverage": 2 if n >= 10 else 1, "max_slippage_pct": 99})
        tr = run(g, p); s1 = stats(tr); s2 = stats(tr[tr.open >= pd.Timestamp("2026-10-31", tz=TZ)]) if not tr.empty else {}
        print(f"  ${n}: 全部 均值 {s1.get('mean_net_pct')}% 胜率 {s1.get('win')}% n={s1.get('n')} | 10-31后 均值 {s2.get('mean_net_pct')}% 胜率 {s2.get('win')}% n={s2.get('n')}")
    if a.grid_search:
        print("\n== 只做多的开/平时间网格（10-31 后，扣全部成本，$50）均值净收益%")
        rows = []
        for o, c in itertools.product(range(24), range(24)):
            if o == c: continue
            p = Params(**{**base, "long_open_hour": o, "long_close_hour": c, "stop_loss_pct": 99, "max_chase_pct": 99, "max_adverse_funding_pct_day": 99})
            tr = run(g, p)
            for per, lo in [("all", None), ("post", "2026-10-31")]:
                sub = tr if lo is None else tr[tr.open >= pd.Timestamp(lo, tz=TZ)]
                if len(sub): rows.append(dict(o=o, c=c, per=per, n=len(sub), mean=sub.net_pct.mean(), win=(sub.net > 0).mean()))
        G = pd.DataFrame(rows); G.to_csv("grid_long.csv", index=False)
        for per in ["all", "post"]:
            x = G[G.per == per].sort_values("mean", ascending=False)
            print(per, "前 8："); print(x.head(8).round(2).to_string(index=False))
            print(per, "正收益组合占比 %.0f%%" % ((x["mean"] > 0).mean() * 100))
    json.dump(res, open("backtest_summary.json", "w"), ensure_ascii=False, indent=1, default=str)

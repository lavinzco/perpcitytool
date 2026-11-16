# 旧版单仓位策略（8% 价格止损），只供 backtest.py 复现历史回测；实盘/模拟用 strategy.py。
"""HORMUZ-SHIPS 日内形态策略：纯逻辑，无网络、无副作用，供实盘循环和回测共用。

时间一律按香港时间（Asia/Hong_Kong，UTC+8）的小时数（可带小数）处理。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json, os

FEE_RATE = 0.0111          # 每次 swap（开、加、减、平）按名义金额收 1.11%，链上实测 Open/Close 均为 1.11%
# 用户硬性上限（2026-11-09）：单笔保证金 ≤100u，杠杆 ≤4x，单一市场总仓位名义 ≤300u
HARD_MAX_ORDER_MARGIN_USD = 100.0
HARD_MAX_LEVERAGE = 4.0
HARD_MAX_MARKET_NOTIONAL_USD = 300.0
UTIL_FEE_PER_DAY = 0.01    # 利用率费：名义金额 1%/天（子图 longUtilFeePerDay=0.01，实测一致）


@dataclass
class Params:
    long_open_hour: float = 7.0      # 开多（香港时间）
    long_close_hour: float = 20.0    # 平多
    short_enabled: bool = False      # 是否在夜间做空
    short_open_hour: float = 21.0
    short_close_hour: float = 4.0
    notional_usd: float = 50.0       # 每笔名义金额
    leverage: float = 2.0            # 保证金 = 名义 / 杠杆（交易所上限 10x）
    max_notional_usd: float = 200.0  # 总持仓名义上限
    max_slippage_pct: float = 1.5    # 预估滑点超过就不下单；实盘也作为 amt1Limit 的容忍度
    slip_pct_per_100usd: float = 0.9 # 滑点模型：每 $100 名义约 0.9%（10-31 后成交实测中位数）
    stop_loss_pct: float = 8.0       # mark 相对开仓价反向超过此值即平仓
    max_chase_pct: float = 5.0       # 开多时 mark 已高于指数超过此值就不追；开空对称
    max_adverse_funding_pct_day: float = 6.0  # 持仓方向要付的 funding 超过 名义%/天 就不开
    entry_grace_hours: float = 2.0   # 只在开仓时刻后这么多小时内开新仓，错过就等明天
    min_margin_usd: float = 5.0      # 交易所最小保证金

    @classmethod
    def from_env(cls, path: str | None = None) -> "Params":
        p = cls()
        path = path or os.environ.get("HORMUZ_PARAMS")
        if path and os.path.exists(path):
            for k, v in json.load(open(path)).items():
                if not hasattr(p, k):
                    raise ValueError(f"未知参数 {k}")
                setattr(p, k, type(getattr(p, k))(v))
        for k in asdict(p):
            ev = os.environ.get("HZ_" + k.upper())
            if ev is not None:
                cur = getattr(p, k)
                setattr(p, k, ev.lower() in ("1", "true", "yes") if isinstance(cur, bool) else type(cur)(ev))
        p.validate()
        return p

    def validate(self):
        for h in (self.long_open_hour, self.long_close_hour, self.short_open_hour, self.short_close_hour):
            if not 0 <= h < 24:
                raise ValueError("小时必须在 [0,24)")
        if self.long_open_hour == self.long_close_hour:
            raise ValueError("开多与平多时间不能相同")
        if self.short_enabled and self.short_open_hour == self.short_close_hour:
            raise ValueError("开空与平空时间不能相同")
        if not 0 < self.leverage <= HARD_MAX_LEVERAGE:
            raise ValueError(f"杠杆须在 (0,{HARD_MAX_LEVERAGE}]（用户上限）")
        if self.notional_usd / self.leverage > HARD_MAX_ORDER_MARGIN_USD:
            raise ValueError(f"单笔保证金不能超过 {HARD_MAX_ORDER_MARGIN_USD}u（用户上限）")
        if self.max_notional_usd > HARD_MAX_MARKET_NOTIONAL_USD:
            raise ValueError(f"单一市场总仓位名义不能超过 {HARD_MAX_MARKET_NOTIONAL_USD}u（用户上限）")
        if self.notional_usd <= 0 or self.notional_usd > self.max_notional_usd:
            raise ValueError("notional_usd 须 >0 且不超过 max_notional_usd")
        if self.notional_usd / self.leverage < self.min_margin_usd:
            raise ValueError("保证金低于交易所最小值 $5")


def in_window(hour: float, start: float, end: float) -> bool:
    """[start, end) 区间，支持跨午夜（如 21→4）。"""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def hours_since(hour: float, start: float) -> float:
    return (hour - start) % 24


def target_side(hour: float, p: Params) -> int:
    """按时间表应持有的方向：+1 多，-1 空，0 空仓。"""
    if in_window(hour, p.long_open_hour, p.long_close_hour):
        return 1
    if p.short_enabled and in_window(hour, p.short_open_hour, p.short_close_hour):
        return -1
    return 0


def est_slippage_pct(notional: float, p: Params) -> float:
    return p.slip_pct_per_100usd * notional / 100.0


@dataclass
class Market:
    mark: float                     # AMM 现价（可成交价格的参照）
    index: float                    # 指数
    funding_per_day: float = 0.0    # 每单位每天 USD；>0 多头付空头
    capacity_long_usd: float = float("inf")   # 剩余可开多名义（USD）
    capacity_short_usd: float = float("inf")


@dataclass
class Position:
    side: int          # +1 / -1
    size: float        # 单位数（>0）
    entry_price: float
    opened_hour: float = 0.0


@dataclass
class Decision:
    action: str                 # "open" | "close" | "hold"
    side: int = 0
    notional: float = 0.0
    reason: str = ""
    blocked: list = field(default_factory=list)


def open_guards(side: int, m: Market, p: Params, current_notional: float) -> list[str]:
    """返回阻止开仓的原因列表（空 = 可以开）。"""
    why = []
    dev = (m.mark / m.index - 1) * 100 if m.index > 0 else 0.0
    if side > 0 and dev > p.max_chase_pct:
        why.append(f"mark 高于指数 {dev:.1f}% > {p.max_chase_pct}%，不追多")
    if side < 0 and -dev > p.max_chase_pct:
        why.append(f"mark 低于指数 {-dev:.1f}% > {p.max_chase_pct}%，不追空")
    adverse = side * m.funding_per_day / m.mark * 100 if m.mark > 0 else 0.0
    if adverse > p.max_adverse_funding_pct_day:
        why.append(f"该方向需付 funding {adverse:.1f}%/天 > {p.max_adverse_funding_pct_day}%")
    if current_notional + p.notional_usd > p.max_notional_usd + 1e-9:
        why.append("超过最大持仓名义")
    s = est_slippage_pct(p.notional_usd, p)
    if s > p.max_slippage_pct:
        why.append(f"预估滑点 {s:.2f}% > {p.max_slippage_pct}%")
    cap = m.capacity_long_usd if side > 0 else m.capacity_short_usd
    if p.notional_usd > cap:
        why.append(f"池子剩余容量 ${cap:.0f} 不足")
    if m.mark <= 0 or m.index <= 0:
        why.append("价格数据无效")
    return why


def decide(hour: float, m: Market, pos: Position | None, p: Params, stopped: bool = False) -> Decision:
    """单步决策。每次最多一个动作：先平（止损/到点/换向），再开。"""
    want = target_side(hour, p)
    if pos is not None:
        pnl_pct = pos.side * (m.mark / pos.entry_price - 1) * 100
        if pnl_pct <= -p.stop_loss_pct:
            return Decision("close", pos.side, reason=f"止损 {pnl_pct:.1f}%")
        if want != pos.side:
            return Decision("close", pos.side, reason="到点平仓" if want == 0 else "换向平仓")
        return Decision("hold", pos.side, reason="持有")
    if want == 0:
        return Decision("hold", reason="窗口外空仓")
    if stopped:
        return Decision("hold", want, reason="本窗口已止损，等下一轮")
    start = p.long_open_hour if want > 0 else p.short_open_hour
    if hours_since(hour, start) > p.entry_grace_hours:
        return Decision("hold", want, reason="已错过开仓时段，等下一轮")
    blocked = open_guards(want, m, p, 0.0)
    if blocked:
        return Decision("hold", want, reason="风控拦截", blocked=blocked)
    return Decision("open", want, notional=p.notional_usd, reason="按时间表开仓")


def fill_price(mark: float, side_of_swap: int, notional: float, p: Params) -> float:
    """模拟成交价：买（+1）比 mark 贵，卖（-1）比 mark 便宜。"""
    return mark * (1 + side_of_swap * est_slippage_pct(notional, p) / 100)


def trade_pnl(side: int, size: float, entry_fill: float, exit_fill: float,
              funding_usd_per_unit: float, hold_days: float) -> dict:
    """一笔完整交易的净收益拆分（USD）。funding_usd_per_unit 为持仓期间累计的 funding（>0 多头付）。"""
    gross = side * size * (exit_fill - entry_fill)
    fees = FEE_RATE * size * (entry_fill + exit_fill)
    funding = -side * size * funding_usd_per_unit
    util = UTIL_FEE_PER_DAY * size * entry_fill * hold_days
    return dict(gross=gross, fees=fees, funding=funding, util=util,
                net=gross - fees + funding - util)

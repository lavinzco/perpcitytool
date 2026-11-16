"""HORMUZ-SHIPS 日内多空策略：纯逻辑，无网络、无副作用，供实盘循环、模拟和测试共用。

时间一律按香港时间（Asia/Hong_Kong，UTC+8）的小时数（可带小数）处理。
规则（用户 2026-11-09 设定，全部为硬性）：
- 只做多；每天早上开仓目标名义 200u（默认 7:00 开、20:00 平）
- 杠杆必须在 [2, 4]；单笔保证金 ≤100u；单一市场总仓位名义 ≤300u
- 开仓、平仓都分批；每批先报价，价格冲击超上限就这一步不下
- 未实现亏损达到合计保证金的 30% 即全部止损，当天不再开
空单腿（short_enabled=true 时才开，2026-11-09 用户追加）：
- 19:00 起分批开空（窗口 1 小时），第二天 05:00 起分批平空，06:00 没平完就放宽冲击上限强平
- 其余规则与多单一样；多空各自算止损，一条腿止损只停那条腿当期
- short_enabled=false（默认）时行为与纯多头版本完全一致
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json, os

FEE_RATE = 0.0111          # 每次 swap 按名义收 1.11%（链上 lpFee 1% + insurance 0.1% + creator 0.01%）
UTIL_FEE_PER_DAY = 0.01    # 多头利用率费：名义 1%/天（链上 rates.longUtilFeePerDay=0.01）
SHORT_UTIL_FEE_PER_DAY = 0.01  # 空头利用率费：名义 1%/天（链上 rates.shortUtilFeePerDay=0.01，2026-11-09 读）
# 用户硬性上限（2026-11-09）
HARD_MAX_ORDER_MARGIN_USD = 100.0
HARD_MIN_LEVERAGE = 2.0
HARD_MAX_LEVERAGE = 4.0
HARD_MAX_MARKET_NOTIONAL_USD = 300.0
HARD_MAX_DAILY_TARGET_USD = 200.0   # 每条腿每天开仓名义上限
HARD_STOP_LOSS_MARGIN_PCT = 30.0   # 止损线不允许放宽到 30% 以上
EXCHANGE_MIN_MARGIN_USD = 5.0


@dataclass
class Params:
    long_open_hour: float = 7.0         # 开多（香港时间）
    long_close_hour: float = 20.0       # 平多
    target_notional_usd: float = 200.0  # 每天开仓目标名义
    batch_notional_usd: float = 50.0    # 每批名义
    batch_interval_min: float = 10.0    # 两批之间至少间隔（分钟）；cron 每 5 分钟一步
    leverage: float = 2.0               # 必须在 [2,4]
    max_notional_usd: float = 300.0     # 单一市场总仓位名义上限（含钱包里其他仓位）
    max_impact_pct: float = 1.5         # 每批价格冲击上限（不含 1.11% 手续费），超了这步不下
    max_slippage_pct: float = 1.5       # 链上 amt1Limit 相对报价的容忍度
    stop_loss_margin_pct: float = 30.0  # 未实现亏损 ≥ 合计保证金的这个比例 → 全部止损
    stop_max_impact_pct: float = 4.0    # 止损/强平时放宽的价格冲击上限
    stop_slippage_pct: float = 3.0      # 止损/强平时的 amt1Limit 容忍度
    close_force_after_hours: float = 1.0  # 平仓时刻后这么久还没平完，改用放宽的限制、不再等间隔
    max_chase_pct: float = 5.0          # mark 高于指数超过此值不追多
    max_adverse_funding_pct_day: float = 6.0  # 多头要付的 funding 超过名义%/天 就不开
    entry_grace_hours: float = 2.0      # 开仓时刻后这么多小时内可以继续凑批次
    usdc_buffer_usd: float = 0.5        # 开仓后钱包至少留这么多 USDC
    slip_pct_per_100usd: float = 0.8    # 无链上报价时的冲击模型（实测 50u≈0.41%）
    ignore_position_ids: list = field(default_factory=lambda: [117])  # 不归 bot 管的仓位（不平、不计入 state）
    # ---- 空单腿（默认关闭；关闭时行为与纯多头版完全一致）----
    short_enabled: bool = False
    short_open_hour: float = 19.0       # 开空（香港时间）；空单交易日从这里算
    short_close_hour: float = 5.0       # 第二天平空
    short_target_notional_usd: float = 200.0   # 每个空单交易日开仓目标名义（≤200）
    short_entry_grace_hours: float = 1.0       # 开空时刻后这么多小时内可以继续凑批次
    short_close_force_after_hours: float = 1.0 # 平空时刻后这么久还没平完就放宽限制
    daily_total_open_cap_usd: float = 0.0      # 同一日历日多+空合计开仓名义上限；0 = 不设（多空各自 ≤ 各自目标）
    stale_abort_seconds: float = 120.0         # 一步墙钟超过此时长则放弃普通开平；0 = 关闭；止损仍执行。缺省即可，不必写入 params JSON

    @classmethod
    def from_env(cls, path: str | None = None) -> "Params":
        p = cls()
        path = path or os.environ.get("HORMUZ_PARAMS")
        if path and os.path.exists(path):
            for k, v in json.load(open(path)).items():
                if not hasattr(p, k):
                    raise ValueError(f"未知参数 {k}")
                setattr(p, k, _conv(getattr(p, k), v))
        for k in asdict(p):
            ev = os.environ.get("HZ_" + k.upper())
            if ev is not None:
                setattr(p, k, _conv(getattr(p, k), ev))
        p.validate()
        return p

    @property
    def batch_margin_usd(self) -> float:
        return self.batch_notional_usd / self.leverage

    def leg(self, side: str) -> "Leg":
        if side == "long":
            return Leg("long", self.long_open_hour, self.long_close_hour, self.target_notional_usd,
                       self.entry_grace_hours, self.close_force_after_hours)
        if side == "short":
            return Leg("short", self.short_open_hour, self.short_close_hour, self.short_target_notional_usd,
                       self.short_entry_grace_hours, self.short_close_force_after_hours)
        raise ValueError(f"未知方向 {side}")

    def validate(self):
        for h in (self.long_open_hour, self.long_close_hour):
            if not 0 <= h < 24:
                raise ValueError("小时必须在 [0,24)")
        if self.long_open_hour == self.long_close_hour:
            raise ValueError("开多与平多时间不能相同")
        if not HARD_MIN_LEVERAGE <= self.leverage <= HARD_MAX_LEVERAGE:
            raise ValueError(f"杠杆须在 [{HARD_MIN_LEVERAGE:g},{HARD_MAX_LEVERAGE:g}]（用户规则）")
        if self.batch_margin_usd > HARD_MAX_ORDER_MARGIN_USD:
            raise ValueError(f"单笔保证金不能超过 {HARD_MAX_ORDER_MARGIN_USD:g}u（用户上限）")
        if self.batch_margin_usd < EXCHANGE_MIN_MARGIN_USD:
            raise ValueError("每批保证金低于交易所最小值 $5")
        if not 0 < self.max_notional_usd <= HARD_MAX_MARKET_NOTIONAL_USD:
            raise ValueError(f"单一市场总仓位名义不能超过 {HARD_MAX_MARKET_NOTIONAL_USD:g}u（用户上限）")
        if not 0 < self.target_notional_usd <= min(HARD_MAX_DAILY_TARGET_USD, self.max_notional_usd):
            raise ValueError(f"每日目标名义须 >0 且 ≤ {HARD_MAX_DAILY_TARGET_USD:g}u 和总仓位上限")
        if not 0 < self.batch_notional_usd <= self.target_notional_usd:
            raise ValueError("每批名义须 >0 且不超过每日目标")
        if not 0 < self.stop_loss_margin_pct <= HARD_STOP_LOSS_MARGIN_PCT:
            raise ValueError(f"止损比例须在 (0,{HARD_STOP_LOSS_MARGIN_PCT:g}]（用户规则）")
        if self.batch_interval_min < 0 or self.entry_grace_hours < 0 or self.close_force_after_hours < 0:
            raise ValueError("时间参数不能为负")
        if self.stale_abort_seconds < 0:
            raise ValueError("stale_abort_seconds 不能为负")
        if not 0 < self.max_impact_pct <= self.stop_max_impact_pct:
            raise ValueError("max_impact_pct 须 >0 且不大于 stop_max_impact_pct")
        if self.max_slippage_pct <= 0 or self.stop_slippage_pct <= 0:
            raise ValueError("滑点容忍度须 >0")
        if not 0 <= self.daily_total_open_cap_usd <= 2 * HARD_MAX_DAILY_TARGET_USD:
            raise ValueError(f"daily_total_open_cap_usd 须在 [0,{2 * HARD_MAX_DAILY_TARGET_USD:g}]（0 = 不设合计上限）")
        if self.short_enabled:
            for h in (self.short_open_hour, self.short_close_hour):
                if not 0 <= h < 24:
                    raise ValueError("小时必须在 [0,24)")
            if self.short_open_hour == self.short_close_hour:
                raise ValueError("开空与平空时间不能相同")
            if not 0 < self.short_target_notional_usd <= min(HARD_MAX_DAILY_TARGET_USD, self.max_notional_usd):
                raise ValueError(f"空单每日目标名义须 >0 且 ≤ {HARD_MAX_DAILY_TARGET_USD:g}u 和总仓位上限")
            if self.batch_notional_usd > self.short_target_notional_usd:
                raise ValueError("每批名义不能超过空单每日目标")
            if self.short_entry_grace_hours < 0 or self.short_close_force_after_hours < 0:
                raise ValueError("时间参数不能为负")
            # 多空持仓时段不能重叠（否则两边同时持有 400u > 300u 总上限）
            lo, ls = self.leg("long"), self.leg("short")
            if _windows_overlap(lo.open_hour, (lo.close_hour + lo.force_after_hours) % 24,
                                ls.open_hour, (ls.close_hour + ls.force_after_hours) % 24):
                raise ValueError("多单与空单的持仓时段（开仓→平仓+强平宽限）重叠")


def _conv(cur, v):
    if isinstance(cur, bool):
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
    if isinstance(cur, list):
        if isinstance(v, list):
            return [int(x) for x in v]
        return [int(x) for x in str(v).replace(" ", "").split(",") if x]
    return type(cur)(v)


@dataclass
class Leg:
    """一条腿（多或空）的时间表与目标。"""
    side: str
    open_hour: float
    close_hour: float
    target_notional_usd: float
    entry_grace_hours: float
    force_after_hours: float


def _windows_overlap(a0, a1, b0, b1, step=1 / 60) -> bool:
    """两个 [start,end) 循环时段是否有交集（按分钟采样）。"""
    return any(in_window(k * step, a0, a1) and in_window(k * step, b0, b1) for k in range(int(24 / step)))


def in_window(hour: float, start: float, end: float) -> bool:
    """[start, end) 区间，支持跨午夜。"""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def hours_since(hour: float, start: float) -> float:
    return (hour - start) % 24


@dataclass
class Market:
    mark: float                     # AMM 现价
    index: float                    # 指数
    funding_per_day: float = 0.0    # 每单位每天 USD；>0 多头付空头
    capacity_long_usd: float = float("inf")
    capacity_short_usd: float = float("inf")   # 空单腿用


@dataclass
class Lot:
    """一批开出的仓位（链上独立 positionId）。"""
    id: str
    margin: float          # 投入保证金（含开仓手续费，钱包实际转出）
    notional: float        # 开仓名义 |usdDelta|
    size: float            # 单位数
    entry_price: float
    close_value: float     # 按当前价格估算立刻平仓能拿回的 USDC（含平仓费、冲击、funding、利用率费）
    day: str = ""          # 开仓所属交易日
    overdue: bool = False  # 属于之前交易日、早该平掉
    side: str = "long"     # "long" | "short"


@dataclass
class Decision:
    action: str                    # "open" | "close" | "hold"
    reason: str = ""
    margin: float = 0.0            # open：本批保证金
    notional: float = 0.0          # open：本批名义（计划值）
    ids: list = field(default_factory=list)   # close：要平的 positionId
    stop: bool = False             # close：是否止损（放宽限制、全平）
    max_impact_pct: float = 0.0
    slippage_pct: float = 0.0
    blocked: list = field(default_factory=list)
    side: str = "long"


def stop_status(lots: list[Lot], stop_pct: float) -> dict:
    """合计口径：亏损 = Σ投入保证金 − Σ估算平仓价值；亏损 ≥ stop_pct% × Σ投入保证金 即触发。"""
    margin = sum(l.margin for l in lots)
    value = sum(l.close_value for l in lots)
    loss = margin - value
    pct = loss / margin * 100 if margin > 0 else 0.0
    return dict(margin=margin, value=value, loss=loss, loss_pct=pct,
                triggered=bool(lots) and margin > 0 and pct >= stop_pct - 1e-9)


def stop_drop_pct(leverage: float, stop_pct: float = 30.0, open_impact_pct: float = 0.41,
                  close_impact_pct: float = 0.40, hold_hours: float = 0.0,
                  fee: float = FEE_RATE, util_per_day: float = UTIL_FEE_PER_DAY, funding_pct: float = 0.0,
                  side: str = "long") -> float:
    """止损线换算成相对开仓时现价的不利变动（%）：多头为跌幅 d，空头为涨幅 r。
    多：亏损/保证金 = L·[f + u + 1 − (1−d)(1−i_c)(1−f)/(1+i_o)]
    空：亏损/保证金 = L·[f + u − 1 + (1+r)(1+i_c)(1+f)/(1−i_o)]   （开空卖出、平空买回）
    令其 = stop_pct 解 d / r。u = 利用率费 + 该方向付出的净 funding（占名义），f = 1.11%，i_o / i_c 为开/平价格冲击。"""
    u = util_per_day * hold_hours / 24 + funding_pct / 100
    if side == "short":
        one_plus_r = (1 - fee - u + stop_pct / 100 / leverage) * (1 - open_impact_pct / 100) / (
            (1 + close_impact_pct / 100) * (1 + fee))
        return (one_plus_r - 1) * 100
    one_minus_d = (1 + fee + u - stop_pct / 100 / leverage) * (1 + open_impact_pct / 100) / (
        (1 - close_impact_pct / 100) * (1 - fee))
    return (1 - one_minus_d) * 100


def open_guards(m: Market, p: Params, *, notional: float, margin: float, all_notional: float,
                opened_today: float, wallet_usdc: float, side: str = "long", other_opened_today: float = 0.0) -> list[str]:
    """返回阻止本批开仓的原因（空 = 可以开）。all_notional 为钱包在该市场的全部多空仓位名义绝对值（含被忽略的）。
    opened_today 为本条腿当期已开；other_opened_today 为另一条腿同一日历日已开（只用于 daily_total_open_cap_usd）。"""
    why = []
    if m.mark <= 0 or m.index <= 0:
        return ["价格数据无效"]
    short = side == "short"
    leg = p.leg(side)
    dev = (m.mark / m.index - 1) * 100
    if short:
        if -dev > p.max_chase_pct:
            why.append(f"mark 低于指数 {-dev:.1f}% > {p.max_chase_pct}%，不追空")
        adverse = -m.funding_per_day / m.mark * 100      # funding<0 时空头付钱
        if adverse > p.max_adverse_funding_pct_day:
            why.append(f"空头需付 funding {adverse:.1f}%/天 > {p.max_adverse_funding_pct_day}%")
    else:
        if dev > p.max_chase_pct:
            why.append(f"mark 高于指数 {dev:.1f}% > {p.max_chase_pct}%，不追多")
        adverse = m.funding_per_day / m.mark * 100
        if adverse > p.max_adverse_funding_pct_day:
            why.append(f"多头需付 funding {adverse:.1f}%/天 > {p.max_adverse_funding_pct_day}%")
    if margin > HARD_MAX_ORDER_MARGIN_USD + 1e-9:
        why.append(f"本批保证金 {margin:.2f} > {HARD_MAX_ORDER_MARGIN_USD:g}u")
    if margin < EXCHANGE_MIN_MARGIN_USD - 1e-9:
        why.append("本批保证金低于交易所最小 $5")
    cap = min(p.max_notional_usd, HARD_MAX_MARKET_NOTIONAL_USD)
    if all_notional + notional > cap + 1e-9:
        why.append(f"总仓位名义 {all_notional:.1f}+{notional:.1f} > {cap:g}u")
    if opened_today + notional > leg.target_notional_usd * 1.01 + 1e-9:   # 1% 容差：链上名义与计划值有小偏差
        why.append(f"{'空单' if short else ''}当日已开 {opened_today:.1f}+{notional:.1f} 超过目标 {leg.target_notional_usd:g}u")
    if p.daily_total_open_cap_usd > 0 and \
            opened_today + other_opened_today + notional > p.daily_total_open_cap_usd * 1.01 + 1e-9:
        why.append(f"多空合计当日已开 {opened_today + other_opened_today:.1f}+{notional:.1f} "
                   f"超过合计上限 {p.daily_total_open_cap_usd:g}u")
    if wallet_usdc < margin + p.usdc_buffer_usd:
        why.append(f"钱包 USDC {wallet_usdc:.2f} 不够本批保证金 {margin:.2f}+缓冲")
    cap_side = m.capacity_short_usd if short else m.capacity_long_usd
    if notional > cap_side:
        why.append(f"池子剩余可开{'空' if short else '多'} ${cap_side:.0f} 不足")
    return why


def decide(hour: float, m: Market, lots: list[Lot], p: Params, *, stopped: bool, opened_today: float,
           minutes_since_last: float, all_notional: float, wallet_usdc: float,
           side: str = "long", other_opened_today: float = 0.0) -> Decision:
    """单条腿的单步决策。优先级：止损 > 平过期仓 > 到点分批平 > 窗口内分批开 > 持有。
    lots 只取本条腿（side 相同）的仓位；stopped / opened_today / minutes_since_last 都是本条腿当期的。"""
    lots = [l for l in lots if getattr(l, "side", "long") == side]
    leg = p.leg(side)
    D = lambda *a, **k: Decision(*a, side=side, **k)
    st = stop_status(lots, p.stop_loss_margin_pct)
    if lots and (st["triggered"] or stopped):
        return D("close", f"止损：亏损 {st['loss']:.2f}u = 保证金 {st['loss_pct']:.1f}% ≥ {p.stop_loss_margin_pct:g}%"
                 if st["triggered"] else "止损后继续平剩余仓位",
                 ids=[l.id for l in lots], stop=True,
                 max_impact_pct=p.stop_max_impact_pct, slippage_pct=p.stop_slippage_pct)
    in_win = in_window(hour, leg.open_hour, leg.close_hour)
    interval_ok = minutes_since_last >= p.batch_interval_min - 1e-9
    overdue = [l for l in lots if l.overdue]
    if overdue:
        return D("close", "平掉之前交易日没平完的仓位", ids=[overdue[0].id], stop=True,
                 max_impact_pct=p.stop_max_impact_pct, slippage_pct=p.stop_slippage_pct)
    if lots and not in_win:
        late = hours_since(hour, leg.close_hour) >= leg.force_after_hours
        if not (interval_ok or late):
            return D("hold", "等平仓批次间隔")
        lot = max(lots, key=lambda l: l.notional)
        if late:
            return D("close", "平仓超时，放宽限制", ids=[lot.id], stop=True,
                     max_impact_pct=p.stop_max_impact_pct, slippage_pct=p.stop_slippage_pct)
        return D("close", "到点分批平仓", ids=[lot.id],
                 max_impact_pct=p.max_impact_pct, slippage_pct=p.max_slippage_pct)
    if not in_win:
        return D("hold", "窗口外空仓")
    if side == "short" and not p.short_enabled:
        return D("hold", "空单未启用（short_enabled=false），不开空")
    if stopped:
        return D("hold", "今天已止损，不再开")
    if hours_since(hour, leg.open_hour) > leg.entry_grace_hours:
        return D("hold", "开仓时段已过，按已开部分持有" if lots else "已错过开仓时段，等明天")
    remaining = leg.target_notional_usd - opened_today
    if remaining < max(p.batch_notional_usd * 0.5, p.leverage * EXCHANGE_MIN_MARGIN_USD):
        return D("hold", "今日目标已开满，持有")
    if not interval_ok:
        return D("hold", "等开仓批次间隔")
    notional = min(p.batch_notional_usd, remaining)
    margin = notional / p.leverage
    blocked = open_guards(m, p, notional=notional, margin=margin, all_notional=all_notional,
                          opened_today=opened_today, wallet_usdc=wallet_usdc,
                          side=side, other_opened_today=other_opened_today)
    if blocked:
        return D("hold", "风控拦截", blocked=blocked)
    return D("open", "按时间表分批开空" if side == "short" else "按时间表分批开多", margin=margin, notional=notional,
             max_impact_pct=p.max_impact_pct, slippage_pct=p.max_slippage_pct)


def model_impact_pct(notional: float, p: Params) -> float:
    """无链上报价时的价格冲击模型（不含手续费）。"""
    return p.slip_pct_per_100usd * notional / 100.0


def sim_open(mark: float, margin: float, leverage: float, impact_pct: float, side: str = "long") -> dict:
    """模拟开仓（与链上一致的口径）：开仓费从保证金里扣，名义 = 保证金 × 杠杆。
    多头买入成交价高于现价；空头卖出成交价低于现价。size 一律为正数。"""
    notional = margin * leverage
    fill = mark * (1 - impact_pct / 100) if side == "short" else mark * (1 + impact_pct / 100)
    return dict(notional=notional, size=notional / fill, entry_price=fill, margin_net=margin - FEE_RATE * notional)


def close_proceeds(size: float, mark: float, close_impact_pct: float, side: str = "long") -> float:
    """平仓那笔 swap 的 USD 腿（带符号，与 SDK effectiveUsdDelta 同号）：
    平多 = 卖出所得（扣冲击和 1.11% 费，为正）；平空 = 买回花费（加冲击和 1.11% 费，为负）。size 为正数。"""
    if side == "short":
        return -size * mark * (1 + close_impact_pct / 100) * (1 + FEE_RATE)
    return size * mark * (1 - close_impact_pct / 100) * (1 - FEE_RATE)


def entry_usd_leg(notional: float, side: str = "long") -> float:
    """开仓 usd 腿（带符号，与链上 position.delta.amount1 同号）：多头付出为负，空头收到为正。"""
    return notional if side == "short" else -notional


def sim_close_value(margin_net: float, notional: float, size: float, mark: float, close_impact_pct: float,
                    owed: float = 0.0, side: str = "long") -> float:
    """估算平仓价值 = 链上保证金 − 应付费用 + 平仓 swap 的 USD 腿 + 开仓 USD 腿。
    多：+ 卖出所得 − 开仓名义；空：− 买回花费 + 开仓卖出所得名义。owed 为该仓位应付的 funding+利用率费（收到为负）。"""
    return margin_net - owed + close_proceeds(size, mark, close_impact_pct, side) + entry_usd_leg(notional, side)

# HORMUZ-SHIPS 日内多空机器人（分批开平 + 保证金 30% 止损）

Perp City 的 HORMUZ-SHIPS 永续合约（Arbitrum One，`0xb9F540CE4315e7F4382C96532f8eC3903899D86d`）。
按指数日内规律：**每天早上 7:00 起分批开多，晚上 20:00 起分批平多**（香港时间，可调）。

**默认 DRY_RUN=1**：只读行情和链上报价、在 `state/state.json` 里记模拟成交，不发任何交易。

## 空单腿（2026-11-09 追加，`short_enabled=true` 才启用；默认关闭，关闭时与纯多头版逐步一致）
- 时间表：19:00 起分批开空（`short_entry_grace_hours`=1，窗口到 20:00），第二天 05:00 起每 ≥10 分钟平一批，06:00（`short_close_force_after_hours`=1）还没平完就放宽到 `stop_max_impact_pct` 强平。
- 空单交易日从 19:00 算（`short_day`），跨午夜不重置；多单的按日重置和「平掉前一交易日遗留」只看多单，不会动空单。
- 规则与多单相同：50u/批、≥10 分钟、2~4x、单笔保证金 ≤100u、单市场多空名义绝对值合计 ≤300u（含 ignore 列表）、冲击上限、滑点、USDC 检查。
- 止损多空各算各的：空单那条腿亏损 ≥ 空单保证金 30% → 全平空单，`short_stopped`，本期（到下一个 19:00）不再开空；多单不受影响，反之亦然。
- 追价对称：mark **低于**指数超过 `max_chase_pct` 不追空；funding<0（空头付钱）超过 `max_adverse_funding_pct_day` 不开空。
- 每期开仓名义：`short_target_notional_usd`（默认 200，≤200）；`daily_total_open_cap_usd`（默认 0 = 不设合计，多空各自 ≤ 各自目标）；>0 时同一日历日（D 日 7:00 的多单 + D 日 19:00 的空单）合计不超过它。
- 参数校验：开空时多单「开→平+强平宽限」与空单的同一区间不能重叠（例如多单 20:00 平就会被拒）。
- 执行顺序：每步先算两条腿的决策；有平仓（含止损）就只平，不开；否则最多开一条腿。
- `trade.mjs`：开空必须同时传 `isLong:false, allowShort:true, side:"short"`；开空 amt1Limit = 最少收 |usd|×(1−滑点)，平空（买回）amt1Limit = 最多付 |usd|×(1+滑点)，都用 SDK `calculateTakerSlippageLimit`。`close` 传 `side` 时先核对链上方向。只读 `quote_open` 支持 `isLong:false`，`quote_close` 的 `perpDelta` 可为负（空单）。
- 估值：平空价值 = 链上保证金 − 应付 funding/利用率费（空头收 funding 时为负）− 买回花费（含 1.11% 费和冲击）+ 开空卖出所得；止损涨幅换算见 `stop_drop_pct(side="short")`。

## 用户规则（硬性，2026-11-09）
| 规则 | 落在哪里 |
|---|---|
| 多空 | 空单需 `short_enabled=true`；`trade.mjs` 只有显式 `allowShort:true, side:"short"` 才接受 `isLong:false` |
| 每天开仓目标名义 200u | `target_notional_usd`，校验 ≤200；当日累计超目标（1% 容差）就不开 |
| 杠杆 2~4x，默认 2x | `Params.validate` 与 `trade.mjs` 都拒绝 <2 或 >4 |
| 单笔保证金 ≤100u | `HARD_MAX_ORDER_MARGIN_USD` + `trade.mjs` |
| 单一市场总仓位名义 ≤300u | 按**钱包在该市场的全部仓位**算（含忽略列表里的仓位，如 117）；Python 先查，`trade.mjs` 下单前再从链上扫一遍 |
| 开、平都分批，冲击超限这步不下 | 每批先报价（链上 tick map 精确报价），价格冲击（不含 1.11% 手续费）> `max_impact_pct` 就跳过、下一步再试 |
| 未实现亏损 ≥ 保证金 30% 全部止损，当天不再开 | `stop_loss_margin_pct`，校验 ≤30；替换了旧的 8% 价格止损 |
| mark 高于指数 5% 不追、funding 太贵不开 | `max_chase_pct`、`max_adverse_funding_pct_day` |
| 钱包 USDC 要够 | 每批前检查 USDC ≥ 本批保证金 + `usdc_buffer_usd` |

## 每一步（cron 每 5 分钟跑一次 `bot.py --once`）做什么
优先级：**止损 > 平掉前一交易日遗留仓位 > 到点分批平 > 窗口内分批开 > 持有**，每步最多做一件事（止损除外）。
- **开仓**：7:00 后、`entry_grace_hours`（2 小时）内，每隔 ≥`batch_interval_min`（10 分钟）开一批 `batch_notional_usd`（50u 名义 = 2x 下 25u 保证金），最后一批取剩余额。报价冲击超限、风控拦截、余额不够都只是这一步不下；过了 2 小时还没凑满，就按已开部分持有。
- **平仓**：20:00 起每隔 ≥10 分钟平一批（先平名义最大的），冲击超限这步不平。平仓时刻后 `close_force_after_hours`（1 小时）还没平完，改用放宽的冲击上限 `stop_max_impact_pct`（4%）、不再等间隔。
- **止损**：同一步把所有仓位依次全平，用放宽的冲击上限 4% 和 amt1Limit 容忍度 3%；没平掉的下一步继续平；当天不再开。
- 每批是链上独立的 positionId，`state.json` 的 `lots` 记录每批的 id、投入保证金、名义、均价、tx。

### 链上核对
`trade.mjs positions` 扫最近 400 个 posId 的 `ownerOf`，找出本钱包在该 perp 的所有仓位，再用 `balanceOf` 核对数量；数量对不上就不开新仓。
实盘时**以链上为准**：state 里有但链上没有的（被平/被清算）移出 state；链上有但 state 没记的，按 bot 仓位接管并记警告。
`ignore_position_ids`（默认 `[117]`）里的仓位：bot 不平、不纳入 state、不参与止损计算，但**计入 300u 总上限**。

### 止损的亏损口径
对每一批 bot 仓位，用 SDK 读链上数据，估算「现在立刻平掉能拿回多少 USDC」：
```
平仓估算价值 = 链上保证金（已扣开仓手续费）
             − 应付 funding − 应付利用率费（链上累计到上次 touch，再按当前费率外推到现在）
             + 平仓卖出所得（estimateTakerAdjust，按当前池子、含 1.11% 平仓费和价格冲击）
             − 开仓名义（usdDelta）
亏损 = Σ投入保证金（含开仓手续费）− Σ平仓估算价值；亏损 ≥ 30% × Σ投入保证金 → 全部止损
```
按**合计**仓位算，不是逐批。注意：
- 开仓瞬间就已经「亏」掉来回手续费 + 冲击：2x 约 6% 保证金，3x 约 9%，4x 约 12%。
- 每批平仓报价是各自独立按当前池子算的，没算前一批平仓对池子的冲击，真实全平会比估算略差。
- funding 为负（多头收钱）时会让估值变好；它随时会变。

### 止损线换算成价格跌幅（相对开仓时的 AMM 现价）
| 杠杆 | 刚开完（冲击 0.41%/0.40%） | 持有 13 小时后（利用率费 1%/天） | 池子不恢复、200u 一次性冲击 1.65%/1.6% |
|---|---|---|---|
| 2x | 跌 12.2% | 跌 11.7% | 跌 10.0% |
| 3x | 跌 7.1% | 跌 6.6% | 跌 4.8% |
| 4x | 跌 4.6% | 跌 4.0% | 跌 2.2% |

（未计 funding；公式见 `strategy.stop_drop_pct`。）这个市场日内摆动常有 10~20%，3~4x 下止损会比较容易被打到。

## 分批能省多少冲击（2026-11-09 16:0x 香港时间 链上只读报价）
| 方式 | 价格冲击（不含手续费） |
|---|---|
| 一次性开 200u | 1.65% |
| 每批 ~50u（4 批），每批单独报价 | 0.41% / 批 |
| 每批 ~40u（5 批），每批单独报价 | 0.33% / 批 |
| 4 批但两批之间**没人交易、池子不恢复** | 0.41 / 1.23 / 2.07 / 2.91%，平均仍是 1.65% |
| 平 200u 一次 / 平 50u 一批 | 1.60% / 0.40% |

局限：同一时刻的报价不会反映前一批的冲击。这是 AMM，**分批本身不省冲击，省的是批次之间价格被别人交易/套利拉回来的那部分**。如果间隔里没人交易，第 2 批起冲击会越来越大，超过 `max_impact_pct` 就停下等。

## 参数（`params.example.json`，或环境变量 `HZ_` + 大写参数名，如 `HZ_LEVERAGE=3`）
| 参数 | 默认 | 说明 |
|---|---|---|
| `long_open_hour` / `long_close_hour` | 7 / 20 | 开多、平多时刻（香港时间，可带小数） |
| `target_notional_usd` | 200 | 每天开仓目标名义（≤200） |
| `batch_notional_usd` | 50 | 每批名义（2x 下保证金 25u） |
| `batch_interval_min` | 10 | 两批之间至少隔多少分钟 |
| `leverage` | 2 | 必须在 [2,4] |
| `max_notional_usd` | 300 | 单一市场总仓位名义（≤300） |
| `max_impact_pct` | 1.5 | 每批价格冲击上限（不含 1.11% 手续费） |
| `max_slippage_pct` | 1.5 | 链上 amt1Limit 相对报价的容忍度 |
| `stop_loss_margin_pct` | 30 | 亏损达合计保证金的这个比例全部止损（≤30） |
| `stop_max_impact_pct` / `stop_slippage_pct` | 4 / 3 | 止损和超时强平时放宽的限制 |
| `close_force_after_hours` | 1 | 平仓时刻后多久还没平完就放宽 |
| `entry_grace_hours` | 2 | 开仓时刻后多长时间内可以继续凑批 |
| `max_chase_pct` | 5 | mark 高于指数超过此值不追多 |
| `max_adverse_funding_pct_day` | 6 | 多头要付 funding 超过名义 6%/天不开 |
| `usdc_buffer_usd` | 0.5 | 开仓后钱包至少留的 USDC |
| `ignore_position_ids` | [117] | 不归 bot 管的仓位（环境变量写法 `HZ_IGNORE_POSITION_IDS=117,120`） |
| `short_enabled` | false | 开空单腿 |
| `short_open_hour` / `short_close_hour` | 19 / 5 | 开空、次日平空 |
| `short_target_notional_usd` | 200 | 每期开空目标名义（≤200） |
| `short_entry_grace_hours` / `short_close_force_after_hours` | 1 / 1 | 开空窗口、平空超时放宽 |
| `daily_total_open_cap_usd` | 0 | 同一日历日多+空合计开仓上限，0 = 不设 |

## 文件
| 文件 | 作用 |
|---|---|
| `strategy.py` | 纯策略逻辑（分批、上限、止损、杠杆范围），无副作用 |
| `bot.py` | 主程序：读行情 → 核对仓位 → 决策 → 报价 → 执行（DRY_RUN 只模拟） |
| `datafeed.py` | 只读行情（Bot API 或公开子图 + 指数代理） |
| `live/trade.mjs` | 链上接口。只读：`positions` / `quote_open` / `quote_close`；实盘：`open` / `close`（需三个开关） |
| `strategy_v1.py`, `backtest.py` | 旧版单仓位策略，只用来复现之前的回测 |
| `backtest_short.py` | 简单回测：19:00 开空→次日 05:00 平空 vs 7:00→16:00 多单 |
| `params.live_short.example.json` | params.live.json + 空单字段（启用空单的示例） |
| `tests/legacy/` | 加空单前的 strategy.py / bot.py 快照，只给「关闭空单时与旧行为一致」的测试用 |
| `tests/` | 单测 + 性质测试，`python3 -m unittest discover -s tests` |

`trade.mjs close` 接口与之前兼容：`{action:"close", positionId, slippagePct, perp}`，不传 `maxImpactPct` 时行为不变。

## 运行（模拟）
```bash
cd /workspace/hormuz/bot
python3 -m unittest discover -s tests
python3 bot.py --once --params params.example.json
# 注入时间做端到端模拟（只在 DRY_RUN 下允许），用单独的 state：
HORMUZ_STATE=/tmp/e2e/state.json HORMUZ_LOGDIR=/tmp/e2e python3 bot.py --once --at 2026-11-10T07:00
```
有 `RPC_URL` + `PRIVATE_KEY` 时，模拟也用链上只读报价（`HZ_NO_CHAIN=1` 改用模型）；模拟钱包余额默认读链上 USDC，可用 `SIM_WALLET_USDC=100` 覆盖。

## cron（每 5 分钟一步，**目前没有开**）
```cron
*/5 * * * * cd /workspace/hormuz/bot && /usr/bin/python3 bot.py --once --params params.example.json >> logs/cron.out 2>&1
```
脚本内部固定用 Asia/Hong_Kong；有文件锁，重叠不会重复下单。

## 实盘（需要你明确说「开实盘」，脚本不会自动开启）
```bash
export DRY_RUN=0 LIVE_TRADING=1 CONFIRM_LIVE=I_UNDERSTAND_REAL_MONEY
export PRIVATE_KEY=0x...  RPC_URL=https://...   # 只经环境变量传给 live/trade.mjs，Python 不读取
```
钱包需要 Arbitrum One 原生 USDC（`0xaf88…5831`）。2x 开满 200u 名义需要约 100u 保证金，加缓冲至少 100.5u；ETH 付 gas。

## 已知限制
- 同一时刻的报价不反映前一批的冲击；模拟里每批冲击都按当时池子单独算，偏乐观。
- 模拟的 funding 用当前费率乘持有时长；实盘以链上为准。
- 链上扫描只看最近 400 个 posId；更早的仓位会让 `balanceOf` 对不上，此时 bot 拒绝开仓。
- 页面对部分地区显示 “Not available in your region”，请自行确认当地法规与服务条款。

"""只读行情。优先 Bot API（需 PERPCITY_API_KEY），否则用公开的 Goldsky 子图 + perp.city 指数代理。"""
from __future__ import annotations
import json, os, time, urllib.request
from strategy import Market

PERP = os.environ.get("PERP_ADDRESS", "0xb9F540CE4315e7F4382C96532f8eC3903899D86d")  # HORMUZ-SHIPS
BEACON = os.environ.get("BEACON_ADDRESS", "0x6a32c91e1f99dd5befaaf50fa9da1a4f1ddb96a2")
BOT_API = os.environ.get("PERPCITY_BOT_API", "https://bot-api.perp.city")
SUBGRAPH = os.environ.get("PERPCITY_SUBGRAPH",
    "https://api.goldsky.com/api/public/project_cmbawn40q70fj01ws4jmsfj7f/subgraphs/perp-city-mainnet/2961f841-20261003_171728/gn")
INDEX_PROXY = f"https://perp.city/api/proxy/beacons/{BEACON}/index"
UA = {"user-agent": "hormuz-bot/0.1"}
Q96 = 2 ** 96


def _get(url, headers=None, data=None, timeout=20):
    h = dict(UA, **(headers or {}))
    if data is not None:
        h["content-type"] = "application/json"; data = json.dumps(data).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(int(e.headers.get("Retry-After", "5"))); continue
            raise
        except urllib.error.URLError:
            if attempt < 2: time.sleep(2); continue
            raise


def from_bot_api(key: str) -> tuple[Market, dict]:
    d = _get(f"{BOT_API}/markets/{PERP}/live", {"X-API-Key": key})
    spot = float(d.get("spot_price") or d["mark_price"])
    m = Market(mark=spot, index=float(d["index_price"]),
               funding_per_day=float(d.get("funding_per_day_usd") or d.get("funding_rate_daily") or 0))
    return m, {"source": "bot-api", "raw": d}


def from_public() -> tuple[Market, dict]:
    q = ('{perp(id:"%s"){sqrtPriceX96 fundingPerDay openInterestLong openInterestShort capacityLong '
         'capacityShort emaAmmPriceX96 emaIndexPriceX96 lastTouchedTimestamp}}' % PERP.lower())
    p = _get(SUBGRAPH, data={"query": q})["data"]["perp"]
    spot = (int(p["sqrtPriceX96"]) / Q96) ** 2
    ix = _get(INDEX_PROXY)
    cap_l = max(int(p["capacityLong"]) - int(p["openInterestLong"]), 0) / 1e6 * spot
    cap_s = max(int(p["capacityShort"]) - int(p["openInterestShort"]), 0) / 1e6 * spot
    m = Market(mark=spot, index=float(ix["index_price"]), funding_per_day=int(p["fundingPerDay"]) / 1e18,
               capacity_long_usd=cap_l, capacity_short_usd=cap_s)
    return m, {"source": "subgraph+index-proxy", "index_ts": ix.get("timestamp"),
               "ema_mark": int(p["emaAmmPriceX96"]) / Q96, "ema_index": int(p["emaIndexPriceX96"]) / Q96,
               "last_touched": int(p["lastTouchedTimestamp"])}


def snapshot() -> tuple[Market, dict]:
    key = os.environ.get("PERPCITY_API_KEY")
    if key:
        try:
            m, meta = from_bot_api(key)
            # Bot API 不给剩余容量，补一次子图
            try:
                pm, _ = from_public(); m.capacity_long_usd, m.capacity_short_usd = pm.capacity_long_usd, pm.capacity_short_usd
            except Exception:
                pass
            return m, meta
        except Exception as e:  # 降级到公开源
            m, meta = from_public(); meta["bot_api_error"] = str(e); return m, meta
    return from_public()

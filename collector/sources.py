"""数据源适配层。

铁律：本层只取原始值，不做任何单位换算。
raw_value / raw_ccy / source_ts 原样入库，口径换算全部留给分析层——
因为含金量 k、FX、基准源这些参数一定会被反复调整，
调一次就要重采数据的设计是不可接受的。

所有网络调用强制超时（见 ~/.claude/CLAUDE.md 全局约定）。
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

HTTP_TIMEOUT = 12
UA = "Mozilla/5.0 (X11; Linux x86_64)"

# Pyth XAU/USD feed id
PYTH_XAU_FEED = "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"
# Chainlink XAU/USD 主网聚合器；公共 RPC 必须带 User-Agent，否则 403
CHAINLINK_XAU_FEED = "0x214eD9Da11D2fbe465a6fc601a91E62EbEc1a0D6"
_CL_DECIMALS: int | None = None   # decimals() 恒定，缓存以省一半 RPC 调用

ETH_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://1rpc.io/eth",
    "https://eth-mainnet.public.blastapi.io",
]

# 单位标签
USD_OZ = "USD_PER_OZ"       # 美元/盎司
HKD_SHARE = "HKD_PER_SHARE"  # 港币/份
HKD_USD = "HKD_PER_USD"      # 港币/美元
RATE = "RATE"                # 无量纲（如 funding rate）


@dataclass
class Reading:
    source: str
    value: float | None = None
    ccy: str | None = None
    source_ts: float | None = None   # 数据源自己的时间戳（unix 秒）
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _get_json(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post_json(url: str, payload: dict, timeout: int = HTTP_TIMEOUT) -> Any:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, body, {"Content-Type": "application/json", "User-Agent": UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _err(source: str, e: Exception) -> Reading:
    return Reading(source=source, error=f"{type(e).__name__}: {str(e)[:180]}")


# ---------------------------------------------------------------- Pyth

def fetch_pyth() -> list[Reading]:
    """Pyth XAU/USD —— 真预言机，亚秒级更新，带置信区间 conf。"""
    try:
        url = (
            "https://hermes.pyth.network/v2/updates/price/latest"
            f"?ids[]={PYTH_XAU_FEED}&parsed=true&encoding=hex"
        )
        d = _get_json(url)
        p = d["parsed"][0]["price"]
        expo = int(p["expo"])
        price = int(p["price"]) * (10 ** expo)
        conf = int(p["conf"]) * (10 ** expo)
        return [
            Reading(
                source="pyth_xauusd",
                value=price,
                ccy=USD_OZ,
                source_ts=float(p["publish_time"]),
                # conf 是 Pyth 的价格不确定度，用它可以判断该报价可不可信
                meta={"conf": conf, "conf_bps": (conf / price * 1e4) if price else None, "expo": expo},
            )
        ]
    except Exception as e:
        return [_err("pyth_xauusd", e)]


# ----------------------------------------------------------- Chainlink

def fetch_chainlink() -> list[Reading]:
    """Chainlink XAU/USD —— heartbeat 慢（约 1h 或 0.5% 偏离才更新）。

    age_sec 会长期很大，这不是故障，正是要测量的对象：
    陈旧期内累积的偏离 = 锚它做报价时的套利打击面。
    """
    last_exc: Exception | None = None
    for rpc in ETH_RPCS:
        try:
            def call(data: str) -> str:
                res = _post_json(rpc, {
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": CHAINLINK_XAU_FEED, "data": data}, "latest"],
                })
                if "result" not in res:
                    raise RuntimeError(str(res)[:150])
                return res["result"]

            global _CL_DECIMALS
            raw = call("0xfeaf968c")[2:]  # latestRoundData()
            words = [raw[i * 64:(i + 1) * 64] for i in range(5)]
            answer = int(words[1], 16)
            updated_at = int(words[3], 16)
            if _CL_DECIMALS is None:
                _CL_DECIMALS = int(call("0x313ce567"), 16)  # decimals()
            decimals = _CL_DECIMALS
            return [
                Reading(
                    source="chainlink_xauusd",
                    value=answer / (10 ** decimals),
                    ccy=USD_OZ,
                    source_ts=float(updated_at),
                    meta={"round_id": int(words[0], 16), "decimals": decimals, "rpc": rpc},
                )
            ]
        except Exception as e:
            last_exc = e
            continue
    return [_err("chainlink_xauusd", last_exc or RuntimeError("all RPCs failed"))]


# ------------------------------------------------------------- Binance

def fetch_binance_spot() -> list[Reading]:
    """PAXG / XAUT 现货最优买卖价（一次请求拿两个标的）。"""
    try:
        url = ('https://api.binance.com/api/v3/ticker/bookTicker'
               '?symbols=%5B%22PAXGUSDT%22,%22XAUTUSDT%22%5D')
        rows = _get_json(url)
        now = time.time()
        out: list[Reading] = []
        for row in rows:
            tag = {"PAXGUSDT": "paxg", "XAUTUSDT": "xaut"}.get(row["symbol"])
            if not tag:
                continue
            bid, ask = float(row["bidPrice"]), float(row["askPrice"])
            for side, px, qty in (("bid", bid, row["bidQty"]), ("ask", ask, row["askQty"])):
                out.append(Reading(
                    source=f"binance_{tag}_spot_{side}",
                    value=px, ccy=USD_OZ, source_ts=now,
                    meta={"qty": float(qty),
                          "spread_bps": (ask - bid) / ((ask + bid) / 2) * 1e4 if bid and ask else None},
                ))
        return out or [_err("binance_spot", RuntimeError("no rows"))]
    except Exception as e:
        return [_err("binance_spot", e)]


def fetch_binance_perp() -> list[Reading]:
    """PAXG / XAUT 永续的 mark / index / funding —— 你实际对冲用的就是这条腿。"""
    out: list[Reading] = []
    for sym, tag in (("PAXGUSDT", "paxg"), ("XAUTUSDT", "xaut")):
        try:
            d = _get_json(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}")
            ts = float(d["time"]) / 1000.0
            meta = {"funding_rate": float(d["lastFundingRate"]),
                    "next_funding_time": d["nextFundingTime"]}
            out.append(Reading(source=f"binance_{tag}_perp_mark", value=float(d["markPrice"]),
                               ccy=USD_OZ, source_ts=ts, meta=meta))
            out.append(Reading(source=f"binance_{tag}_perp_index", value=float(d["indexPrice"]),
                               ccy=USD_OZ, source_ts=ts, meta={}))
        except Exception as e:
            out.append(_err(f"binance_{tag}_perp_mark", e))
    return out


# ------------------------------------------------------------------ FX

def fetch_fx() -> list[Reading]:
    """USD/HKD。

    注意：这是日频源。联系汇率制下日内漂移通常 < 5bps，
    相对于要观测的 28bps spread 属次要误差，但分析时必须标注。
    meta.date 记录报价日期，用于判断新鲜度。
    """
    try:
        d = _get_json("https://api.frankfurter.app/latest?from=USD&to=HKD")
        return [Reading(source="fx_usdhkd", value=float(d["rates"]["HKD"]),
                        ccy=HKD_USD, source_ts=None,
                        meta={"date": d.get("date"), "frequency": "daily"})]
    except Exception as e:
        return [_err("fx_usdhkd", e)]


# ---------------------------------------------------------------- 3030

def fetch_futu_3030(quote_ctx) -> list[Reading]:
    """3030.HK 盘口。取 bid/ask 两条，量与更新时间放 meta。

    薄盘 ETF 的真实价格在盘口而非成交价：成交价 90% 的分钟没有更新，
    且 tick=0.005HKD@7.0 已达 7.1bps 分辨率。
    休市时段返回 error='market_closed'，不影响其余数据源。
    """
    from futu import RET_OK
    try:
        r, ob = quote_ctx.get_order_book("HK.03030", num=5)
        if r != RET_OK:
            return [_err("futu_3030_bid", RuntimeError(str(ob)[:150]))]
        bids, asks = ob.get("Bid") or [], ob.get("Ask") or []
        if not bids or not asks:
            return [Reading(source="futu_3030_bid", error="empty_book"),
                    Reading(source="futu_3030_ask", error="empty_book")]
        svr_ts = ob.get("svr_recv_time_bid") or ob.get("svr_recv_time_ask")
        out = []
        for name, lvl in (("bid", bids[0]), ("ask", asks[0])):
            out.append(Reading(
                source=f"futu_3030_{name}", value=float(lvl[0]), ccy=HKD_SHARE,
                source_ts=None,
                meta={"volume": float(lvl[1]), "order_count": int(lvl[2]),
                      "depth5": [[float(x[0]), float(x[1])] for x in (bids if name == "bid" else asks)[:5]],
                      "svr_recv_time": str(svr_ts) if svr_ts else None},
            ))
        return out
    except Exception as e:
        return [_err("futu_3030_bid", e)]


ALL_HTTP_FETCHERS = [fetch_pyth, fetch_chainlink, fetch_binance_spot, fetch_binance_perp, fetch_fx]

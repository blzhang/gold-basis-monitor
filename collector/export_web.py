"""导出网页数据。复用 analyze.py 的口径，保证页面与命令行报告同源。

产出两个文件：
  data.json       最近 RECENT_H 小时，全分辨率(15s)  —— 页面默认加载，高频刷新
  data_long.json  最近 LONG_D 天，降采样到 5 分钟    —— 切长周期时按需加载
分开是因为全分辨率长周期的 JSON 会到 MB 级，没必要每次刷新都传。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze as A

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("GOLDMON_WEB", os.path.join(_REPO, "web"))
RECENT_H = 8        # 全分辨率窗口（小时）
LONG_D = 14         # 长周期窗口（天）
LONG_RULE = "5min"  # 长周期降采样粒度
BASE = A.BASE

# (列后缀, 图例, 颜色) —— d_ 相对 Pyth，b_ 相对 3030
SERIES = [
    ("chainlink_xauusd",        "Chainlink",  "#e0533d"),
    ("binance_paxg_spot_mid",   "PAXG 现货",  "#e8a33d"),
    ("binance_paxg_perp_mark",  "PAXG perp",  "#c2762a"),
    ("binance_xaut_spot_mid",   "XAUT 现货",  "#4f9bd9"),
    ("binance_xaut_perp_mark",  "XAUT perp",  "#2f6fa8"),
]
D_EXTRA = [("implied_3030_usd_oz", "3030.HK", "#5aab6b")]   # 只在 d_ 图出现
B_EXTRA = [(BASE,                  "Pyth",    "#7a5cc4")]   # 只在 b_ 图出现


def clean(seq, nd=3) -> list:
    out = []
    for v in seq:
        if v is None:
            out.append(None); continue
        f = float(v)
        out.append(None if (math.isnan(f) or math.isinf(f)) else round(f, nd))
    return out


def build(w: pd.DataFrame, k, ticks: pd.DataFrame, full: bool) -> dict:
    d: dict = {
        "updated": time.time(), "k": k, "base": BASE, "n_slots": int(len(w)),
        "full_res": full,
        "t": [int(x.timestamp()) for x in w.index],
        "d": {}, "b": {}, "labels": {},
    }
    for col, lab, c in SERIES + D_EXTRA:
        if f"d_{col}" in w.columns:
            d["d"][col] = clean(w[f"d_{col}"], 2)
            d["labels"][col] = [lab, c]
    for col, lab, c in SERIES + B_EXTRA:
        if f"b_{col}" in w.columns:
            d["b"][col] = clean(w[f"b_{col}"], 2)
            d["labels"].setdefault(col, [lab, c])

    if "hk_open" in w.columns:
        d["hk_open"] = [bool(x) if x == x else None for x in w["hk_open"]]

    for col, key, nd in [
        (BASE, "pyth", 2), ("chainlink_xauusd__age", "cl_age", 0),
        ("mid_3030_hkd", "mid_3030", 4), ("futu_3030_bid", "bid_3030", 4),
        ("futu_3030_ask", "ask_3030", 4), ("spread_3030_bps", "spread_3030", 1),
        ("implied_3030_usd_oz", "implied_3030", 2), ("fx_usdhkd", "fx", 4),
    ]:
        if col in w.columns:
            d[key] = clean(w[col], nd)

    last = w.iloc[-1]
    latest = {"ts": int(w.index[-1].timestamp())}
    for col in [BASE, "chainlink_xauusd", "binance_paxg_spot_mid", "binance_paxg_perp_mark",
                "binance_xaut_spot_mid", "binance_xaut_perp_mark", "mid_3030_hkd",
                "spread_3030_bps", "fx_usdhkd", "implied_3030_usd_oz", "chainlink_xauusd__age"]:
        if col in w.columns and pd.notna(last.get(col)):
            latest[col] = round(float(last[col]), 4)
    for pre in ("d_", "b_"):
        for col, _l, _c in SERIES + D_EXTRA + B_EXTRA:
            key = pre + col
            if key in w.columns and pd.notna(last.get(key)):
                latest[key] = round(float(last[key]), 2)
    d["latest"] = latest

    stats = {}
    for pre in ("d", "b"):
        stats[pre] = {}
        for col, lab, _c in SERIES + (D_EXTRA if pre == "d" else B_EXTRA):
            cn = f"{pre}_{col}"
            if cn in w.columns:
                s = w[cn].dropna()
                if len(s) >= 2:
                    stats[pre][col] = {"label": lab, "n": int(len(s)),
                                       "mean": round(float(s.mean()), 1),
                                       "std": round(float(s.std()), 1),
                                       "min": round(float(s.min()), 1),
                                       "max": round(float(s.max()), 1),
                                       "absmax": round(float(s.abs().max()), 1)}
    d["stats"] = stats

    a = "chainlink_xauusd__age"
    if a in w.columns and w[a].notna().sum() > 1:
        age = w[a].dropna() / 60
        d["cl_stale"] = {"mean_min": round(float(age.mean()), 1),
                         "p95_min": round(float(age.quantile(.95)), 1),
                         "max_min": round(float(age.max()), 1)}
    if len(ticks):
        r = (ticks.ask_vol / ticks.bid_vol).replace([np.inf, -np.inf], np.nan).dropna()
        d["ticks"] = {"n": int(len(ticks)),
                      "depth_ratio_median": round(float(r.median()), 2) if len(r) else None}
    return d


def write(obj: dict, name: str) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = os.path.join(OUT_DIR, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    dst = os.path.join(OUT_DIR, name)
    os.replace(tmp, dst)          # 原子替换，页面不会读到半个文件
    return os.path.getsize(dst)


_K: float | None = None      # 含金量，由长窗口统一校准后供短窗口复用


def run_long() -> str:
    """全窗口：重算 k 并导出降采样长周期文件。开销大，低频调用。"""
    global _K
    since = (pd.Timestamp.now("UTC") - pd.Timedelta(days=LONG_D)).strftime("%Y-%m-%d")
    snap, w, ticks = A.load(A.DB_PATH, since)
    if snap.empty or w.empty:
        return "no data"
    w, k = A.derive(w)
    _K = k
    lw = w.resample(LONG_RULE).last().dropna(how="all")
    n = write(build(lw, k, ticks, False), "data_long.json")
    return f"data_long {len(lw)}pt/{n//1024}KB k={k:.6e}" if k else f"data_long {len(lw)}pt/{n//1024}KB"


def run_recent() -> str:
    """短窗口：只读最近数据，复用长窗口的 k。高频调用。"""
    since = (pd.Timestamp.now("UTC") - pd.Timedelta(hours=RECENT_H + 1)).strftime("%Y-%m-%d %H:%M:%S")
    snap, w, ticks = A.load(A.DB_PATH, since)
    if snap.empty or w.empty:
        return "no data"
    w, _ = A.derive(w, k_fixed=_K)
    cut = w.index.max() - pd.Timedelta(hours=RECENT_H)
    recent = w[w.index >= cut]
    if not len(recent):
        recent = w
    n = write(build(recent, _K, ticks, True), "data.json")
    return f"data {len(recent)}pt/{n//1024}KB"


def run_latest() -> str:
    """只写最新快照（约 1KB）。卡片每 15 秒要它，图表序列则不必这么频繁重传：
    完整 data.json 约 18KB(gzip)，每 15 秒一次相当于 103MB/天/页面。"""
    since = (pd.Timestamp.now("UTC") - pd.Timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    snap, w, ticks = A.load(A.DB_PATH, since)
    if snap.empty or w.empty:
        return "no data"
    w, _ = A.derive(w, k_fixed=_K)
    full = build(w.tail(2), _K, ticks, True)
    out = {"updated": full["updated"], "latest": full["latest"], "k": _K}
    n = write(out, "latest.json")
    return f"latest {n}B"


def run_once() -> str:
    return run_long() + "  |  " + run_recent() + "  |  " + run_latest()


def main() -> None:
    daemon = "--daemon" in sys.argv
    every = 15
    for a in sys.argv:
        if a.startswith("--every="):
            every = int(a.split("=")[1])
    if not daemon:
        print(run_once()); return
    print(f"exporter daemon started, every={every}s -> {OUT_DIR}", flush=True)
    last = {"long": 0.0, "recent": 0.0}
    fails = {"long": 0, "recent": 0}
    while True:
        t0 = time.time()
        parts = []
        # 每档独立 try 且无论成败都推进时钟：否则长窗口一旦持续失败，
        # 会把短窗口和最新快照一起拖死，页面冻结而服务仍显示 active。
        for key, period, fn in (("long", 300, run_long), ("recent", 60, run_recent)):
            if t0 - last[key] < period + fails[key] * 60:   # 失败后线性退避
                continue
            try:
                parts.append(fn())
                fails[key] = 0
            except Exception as e:
                fails[key] = min(fails[key] + 1, 5)
                parts.append(f"{key} ERROR({fails[key]}) {type(e).__name__}: {str(e)[:120]}")
            finally:
                last[key] = t0
        try:
            parts.append(run_latest())         # 最新快照：每轮都写，卡片保持 15 秒实时
        except Exception as e:
            parts.append(f"latest ERROR {type(e).__name__}: {str(e)[:120]}")
        print(f"{time.strftime('%H:%M:%S', time.gmtime())}Z {'  |  '.join(parts)} "
              f"({time.time()-t0:.1f}s)", flush=True)
        time.sleep(max(1.0, every - (time.time() - t0)))


if __name__ == "__main__":
    main()

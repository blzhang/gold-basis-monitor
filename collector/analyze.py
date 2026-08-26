"""分析器：只读库，可对任意区间重跑。

口径
  基准源 = Pyth XAU/USD（更新最快、是现货金参考而非代币）
  偏离   d_i = 1e4 * (P_i / P_pyth - 1)   单位 bps

  3030 换算：implied_usd_oz = mid_HKD / fx_hkd_per_usd / k   (k = oz/份)
  k 用"校准口径"：取样本期比值中位数反解，抹掉绝对水平差、只留日内相对偏离。
  这会一并抹掉 3030 的系统性溢价/折价 —— 要测绝对基差需接官方每日 NAV。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("GOLDMON_DB", os.path.join(_REPO, "data", "goldmon.db"))
BASE = "pyth_xauusd"
HKT = "Asia/Hong_Kong"

# 参与横向对比的源
CRYPTO = ["chainlink_xauusd", "binance_paxg_spot_mid", "binance_paxg_perp_mark",
          "binance_paxg_perp_index", "binance_xaut_spot_mid", "binance_xaut_perp_mark"]


def load(db: str, since: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not os.path.exists(db):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    conn = sqlite3.connect(db)
    if not conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='snapshots'"
    ).fetchone()[0]:
        conn.close()
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    q = "SELECT slot_ts, source, value, age_sec, source_ts, meta, error FROM snapshots"
    params: list = []
    if since:
        q += " WHERE slot_ts >= ?"
        params.append(int(pd.Timestamp(since, tz="UTC").timestamp()))
    snap = pd.read_sql_query(q, conn, params=params)
    ticks = pd.read_sql_query("SELECT ts, bid, ask, bid_vol, ask_vol FROM book_ticks", conn)
    conn.close()
    if snap.empty:
        return snap, pd.DataFrame(), ticks

    ok = snap[snap.error.isna() & snap.value.notna()]
    wide = ok.pivot_table(index="slot_ts", columns="source", values="value", aggfunc="last")
    age = ok.pivot_table(index="slot_ts", columns="source", values="age_sec", aggfunc="last")
    sts = ok.pivot_table(index="slot_ts", columns="source", values="source_ts", aggfunc="last")
    wide.index = pd.to_datetime(wide.index, unit="s", utc=True).tz_convert(HKT)
    age.index = sts.index = wide.index
    return snap, pd.concat([wide, age.add_suffix("__age"), sts.add_suffix("__srcts")], axis=1), ticks


def derive(w: pd.DataFrame, k_fixed: float | None = None) -> tuple[pd.DataFrame, float | None]:
    """派生中间价、3030 隐含金价、各源 bps 偏离。

    k_fixed: 外部传入含金量。分频导出时必须传，否则短窗口与长窗口
    会各自校准出不同的 k，同一条曲线在切换时间范围时会跳变。
    """
    # 分层采样下慢变量是稀疏的，前向填充。对 FX(日频源) 与 Chainlink(链上值确实不变)
    # 这是正确语义，不是掩盖缺失。
    for c in ("fx_usdhkd", "chainlink_xauusd",
              "chainlink_xauusd__srcts", "fx_usdhkd__srcts"):
        if c in w.columns:
            w[c] = w[c].ffill()

    # Chainlink 陈旧度必须用链上 updatedAt 相对当前 slot 重算：
    # 填充后若沿用采集时刻的 age_sec，陈旧度会被压成常数，锯齿就消失了。
    if "chainlink_xauusd__srcts" in w.columns:
        # 不用 astype("int64")：pandas 3 的索引精度是 datetime64[s]，
        # 除以 1e9 会把秒当成纳秒。用 .timestamp() 与精度无关。
        now_s = pd.Series(w.index.map(pd.Timestamp.timestamp), index=w.index)
        # clip(0)：slot_ts 是对齐值，链上更新可能落在 slot 之后导致负值，陈旧度无负数语义
        w["chainlink_xauusd__age"] = (now_s - w["chainlink_xauusd__srcts"]).clip(lower=0)

    def mid(a: str, b: str, out: str):
        if a in w.columns and b in w.columns:
            w[out] = (w[a] + w[b]) / 2

    mid("binance_paxg_spot_bid", "binance_paxg_spot_ask", "binance_paxg_spot_mid")
    mid("binance_xaut_spot_bid", "binance_xaut_spot_ask", "binance_xaut_spot_mid")
    mid("futu_3030_bid", "futu_3030_ask", "mid_3030_hkd")

    if {"futu_3030_bid", "futu_3030_ask"} <= set(w.columns):
        w["spread_3030_bps"] = (w.futu_3030_ask - w.futu_3030_bid) / w.mid_3030_hkd * 1e4

    k = k_fixed
    if {"mid_3030_hkd", "fx_usdhkd", BASE} <= set(w.columns):
        if k is None:
            ratio = w.mid_3030_hkd / w.fx_usdhkd / w[BASE]
            ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
            if len(ratio):
                k = float(ratio.median())
        if k:
            w["implied_3030_usd_oz"] = w.mid_3030_hkd / w.fx_usdhkd / k

    if BASE in w.columns:
        for c in CRYPTO + ["implied_3030_usd_oz"]:
            if c in w.columns:
                w[f"d_{c}"] = (w[c] / w[BASE] - 1) * 1e4

    # 以 3030 为基准 —— 项目底层资产就是它，用 perp 对冲时的真实净敞口是 b_*，不是 d_*。
    # k 的校准只改变 b_* 的水平，不影响其波动率，而波动率才是对冲误差。
    if "implied_3030_usd_oz" in w.columns:
        for c in CRYPTO + [BASE]:
            if c in w.columns:
                w[f"b_{c}"] = (w[c] / w["implied_3030_usd_oz"] - 1) * 1e4
    return w, k


def hk_session(idx: pd.DatetimeIndex) -> pd.Series:
    t = idx.tz_convert(HKT)
    mins = t.hour * 60 + t.minute
    weekday = t.dayofweek < 5
    morning = (mins >= 570) & (mins < 720)    # 09:30-12:00
    afternoon = (mins >= 780) & (mins < 960)  # 13:00-16:00
    return pd.Series(weekday & (morning | afternoon), index=idx)


def fmt_stats(s: pd.Series) -> str:
    s = s.dropna()
    if len(s) < 2:
        return f"n={len(s):<4} (样本不足)"
    return (f"n={len(s):<4} mean={s.mean():+7.1f} σ={s.std():6.1f} "
            f"p5={s.quantile(.05):+7.1f} p50={s.median():+7.1f} p95={s.quantile(.95):+7.1f} "
            f"|max|={s.abs().max():6.1f}")


def report(db: str, since: str | None) -> None:
    snap, w, ticks = load(db, since)
    if snap.empty:
        print(f"暂无数据：{db}")
        print("先运行 collector/collect.py 采集一段时间。")
        return
    w, k = derive(w)

    span = f"{w.index.min():%Y-%m-%d %H:%M} → {w.index.max():%Y-%m-%d %H:%M} HKT"
    print("=" * 78)
    print(f"黄金基差监控报告   样本 {len(w)} 个采样点   {span}")
    print("=" * 78)

    err = snap[snap.error.notna()]
    if len(err):
        print(f"\n【采集健康度】失败 {len(err)} 条 / 共 {len(snap)} 条 "
              f"({len(err)/len(snap)*100:.1f}%)")
        for src, c in err.source.value_counts().head(6).items():
            print(f"    {src:30s} {c}")
    else:
        print(f"\n【采集健康度】{len(snap)} 条记录，零失败")

    # ---- 1. 各源相对 Pyth 的偏离
    print(f"\n【1. 各源 vs Pyth 偏离 (bps)】基准={BASE}")
    for c in CRYPTO + ["implied_3030_usd_oz"]:
        col = f"d_{c}"
        if col in w.columns:
            print(f"  {c:28s} {fmt_stats(w[col])}")
    if k:
        print(f"\n  3030 校准含金量 k = {k:.6e} oz/份  (= 1/{1/k:,.0f})")

    # ---- 2. Chainlink 陈旧度：套利打击面
    a = "chainlink_xauusd__age"
    if a in w.columns and w[a].notna().sum() > 1:
        age = w[a].dropna() / 60
        print(f"\n【2. Chainlink 陈旧度】—— 锚它做报价的套利打击面")
        print(f"  age(分钟): mean={age.mean():.1f} p50={age.median():.1f} "
              f"p95={age.quantile(.95):.1f} max={age.max():.1f}")
        d = w.get("d_chainlink_xauusd")
        if d is not None and d.notna().sum() > 2:
            fresh = d[w[a] < 300].abs().dropna()
            stale = d[w[a] > 1800].abs().dropna()
            for lab, ser in (("刚更新(<5min) ", fresh), ("陈旧(>30min)  ", stale)):
                if len(ser):
                    print(f"  {lab}  |偏离| 均值 {ser.mean():6.1f} bps  n={len(ser)}")
                else:
                    print(f"  {lab}  无样本")
            if len(stale) and len(fresh):
                print(f"  → 陈旧带来的额外偏离约 {stale.mean()-fresh.mean():+.1f} bps")

    # ---- 3. 代币间基差（健康度）
    if {"d_binance_paxg_spot_mid", "d_binance_xaut_spot_mid"} <= set(w.columns):
        diff = w.d_binance_paxg_spot_mid - w.d_binance_xaut_spot_mid
        print(f"\n【3. PAXG − XAUT 代币间基差 (bps)】赎回压力/流动性健康度")
        print(f"  {fmt_stats(diff)}")

    # ---- 4. 3030 盘口
    if "spread_3030_bps" in w.columns:
        sp = w.spread_3030_bps.dropna()
        if len(sp):
            print(f"\n【4. 3030 二级盘口】")
            print(f"  spread(bps): {fmt_stats(sp)}")
            print(f"  → 半宽 {sp.median()/2:.1f} bps；偏离小于此值的部分本质不可交易")
        for side in ("bid", "ask"):
            m = f"futu_3030_{side}"
            if m in w.columns:
                pass
    if len(ticks):
        tk = ticks.copy()
        tk["ts"] = pd.to_datetime(tk.ts, unit="s", utc=True).dt.tz_convert(HKT)
        per_day = tk.groupby(tk.ts.dt.date).size()
        print(f"  盘口变动 tick: 共 {len(tk)} 笔"
              + (f"，日均 {per_day.mean():.0f} 笔" if len(per_day) else ""))
        if {"bid_vol", "ask_vol"} <= set(tk.columns):
            r = (tk.ask_vol / tk.bid_vol).replace([np.inf, -np.inf], np.nan).dropna()
            if len(r):
                print(f"  卖/买深度比中位数 {r.median():.2f}  (>1 表示卖盘更厚)")

    # ---- 5. 时段效应
    if "d_implied_3030_usd_oz" in w.columns:
        d = w.d_implied_3030_usd_oz.dropna()
        if len(d) > 4:
            print(f"\n【5. 3030 隐含金价偏离的时段分布 (bps)】")
            hk = w.loc[d.index]
            mins = d.index.hour * 60 + d.index.minute
            buckets = {
                "开盘 09:30-10:00": (mins >= 570) & (mins < 600),
                "上午 10:00-12:00": (mins >= 600) & (mins < 720),
                "午后 13:00-15:00": (mins >= 780) & (mins < 900),
                "伦敦开 15:00-16:00": (mins >= 900) & (mins < 960),
            }
            for label, mask in buckets.items():
                sub = d[mask]
                if len(sub):
                    print(f"  {label:22s} {fmt_stats(sub)}")

    # ---- 6. 港股时段 vs 全天（加密侧）
    if "d_binance_paxg_perp_mark" in w.columns:
        d = w.d_binance_paxg_perp_mark.dropna()
        if len(d) > 4:
            insess = hk_session(d.index)
            print(f"\n【6. PAXG perp 偏离：港股时段 vs 其余时间 (bps)】")
            print(f"  港股盘中   {fmt_stats(d[insess.values])}")
            print(f"  非港股时段 {fmt_stats(d[~insess.values])}")

    print("\n" + "=" * 78)
    n = len(w)
    if n < 72:
        print(f"注意：当前仅 {n} 个点。72 点(6小时)只够看形态，σ 估计误差约 ±8%；")
        print("     做正式统计建议连跑 5-10 个交易日。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--since", default=None, help="起始时间，如 2026-08-26")
    a = ap.parse_args()
    report(a.db, a.since)

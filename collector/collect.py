"""采集器：5 分钟对齐快照 + 3030 盘口变动旁录。

设计要点
  - 只写库，不做换算（口径参数会反复调，不能让它污染原始数据）
  - 任一数据源失败绝不中断整轮：记 error 入库，缺格好过停机
  - slot_ts（对齐格）与 ts（实际采集时刻）分开存，才能事后审计对齐误差
  - 加密侧 24×7；3030 休市时段照采，market_state 一并记录，由分析层决定取舍
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sources as S

DB_PATH = os.environ.get("GOLDMON_DB", "/opt/goldmon/data/goldmon.db")
SLOT_SEC = 15           # 主采样周期
ROUND_BUDGET = 12       # 单轮采集超时预算（秒）
RETAIN_DAYS = 90        # 数据保留天数：15 秒采样每天约 7.5 万行，无上限会持续吃盘
CODE = "HK.03030"

# 分层采样：瓶颈不是限频（Binance 15s 采样仅用限额的 0.13%），
# 而是"采更密没有信息增益"——Chainlink 是小时级 heartbeat，FX 是日频源。
# every = 每隔几轮采一次
TIERS = [
    (S.fetch_pyth,          1),   # 15s  基准源，要跟得最紧
    (S.fetch_goldapi,       1),   # 15s  基准备用源（Pyth 端点可能 401）
    (S.fetch_binance_spot,  1),   # 15s
    (S.fetch_binance_perp,  1),   # 15s
    (S.fetch_chainlink,     4),   # 60s  小时级更新，够定位它何时跳
    (S.fetch_fx,           60),   # 900s 日频源
]
MARKET_STATE_EVERY = 4

_stop = threading.Event()
_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z {msg}", flush=True)


# ------------------------------------------------------------------ DB

def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")     # 允许 push 线程与主线程并发写
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_ts   INTEGER NOT NULL,   -- 对齐后的 5 分钟格 (unix, UTC)
        ts        REAL    NOT NULL,   -- 实际采集时刻
        source    TEXT    NOT NULL,
        value     REAL,
        ccy       TEXT,
        source_ts REAL,               -- 数据源自身时间戳
        age_sec   REAL,               -- ts - source_ts，新鲜度；分析必须按它分层
        meta      TEXT,
        error     TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_snap_slot   ON snapshots(slot_ts);
    CREATE INDEX IF NOT EXISTS ix_snap_source ON snapshots(source, slot_ts);

    CREATE TABLE IF NOT EXISTS book_ticks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         REAL NOT NULL,
        code       TEXT NOT NULL,
        bid        REAL, ask        REAL,
        bid_vol    REAL, ask_vol    REAL,
        bid_orders INTEGER, ask_orders INTEGER
    );
    CREATE INDEX IF NOT EXISTS ix_tick_ts ON book_ticks(ts);
    """)
    conn.commit()


def write_readings(conn: sqlite3.Connection, slot_ts: int, readings: list[S.Reading]) -> None:
    rows = []
    for r in readings:
        ts = time.time()
        age = (ts - r.source_ts) if r.source_ts else None
        rows.append((slot_ts, ts, r.source, r.value, r.ccy, r.source_ts, age,
                     json.dumps(r.meta, ensure_ascii=False) if r.meta else None, r.error))
    conn.executemany(
        "INSERT INTO snapshots(slot_ts,ts,source,value,ccy,source_ts,age_sec,meta,error)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def prune(conn: sqlite3.Connection) -> None:
    """删除超出保留期的数据。每天调用一次即可。"""
    cut = time.time() - RETAIN_DAYS * 86400
    a = conn.execute("DELETE FROM snapshots WHERE slot_ts < ?", (int(cut),)).rowcount
    b = conn.execute("DELETE FROM book_ticks WHERE ts < ?", (cut,)).rowcount
    conn.commit()
    if a or b:
        log(f"[prune] 清理 {a} 条 snapshots, {b} 条 book_ticks (保留 {RETAIN_DAYS} 天)")


# ------------------------------------------------------- 3030 盘口旁录

class BookRecorder:
    """富途 push 回调：盘口每次真实变动就记一笔。

    价值在于给每个 5 分钟数据点标上"新鲜度"——薄盘下 3030 可能几十分钟
    不动，不知道这点就无法区分"真的没偏离"和"报价根本没更新"。
    """

    def __init__(self, db_path: str):
        self.conn = connect(db_path)
        self.lock = threading.Lock()
        self.count = 0
        self.last: tuple | None = None

    def handle(self, data: dict) -> None:
        try:
            bids, asks = data.get("Bid") or [], data.get("Ask") or []
            if not bids or not asks:
                return
            row = (float(bids[0][0]), float(asks[0][0]),
                   float(bids[0][1]), float(asks[0][1]),
                   int(bids[0][2]), int(asks[0][2]))
            if row == self.last:      # 去重：只记真正变动
                return
            self.last = row
            with self.lock:
                self.conn.execute(
                    "INSERT INTO book_ticks(ts,code,bid,ask,bid_vol,ask_vol,bid_orders,ask_orders)"
                    " VALUES(?,?,?,?,?,?,?,?)", (time.time(), CODE) + row)
                self.conn.commit()
                self.count += 1
        except Exception as e:
            log(f"[book] handler error: {type(e).__name__}: {e}")


def make_futu_ctx(recorder: BookRecorder, timeout: float = 8.0):
    """建立富途连接并挂上盘口推送。失败或超时返回 None，绝不阻塞其余数据源。

    OpenQuoteContext 的构造是同步阻塞的：OpenD 进程在但不响应时它可以挂很久，
    会把整个主循环连同 24×7 的加密源一起拖停。故放进守护线程并设超时。
    """
    box: dict = {}

    def _build():
        try:
            box["ctx"] = _connect_futu(recorder)
        except Exception as e:
            box["err"] = e

    th = threading.Thread(target=_build, daemon=True, name="futu-connect")
    th.start()
    th.join(timeout)
    if th.is_alive():
        log(f"[futu] connect timed out after {timeout}s, continuing without 3030")
        return None
    if "err" in box:
        log(f"[futu] connect failed: {type(box['err']).__name__}: {box['err']}")
        return None
    return box.get("ctx")


def _connect_futu(recorder: BookRecorder):
    try:
        from futu import OpenQuoteContext, OrderBookHandlerBase, SubType, RET_OK

        class Handler(OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    recorder.handle(data)
                return RET_OK, data

        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ctx.set_handler(Handler())
        ret, msg = ctx.subscribe([CODE], [SubType.ORDER_BOOK], subscribe_push=True)
        if ret != RET_OK:
            log(f"[futu] subscribe failed: {msg}")
            ctx.close()
            return None
        log("[futu] connected, ORDER_BOOK push subscribed")
        return ctx
    except Exception as e:
        log(f"[futu] connect failed: {type(e).__name__}: {e}")
        return None


def fetch_market_state(ctx) -> S.Reading:
    """港股市场状态，供分析层区分盘中/休市。"""
    try:
        from futu import RET_OK
        ret, st = ctx.get_global_state()
        if ret != RET_OK:
            return S.Reading(source="futu_market_state", error=str(st)[:150])
        hk = st.get("market_hk")
        return S.Reading(source="futu_market_state", value=None, ccy="STATE",
                         meta={"market_hk": str(hk)})
    except Exception as e:
        return S.Reading(source="futu_market_state", error=f"{type(e).__name__}: {e}")


# ------------------------------------------------------------- 主循环

def collect_round(conn, futu_ctx) -> tuple[int, list[S.Reading]]:
    """采一轮。所有 HTTP 源并发，单源失败不影响其他源。

    轮次号由 slot_ts 推导而非内部计数器，服务重启后节奏不乱，
    Chainlink 始终落在整 60 秒边界上。
    """
    slot_ts = int(time.time() // SLOT_SEC * SLOT_SEC)
    rnd = slot_ts // SLOT_SEC
    due = [f for f, every in TIERS if rnd % every == 0]
    readings: list[S.Reading] = []

    pool = ThreadPoolExecutor(max_workers=len(due))
    try:
        futs = {pool.submit(f): f.__name__ for f in due}
        deadline = time.time() + ROUND_BUDGET
        for fut, name in futs.items():
            try:
                # 逐个取并各自计时：超时的源记 error，其余读数照常入库
                readings.extend(fut.result(timeout=max(0.05, deadline - time.time())))
            except Exception as e:
                readings.append(S.Reading(
                    source=name, error=f"timeout/{type(e).__name__}: {str(e)[:120]}"))
    finally:
        # wait=False：不等慢线程，否则单个卡死的源会把整轮拖过预算
        pool.shutdown(wait=False, cancel_futures=True)

    if futu_ctx is not None:
        readings.extend(S.fetch_futu_3030(futu_ctx))   # 本地网关，每轮都采
        if rnd % MARKET_STATE_EVERY == 0:
            readings.append(fetch_market_state(futu_ctx))
    else:
        readings.append(S.Reading(source="futu_3030_bid", error="no_futu_connection"))
        readings.append(S.Reading(source="futu_3030_ask", error="no_futu_connection"))

    write_readings(conn, slot_ts, readings)
    return slot_ts, readings


def summarize(readings: list[S.Reading]) -> str:
    ok = [r for r in readings if r.error is None and r.value is not None]
    bad = [r for r in readings if r.error]
    pyth = next((r.value for r in ok if r.source == "pyth_xauusd"), None)
    bits = [f"ok={len(ok)} err={len(bad)}"]
    if pyth:
        bits.append(f"pyth={pyth:.2f}")
        for src, label in (("chainlink_xauusd", "cl"), ("binance_paxg_perp_mark", "paxg")):
            v = next((r.value for r in ok if r.source == src), None)
            if v:
                bits.append(f"{label}={(v / pyth - 1) * 1e4:+.1f}bps")
    b = next((r.value for r in ok if r.source == "futu_3030_bid"), None)
    a = next((r.value for r in ok if r.source == "futu_3030_ask"), None)
    if b and a:
        bits.append(f"3030mid={(a + b) / 2:.4f}")
    if bad:
        bits.append("failed=" + ",".join(sorted({r.source for r in bad}))[:80])
    return "  ".join(bits)


def main() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())

    conn = connect()
    init_db(conn)
    recorder = BookRecorder(DB_PATH)
    futu_ctx = make_futu_ctx(recorder)
    last_prune = time.time()      # 启动时不清理，避免重启风暴反复扫全表
    log(f"collector started, db={DB_PATH}, slot={SLOT_SEC}s, retain={RETAIN_DAYS}d")

    try:
        while not _stop.is_set():
            t0 = time.time()
            try:
                slot_ts, readings = collect_round(conn, futu_ctx)
                log(f"slot={time.strftime('%H:%M:%S', time.gmtime(slot_ts))}Z "
                    f"({time.time() - t0:.1f}s)  {summarize(readings)}  ticks={recorder.count}")
            except Exception as e:
                log(f"round FAILED: {type(e).__name__}: {e}")

            # 富途断线则下一轮重连，期间其余数据源照常
            if futu_ctx is None:
                futu_ctx = make_futu_ctx(recorder)

            if time.time() - last_prune > 86400:
                try:
                    prune(conn)
                except Exception as e:
                    log(f"[prune] failed: {type(e).__name__}: {e}")
                last_prune = time.time()

            nxt = (time.time() // SLOT_SEC + 1) * SLOT_SEC
            while not _stop.is_set() and time.time() < nxt:
                _stop.wait(min(2.0, nxt - time.time()))
    finally:
        log("stopping...")
        if futu_ctx is not None:
            try:
                futu_ctx.close()
            except Exception:
                pass
        conn.close()
        log("stopped")


if __name__ == "__main__":
    main()

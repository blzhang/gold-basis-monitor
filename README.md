# gold-basis-monitor

> Continuously measures the price gap between a physically-backed gold ETF (3030.HK)
> and mainstream crypto gold price sources (Pyth, Chainlink, PAXG, XAUT) at 15-second
> resolution — built to answer which oracle a gold-RWA token should anchor to, and how
> large the intraday hedging basis actually is.

把港股实物黄金 ETF **3030.HK** 的盘口，与四个主流加密金价源做 15 秒级连续比对，
落库、统计、可视化。用来回答三个问题：

1. **预言机选型** —— 代币化黄金的链上报价该锚哪个源？
2. **日内对冲基差** —— 用 PAXG/XAUT 永续对冲 ETF 底仓时，瞬时敞口有多大？
3. **做市带标定** —— 二级盘的报价带会不会被偏离击穿？

## 为什么需要它

如果你的底层资产是一只**二级盘极薄**的 ETF，常规做法会在两个地方出错：

**一、用成交价会得到垃圾数据。** 3030.HK 一天 330 个交易分钟里，**90% 以上没有成交**；
最小变动 0.005 HKD @ 7.0 ≈ **7.1 bps**，价格分辨率本身就和要观测的价差同量级。
按 5 分钟切格子、前值填充算出来的"基差"，主体是没成交造成的滞后，不是真实定价偏离。
所以这里一律取**盘口中价**，并旁录盘口每次真实变动。

**二、拿现货金价当基准会算错敞口。** 底层是 3030，那么用永续对冲时的真实净敞口是
`perp vs 3030`，不是 `perp vs XAU/USD`。本项目同时输出两套口径。

## 已观测到的结果

一个小时窗口的实测（bps，相对 Pyth XAU/USD）：

| 源 | 均值 | σ | 说明 |
|---|---|---|---|
| Chainlink XAU/USD | +8.7 | **6.2** | 波动几乎全部来自自身不更新 |
| PAXG perp (mark) | −19.5 | 0.8 | 折价深但**极稳定** |
| XAUT perp (mark) | −37.2 | 0.8 | 折价更深，同样稳定 |
| 3030.HK 隐含金价 | −0.1 | 1.6 | |

**Chainlink 是 deviation 驱动，不是 heartbeat 驱动。** 实测三次链上更新：

| 链上更新时刻 | 价格 | 距上次 | 价格跳幅 |
|---|---|---|---|
| 10:41:59 | 4666.54 | — | — |
| 11:07:59 | 4651.26 | 26.0 分 | −32.7 bps |
| 13:45:23 | 4637.18 | **157.4 分** | −30.3 bps |

两次更新的跳幅几乎相同（30.3 / 32.7 bps），间隔却差 6 倍——触发它的是
**deviation 阈值约 0.3%**，而非固定心跳。金价走得快就早更新，走得慢就一直拖。

这个区别对选型很重要：**陈旧偏离不是无界的，上限就是 deviation 阈值本身（约 30 bps）。**
锚 Chainlink 的套利打击面因此是一个**有界**风险，而不是"越久越危险"。
（3 次更新样本仍少，方向性结论需更多数据确认。）

结论：**稳定的偏移可以用常数修正掉，锯齿状的陈旧偏差不行。**
一个 σ=0.8bps 的深折价源，比一个 σ=6.2bps 的"准确"源更适合做锚。

## 架构

```
collector/sources.py      数据源适配层，每源一函数，只取原始值
collector/collect.py      分层采样主循环 + 盘口 push 旁录 → SQLite
collector/analyze.py      只读库出统计，可对任意历史区间重跑
collector/export_web.py   导出网页数据（复用 analyze 的口径计算）
web/index.html            零依赖单页看板（原生 SVG，不走 CDN）
```

**核心约束：采集层不做任何单位换算。** 原始值、原始币种、源时间戳原样入库，
口径换算（含金量 k、FX、基准源）全部留在分析层。因为这些参数一定会反复调整，
"调一次口径就得重采数据"的设计不可接受。

## 数据源

| 源 | 通道 | 采样周期 | 备注 |
|---|---|---|---|
| 3030.HK bid/ask/深度 | Futu OpenD 本地网关 | 15 秒 + tick 旁录 | 需富途账号与港股行情权限 |
| Pyth XAU/USD | Hermes REST | 15 秒 | 免 key，带 conf 置信区间 |
| Chainlink XAU/USD | 3× 公共 ETH RPC failover | 60 秒 | 免 key，**公共 RPC 必须带 User-Agent，否则 403** |
| PAXG 现货 / 永续 mark·index·funding | Binance | 15 秒 | 免 key |
| XAUT 现货 / 永续 | Binance | 15 秒 | 免 key |
| USD/HKD | frankfurter.app | 15 分钟 | 免 key，**日频源** |

**分层采样的依据不是限频，是信息增益。** 实测 15 秒采样时 Binance 现货
`x-mbx-used-weight-1m` 仅 18/6000（**限额的 0.13%**），余量极大；
真正的理由是 Chainlink 本身就是小时级 heartbeat、FX 本身就是日频，采更密只是浪费。

## 两套基准口径

| 口径 | 列前缀 | 含义 |
|---|---|---|
| **相对 3030**（看板默认） | `b_` | 底层资产视角。用永续对冲的真实净敞口 |
| 相对 Pyth | `d_` | 现货金市场视角，看谁跟得最紧 |

3030 隐含金价 = `mid_HKD / FX / k`，`k` 为每份含金量（oz/share），由长窗口比值中位数校准反解。

> 该校准会**抹掉系统性溢价/折价**，只保留相对偏离；但**不影响波动率**，
> 而波动率才是对冲误差。要测绝对基差需接入基金官方每日 NAV。

### 3030 基准下的量化台阶

用 3030 做分母时，它自身的价格量化会传导到**每一条**曲线：

```
b_i = 1e4 × (源价 / 3030隐含金价 − 1)
                      ↑ 分母每跳一档，所有曲线同步跳一档
```

3030 最小变动 0.005 HKD，mid 取买卖中价后粒度减半 ≈ **3.6 bps**。
Chainlink 在两次链上更新之间价格是常数，此时它的曲线起伏**完全是 3030 的倒影**——
实测某 32 分钟窗口内 Chainlink 链上价唯一值只有 1 个（4651.26），
而 `b_chainlink` 却在 7.7~25.5 bps 之间走出 7 级台阶，台阶间距实测 3.56/3.57/3.58 bps。

同一时段两种基准的抖动对比（相邻点变化绝对值均值，bps）：

| 源 | 相对 3030 | 相对 Pyth |
|---|---|---|
| Chainlink | 1.23 | 0.99 |
| PAXG perp | **1.54** | 0.50 |
| XAUT perp | **1.44** | 0.51 |

两个口径都真实但含义不同：`b_` 是持有 3030 底仓时的**账面敞口**（台阶是底仓账面价值的真实跳动，
但不可交易——你无法在 tick 之间成交）；`d_` 才是各源自身相对现货金的**经济偏离**。
报告统计量时需要说明用的是哪个口径。

## 交易时段过滤（重要）

非交易时段（午休 12:00–13:00、收市、节假日）的 3030 盘口是**僵尸报价**。实测：

| | n | spread 中位 | mid 唯一值 | PAXG 基差 σ |
|---|---|---|---|---|
| 交易时段 | 308 | 28.5 bps | 9 | **3.07** |
| 午休 | 240 | 49.9 bps | **2** | 10.6 |

240 个采样点里 mid 只有 2 个不同值，spread 却宽到 49.9bps。不过滤会把基差 σ
从 3.07 夸大到 9.14（**+198%**）——而基差波动率正是对冲误差的核心指标。

`analyze.derive()` 用富途 `market_state`（`MORNING`/`AFTERNOON`/`REST`/`CLOSED`）过滤，
能自动覆盖节假日与临时休市，状态缺失时回退到 HKT 时段判断。

> 坑：`market_state` 的 `value` 为 NULL（状态存在 `meta` 里），不会通过
> `value.notna()` 的过滤，必须在 `load()` 里单独解析——否则这个过滤永远不会生效。

## 数据模型

```sql
snapshots(slot_ts, ts, source, value, ccy, source_ts, age_sec, meta, error)
book_ticks(ts, code, bid, ask, bid_vol, ask_vol, bid_orders, ask_orders)
```

设计要点：

- **长表而非宽表** —— 加一个源不改 schema，缺一个源不产生空列
- **`slot_ts`（对齐格）与 `ts`（实际采集时刻）分开存** —— 才能事后审计对齐误差
- **`age_sec` 是关键字段** —— 不带新鲜度标注的偏离数字无法解释。
  Chainlink 的 `age` 长期是几千秒，这不是故障，正是被测量的对象
- **任一源失败绝不中断整轮** —— 记 `error` 入库，缺格好过停机

## 快速开始

只跑加密侧（不需要富途账号，四个源全部免 key）：

```bash
pip install pandas numpy
python collector/collect.py          # Ctrl-C 停止；数据落到 data/goldmon.db
python collector/analyze.py          # 出统计报告
```

加上 3030.HK 需要富途 OpenD：安装并登录 OpenD（见 `deploy/FutuOpenD.xml.example`），
确认 `127.0.0.1:11111` 可连，然后 `pip install futu-api`。采集器会自动接入；
连不上时该源记 `error`，其余源照常采集。

生成看板：

```bash
python collector/export_web.py       # 生成 web/data.json 与 web/data_long.json
cd web && python -m http.server 8080 # 打开 http://localhost:8080
```

## 部署

`deploy/` 下是 systemd 单元与 nginx 片段示例（凭证位置均为占位符）：

```
goldmon-opend.service       富途行情网关，-login_by_remember 免密启动
goldmon-collector.service   15 秒分层采集
goldmon-export.service      看板导出 daemon（常驻，避免重复 import pandas）
```

环境变量：`GOLDMON_DB` 指定数据库路径，`GOLDMON_WEB` 指定看板输出目录。

**OpenD 凭证不落配置文件**：用 `-console=1 -remember=1` 手工登录一次
（首次新设备会要手机验证码，用 `input_phone_verify_code -code=XXXXXX` 输入），
之后 systemd 永久使用 `-login_by_remember=1`。

## 设计决策与踩过的坑

**X 轴必须按真实时间映射，不能按索引。** 采样频率变化（5 分钟改 15 秒）或存在采集缺口时，
索引等距会把时间轴压扭，图上会出现 `11:05 → 11:35 → 11:58 → 11:59` 这种刻度。

**分频导出时 `k` 必须全局统一。** 短窗口与长窗口若各自校准含金量，
同一条曲线在切换时间范围时会跳变。长窗口算一次，短窗口用 `derive(w, k_fixed=...)` 复用。

**慢变量前向填充后，Chainlink 陈旧度必须按链上 `updatedAt` 重算。**
若沿用采集时刻记录的 `age_sec`，填充会把陈旧度压成常数，锯齿直接消失——
而锯齿正是整个项目最重要的观测对象。

**pandas 3 的 DatetimeIndex 精度是 `datetime64[s]`。** `astype("int64")` 出来已经是秒，
再除以 1e9 会把秒当纳秒。用 `.map(pd.Timestamp.timestamp)`，与精度无关。

**`Index - Series` 会对齐失败产生垃圾值。** 先包成 `pd.Series(..., index=w.index)`。

**systemd 不传 `HOME`**，而 futu-api 需要它定位日志目录，单元里须显式设置。

**`pivot_table` 会丢弃整行全 NaN 的 slot。** 若某个 slot 内所有源的 `age_sec` 都为 NULL，
`age` 的行数就比 `wide` 少，按位置赋值 index 会抛 `ValueError` 并让导出器永久卡死。
必须 `reindex` 对齐，不能靠位置。

**`as_completed(timeout=)` 超时会跳出整个 `with` 块**，已成功的读数一并丢弃，
与"缺格好过停机"的原则相悖。改为逐个 `fut.result(timeout=剩余预算)`，
并用 `shutdown(wait=False, cancel_futures=True)` 避免慢线程把整轮拖过预算。

**`OpenQuoteContext` 构造是同步阻塞的。** OpenD 进程在但不响应时它能挂很久，
会把主循环连同 24×7 的加密源一起拖停。须放进带超时的守护线程。

**OpenD 登录失败即退出，配合 `Restart=always` 会烧光账号锁定额度**（20 秒重试
≈ 2.5 分钟耗尽 10 次）。必须设 `StartLimitIntervalSec` / `StartLimitBurst` 熔断，
并改用 `Restart=on-failure`。

**触屏上不要无条件 `preventDefault()`。** 图表捕获层若吞掉所有 `touchmove`，
手指落在图上就无法纵向翻页。应按手势主方向分流：纵向放行，横向才拦。

**SVG 的 viewBox 宽度应取容器实际 CSS 宽度。** 固定 1000 宽再缩放到手机上，
10.5px 的文字会渲染成 3.6px，根本读不了。

**富途 OpenD 的 `update` 命令会把配置重置成出厂默认**（账号 100000 / 密码 123456），
升级前务必备份 `FutuOpenD.xml`。

## 已知限制

1. **USD/HKD 是日频源。** 联系汇率制下日内漂移通常 < 5 bps，相对 28 bps 的
   3030 买卖价差属次要误差，但涉及绝对水平的结论必须标注。
2. **含金量 `k` 用校准口径**，会抹掉系统性溢价/折价。要测绝对基差需接入官方每日 NAV。
3. **3030 的 tick = 7.1 bps**，价格分辨率与观测目标同量级，比这更细的价差结构观测不到。
4. **样本量。** 72 个点（6 小时）只够看形态，σ 的估计误差约 ±8%；
   做正式统计建议连跑 5–10 个交易日。

## License

MIT

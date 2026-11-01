"""
第二轮：围绕第一轮冠军（10 日新低 + 阶梯 25/25/50 + MA20 出场 + 追踪止盈）做细化

  1. 参数敏感度：追踪比例、阶梯跌幅、首笔比例、熔断阈值、持有上限 —— 看是不是孤立尖峰
  2. 第二天开盘成交 vs 当天收盘成交
  3. 分批卖出：先卖一半锁利，剩下的跟追踪
  4. 按 SOX 位置调仓位：SOX 在 200 日均线上方全仓阶梯，下方半仓
  5. 逐年收益、最大回撤发生在哪段、水下时间
  6. "早买"诊断：首笔买入后还跌了多少、到最低点用了几天
"""

import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)

real = pd.read_pickle(os.path.join(L.CACHE, "real.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["2011-01-01":]
synth = pd.read_pickle(os.path.join(L.CACHE, "synth.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["1995-06-01":]
oos = real.loc["2019-01-01":]

BASE = dict(steps=(0.0, -0.2, -0.4), frac=(0.25, 0.25, 0.5), cap=126, breaker=0.40)


def row(label, D, entry, rule, **kw):
    m, curve, t = L.evaluate(D, entry, rule, label, **kw)
    return m, curve, t


def three(label, entry_fn, rule, **kw):
    """真实 / 样本外 / 压测 三段"""
    out = {}
    for name, D in (("真实", real), ("样本外", oos), ("压测", synth)):
        m, c, t = row(label, D, entry_fn(D), rule, **kw)
        out[name] = (m, c, t)
    r = dict(策略=label)
    for name, (m, c, t) in out.items():
        r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
    m = out["真实"][0]
    r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 最差一笔=m["最差一笔"],
             水下天数=m["最长水下天数"])
    return r, out


def show(rows):
    df = pd.DataFrame(rows)
    s = df.copy()
    for c in s.columns:
        if c.endswith(("年化", "回撤", "胜率", "最差一笔")):
            s[c] = s[c].map(lambda x: f"{x:+.0%}" if pd.notna(x) else "—")
        elif c.endswith(("比", "每年笔数", "平均持有")):
            s[c] = s[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    print(s.to_string(index=False)); print()
    return df


low10 = lambda D: D.low10.values
rule_ma20 = L.exit_ma("ma20")
rule_5_ma20 = L.exit_gain_or_ma(0.05)
rule_8_ma20 = L.exit_gain_or_ma(0.08)

# ───────── 1. 敏感度 ─────────
print("═══ 1a. 追踪止盈比例（10日新低 + 阶梯 + MA20出场）═══")
rows = [three("无追踪（基准B改MA20）", low10, rule_ma20, **BASE)[0]]
for tr in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20):
    rows.append(three(f"追踪 {tr:.0%}", low10, rule_ma20, trail=tr, **BASE)[0])
show(rows)

print("═══ 1b. 固定目标 + MA20（无追踪）═══")
rows = []
for g in (0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
    rows.append(three(f"+{g:.0%} 或 MA20", low10, L.exit_gain_or_ma(g), **BASE)[0])
show(rows)

print("═══ 1c. 目标 + MA20 + 追踪 组合 ═══")
rows = []
for g in (0.05, 0.08, 0.12):
    for tr in (0.08, 0.10, 0.12):
        rows.append(three(f"+{g:.0%} 或 MA20，追踪 {tr:.0%}", low10, L.exit_gain_or_ma(g), trail=tr, **BASE)[0])
show(rows)

print("═══ 1d. 阶梯跌幅（MA20 + 追踪10%）═══")
rows = []
for step in (0.10, 0.15, 0.20, 0.25, 0.30):
    kw = dict(BASE); kw["steps"] = (0.0, -step, -2 * step)
    rows.append(three(f"每跌 {step:.0%} 加一笔", low10, rule_ma20, trail=0.10, **kw)[0])
show(rows)

print("═══ 1e. 分批比例（MA20 + 追踪10%，每跌20%）═══")
rows = []
for fr in ((0.25, 0.25, 0.5), (1 / 3, 1 / 3, 1 / 3), (0.5, 0.25, 0.25), (0.2, 0.3, 0.5), (0.4, 0.6), (0.5, 0.5),
           (0.25, 0.25, 0.25, 0.25)):
    kw = dict(BASE); kw["frac"] = fr
    kw["steps"] = tuple(-0.2 * i for i in range(len(fr)))
    rows.append(three("/".join(f"{x:.0%}" for x in fr), low10, rule_ma20, trail=0.10, **kw)[0])
show(rows)

print("═══ 1f. 熔断阈值 与 持有上限（MA20 + 追踪10%）═══")
rows = []
for br in (None, 0.30, 0.35, 0.40, 0.50):
    kw = dict(BASE); kw["breaker"] = br
    rows.append(three(f"熔断 {br if br is None else f'{br:.0%}'}", low10, rule_ma20, trail=0.10, **kw)[0])
for cap in (42, 63, 126, 252, None):
    kw = dict(BASE); kw["cap"] = cap
    rows.append(three(f"持有上限 {cap}", low10, rule_ma20, trail=0.10, **kw)[0])
show(rows)

# ───────── 2. 第二天开盘成交 ─────────
print("═══ 2. 当天收盘成交 vs 第二天开盘成交 ═══")
rows = []
for label, rule, tr in (("MA20+追踪10%", rule_ma20, 0.10), ("+5%或MA20", rule_5_ma20, None), ("+8%或MA20（基准B）", rule_8_ma20, None)):
    rows.append(three(f"{label} 收盘成交", low10, rule, trail=tr, **BASE)[0])
    rows.append(three(f"{label} 开盘成交", low10, rule, trail=tr, fill="open", **BASE)[0])
show(rows)

# ───────── 3. 分批卖出 ─────────
print("═══ 3. 分批卖出（引擎不直接支持，用两半仓位合成：一半 +5% 卖，一半 MA20+追踪）═══")
def split_curve(D, ruleA, ruleB, trA=None, trB=None, w=0.5):
    cA, tA = L.simulate(D, low10(D), ruleA, trail=trA, **BASE)
    cB, tB = L.simulate(D, low10(D), ruleB, trail=trB, **BASE)
    c = w * cA + (1 - w) * cB
    t = pd.concat([tA, tB]).sort_values("k1")
    m = L.metrics(c, t); m["最长水下天数"] = L.underwater_days(c)
    return m
rows = []
for label, a, ta, b, tb in (("一半+5%，一半MA20追踪10%", L.exit_gain(0.05), None, rule_ma20, 0.10),
                            ("一半+8%，一半MA20追踪10%", L.exit_gain(0.08), None, rule_ma20, 0.10),
                            ("一半+5%或MA20，一半MA20追踪15%", rule_5_ma20, None, rule_ma20, 0.15),
                            ("一半+3%，一半20日新高追踪10%", L.exit_gain(0.03), None, L.exit_hi(20), 0.10)):
    r = dict(策略=label)
    for name, D in (("真实", real), ("样本外", oos), ("压测", synth)):
        m = split_curve(D, a, b, ta, tb)
        r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实":
            r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"] / 2, 平均持有=m["平均持有"], 最差一笔=m["最差一笔"], 水下天数=m["最长水下天数"])
    rows.append(r)
show(rows)

# ───────── 4. 按 SOX 位置调仓位 ─────────
print("═══ 4. 仓位倍数：SOX 在 200 日线上方 1.0，下方 x（MA20 + 追踪10%）═══")
rows = []
for below in (1.0, 0.75, 0.5, 0.25, 0.0):
    size_fn = lambda D, b=below: np.where(D.sx_up.values, 1.0, b)
    r = dict(策略=f"线下仓位 {below:.2f}")
    for name, D in (("真实", real), ("样本外", oos), ("压测", synth)):
        m, c, t = row("", D, low10(D), rule_ma20, trail=0.10, size=size_fn(D), **BASE)
        r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实":
            r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 最差一笔=m["最差一笔"], 水下天数=m["最长水下天数"])
    rows.append(r)
# VIX 过滤
for vmin in (15, 18, 20, 25):
    ent = lambda D, v=vmin: (D.low10 & (D.vix >= v)).values
    rows.append(three(f"只在 VIX≥{vmin} 时买", ent, rule_ma20, trail=0.10, **BASE)[0])
show(rows)

# ───────── 5. 冠军的逐年收益与回撤明细 ─────────
print("═══ 5. 冠军（10日新低 + 25/25/50 + MA20 + 追踪10%）逐年 ═══")
m, curve, t = row("冠军", real, low10(real), rule_ma20, trail=0.10, **BASE)
bh = real.c / real.c.iloc[0]
yr = pd.DataFrame({"策略": curve.resample("YE").last().pct_change(), "SOXL买入持有": bh.resample("YE").last().pct_change()})
yr.iloc[0] = [curve.resample("YE").last().iloc[0] - 1, bh.resample("YE").last().iloc[0] - 1]
yr.index = yr.index.year
yr["笔数"] = t.groupby(real.index[t.k1].year).size()
yr["胜率"] = t.groupby(real.index[t.k1].year).ret.apply(lambda s: (s > 0).mean())
print(yr.to_string(float_format=lambda x: f"{x:+.1%}"))
dd = curve / curve.cummax() - 1
worst = dd.idxmin()
peak = curve.loc[:worst].idxmax()
rec = curve.loc[worst:][curve.loc[worst:] >= curve.loc[peak]]
print(f"\n最大回撤 {dd.min():.1%}：从 {peak.date()} 到 {worst.date()}，"
      f"{'恢复于 ' + str(rec.index[0].date()) if len(rec) else '尚未恢复'}")
print("\n亏损最多的 8 笔：")
tt = t.copy(); tt["入场"] = real.index[tt.k0].date; tt["出场"] = real.index[tt.k1].date
print(tt.sort_values("ret").head(8)[["入场", "出场", "ret", "fills", "held", "why"]].to_string(index=False, float_format=lambda x: f"{x:+.1%}"))
print("\n出场原因分布："); print(tt.groupby("why").agg(笔数=("ret", "size"), 平均收益=("ret", "mean"), 胜率=("ret", lambda s: (s > 0).mean())).to_string(float_format=lambda x: f"{x:+.1%}"))

# ───────── 6. 早买诊断 ─────────
print("\n═══ 6. 早买诊断：首笔买入后最深还跌多少、几天到底 ═══")
c = real.c.values
rows = []
for _, r in t.iterrows():
    seg = c[r.k0:r.k1 + 1]
    trough = seg.argmin()
    rows.append(dict(首笔后最深跌幅=seg.min() / c[r.k0] - 1, 到底天数=trough, 笔数=r.fills))
dg = pd.DataFrame(rows)
print(dg.describe(percentiles=[0.25, 0.5, 0.75, 0.9]).T.to_string(float_format=lambda x: f"{x:.2f}"))
print("\n按买入笔数分组的首笔后最深跌幅：")
print(dg.groupby("笔数").首笔后最深跌幅.describe()[["count", "mean", "min", "50%"]].to_string(float_format=lambda x: f"{x:.2f}"))

curve.to_frame("冠军").join(bh.rename("SOXL")).to_csv(os.path.join(RES, "champion_curve.csv"))
tt.to_csv(os.path.join(RES, "champion_trades.csv"), index=False)

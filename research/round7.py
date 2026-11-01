"""第七轮：用户偏好的框架——超卖买入、可分批加仓、持有一阵子、+10~15% 卖出"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L
pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)
real = pd.read_pickle(os.path.join(L.CACHE, "real.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["2011-01-01":]
synth = pd.read_pickle(os.path.join(L.CACHE, "synth.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["1995-06-01":]
oos = real.loc["2019-01-01":]
SETS = (("真实", real), ("样本外", oos), ("压测", synth))

ENTRIES = {
    "10日新低": lambda D: D.low10,
    "RSI(14)<40": lambda D: D.rsi14 < 40,
    "RSI(14)<35": lambda D: D.rsi14 < 35,
    "RSI(3)<15": lambda D: D.rsi3 < 15,
    "RSI(2)<10": lambda D: D.rsi2 < 10,
    "跌破布林下轨": lambda D: D.c < D.bbl,
    "低于MA20 10%": lambda D: D.dev20 <= -0.10,
    "低于MA20 15%": lambda D: D.dev20 <= -0.15,
    "距60日高点-30%": lambda D: D.dd_60 <= -0.30,
    "RSI(14)<40 且 布林%B<0.2": lambda D: (D.rsi14 < 40) & (D.pctb < 0.2),
}
LADDERS = {
    "25/25/50 每跌20%": dict(steps=(0, -0.2, -0.4), frac=(0.25, 0.25, 0.5)),
    "20/20/40 每跌20%": dict(steps=(0, -0.2, -0.4), frac=(0.2, 0.2, 0.4)),
    "25/25/25/25 每跌15%": dict(steps=(0, -0.15, -0.30, -0.45), frac=(0.25,) * 4),
    "20/30/50 每跌15%": dict(steps=(0, -0.15, -0.30), frac=(0.2, 0.3, 0.5)),
}

def run(label, entry_fn, rule, ladder, cap, breaker=0.40, trail=None, **extra):
    r = dict(策略=label)
    for name, D in SETS:
        c, t = L.simulate(D, entry_fn(D).fillna(False).values, rule, cap=cap, breaker=breaker, trail=trail, **ladder, **extra)
        m = L.metrics(c, t)
        r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实":
            r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 最长持有=m["最长持有"], 最差一笔=m["最差一笔"], 水下天数=L.underwater_days(c))
    return r

def show(rows, sort="真实比", n=None):
    df = pd.DataFrame(rows)
    if sort: df = df.sort_values(sort, ascending=False)
    if n: df = df.head(n)
    s = df.copy()
    for c in s.columns:
        if c.endswith(("年化", "回撤", "胜率", "最差一笔")): s[c] = s[c].map(lambda x: f"{x:+.0%}" if pd.notna(x) else "—")
        elif c.endswith(("比", "每年笔数", "平均持有")): s[c] = s[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    print(s.to_string(index=False)); print()
    return df

print("═══ 1. 入场信号 × 目标 10% / 12% / 15%（25/25/50，不设持有上限，只保留 SOX 熔断）═══")
rows = []
for en, ef in ENTRIES.items():
    for g in (0.10, 0.12, 0.15):
        rows.append(run(f"{en} | +{g:.0%}", ef, L.exit_gain(g), LADDERS["25/25/50 每跌20%"], cap=None))
df1 = show(rows, n=20)

print("═══ 2. 同上，但加 6 个月持有上限（到期不管盈亏都卖）═══")
rows = []
for en, ef in ENTRIES.items():
    for g in (0.10, 0.15):
        rows.append(run(f"{en} | +{g:.0%} | 6个月上限", ef, L.exit_gain(g), LADDERS["25/25/50 每跌20%"], cap=126))
df2 = show(rows, n=12)

print("═══ 3. 目标 + 安全阀：+12% 或 站上 MA50 / 或 RSI(14)≥60 / 或 20 日新高（拿久一点但不死扛）═══")
rows = []
for en in ("10日新低", "RSI(14)<40", "RSI(3)<15", "跌破布林下轨"):
    ef = ENTRIES[en]
    for g in (0.10, 0.12, 0.15):
        rows.append(run(f"{en} | +{g:.0%} 或 MA50", ef, L.exit_any(L.exit_gain(g), L.exit_ma("ma50")), LADDERS["25/25/50 每跌20%"], cap=None))
        rows.append(run(f"{en} | +{g:.0%} 或 RSI14≥60", ef, L.exit_any(L.exit_gain(g), L.exit_rsi("rsi14", 60)), LADDERS["25/25/50 每跌20%"], cap=None))
        rows.append(run(f"{en} | +{g:.0%} 或 20日新高", ef, L.exit_any(L.exit_gain(g), L.exit_hi(20)), LADDERS["25/25/50 每跌20%"], cap=None))
    rows.append(run(f"{en} | +12% 追踪8%(涨过8%后回落8%卖)", ef, L.exit_gain(0.12), LADDERS["25/25/50 每跌20%"], cap=None, trail=0.08))
df3 = show(rows, n=20)

print("═══ 4. 最佳几个的阶梯方式与护栏 ═══")
rows = []
for ln, ld in LADDERS.items():
    rows.append(run(f"10日新低 | +12% 或 MA50 | {ln}", ENTRIES["10日新低"], L.exit_any(L.exit_gain(0.12), L.exit_ma("ma50")), ld, cap=None))
    rows.append(run(f"RSI(14)<40 | +12% | {ln}", ENTRIES["RSI(14)<40"], L.exit_gain(0.12), ld, cap=None))
for en, rule, rn in (("10日新低", L.exit_any(L.exit_gain(0.12), L.exit_ma("ma50")), "+12%或MA50"), ("RSI(14)<40", L.exit_gain(0.12), "+12%")):
    r = dict(策略=f"{en} | {rn} | 25/25/50 | MA200下不加仓")
    for name, D in SETS:
        c, t = L.simulate(D, ENTRIES[en](D).fillna(False).values, rule, cap=None, breaker=0.40, no_add=(D.c < D.ma200).values, **LADDERS["25/25/50 每跌20%"])
        m = L.metrics(c, t); r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实": r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 最长持有=m["最长持有"], 最差一笔=m["最差一笔"], 水下天数=L.underwater_days(c))
    rows.append(r)
show(rows)

print("═══ 5. 最佳方案的逐年与持有时间分布 ═══")
for label, en, rule in (("10日新低 +12%或MA50", "10日新低", L.exit_any(L.exit_gain(0.12), L.exit_ma("ma50"))), ("RSI(14)<40 +12%", "RSI(14)<40", L.exit_gain(0.12))):
    c, t = L.simulate(real, ENTRIES[en](real).fillna(False).values, rule, cap=None, breaker=0.40, **LADDERS["25/25/50 每跌20%"])
    y = c.resample("YE").last().pct_change(); y.iloc[0] = c.resample("YE").last().iloc[0] - 1; y.index = y.index.year
    print(f"\n{label}：持有天数分位 25%={t.held.quantile(.25):.0f} 50%={t.held.median():.0f} 75%={t.held.quantile(.75):.0f} 90%={t.held.quantile(.9):.0f} 最长={t.held.max()}；"
          f"持有超过 3 个月的 {(t.held>63).mean():.0%}，超过 6 个月的 {(t.held>126).mean():.0%}")
    print("逐年：" + "  ".join(f"{i}:{v:+.0%}" for i, v in y.items()))
    print("出场原因：" + str(t.groupby("why").ret.agg(["size", "mean"]).round(3).to_dict()))
    dd = c / c.cummax() - 1
    print(f"最大回撤 {dd.min():.1%} 在 {dd.idxmin().date()}，最长水下 {L.underwater_days(c)} 天")

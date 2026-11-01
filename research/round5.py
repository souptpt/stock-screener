"""第五轮：把最稳的出场（+5%或MA20）和护栏组合起来，蒙特卡洛 + 压测，产出最终菜单"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L
from round4 import mc, show, real, synth, BASE, rule_5, rule_ma20, N
low10 = real.low10.values
def guard_real(name):
    if name == "MA200下不加仓": return dict(no_add=(real.c < real.ma200).values)
    if name == "MA200下不开仓": return dict(buy_halt=(real.c < real.ma200).values)
    return {}
def guard_synth(name):
    if name == "MA200下不加仓": return dict(no_add=(synth.c < synth.ma200).values)
    if name == "MA200下不开仓": return dict(buy_halt=(synth.c < synth.ma200).values)
    return {}
combos = []
for frac_name, frac in (("25/25/50", (0.25, 0.25, 0.5)), ("20/30/50", (0.2, 0.3, 0.5)), ("15/15/30", (0.15, 0.15, 0.30)), ("20/20/40", (0.2, 0.2, 0.4))):
    for g in ("无", "MA200下不加仓"):
        for xn, rule, tr in (("+5%或MA20", rule_5, None), ("MA20+追踪10%", rule_ma20, 0.10)):
            combos.append((f"{frac_name} | {xn} | {g}", frac, g, rule, tr))
rows = []
for label, frac, g, rule, tr in combos:
    kw = dict(BASE); kw["frac"] = frac
    r = mc(real, low10, rule, label, trail=tr, **kw, **guard_real(g))
    m, c, t = L.evaluate(synth, synth.low10.values, rule, label, trail=tr, **kw, **guard_synth(g))
    r["压测年化"] = m["年化"]; r["压测回撤"] = m["最大回撤"]
    m2, c2, t2 = L.evaluate(real, low10, rule, label, trail=tr, **kw, **guard_real(g))
    r["胜率"] = m2["胜率"]; r["每年笔数"] = m2["每年笔数"]; r["平均持有天"] = m2["平均持有"]
    rows.append(r)
df = pd.DataFrame(rows)
# 稳健分：年化中位 ÷ |回撤90分位 与 压测回撤 中更差者|
df["稳健比"] = df.年化中位 / df[["回撤90分位", "压测回撤"]].min(axis=1).abs()
df = df.sort_values("稳健比", ascending=False)
df.to_csv("results/final_menu.csv", index=False)
s = df.copy()
for c in s.columns:
    if c in ("每年笔数", "平均持有天", "稳健比"): s[c] = s[c].map(lambda x: f"{x:.2f}")
    elif c != "策略": s[c] = s[c].map(lambda x: f"{x:+.0%}")
print(s[["策略", "年化中位", "年化10分位", "回撤中位", "回撤90分位", "回撤最差", "回撤超40比例", "压测年化", "压测回撤", "胜率", "每年笔数", "平均持有天", "稳健比"]].to_string(index=False))

# 推荐方案逐年 + 2020/2022/2025 三次大跌中的表现
print("\n═══ 推荐候选逐年（真实数据，无噪声）═══")
picks = {
    "稳健：15/15/30 +5%或MA20 MA200下不加仓": dict(frac=(0.15, 0.15, 0.30), rule=rule_5, tr=None, g="MA200下不加仓"),
    "平衡：25/25/50 +5%或MA20 MA200下不加仓": dict(frac=(0.25, 0.25, 0.5), rule=rule_5, tr=None, g="MA200下不加仓"),
    "进取：25/25/50 +5%或MA20 无护栏": dict(frac=(0.25, 0.25, 0.5), rule=rule_5, tr=None, g="无"),
}
bh = real.c / real.c.iloc[0]
yr = pd.DataFrame({"SOXL买入持有": bh.resample("YE").last().pct_change()})
yr.iloc[0, 0] = bh.resample("YE").last().iloc[0] - 1
for name, p in picks.items():
    kw = dict(BASE); kw["frac"] = p["frac"]
    c, t = L.simulate(real, low10, p["rule"], trail=p["tr"], **kw, **guard_real(p["g"]))
    y = c.resample("YE").last().pct_change(); y.iloc[0] = c.resample("YE").last().iloc[0] - 1
    yr[name.split("：")[0]] = y
    dd = c / c.cummax() - 1
    print(f"{name}: 2020-03 最深 {dd.loc['2020-02':'2020-06'].min():.1%}，2022 最深 {dd.loc['2022'].min():.1%}，"
          f"2025-04 最深 {dd.loc['2025-02':'2025-06'].min():.1%}，全程 {dd.min():.1%}，最长水下 {L.underwater_days(c)} 天")
yr.index = yr.index.year
print(yr.to_string(float_format=lambda x: f"{x:+.1%}"))

"""第三轮：执行方式。收盘成交 vs 开盘成交 vs 限价单（触发价事先算好）；隔夜跳空诊断；最终候选菜单"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
real = pd.read_pickle(os.path.join(L.CACHE, "real.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["2011-01-01":]
synth = pd.read_pickle(os.path.join(L.CACHE, "synth.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["1995-06-01":]
oos = real.loc["2019-01-01":]
BASE = dict(steps=(0.0, -0.2, -0.4), frac=(0.25, 0.25, 0.5), cap=126, breaker=0.40)
rule_ma20 = L.exit_ma("ma20"); rule_5 = L.exit_gain_or_ma(0.05)

def trig10(D):  # 明天收盘 ≤ 前 9 日最低收盘 → 10 日新低；作为限价触发价
    return D.c.rolling(9).min().shift(1).values

def three(label, D_entry, rule, **kw):
    r = dict(策略=label)
    for name, D in (("真实", real), ("样本外", oos), ("压测", synth)):
        kw2 = dict(kw)
        if kw.get("fill") == "limit": kw2["trig"] = trig10(D)
        m, c, t = L.evaluate(D, D_entry(D), rule, label, **kw2)
        r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实": r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 最差一笔=m["最差一笔"], 水下天数=m["最长水下天数"])
    return r

def show(rows):
    s = pd.DataFrame(rows)
    for c in s.columns:
        if c.endswith(("年化", "回撤", "胜率", "最差一笔")): s[c] = s[c].map(lambda x: f"{x:+.0%}" if pd.notna(x) else "—")
        elif c.endswith(("比", "每年笔数", "平均持有")): s[c] = s[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    print(s.to_string(index=False)); print()

low10 = lambda D: D.low10.values
print("═══ 1. 隔夜跳空诊断：10 日新低收盘之后，第二天开盘相对前收 ═══")
D = real
gap = (D.o.shift(-1) / D.c - 1)[D.low10]
nxt = (D.c.shift(-1) / D.c - 1)[D.low10]
print(f"信号日数 {len(gap)}；次日开盘平均跳空 {gap.mean():+.2%}（中位 {gap.median():+.2%}，高开比例 {(gap>0).mean():.0%}）；"
      f"次日收盘平均 {nxt.mean():+.2%}（上涨比例 {(nxt>0).mean():.0%}）")
allg = (D.o.shift(-1) / D.c - 1).dropna()
print(f"对照：所有交易日次日开盘平均跳空 {allg.mean():+.2%}，高开比例 {(allg>0).mean():.0%}")
# MA20 卖出信号后的隔夜
sell_sig = (D.c > D.ma20) & (D.c.shift(1) <= D.ma20.shift(1))
g2 = (D.o.shift(-1) / D.c - 1)[sell_sig]
print(f"站上 MA20 当天 → 次日开盘平均 {g2.mean():+.2%}（高开比例 {(g2>0).mean():.0%}）\n")

print("═══ 2. 三种执行方式（10日新低 + 25/25/50）═══")
rows = []
for xl, rule, tr in (("MA20+追踪10%", rule_ma20, 0.10), ("+5%或MA20", rule_5, None)):
    rows.append(three(f"{xl} | 收盘成交", low10, rule, trail=tr, **BASE))
    rows.append(three(f"{xl} | 次日开盘成交", low10, rule, trail=tr, fill="open", **BASE))
    rows.append(three(f"{xl} | 限价买入(触发价)+收盘卖出", low10, rule, trail=tr, fill="limit", **BASE))
show(rows)

print("═══ 3. 限价执行下的参数敏感度（MA20 + 追踪）═══")
rows = []
for tr in (0.08, 0.10, 0.12, 0.15):
    rows.append(three(f"追踪 {tr:.0%}", low10, rule_ma20, trail=tr, fill="limit", **BASE))
for step in (0.15, 0.20, 0.25):
    kw = dict(BASE); kw["steps"] = (0.0, -step, -2 * step)
    rows.append(three(f"每跌 {step:.0%} 加仓", low10, rule_ma20, trail=0.10, fill="limit", **kw))
for fr in ((0.2, 0.3, 0.5), (0.25, 0.25, 0.5), (1/3, 1/3, 1/3)):
    kw = dict(BASE); kw["frac"] = fr
    rows.append(three("/".join(f"{x:.0%}" for x in fr), low10, rule_ma20, trail=0.10, fill="limit", **kw))
show(rows)

print("═══ 4. 最终候选菜单（限价执行）═══")
rows = []
rows.append(three("A 平衡：10日新低 25/25/50，MA20+追踪10%", low10, rule_ma20, trail=0.10, fill="limit", **BASE))
rows.append(three("A' 平衡：同上，+5%或MA20", low10, rule_5, fill="limit", **BASE))
kw = dict(BASE); kw["steps"] = (0.0, -0.10, -0.20)
rows.append(three("B 收益优先：每跌10%加仓，MA20+追踪10%", low10, rule_ma20, trail=0.10, fill="limit", **kw))
rows.append(three("C 稳健：只在 VIX≥20 买，MA20+追踪10%", lambda D: (D.low10 & (D.vix >= 20)).values, rule_ma20, trail=0.10, fill="limit", **BASE))
rows.append(three("D 保守：VIX创20日新高，+3%", lambda D: (D.vix_hi20 & (D.vix >= 20)).values, L.exit_gain(0.03), fill="close", **BASE))
rows.append(three("对照：原 B（+8%或MA20，收盘成交）", low10, L.exit_gain_or_ma(0.08), fill="close", **BASE))
show(rows)

print("═══ 5. 候选 A 限价执行：逐年 ═══")
m, curve, t = L.evaluate(real, low10(real), rule_ma20, "A", trail=0.10, fill="limit", trig=trig10(real), **BASE)
bh = real.c / real.c.iloc[0]
yr = pd.DataFrame({"A": curve.resample("YE").last().pct_change(), "SOXL": bh.resample("YE").last().pct_change()})
yr.iloc[0] = [curve.resample("YE").last().iloc[0] - 1, bh.resample("YE").last().iloc[0] - 1]
yr.index = yr.index.year
print(yr.to_string(float_format=lambda x: f"{x:+.1%}"))
dd = curve / curve.cummax() - 1
print("\n回撤超过 15% 的时段：")
in_dd = False
for d, v in dd.items():
    if v <= -0.15 and not in_dd: in_dd = True; start = d; worst = v
    elif in_dd:
        worst = min(worst, v)
        if v == 0: print(f"  {start.date()} → {d.date()}  最深 {worst:.1%}"); in_dd = False
if in_dd: print(f"  {start.date()} → 尚未恢复  最深 {worst:.1%}")
curve.to_frame("A").join(bh.rename("SOXL")).to_csv("results/candidateA_curve.csv")
tt = t.copy(); tt["入场"] = real.index[tt.k0].date; tt["出场"] = real.index[tt.k1].date
tt.to_csv("results/candidateA_trades.csv", index=False)

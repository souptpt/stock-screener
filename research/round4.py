"""第四轮：脆弱性测量（成交价蒙特卡洛）与崩盘护栏"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
real = pd.read_pickle(os.path.join(L.CACHE, "real.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["2011-01-01":]
synth = pd.read_pickle(os.path.join(L.CACHE, "synth.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["1995-06-01":]
BASE = dict(steps=(0.0, -0.2, -0.4), frac=(0.25, 0.25, 0.5), cap=126, breaker=0.40)
rule_ma20 = L.exit_ma("ma20"); rule_5 = L.exit_gain_or_ma(0.05); rule_8 = L.exit_gain_or_ma(0.08)
N = 300

def mc(D, entry, rule, label, n=N, noise=0.01, seed=0, **kw):
    rng = np.random.default_rng(seed)
    cagr, mdd, worst = [], [], []
    for _ in range(n):
        c, t = L.simulate(D, entry, rule, noise=noise, rng=rng, **kw)
        m = L.metrics(c, t); cagr.append(m["年化"]); mdd.append(m["最大回撤"]); worst.append(m["最差一笔"])
    cagr, mdd = np.array(cagr), np.array(mdd)
    m0 = L.metrics(*L.simulate(D, entry, rule, **kw))
    return dict(策略=label, 无噪声年化=m0["年化"], 无噪声回撤=m0["最大回撤"],
                年化中位=np.median(cagr), 年化10分位=np.percentile(cagr, 10),
                回撤中位=np.median(mdd), 回撤90分位=np.percentile(mdd, 10), 回撤最差=mdd.min(),
                回撤超40比例=(mdd <= -0.40).mean(), 回撤超30比例=(mdd <= -0.30).mean())

def show(rows):
    s = pd.DataFrame(rows)
    for c in s.columns:
        if c != "策略": s[c] = s[c].map(lambda x: f"{x:+.0%}" if pd.notna(x) else "—")
    print(s.to_string(index=False)); print()

low10 = real.low10.values
print(f"═══ 1. 成交价加 1% 随机噪声，{N} 次：真实 2011-26 ═══")
rows = []
rows.append(mc(real, low10, rule_ma20, "冠军：MA20+追踪10%", trail=0.10, **BASE))
rows.append(mc(real, low10, rule_5, "+5%或MA20", **BASE))
rows.append(mc(real, low10, rule_8, "原B：+8%或MA20", **BASE))
rows.append(mc(real, (real.low10 & (real.vix >= 20)).values, rule_ma20, "VIX≥20 过滤，MA20+追踪10%", trail=0.10, **BASE))
rows.append(mc(real, (real.vix_hi20 & (real.vix >= 20)).values, L.exit_gain(0.03), "VIX创20日新高，+3%", **BASE))
show(rows)

print("═══ 2. 崩盘护栏（冠军基础上，无噪声 + 噪声中位/90分位）═══")
guards = {
    "无护栏": {},
    "SOX距高点-25%起停买(不强卖)": dict(buy_halt=(real.sx_dd <= -0.25).values),
    "SOX距高点-30%起停买": dict(buy_halt=(real.sx_dd <= -0.30).values),
    "SOXL在自身MA200下方不加仓": dict(no_add=(real.c < real.ma200).values),
    "SOXL在MA200下方不开新仓": dict(buy_halt=(real.c < real.ma200).values),
    "SOX在MA200下方不加仓": dict(no_add=(~real.sx_up).values),
    "单笔止损30%": dict(maxloss=0.30),
    "单笔止损40%": dict(maxloss=0.40),
    "熔断阈值30%": dict(breaker=0.30),
    "首笔20%、总仓位上限60%（20/20/20）": dict(frac=(0.2, 0.2, 0.2)),
    "首笔15%（15/15/30）": dict(frac=(0.15, 0.15, 0.30)),
    "两笔制 25/25，第二笔跌25%": dict(steps=(0.0, -0.25), frac=(0.25, 0.25)),
}
rows = []
for name, g in guards.items():
    kw = dict(BASE); kw.update({k: v for k, v in g.items() if k in ("breaker", "frac", "steps")})
    extra = {k: v for k, v in g.items() if k not in ("breaker", "frac", "steps")}
    rows.append(mc(real, low10, rule_ma20, name, trail=0.10, **kw, **extra))
show(rows)

print("═══ 3. 同样的护栏在压测（1995-2026 合成）上，无噪声 ═══")
rows = []
for name, g in guards.items():
    kw = dict(BASE); kw.update({k: v for k, v in g.items() if k in ("breaker", "frac", "steps")})
    extra = {}
    if "buy_halt" in g:
        thr = -0.25 if "25" in name else (-0.30 if "30" in name else None)
        extra["buy_halt"] = (synth.sx_dd <= thr).values if thr else (synth.c < synth.ma200).values
    if "no_add" in g:
        extra["no_add"] = (~synth.sx_up).values if "SOX在" in name else (synth.c < synth.ma200).values
    if "maxloss" in g: extra["maxloss"] = g["maxloss"]
    m, c, t = L.evaluate(synth, synth.low10.values, rule_ma20, name, trail=0.10, **kw, **extra)
    rows.append(dict(策略=name, 压测年化=m["年化"], 压测回撤=m["最大回撤"], 胜率=m["胜率"], 最差一笔=m["最差一笔"]))
show(rows)

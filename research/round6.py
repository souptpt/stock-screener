"""第六轮：不同家族的方法。A 趋势跟踪（买强势）  B 开盘可执行（跳空低开）  C 组合  D 波动率定仓"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
real = pd.read_pickle(os.path.join(L.CACHE, "real.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["2011-01-01":]
synth = pd.read_pickle(os.path.join(L.CACHE, "synth.pkl")).dropna(subset=["ma200", "sx_dd"]).loc["1995-06-01":]
oos = real.loc["2019-01-01":]
BASE = dict(steps=(0.0, -0.2, -0.4), frac=(0.2, 0.2, 0.4), cap=None, breaker=0.40)
rule_5 = L.exit_gain_or_ma(0.05)
SETS = (("真实", real), ("样本外", oos), ("压测", synth))

def sim_w(D, w, cost=L.COST):
    """按每日目标仓位 w（用前一天信息算出，当天收盘调仓）模拟；闲置计息"""
    w = pd.Series(w, index=D.index).clip(0, 1.5).fillna(0.0)
    w_prev = w.shift(1).fillna(0.0)
    ret = w_prev * D.r + (1 - w_prev).clip(lower=0) * D.cash - (w - w_prev).abs() * cost
    curve = (1 + ret).cumprod()
    # 把连续持仓当作一笔
    pos = (w > 0).astype(int); grp = (pos.diff().fillna(pos) != 0).cumsum()
    trades = []
    for g, seg in curve.groupby(grp):
        if w.loc[seg.index[0]] > 0:
            i0 = D.index.get_loc(seg.index[0]); i1 = D.index.get_loc(seg.index[-1])
            trades.append(dict(k0=i0, k1=i1, ret=seg.iloc[-1] / (curve.shift(1).loc[seg.index[0]] or 1) - 1, fills=1, held=i1 - i0, why=""))
    return curve, pd.DataFrame(trades, columns=["k0", "k1", "ret", "fills", "held", "why"])

def mrow(label, fn):
    r = dict(策略=label)
    for name, D in SETS:
        c, t = fn(D)
        m = L.metrics(c, t)
        r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实":
            r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 水下天数=L.underwater_days(c))
    return r

def show(rows):
    s = pd.DataFrame(rows)
    for c in s.columns:
        if c.endswith(("年化", "回撤", "胜率")): s[c] = s[c].map(lambda x: f"{x:+.0%}" if pd.notna(x) else "—")
        elif c.endswith(("比", "每年笔数", "平均持有")): s[c] = s[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    print(s.to_string(index=False)); print()

print("═══ A. 趋势跟踪（买强势，每日调仓）═══")
rows = [mrow("买入持有 SOXL", lambda D: sim_w(D, np.ones(len(D))))]
for col in ("ma50", "ma100", "ma200"):
    if col == "ma100": D0 = None
    rows.append(mrow(f"SOXL 在 {col.upper()} 上方持有，下方空仓", lambda D, col=col: sim_w(D, (D.c > (D.c.rolling(100).mean() if col == "ma100" else D[col])).astype(float))))
rows.append(mrow("SOX 在 200 日线上方持有", lambda D: sim_w(D, D.sx_up.astype(float))))
rows.append(mrow("SOX 在 200 日线上 且 50 日线上", lambda D: sim_w(D, (D.sx_up & (D.sox > D.sx_ma50)).astype(float))))
rows.append(mrow("SOX 6 个月动量>0 持有", lambda D: sim_w(D, (D.sox / D.sox.shift(126) > 1).astype(float))))
rows.append(mrow("SOX 12 个月动量>0 持有", lambda D: sim_w(D, (D.sox / D.sox.shift(252) > 1).astype(float))))
rows.append(mrow("20 日新高买入，跌破 MA50 卖出", lambda D: sim_w(D, pd.Series(np.where(D.hi20, 1.0, np.where(D.c < D.ma50, 0.0, np.nan)), index=D.index).ffill().fillna(0))))
rows.append(mrow("50 日新高买入，20 日新低卖出", lambda D: sim_w(D, pd.Series(np.where(D.c >= D.c.rolling(50).max(), 1.0, np.where(D.low20, 0.0, np.nan)), index=D.index).ffill().fillna(0))))
# 趋势 + 波动率目标
for tv in (0.4, 0.6):
    rows.append(mrow(f"SOX 在 200 日线上方，目标波动 {tv:.0%}", lambda D, tv=tv: sim_w(D, D.sx_up.astype(float) * (tv / D.vol20).clip(upper=1.0))))
rows.append(mrow("SOX 200 日线上 × 目标波动 50%，线下 0", lambda D: sim_w(D, D.sx_up.astype(float) * (0.5 / D.vol20).clip(upper=1.0))))
show(rows)

print("═══ B. 开盘可执行：跳空低开当场在开盘买（当天开盘成交；卖出按 +5% 或 MA20 在收盘）═══")
def gap_entry(D, g):
    gap = D.o / D.c.shift(1) - 1
    sig = (gap <= -g).values
    # 用 fill=open 的机制：前一天标记，当天开盘成交
    e = np.zeros(len(D), dtype=bool); e[:-1] = sig[1:]
    return e
for g in (0.04, 0.06, 0.08, 0.10):
    rows = []
    r = dict(策略=f"低开 ≥{g:.0%} 开盘买，20/20/40 阶梯")
    for name, D in SETS:
        c, t = L.simulate(D, gap_entry(D, g), rule_5, fill="open", **BASE)
        m = L.metrics(c, t); r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实": r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 水下天数=L.underwater_days(c))
    rows.append(r)
    show(rows) if g == 0.10 else None
    if g == 0.04: allrows = [r]
    else: allrows.append(r)
# 低开 + 已经是 10 日新低附近
r = dict(策略="低开 ≥5% 且 昨收为 10 日新低，开盘买")
for name, D in SETS:
    e = gap_entry(D, 0.05) & np.r_[False, D.low10.values[:-1]]
    e2 = np.zeros(len(D), dtype=bool); e2[:-1] = (gap_entry(D, 0.05)[1:] if False else e[1:])
    c, t = L.simulate(D, (gap_entry(D, 0.05) & D.low10.values), rule_5, fill="open", **BASE)
    m = L.metrics(c, t); r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
    if name == "真实": r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 水下天数=L.underwater_days(c))
allrows.append(r)
# 对照：10 日新低收盘买
r = dict(策略="对照：10日新低收盘买，20/20/40，+5%或MA20")
for name, D in SETS:
    c, t = L.simulate(D, D.low10.values, rule_5, **BASE)
    m = L.metrics(c, t); r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
    if name == "真实": r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 水下天数=L.underwater_days(c))
allrows.append(r)
show(allrows)

print("═══ D. 抄底阶梯 + 波动率定仓（波动越大买越少）═══")
rows = []
for tv in (0.6, 0.8, 1.0, 1.2):
    r = dict(策略=f"10日新低 25/25/50，仓位 = {tv:.0%}/20日波动（上限1.5）")
    for name, D in SETS:
        size = (tv / D.vol20).clip(upper=1.5).fillna(1.0).values
        kw = dict(BASE); kw["frac"] = (0.25, 0.25, 0.5)
        c, t = L.simulate(D, D.low10.values, rule_5, size=size, **kw)
        m = L.metrics(c, t); r[f"{name}年化"] = m["年化"]; r[f"{name}回撤"] = m["最大回撤"]; r[f"{name}比"] = m["收益回撤比"]
        if name == "真实": r.update(胜率=m["胜率"], 每年笔数=m["每年笔数"], 平均持有=m["平均持有"], 水下天数=L.underwater_days(c))
    rows.append(r)
show(rows)

print("═══ C. 组合：一半趋势 + 一半抄底阶梯（各自独立账户，不再平衡）═══")
def combo(D, trend_fn, wt=0.5):
    ct, _ = sim_w(D, trend_fn(D))
    cd, td = L.simulate(D, D.low10.values, rule_5, **BASE)
    return wt * ct + (1 - wt) * cd, td
rows = []
rows.append(mrow("对照：抄底阶梯 20/20/40 单独", lambda D: L.simulate(D, D.low10.values, rule_5, **BASE)))
rows.append(mrow("对照：SOX 200 日线趋势 单独", lambda D: sim_w(D, D.sx_up.astype(float))))
for wt in (0.3, 0.5, 0.7):
    rows.append(mrow(f"{wt:.0%} SOX200趋势 + {1-wt:.0%} 抄底", lambda D, wt=wt: combo(D, lambda D: D.sx_up.astype(float), wt)))
rows.append(mrow("50% SOX200趋势×目标波动50% + 50% 抄底", lambda D: combo(D, lambda D: D.sx_up.astype(float) * (0.5 / D.vol20).clip(upper=1.0), 0.5)))
rows.append(mrow("50% 12月动量 + 50% 抄底", lambda D: combo(D, lambda D: (D.sox / D.sox.shift(252) > 1).astype(float), 0.5)))
# 制度切换：趋势里只做抄底（线上买 10 日新低），线下只做深跌（20 日新低，半仓）
def regime(D):
    e_up = (D.low10 & D.sx_up).values; e_dn = (D.low20 & ~D.sx_up).values
    size = np.where(D.sx_up.values, 1.0, 0.5)
    return L.simulate(D, e_up | e_dn, rule_5, size=size, **BASE)
rows.append(mrow("制度切换：线上买10日新低全阶梯，线下买20日新低半阶梯", regime))
show(rows)

print("═══ 组合的逐年（真实）：50/50 vs 各自 ═══")
ct, _ = sim_w(real, real.sx_up.astype(float)); cd, _ = L.simulate(real, real.low10.values, rule_5, **BASE); cc = 0.5 * ct + 0.5 * cd
bh = real.c / real.c.iloc[0]
yr = pd.DataFrame({k: v.resample("YE").last().pct_change() for k, v in (("SOXL", bh), ("趋势", ct), ("抄底", cd), ("50/50", cc))})
yr.iloc[0] = [v.resample("YE").last().iloc[0] - 1 for v in (bh, ct, cd, cc)]
yr.index = yr.index.year
print(yr.to_string(float_format=lambda x: f"{x:+.1%}"))
print("两条曲线日收益相关系数：", round(ct.pct_change().corr(cd.pct_change()), 2))

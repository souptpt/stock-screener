"""
第一轮全面扫描：入场信号 × 出场规则 × 仓位方式

用法：python sweep.py            # 需要先跑 soxl_lab.py 生成 data/real.pkl、data/synth.pkl
输出：results/sweep_real.csv、results/sweep_synth.csv、results/sweep_oos.csv，以及终端摘要

评估分三块：
  真实 SOXL 2011-2026 全样本
  样本内 2011-2018 / 样本外 2019-2026（看排名是否稳定）
  合成 3 倍半导体 1995-2026 压力测试（含 2000 和 2008 两次归零级别下跌）
"""

import os
import sys
import itertools
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soxl_lab as L

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
os.makedirs(RES, exist_ok=True)


# ───────────── 入场信号库（都返回布尔 Series） ─────────────

def entries(D):
    E = {}
    E["10日新低"] = D.low10
    E["20日新低"] = D.low20
    E["5日新低"] = D.low5
    E["RSI2<10"] = D.rsi2 < 10
    E["RSI3<15"] = D.rsi3 < 15
    E["RSI5<20"] = D.rsi5 < 20
    E["RSI14<30"] = D.rsi14 < 30
    E["跌破布林下轨"] = D.c < D.bbl
    E["%B<0"] = D.pctb < 0
    E["连跌3天"] = D.downstreak >= 3
    E["连跌4天"] = D.downstreak >= 4
    E["3日跌超15%"] = D.ret3 <= -0.15
    E["5日跌超20%"] = D.ret5 <= -0.20
    E["低于MA20 15%"] = D.dev20 <= -0.15
    E["低于MA50 25%"] = D.dev50 <= -0.25
    E["距60日高点-30%"] = D.dd_60 <= -0.30
    E["距1年高点-50%"] = D.dd_1y <= -0.50
    E["VIX创20日新高"] = D.vix_hi20 & (D.vix >= 20)
    E["VIX≥30"] = D.vix >= 30
    # 确认类：先超卖，再出现第一根反弹
    oversold = (D.rsi3 < 15) | D.low10
    E["超卖后首根阳线"] = oversold.shift(1).fillna(False) & D.up_day
    E["超卖后收复前日高点"] = oversold.shift(1).fillna(False) & D.close_gt_prev_high
    E["10日新低+IBS≥0.5"] = D.low10 & (D.ibs >= 0.5)
    E["RSI3<15+放量1.5倍"] = (D.rsi3 < 15) & (D.vr >= 1.5)
    # 组合类
    E["10日新低+RSI14<35"] = D.low10 & (D.rsi14 < 35)
    E["布林下轨+SOX RSI<35"] = (D.c < D.bbl) & (D.sx_rsi14 < 35)
    E["10日新低+SOX在200日线上"] = D.low10 & D.sx_up
    E["10日新低+SOX在200日线下"] = D.low10 & ~D.sx_up
    return {k: v.fillna(False).values.astype(bool) for k, v in E.items()}


# ───────────── 出场规则库 ─────────────

def exits():
    X = {}
    for g in (0.03, 0.05, 0.08, 0.12, 0.20):
        X[f"+{g:.0%}"] = dict(rule=L.exit_gain(g))
    X["站上MA20"] = dict(rule=L.exit_ma("ma20"))
    X["站上MA10"] = dict(rule=L.exit_ma("ma10"))
    X["站上MA50"] = dict(rule=L.exit_ma("ma50"))
    X["+8%或MA20"] = dict(rule=L.exit_gain_or_ma(0.08))
    X["+5%或MA20"] = dict(rule=L.exit_gain_or_ma(0.05))
    X["+12%或MA20"] = dict(rule=L.exit_gain_or_ma(0.12))
    for lvl in (55, 60, 70):
        X[f"RSI14≥{lvl}"] = dict(rule=L.exit_rsi("rsi14", lvl))
    X["RSI2≥90"] = dict(rule=L.exit_rsi("rsi2", 90))
    X["RSI3≥80"] = dict(rule=L.exit_rsi("rsi3", 80))
    X["10日新高"] = dict(rule=L.exit_hi(10))
    X["20日新高"] = dict(rule=L.exit_hi(20))
    X["%B≥0.8"] = dict(rule=L.exit_pctb(0.8))
    X["%B≥1"] = dict(rule=L.exit_pctb(1.0))
    X["MA20+追踪10%"] = dict(rule=L.exit_ma("ma20"), trail=0.10)
    X["RSI14≥60+追踪15%"] = dict(rule=L.exit_rsi("rsi14", 60), trail=0.15)
    X["20日新高+追踪10%"] = dict(rule=L.exit_hi(20), trail=0.10)
    return X


# ───────────── 仓位方式 ─────────────

SIZING = {
    "全仓": dict(steps=(0.0,), frac=(1.0,)),
    "阶梯25/25/50": dict(steps=(0.0, -0.2, -0.4), frac=(0.25, 0.25, 0.5)),
    "阶梯33×3(跌15%)": dict(steps=(0.0, -0.15, -0.30), frac=(1 / 3, 1 / 3, 1 / 3)),
    "阶梯50/50(跌20%)": dict(steps=(0.0, -0.2), frac=(0.5, 0.5)),
}


def run_grid(D, label, cap=126, breaker=0.40):
    E = entries(D); X = exits()
    rows = []
    total = len(E) * len(X) * len(SIZING)
    k = 0
    for (en, sig), (xn, xcfg), (sn, scfg) in itertools.product(E.items(), X.items(), SIZING.items()):
        k += 1
        if sig.sum() < 5:
            continue
        m, curve, t = L.evaluate(D, sig, xcfg["rule"], f"{en} | {xn} | {sn}",
                                 cap=cap, breaker=breaker, trail=xcfg.get("trail"), **scfg)
        m.update(入场=en, 出场=xn, 仓位=sn, 样本=label)
        rows.append(m)
        if k % 200 == 0:
            print(f"  {label}: {k}/{total}", flush=True)
    return pd.DataFrame(rows)


def main():
    real = pd.read_pickle(os.path.join(L.CACHE, "real.pkl"))
    synth = pd.read_pickle(os.path.join(L.CACHE, "synth.pkl"))
    real = real.dropna(subset=["ma200", "sx_dd"]).loc["2011-01-01":]
    synth = synth.dropna(subset=["ma200", "sx_dd"]).loc["1995-06-01":]

    print("扫描真实 SOXL 全样本 ...")
    R = run_grid(real, "真实2011-26"); R.to_csv(os.path.join(RES, "sweep_real.csv"), index=False)
    print("扫描样本内 / 样本外 ...")
    A = run_grid(real.loc[:"2018-12-31"], "样本内2011-18")
    B = run_grid(real.loc["2019-01-01":], "样本外2019-26")
    print("扫描合成压力测试 ...")
    S = run_grid(synth, "合成1995-26"); S.to_csv(os.path.join(RES, "sweep_synth.csv"), index=False)

    key = ["入场", "出场", "仓位"]
    M = (R.set_index(key)[["年化", "最大回撤", "收益回撤比", "胜率", "每年笔数", "平均持有", "最差一笔"]]
         .join(A.set_index(key)[["年化", "最大回撤", "收益回撤比"]].add_prefix("内_"))
         .join(B.set_index(key)[["年化", "最大回撤", "收益回撤比"]].add_prefix("外_"))
         .join(S.set_index(key)[["年化", "最大回撤", "收益回撤比", "胜率"]].add_prefix("压_")))
    # 综合分：真实收益回撤比、样本外收益回撤比、压测收益回撤比的几何平均（任何一项为负则为负）
    def score(r):
        vals = [r["收益回撤比"], r["外_收益回撤比"], r["压_收益回撤比"]]
        if any(pd.isna(v) or v <= 0 for v in vals):
            return -1.0
        return float(np.prod(vals) ** (1 / 3))
    M["综合分"] = M.apply(score, axis=1)
    M = M.sort_values("综合分", ascending=False)
    M.to_csv(os.path.join(RES, "sweep_oos.csv"))

    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    show = M.head(30).copy()
    for c in show.columns:
        if any(c.endswith(s) for s in ("年化", "最大回撤", "胜率", "最差一笔")):
            show[c] = show[c].map(lambda x: f"{x:+.0%}" if pd.notna(x) else "—")
        elif any(c.endswith(s) for s in ("收益回撤比", "综合分", "每年笔数", "平均持有")):
            show[c] = show[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    print("\n综合分前 30（真实 × 样本外 × 压测 三个收益回撤比的几何平均）：")
    print(show.to_string())

    print("\n按入场信号汇总（阶梯25/25/50 + +8%或MA20 出场，真实全样本）：")
    sub = R[(R.仓位 == "阶梯25/25/50") & (R.出场 == "+8%或MA20")].sort_values("收益回撤比", ascending=False)
    print(L.fmt_table(sub.to_dict("records"))[1].to_string(index=False))
    print("\n按出场规则汇总（10日新低 + 阶梯25/25/50，真实全样本）：")
    sub = R[(R.仓位 == "阶梯25/25/50") & (R.入场 == "10日新低")].sort_values("收益回撤比", ascending=False)
    print(L.fmt_table(sub.to_dict("records"))[1].to_string(index=False))


if __name__ == "__main__":
    main()

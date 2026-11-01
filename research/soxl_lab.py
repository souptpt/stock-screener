"""
SOXL 波段策略研究框架

目标：系统地测试"入场信号 × 出场规则 × 仓位方式"的组合，
用 收益/回撤 的平衡挑方法，并用样本外检验和 1995 年起的合成数据压力测试把靠运气的筛掉。

统一口径（和之前的研究一致）：
  - 收盘价判断信号，按当天收盘价成交；另提供"第二天开盘成交"的版本
  - 每边成本 0.05%，不计税；闲置资金按 13 周国债收益率（^IRX）计息
  - 按实际持股计算，不是每天再平衡

数据：全部来自 Yahoo Finance（yfinance），CNN 恐慌贪婪指数只有 2021-01-22 起是真实数据。
合成 3 倍半导体数据：日收益 = 3 × ^SOX 日收益 − 2 × 国债日利率 − 0.95%/252
"""

import os
import sys
import json
import itertools
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data")
COST = 0.0005


# ═══════════════════════════ 数据 ═══════════════════════════

def _yf(ticker, start):
    import yfinance as yf
    df = yf.download(ticker, start=start, auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo 无数据：{ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def fetch_all(force=False):
    """下载并缓存到 research/data/*.csv。返回 dict。"""
    os.makedirs(CACHE, exist_ok=True)
    spec = {"SOXL": "2010-01-01", "^SOX": "1990-01-01", "^VIX": "1990-01-01",
            "^VIX3M": "2007-01-01", "^IRX": "1990-01-01", "SMH": "2000-01-01"}
    out = {}
    for t, start in spec.items():
        fn = os.path.join(CACHE, t.replace("^", "_") + ".csv")
        if os.path.exists(fn) and not force:
            out[t] = pd.read_csv(fn, index_col=0, parse_dates=True)
        else:
            out[t] = _yf(t, start)
            out[t].to_csv(fn)
            print(f"  已下载 {t}: {len(out[t])} 行，{out[t].index[0].date()} → {out[t].index[-1].date()}")
    fn = os.path.join(CACHE, "fng.csv")
    if os.path.exists(fn) and not force:
        out["FNG"] = pd.read_csv(fn, index_col=0, parse_dates=True).iloc[:, 0]
    else:
        try:
            import requests
            r = requests.get(
                "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2020-09-01",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cnn.com/"}, timeout=20)
            r.raise_for_status()
            pts = r.json()["fear_and_greed_historical"]["data"]
            s = pd.Series({pd.Timestamp(p["x"], unit="ms").normalize(): float(p["y"]) for p in pts})
            s = s[~s.index.duplicated(keep="last")].sort_index().loc["2021-01-22":]
            s.to_frame("fng").to_csv(fn)
            out["FNG"] = s
            print(f"  已下载 F&G: {len(s)} 行")
        except Exception as e:
            print(f"  ⚠ F&G 下载失败：{e}")
            out["FNG"] = None
    return out


def build_frames(raw):
    """返回 (real, synth)：两张同结构的日线表。
    列：o h l c v（c 为调整后收盘，o/h/l 按 c/close 比例调整）、r、cash、
        sox、vix、vix3m、fng（可能为 NaN）"""
    irx = raw["^IRX"]["Close"].dropna() / 100.0
    cash_daily = (irx / 252).clip(lower=0)
    sox = raw["^SOX"]["Close"].dropna()
    vix = raw["^VIX"]["Close"].dropna()
    vix3m = raw["^VIX3M"]["Close"].dropna() if "^VIX3M" in raw else None
    fng = raw.get("FNG")

    def common(df):
        df["cash"] = cash_daily.reindex(df.index).ffill().fillna(0.0)
        df["sox"] = sox.reindex(df.index).ffill()
        df["vix"] = vix.reindex(df.index).ffill()
        df["vix3m"] = vix3m.reindex(df.index).ffill() if vix3m is not None else np.nan
        df["fng"] = fng.reindex(df.index).ffill() if fng is not None else np.nan
        return df.dropna(subset=["c", "sox"])

    # 真实 SOXL
    s = raw["SOXL"].dropna(subset=["Close", "Adj Close"])
    k = s["Adj Close"] / s["Close"]
    real = pd.DataFrame({"o": s["Open"] * k, "h": s["High"] * k, "l": s["Low"] * k,
                         "c": s["Adj Close"], "v": s["Volume"], "close_raw": s["Close"]})
    real["r"] = real.c.pct_change().fillna(0.0)
    real = common(real)

    # 合成 3 倍半导体（1995 起；成交量用 SMH，2000 年前为 NaN）
    sx = raw["^SOX"].dropna(subset=["Close"]).loc["1994-06-01":]
    rs = sx["Close"].pct_change().fillna(0.0)
    cash_s = cash_daily.reindex(sx.index).ffill().fillna(0.0)
    r3 = 3 * rs - 2 * cash_s - 0.0095 / 252
    c = 100 * (1 + r3).cumprod()
    # 开高低按 SOX 当日相对收盘的比例放大 3 倍
    o = c.shift(1) * (1 + 3 * (sx["Open"] / sx["Close"].shift(1) - 1))
    h = np.maximum(c, o.fillna(c)) * (1 + 3 * (sx["High"] / sx["Close"] - 1).clip(lower=0))
    l = np.minimum(c, o.fillna(c)) * (1 - 3 * (1 - sx["Low"] / sx["Close"]).clip(lower=0))
    smh_v = raw["SMH"]["Volume"].reindex(sx.index) if "SMH" in raw else np.nan
    synth = pd.DataFrame({"o": o.fillna(c), "h": h, "l": l, "c": c, "v": smh_v, "close_raw": c})
    synth["r"] = r3
    synth = common(synth)
    return real, synth


# ═══════════════════════════ 指标 ═══════════════════════════

def rsi(s, n):
    """Wilder RSI（严格播种：前 n 期简单均值，其后递推）。向量化实现。"""
    d = s.diff()
    up = d.clip(lower=0.0); dn = -d.clip(upper=0.0)
    au = up.rolling(n).mean().copy(); ad = dn.rolling(n).mean().copy()
    au_v = au.values.copy(); ad_v = ad.values.copy()
    u = up.values; v = dn.values
    first = n  # 第一个有效位置（diff 使第 0 位为 NaN，rolling 后第 n 位是首个均值）
    for i in range(first + 1, len(u)):
        au_v[i] = (au_v[i - 1] * (n - 1) + u[i]) / n
        ad_v[i] = (ad_v[i - 1] * (n - 1) + v[i]) / n
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + au_v / ad_v)
    out = np.where(ad_v == 0, np.where(au_v > 0, 100.0, 50.0), out)
    return pd.Series(out, index=s.index)


def add_indicators(D):
    c = D.c
    for n in (2, 3, 5, 14):
        D[f"rsi{n}"] = rsi(c, n)
    for n in (10, 20, 50, 200):
        D[f"ma{n}"] = c.rolling(n).mean()
    D["ema20"] = c.ewm(span=20, adjust=False).mean()
    sd20 = c.rolling(20).std()
    D["bbl"] = D.ma20 - 2 * sd20
    D["bbu"] = D.ma20 + 2 * sd20
    D["pctb"] = (c - D.bbl) / (D.bbu - D.bbl)
    for n in (5, 10, 20):
        D[f"low{n}"] = c <= c.rolling(n).min()
        D[f"hi{n}"] = c >= c.rolling(n).max()
    D["dd_1y"] = c / c.rolling(252, min_periods=120).max() - 1
    D["dd_2y"] = c / c.rolling(504, min_periods=120).max() - 1
    D["dd_60"] = c / c.rolling(60).max() - 1
    D["dev20"] = c / D.ma20 - 1
    D["dev50"] = c / D.ma50 - 1
    D["dev200"] = c / D.ma200 - 1
    tr = pd.concat([D.h - D.l, (D.h - c.shift()).abs(), (D.l - c.shift()).abs()], axis=1).max(axis=1)
    D["atr14"] = tr.rolling(14).mean()
    D["atr_pct"] = D.atr14 / c
    D["vol20"] = D.r.rolling(20).std() * np.sqrt(252)
    D["ibs"] = ((c - D.l) / (D.h - D.l)).replace([np.inf, -np.inf], np.nan)
    D["vr"] = D.v / D.v.rolling(50).mean().shift(1)
    down = (D.r < 0).astype(int)
    D["downstreak"] = down.groupby((down == 0).cumsum()).cumsum()
    D["ret3"] = c / c.shift(3) - 1
    D["ret5"] = c / c.shift(5) - 1
    D["ret10"] = c / c.shift(10) - 1
    D["up_day"] = D.r > 0
    D["close_gt_prev_high"] = c > D.h.shift(1)
    # 行业与宏观
    D["sx_dd"] = D.sox / D.sox.rolling(252, min_periods=200).max() - 1
    D["sx_ma200"] = D.sox.rolling(200).mean()
    D["sx_up"] = D.sox > D.sx_ma200
    D["sx_ma50"] = D.sox.rolling(50).mean()
    D["sx_rsi14"] = rsi(D.sox, 14)
    D["vix_ratio"] = D.vix / D.vix3m
    D["vix_hi20"] = D.vix >= D.vix.rolling(20).max()
    return D


# ═══════════════════════════ 回测引擎 ═══════════════════════════

def simulate(D, entry, exit_rule, steps=(0.0,), frac=(1.0,), cap=None,
             breaker=0.40, size=None, fill="close", trail=None, cost=COST, trig=None,
             noise=0.0, rng=None, buy_halt=None, maxloss=None, no_add=None):
    """通用阶梯回测，按实际持股计算。

    entry     : 布尔数组，当天收盘触发首笔买入
    exit_rule : 函数 (D, i, avg, k0, fp) -> (是否卖出, 原因)。avg 为平均成本（回测口径），k0 首笔序号
    steps     : 相对第一笔的累计跌幅阶梯，如 (0, -0.2, -0.4)；加仓条件是比上一笔再跌 steps[k]-steps[k-1]
    frac      : 每笔占入场时权益的比例
    cap       : 最长持有交易日数
    breaker   : SOX 距 52 周高点跌超此值清仓停买，直到站上 200 日均线；None 关闭
    size      : 每日仓位倍数数组（可选）
    fill      : "close" 当天收盘成交；"open" 第二天开盘成交；
                "limit" 买入按事先算好的触发价挂限价单（当天最低价触到就成交，成交价 = min(开盘, 触发价)），
                卖出仍按当天收盘（临近收盘看一眼再卖）。需要传 trig（每日触发价数组）
    trig      : fill="limit" 时每日的买入触发价（用前一天数据算出，例如前 9 日最低收盘）
    noise     : 成交价随机扰动的标准差（如 0.01 = 1%），用于测试结果对成交价的敏感程度
    buy_halt  : 布尔数组，为 True 的日子不开新仓、不加仓（例如 SOX 距高点跌超 25%），但不强制卖出
    maxloss   : 单笔相对平均成本亏损超过此比例（如 0.30）时清仓
    no_add    : 布尔数组，为 True 的日子不加仓（可开首笔）
    trail     : 追踪止盈，比持仓期间最高收盘回落此比例卖出（可选）
    """
    c = D.c.values; o = D.o.values; cr = D.cash.values; D_l = D.l.values
    if noise and rng is None:
        rng = np.random.default_rng()
    def px_noise(p):
        return p * (1 + rng.normal(0, noise)) if noise else p
    bh = buy_halt if buy_halt is not None else np.zeros(len(c), dtype=bool)
    na = no_add if no_add is not None else np.zeros(len(c), dtype=bool)
    sxdd = D.sx_dd.values; sxup = D.sx_up.values
    n = len(c)
    curve = np.empty(n); V = 0.0; C = 1.0; sh = 0.0
    fp, fa = [], []; base = 0.0; k0 = -1; peak = 0.0
    trades = []; halt = False
    pending = None  # ("buy", k) / ("sell", why) 下一天开盘执行
    for i in range(n):
        C *= 1 + cr[i]
        # 第二天开盘成交：先执行昨天挂的单
        if pending is not None and fill == "open":
            kind, info = pending; pending = None
            px = o[i] if not np.isnan(o[i]) else c[i]
            if kind == "buy":
                k = info
                if k == 0:
                    base = sh * px + C; k0 = i; fp, fa = [], []; peak = px
                amt = min(base * frac[k], C)
                if amt > 0:
                    C -= amt; sh += amt * (1 - cost) / px; fp.append(px); fa.append(amt)
            elif kind == "sell" and sh > 0:
                avg = sum(fa) / sum(a / p for p, a in zip(fp, fa))
                C += sh * px * (1 - cost)
                trades.append(dict(k0=k0, k1=i, ret=px / avg - 1, fills=len(fp), held=i - k0, why=info))
                sh = 0.0; fp, fa = [], []
        V = sh * c[i]
        if breaker is not None:
            if sxdd[i] <= -breaker:
                halt = True
            elif halt and sxup[i]:
                halt = False
        if sh > 0:
            peak = max(peak, c[i])
            avg = sum(fa) / sum(a / p for p, a in zip(fp, fa))
            held = i - k0
            sell, why = exit_rule(D, i, avg, k0, fp)
            if not sell and trail is not None and c[i] <= peak * (1 - trail) and c[i] > avg:
                sell, why = True, "追踪止盈"
            if not sell and cap and held >= cap:
                sell, why = True, "时间上限"
            if not sell and maxloss is not None and c[i] / avg - 1 <= -maxloss:
                sell, why = True, f"止损{maxloss:.0%}"
            if not sell and breaker is not None and halt:
                sell, why = True, "行业熔断"
            if sell:
                if fill in ("close", "limit"):
                    spx = px_noise(c[i])
                    C += sh * spx * (1 - cost)
                    trades.append(dict(k0=k0, k1=i, ret=spx / avg - 1, fills=len(fp), held=held, why=why))
                    sh = 0.0; V = 0.0; fp, fa = [], []
                else:
                    pending = ("sell", why)
            else:
                k = len(fp)
                if fill == "limit":
                    if k < len(steps):
                        tp = fp[-1] * (1 + steps[k] - steps[k - 1])
                        if D_l[i] <= tp:
                            px = min(o[i], tp) if not np.isnan(o[i]) else tp
                            amt = min(base * frac[k], C); C -= amt
                            sh += amt * (1 - cost) / px; fp.append(px); fa.append(amt)
                elif k < len(steps) and c[i] / fp[-1] - 1 <= steps[k] - steps[k - 1] and not bh[i] and not na[i]:
                    if fill == "close":
                        bpx = px_noise(c[i])
                        amt = min(base * frac[k], C); C -= amt
                        sh += amt * (1 - cost) / bpx; fp.append(bpx); fa.append(amt)
                    else:
                        pending = ("buy", k)
        elif not halt and fill == "limit" and trig is not None and not np.isnan(trig[i]) and D_l[i] <= trig[i]:
            px = min(o[i], trig[i]) if not np.isnan(o[i]) else trig[i]
            base = (sh * c[i] + C) * (1.0 if size is None else float(size[i])); k0 = i; peak = c[i]
            amt = min(base * frac[0], C); C -= amt
            sh = amt * (1 - cost) / px; fp, fa = [px], [amt]
        elif not halt and not bh[i] and entry[i] and pending is None and fill != "limit":
            mult = 1.0 if size is None else float(size[i])
            if fill == "close":
                bpx = px_noise(c[i])
                base = (V + C) * mult; k0 = i; peak = c[i]
                amt = min(base * frac[0], C); C -= amt
                sh = amt * (1 - cost) / bpx; fp, fa = [bpx], [amt]
            else:
                base_mult = mult
                pending = ("buy", 0)
                base = (sh * c[i] + C) * base_mult  # 开盘成交时会重算，这里先占位
        curve[i] = sh * c[i] + C
    t = pd.DataFrame(trades, columns=["k0", "k1", "ret", "fills", "held", "why"])
    return pd.Series(curve, index=D.index), t


# ═══════════════════════════ 出场规则库 ═══════════════════════════

def exit_gain(g):
    return lambda D, i, avg, k0, fp: (D.c.values[i] / avg - 1 >= g, f"+{g:.0%}")


def exit_ma(col):
    return lambda D, i, avg, k0, fp: (D.c.values[i] > D[col].values[i], f"站上{col}")


def exit_rsi(col, lvl):
    return lambda D, i, avg, k0, fp: (D[col].values[i] >= lvl, f"{col}≥{lvl}")


def exit_any(*rules):
    def f(D, i, avg, k0, fp):
        for r in rules:
            s, w = r(D, i, avg, k0, fp)
            if s:
                return True, w
        return False, ""
    return f


def exit_gain_or_ma(g, col="ma20"):
    return exit_any(exit_gain(g), exit_ma(col))


def exit_hi(n):
    """收盘创 n 日新高卖出"""
    return lambda D, i, avg, k0, fp: (bool(D[f"hi{n}"].values[i]), f"{n}日新高")


def exit_bb_mid_or_gain(g):
    return exit_any(exit_gain(g), exit_ma("ma20"))


def exit_pctb(lvl):
    return lambda D, i, avg, k0, fp: (D.pctb.values[i] >= lvl, f"%B≥{lvl}")


# ═══════════════════════════ 评估 ═══════════════════════════

def metrics(curve, trades, cash=None):
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = curve.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = curve / curve.cummax() - 1
    mdd = dd.min()
    daily = curve.pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else np.nan
    n = len(trades)
    out = dict(年化=cagr, 最大回撤=mdd, 夏普=sharpe, 笔数=n, 每年笔数=n / yrs if yrs else np.nan,
               胜率=(trades.ret > 0).mean() if n else np.nan,
               平均每笔=trades.ret.mean() if n else np.nan,
               最差一笔=trades.ret.min() if n else np.nan,
               平均持有=trades.held.mean() if n else np.nan,
               最长持有=trades.held.max() if n else np.nan,
               收益回撤比=(cagr / abs(mdd)) if mdd < 0 else np.nan,
               在场比例=np.nan)
    return out


def underwater_days(curve):
    """最长水下天数（距前高）"""
    peak = curve.cummax()
    under = curve < peak
    mx = cur = 0
    for u in under.values:
        cur = cur + 1 if u else 0
        mx = max(mx, cur)
    return mx


def evaluate(D, entry, exit_rule, label="", **kw):
    curve, t = simulate(D, entry, exit_rule, **kw)
    m = metrics(curve, t)
    m["最长水下天数"] = underwater_days(curve)
    m["策略"] = label
    return m, curve, t


def fmt_table(rows, sort="收益回撤比"):
    df = pd.DataFrame(rows)
    cols = ["策略", "年化", "最大回撤", "收益回撤比", "夏普", "每年笔数", "胜率", "平均每笔",
            "最差一笔", "平均持有", "最长持有", "最长水下天数"]
    df = df[[c for c in cols if c in df.columns]]
    if sort in df.columns:
        df = df.sort_values(sort, ascending=False)
    pct = ["年化", "最大回撤", "胜率", "平均每笔", "最差一笔"]
    show = df.copy()
    for c in pct:
        if c in show:
            show[c] = show[c].map(lambda x: f"{x:+.1%}" if pd.notna(x) else "—")
    for c in ("收益回撤比", "夏普", "每年笔数", "平均持有"):
        if c in show:
            show[c] = show[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    return df, show


if __name__ == "__main__":
    raw = fetch_all(force="--refresh" in sys.argv)
    real, synth = build_frames(raw)
    real = add_indicators(real); synth = add_indicators(synth)
    print(f"真实 SOXL：{real.index[0].date()} → {real.index[-1].date()}，{len(real)} 天")
    print(f"合成 3 倍半导体：{synth.index[0].date()} → {synth.index[-1].date()}，{len(synth)} 天")
    both = real.r.loc["2010-03-12":].align(synth.r, join="inner")
    print(f"真实与合成日收益相关系数：{both[0].corr(both[1]):.3f}")
    real.to_pickle(os.path.join(CACHE, "real.pkl")); synth.to_pickle(os.path.join(CACHE, "synth.pkl"))
    print("已保存 real.pkl / synth.pkl")

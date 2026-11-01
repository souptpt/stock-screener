"""
SOXL 波段信号每日提醒（只提醒，不下单）

每天收盘后用 Yahoo Finance 和 CNN 恐慌贪婪指数，检查两套回测过的策略：
  B（推荐）：收盘创 10 日新低 且 F&G≤45 时买 25%，比上一笔再跌 20% 加 25%、再跌 20% 加 50%；
             比平均成本高 8% 或收盘站上 20 日均线时全部卖出
  D（高胜率）：收盘跌破布林下轨 且（F&G≤25 或 VIX≥25）时买，阶梯同上；只在比平均成本高 3% 时卖
两套都有：最长持有 126 个交易日（约 6 个月）；行业熔断——SOX 收盘距 52 周最高收盘价跌超 40%
时清仓并停止买入，直到 SOX 收盘重新站上 200 日均线。

"现在有没有持仓"是从 2021-01-22（CNN 有真实 F&G 数据的第一天）起按规则逐日模拟出来的，
不读任何券商账户。CNN 接口不通时，F&G 条件改用 VIX 代替（B：VIX≥20；D：只看 VIX≥25）。

单独运行：python soxl_alert.py [--html 输出文件.html]   只打印/输出报告，不发邮件。
在 daily_screener_email.py 里调用 build_report()，作为邮件里的一个区块。
"""

import sys

import pandas as pd
import requests
import yfinance as yf

SIM_START = "2021-01-22"      # CNN F&G 真实数据起点
DATA_START = "2019-06-01"     # 多拉一段，给 SOX 52 周高点和 200 日均线预热

STEPS = (0.0, -0.20, -0.40)   # 相对第一笔的累计跌幅；实际按"比上一笔再跌 20%"触发
FRACS = (0.25, 0.25, 0.50)
CAP_DAYS = 126
BREAKER = 0.40

STRATEGIES = {
    "B": dict(name="B 推荐：10 日新低", gain=0.08, ma20=True),
    "D": dict(name="D 高胜率：跌破布林下轨", gain=0.03, ma20=False),
}

FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}"
FNG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.cnn.com/",
    "Origin": "https://www.cnn.com",
}


# ───────────────────────── 数据 ─────────────────────────

def _download(ticker, start=DATA_START):
    df = yf.download(ticker, start=start, auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo 没有返回 {ticker} 的数据")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


def fetch_fng(start=SIM_START):
    """返回 (按日期索引的 F&G Series, 最新读数, 最新读数的时间)；失败返回 (None, None, None)。"""
    try:
        r = requests.get(FNG_URL.format(start=start), headers=FNG_HEADERS, timeout=20)
        r.raise_for_status()
        j = r.json()
        pts = j["fear_and_greed_historical"]["data"]
        s = pd.Series(
            {pd.Timestamp(p["x"], unit="ms").normalize(): float(p["y"]) for p in pts}
        ).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        now = j.get("fear_and_greed", {})
        score = float(now["score"]) if "score" in now else float(s.iloc[-1])
        ts = now.get("timestamp")
        return s, score, ts
    except Exception as e:
        print(f"  ⚠ CNN 恐慌贪婪指数获取失败：{e}")
        return None, None, None


def load_data():
    soxl = _download("SOXL")
    sox = _download("^SOX")["Close"]
    vix = _download("^VIX")["Close"]

    D = pd.DataFrame(index=soxl.index)
    D["close"] = soxl["Close"]            # 已按拆股调整、未按分红调整：真实成交价
    D["c"] = soxl["Adj Close"]            # 分红再调整：回测口径
    D = D.dropna()
    D["r"] = D.c.pct_change().fillna(0.0)
    D["ma20"] = D.c.rolling(20).mean()
    D["bbl"] = D.ma20 - 2 * D.c.rolling(20).std()
    D["low10"] = D.c <= D.c.rolling(10).min()

    sox_full = sox.dropna()
    sx_hi = sox_full.rolling(252, min_periods=200).max()
    sx_ma200 = sox_full.rolling(200).mean()
    D["sox"] = sox_full.reindex(D.index).ffill()
    D["sx_hi"] = sx_hi.reindex(D.index).ffill()
    D["sx_ma200"] = sx_ma200.reindex(D.index).ffill()
    D["sx_dd"] = D.sox / D.sx_hi - 1
    D["sx_up"] = D.sox > D.sx_ma200
    D["vix"] = vix.reindex(D.index).ffill()

    fng, fng_now, fng_ts = fetch_fng()
    if fng is not None:
        D["fg"] = fng.reindex(D.index.union(fng.index)).ffill().reindex(D.index)
        # 当天 CNN 还没出日线点时，用实时读数补上最后一天
        if pd.isna(D["fg"].iloc[-1]) or fng.index[-1] < D.index[-1]:
            D.loc[D.index[-1], "fg"] = fng_now
    else:
        D["fg"] = float("nan")
    return D, (fng is not None), fng_now, fng_ts


# ───────────────────────── 指标 ─────────────────────────

def wilder_rsi(series, period):
    """严格播种的 Wilder RSI（前 period 期简单均值，其后递推），返回最新一期。"""
    d = series.diff().dropna().tolist()
    if len(d) < period:
        return None
    g = [max(x, 0.0) for x in d]
    l = [max(-x, 0.0) for x in d]
    ag, al = sum(g[:period]) / period, sum(l[:period]) / period
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def signals(D, has_fng):
    if has_fng:
        fg = D.fg
        sig_b = D.low10 & (fg <= 45)
        sig_d = (D.c < D.bbl) & ((fg <= 25) | (D.vix >= 25))
    else:
        sig_b = D.low10 & (D.vix >= 20)
        sig_d = (D.c < D.bbl) & (D.vix >= 25)
    return {"B": sig_b.fillna(False).values, "D": sig_d.fillna(False).values}


# ───────────────────────── 模拟 ─────────────────────────

def simulate(D, sig, gain, ma20, steps=STEPS, frac=FRACS, cap=CAP_DAYS,
             breaker=BREAKER, cost=0.0005):
    """与研究里的 sim3 规则一致（按实际持股计算），但返回逐笔记录和最后一天的持仓状态。
    闲置资金不计息：这里只关心持仓状态，不关心收益率。"""
    c = D.c.values; r = D.r.values; ma = D.ma20.values
    sxdd = D.sx_dd.values; sxup = D.sx_up.values
    n = len(c)
    V, C = 0.0, 1.0
    fp, fa, fd = [], [], []        # 每笔买入的价格、金额、日期序号
    base, k0, halt = 0.0, -1, False
    trades = []
    events = []                    # (日期序号, 事件, 价格)
    for i in range(n):
        V *= 1 + r[i]
        if breaker is not None:
            if sxdd[i] <= -breaker:
                if not halt:
                    events.append((i, "熔断启动", c[i]))
                halt = True
            elif halt and sxup[i]:
                halt = False
                events.append((i, "熔断解除", c[i]))
        if fp:
            avg = sum(fa) / sum(a / p for p, a in zip(fp, fa))
            p = c[i] / avg - 1
            held = i - k0
            why = None
            if p >= gain:
                why = f"比平均成本高 {gain:.0%}"
            elif ma20 and c[i] > ma[i]:
                why = "站上 20 日均线"
            elif cap and held >= cap:
                why = "持有满 6 个月"
            elif breaker is not None and halt:
                why = "行业熔断"
            if why:
                C += V * (1 - cost)
                trades.append(dict(k0=k0, k1=i, ret=p, fills=len(fp), held=held, why=why))
                events.append((i, f"卖出（{why}）", c[i]))
                V = 0.0; fp, fa, fd = [], [], []
            else:
                k = len(fp)
                if k < len(steps) and c[i] / fp[-1] - 1 <= steps[k] - steps[k - 1]:
                    amt = min(base * frac[k], C); C -= amt; V += amt * (1 - cost)
                    fp.append(c[i]); fa.append(amt); fd.append(i)
                    events.append((i, f"加仓第 {k + 1} 笔", c[i]))
        elif not halt and sig[i]:
            base = V + C; k0 = i
            amt = min(base * frac[0], C); C -= amt; V += amt * (1 - cost)
            fp, fa, fd = [c[i]], [amt], [i]
            events.append((i, "买入第 1 笔", c[i]))
    state = dict(halt=halt, fills=list(zip(fd, fp, fa)), k0=k0 if fp else None,
                 invested=sum(fa) / base if fp else 0.0)
    if fp:
        state["avg"] = sum(fa) / sum(a / p for p, a in zip(fp, fa))
    return trades, events, state


# ───────────────────── 明天的触发价 ─────────────────────

def _bbl_trigger(last19):
    """明天收盘价低于多少会跌破布林下轨（20 日均线 − 2 倍标准差，含明天）。"""
    import statistics

    def f(x):
        w = list(last19) + [x]
        return x - (statistics.mean(w) - 2 * statistics.stdev(w))

    lo, hi = 0.0, float(last19[-1]) * 2
    if f(lo) >= 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return lo


def build_report():
    """返回 dict：text（纯文本）、html（邮件区块）、data_date。出错直接抛异常，由调用方兜底。"""
    D, has_fng, fng_now, fng_ts = load_data()
    sim = D.loc[SIM_START:].copy()
    sigs = signals(sim, has_fng)
    last = sim.index[-1]
    n = len(sim)
    k = float(sim.close.iloc[-1] / sim.c.iloc[-1])   # 回测口径 → 真实价格
    px = lambda v: v * k

    close = float(sim.close.iloc[-1])
    prev = float(sim.close.iloc[-2])
    chg = close / prev - 1
    fg = sim.fg.iloc[-1]
    vix = float(sim.vix.iloc[-1])
    sox = float(sim.sox.iloc[-1]); sx_hi = float(sim.sx_hi.iloc[-1])
    sx_dd = sox / sx_hi - 1; sx_ma200 = float(sim.sx_ma200.iloc[-1])
    rsi3 = wilder_rsi(D.c, 3); rsi14 = wilder_rsi(D.c, 14)
    ma20_real = px(float(sim.ma20.iloc[-1])); bbl_real = px(float(sim.bbl.iloc[-1]))

    c_tail = sim.c.values
    low10_next = px(float(min(c_tail[-9:])))                 # 明天收盘 ≤ 此价 = 10 日新低
    bbl_next = _bbl_trigger(c_tail[-19:])
    bbl_next = px(bbl_next) if bbl_next else None
    ma20_next = px(float(sum(c_tail[-19:]) / 19))            # 明天收盘 > 此价 = 站上含明天的 20 日均线

    # 长期抄底档位：距 2 年最高收盘价跌 70% / 80% / 90%
    two_y = D.close.iloc[-504:]
    hi2_date = two_y.idxmax(); hi2 = float(two_y.max())
    after = two_y.loc[hi2_date:]
    tranches = []
    for dd in (0.70, 0.80, 0.90):
        line = hi2 * (1 - dd)
        hit = after[after <= line]
        tranches.append((dd, line, hit.index[0] if len(hit) else None))

    results = {}
    for key, cfg in STRATEGIES.items():
        trades, events, st = simulate(sim, sigs[key], cfg["gain"], cfg["ma20"])
        today = [(e, px(p)) for i, e, p in events if i == n - 1]
        info = dict(cfg=cfg, trades=trades, state=st, today=today)
        if st["fills"]:
            avg = px(st["avg"])
            last_fill = px(st["fills"][-1][1])
            nf = len(st["fills"])
            held = n - 1 - st["k0"]
            info.update(
                status="持仓中",
                avg=avg, nf=nf, held=held,
                entry=sim.index[st["k0"]],
                fills=[(sim.index[i], px(p)) for i, p, _a in st["fills"]],
                invested=st["invested"],
                pnl=close / avg - 1,
                add_px=last_fill * 0.8 if nf < len(FRACS) else None,
                add_frac=FRACS[nf] if nf < len(FRACS) else None,
                sell_px=(min(avg * (1 + cfg["gain"]), ma20_next) if cfg["ma20"]
                         else avg * (1 + cfg["gain"])),
                cap_left=CAP_DAYS - held,
            )
        elif st["halt"]:
            info.update(status="熔断暂停")
        else:
            info.update(status="空仓")
        results[key] = info

    # 过滤条件（明天用的还是今天读数，只能作参考）
    if has_fng:
        filt_b = (f"F&G≤45（现在 {fg:.1f}）", fg <= 45)
        filt_d = (f"F&G≤25 或 VIX≥25（现在 F&G {fg:.1f} / VIX {vix:.1f}）", fg <= 25 or vix >= 25)
    else:
        filt_b = (f"VIX≥20（F&G 取不到，代用；现在 {vix:.1f}）", vix >= 20)
        filt_d = (f"VIX≥25（F&G 取不到，代用；现在 {vix:.1f}）", vix >= 25)
    results["B"]["filter"] = filt_b; results["B"]["buy_px"] = low10_next
    results["D"]["filter"] = filt_d; results["D"]["buy_px"] = bbl_next

    ctx = dict(
        date=last.strftime("%Y-%m-%d"), close=close, chg=chg, fg=fg, has_fng=has_fng,
        vix=vix, sox=sox, sx_hi=sx_hi, sx_dd=sx_dd, sx_line=sx_hi * (1 - BREAKER),
        sx_ma200=sx_ma200, rsi3=rsi3, rsi14=rsi14, ma20=ma20_real, bbl=bbl_real,
        hi2=hi2, hi2_date=hi2_date, tranches=tranches, low10_next=low10_next,
        bbl_next=bbl_next, ma20_next=ma20_next, sim_index=sim.index, px=px,
    )
    return dict(text=_render_text(ctx, results), html=_render_html(ctx, results),
                data_date=ctx["date"], results=results, ctx=ctx)


# ───────────────────────── 输出 ─────────────────────────

def _d(ts):
    return ts.strftime("%m/%d") if ts is not None else "—"


def _action_lines(key, info, ctx):
    """返回 (今日一句话, [明天要盯的价位])"""
    st = info["status"]
    today = "、".join(f"{e} ${p:.2f}" for e, p in info["today"]) or ""
    watch = []
    if st == "熔断暂停":
        watch.append(f"SOX 收盘站上 200 日均线 {ctx['sx_ma200']:,.0f} 后才恢复买入")
    elif st == "空仓":
        label, ok = info["filter"]
        bp = info["buy_px"]
        if bp:
            watch.append(f"收盘 ≤ ${bp:.2f} 触发买入（25%），另需 {label}"
                         + ("，目前满足" if ok else "，目前不满足"))
    else:
        if info["add_px"]:
            watch.append(f"收盘 ≤ ${info['add_px']:.2f} 加仓 {info['add_frac']:.0%}")
        watch.append(f"收盘 ≥ ${info['sell_px']:.2f} 全部卖出"
                     + ("（+8% 或站上 20 日均线，取较低者）" if key == "B" else "（+3%）"))
        watch.append(f"最晚再过 {info['cap_left']} 个交易日按 6 个月上限卖出")
    return today, watch


def _render_text(ctx, results):
    L = [f"── SOXL 波段信号（{ctx['date']} 收盘，只提醒不下单）──",
         f"SOXL ${ctx['close']:.2f}（{ctx['chg']:+.2%}）  RSI(3) {ctx['rsi3']:.1f}  RSI(14) {ctx['rsi14']:.1f}  "
         f"20 日均线 ${ctx['ma20']:.2f}  布林下轨 ${ctx['bbl']:.2f}",
         (f"F&G {ctx['fg']:.1f}  " if ctx["has_fng"] else "F&G 取不到（用 VIX 代替）  ")
         + f"VIX {ctx['vix']:.1f}  SOX {ctx['sox']:,.0f}，距 52 周高点 {ctx['sx_dd']:+.1%}，"
           f"熔断线 {ctx['sx_line']:,.0f}"]
    for key in ("B", "D"):
        info = results[key]
        today, watch = _action_lines(key, info, ctx)
        head = f"[{info['cfg']['name']}] {info['status']}"
        if info["status"] == "持仓中":
            head += (f"：{info['entry']:%m/%d} 起，已买 {info['nf']} 笔（占 {info['invested']:.0%}），"
                     f"均价 ${info['avg']:.2f}，浮动 {info['pnl']:+.1%}")
        L.append(head)
        if today:
            L.append(f"  今天：{today}")
        for w in watch:
            L.append(f"  明天：{w}")
        if info["trades"]:
            t = info["trades"][-1]
            ix = ctx["sim_index"]
            L.append(f"  上一笔：{ix[t['k0']]:%Y-%m-%d} → {ix[t['k1']]:%Y-%m-%d}，"
                     f"{t['fills']} 笔，{t['ret']:+.1%}（{t['why']}）")
    tr = "  ".join(f"-{dd:.0%} ${line:.2f}" + (f"（{hit:%Y-%m-%d} 已触及）" if hit is not None else "")
                   for dd, line, hit in ctx["tranches"])
    L.append(f"长期抄底档位（2 年高点 ${ctx['hi2']:.2f}，{ctx['hi2_date']:%Y-%m-%d}）：{tr}")
    L.append("以上按收盘价判断，盘后价不算；持仓状态是按规则从 2021-01-22 模拟出来的，不是你的账户。")
    return "\n".join(L) + "\n"


def _render_html(ctx, results):
    cell = "padding:7px 12px;border-bottom:1px solid #EFEFEC;vertical-align:top"
    chg_col = "#1A7F37" if ctx["chg"] > 0 else ("#C0392B" if ctx["chg"] < 0 else "#666")
    fg_txt = f"{ctx['fg']:.1f}" if ctx["has_fng"] else '<span style="color:#A85410">取不到，用 VIX 代替</span>'
    dd_col = "#C0392B" if ctx["sx_dd"] <= -0.3 else ("#A85410" if ctx["sx_dd"] <= -0.15 else "#555")

    rows = ""
    for key in ("B", "D"):
        info = results[key]
        today, watch = _action_lines(key, info, ctx)
        st = info["status"]
        badge_bg, badge_fg = {"持仓中": ("#E6EEF8", "#1F3864"),
                              "熔断暂停": ("#FCEBEB", "#A32D2D")}.get(st, ("#F1F1EE", "#666"))
        detail = ""
        if st == "持仓中":
            fills = "、".join(f"{d:%m/%d} ${p:.2f}" for d, p in info["fills"])
            pcol = "#1A7F37" if info["pnl"] >= 0 else "#C0392B"
            detail = (f'<div style="font-size:12px;color:#555;margin-top:3px">均价 ${info["avg"]:.2f}，'
                      f'已买 {info["nf"]} 笔（占 {info["invested"]:.0%}）：{fills}；'
                      f'浮动 <span style="color:{pcol};font-weight:600">{info["pnl"]:+.1%}</span></div>')
        today_html = (f'<div style="margin-top:4px;background:#FFF3CD;padding:3px 8px;border-radius:4px;'
                      f'font-size:12.5px;font-weight:600;display:inline-block">今天：{today}</div>'
                      if today else "")
        last_html = ""
        if info["trades"]:
            t = info["trades"][-1]; ix = ctx["sim_index"]
            tcol = "#1A7F37" if t["ret"] >= 0 else "#C0392B"
            last_html = (f'<div style="font-size:11.5px;color:#999;margin-top:3px">上一笔 '
                         f'{ix[t["k0"]]:%Y-%m-%d} → {ix[t["k1"]]:%Y-%m-%d}，{t["fills"]} 笔，'
                         f'<span style="color:{tcol}">{t["ret"]:+.1%}</span>（{t["why"]}）</div>')
        watch_html = "".join(f'<div style="font-size:12.5px">• {w}</div>' for w in watch)
        rows += (
            f'<tr><td style="{cell};width:34%">'
            f'<div style="font-weight:600">{info["cfg"]["name"]}</div>'
            f'<span style="background:{badge_bg};color:{badge_fg};font-size:11px;padding:1px 7px;'
            f'border-radius:3px">{st}</span>{detail}{today_html}</td>'
            f'<td style="{cell}">{watch_html}{last_html}</td></tr>'
        )

    tr = " &nbsp;·&nbsp; ".join(
        f"-{dd:.0%} <b>${line:.2f}</b>"
        + (f' <span style="color:#A85410">({hit:%m/%d} 已触及)</span>' if hit is not None else "")
        for dd, line, hit in ctx["tranches"])

    return f"""<div style="margin-bottom:24px;border:1px solid #D5DCE6;border-radius:8px;overflow:hidden">
<div style="background:#1F3864;color:#fff;padding:8px 12px;font-size:13px;font-weight:600">
SOXL 波段信号 &nbsp;<span style="font-weight:400;font-size:11.5px;opacity:.85">{ctx['date']} 收盘 · 只提醒，不下单</span></div>
<div style="padding:8px 12px;font-size:12.5px;color:#444;background:#F7F9FC;border-bottom:1px solid #E4E8EE">
SOXL <b>${ctx['close']:.2f}</b> <span style="color:{chg_col}">{ctx['chg']:+.2%}</span>
&nbsp;·&nbsp; RSI(3) {ctx['rsi3']:.1f} &nbsp;·&nbsp; RSI(14) {ctx['rsi14']:.1f}
&nbsp;·&nbsp; 20日均线 ${ctx['ma20']:.2f} &nbsp;·&nbsp; 布林下轨 ${ctx['bbl']:.2f}<br>
F&amp;G {fg_txt} &nbsp;·&nbsp; VIX {ctx['vix']:.1f}
&nbsp;·&nbsp; SOX {ctx['sox']:,.0f}，距52周高点 <span style="color:{dd_col}">{ctx['sx_dd']:+.1%}</span>
（熔断线 {ctx['sx_line']:,.0f}）</div>
<table style="border-collapse:collapse;width:100%;font-size:13px">{rows}</table>
<div style="padding:7px 12px;font-size:12px;color:#555;border-top:1px solid #EFEFEC">
长期抄底档位（2年高点 ${ctx['hi2']:.2f}，{ctx['hi2_date']:%Y-%m-%d}）：{tr}</div>
<div style="padding:6px 12px 8px;font-size:11px;color:#999">
按收盘价判断，盘后价不算。持仓状态是按规则从 2021-01-22 逐日模拟出来的，不读你的账户。历史回测，不构成投资建议。</div>
</div>"""


def main():
    out_html = None
    if "--html" in sys.argv:
        out_html = sys.argv[sys.argv.index("--html") + 1]
    rep = build_report()
    print(rep["text"])
    # 便于核对：把两套策略最近几笔交易都打出来
    ix = rep["ctx"]["sim_index"]
    for key in ("B", "D"):
        print(f"最近交易 {key}：")
        for t in rep["results"][key]["trades"][-6:]:
            print(f"  {ix[t['k0']]:%Y-%m-%d} → {ix[t['k1']]:%Y-%m-%d}  {t['fills']} 笔  "
                  f"{t['ret']:+.1%}  {t['held']} 天  {t['why']}")
    if out_html:
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(f'<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
                    f'max-width:860px">{rep["html"]}</body></html>')
        print(f"HTML 已写入 {out_html}")


if __name__ == "__main__":
    main()

# CLAUDE.md

每日美股筛选器。每个交易日傍晚自动跑一次，找出收盘价**低于 200 日均线 20% 及以上**的股票，生成 Excel 并发邮件。

详细项目史、需求演进、完整故障排查记录见 `每日股票筛选器_项目全记录.md`（在用户本地，不在仓库里）。本文件只写改代码时需要知道的事。

---

## 文件地图

```
daily_screener_email.py      主脚本，所有逻辑都在这里
requirements.txt             yfinance, pandas, openpyxl, requests, lxml, html5lib
russell1000_cache.csv        罗素1000成分股，1022 只，ticker,name,fetched
screen_history.csv           最近 2 个交易日的名单快照，CI 自动更新，不要手改
.github/workflows/daily-screener.yml
```

脚本内的主要函数：

| 函数 | 作用 |
|---|---|
| `batch_download_closes(tickers, batch_size=60)` | 批量抓收盘价，返回 `{ticker: Series}` |
| `calc_rsi(series, period=14)` | Wilder RSI，严格播种版 |
| `detect_gap(series, window)` | 均线窗口内单日跌幅 ≥ `GAP_THRESHOLD` 的跳水，命中返回 `(日期, 跌幅%)` |
| `get_russell1000()` | 五级回退，缓存优先 |
| `load_baseline(current_date)` | 取上一交易日名单做对比 |
| `run_screener()` | 主流程，返回 7 元组 |

`run_screener()` 返回值签名（改动时注意调用方 `main()` 和 `send_email()`，三处 return 都要改）：

```python
return df, fname, data_date, len(r1k), (base_date, added, removed), watch_df, suspect_df
```

---

## 运行链路

```
cron-job.org（周一至周五，美东傍晚）
  └─ POST /repos/souptpt/stock-screener/actions/workflows/daily-screener.yml/dispatches
       Authorization: Bearer <classic PAT, workflow scope>   → 204 = 成功
     └─ GitHub Actions
          ├─ python daily_screener_email.py
          ├─ Yahoo Finance（yfinance，1031 只，约 3 分钟）
          ├─ Gmail SMTP_SSL :465 → tangpingtaotpt@gmail.com
          └─ git commit screen_history.csv 和 russell1000_cache.csv 回仓库（有变化才提交）

Claude 定时任务「每日美股筛选值班」（周一至周五 美东 18:00 左右）
  └─ 在用户交接项目的那个 Claude 会话里继续：
       ├─ 查当天有没有 daily-screener.yml 的 run
       ├─ 读 job 日志，确认有 "✅ 邮件已发送"
       ├─ 当天没有 run → Claude 触发一次
       └─ 正常只留一行记录；失败 / 邮件没发出 → 推送通知到用户手机
```

定时**不在**这个仓库里，在 cron-job.org。Claude 的检查任务在 claude.ai 的定时任务里，也不在仓库里。

Claude 可以触发工作流（2026-10-24 用户给 Claude GitHub App 授权后验证，`workflow_dispatch` 返回 204）。cron-job.org 仍是主触发，不要停用；Claude 只在当天没有任何 run 时补触发一次，当天已有 run（不论成败）绝不再触发，否则一天两封邮件。

---

## 硬约束

改代码前先读这一节。每一条背后都有一次实际事故。

### 1. 不要在 yml 里加 `schedule:`

GitHub 原生 cron 对这个仓库实测完全不触发（试过 5 个时间点）。更糟的是它偶尔会触发，于是和 cron-job.org 撞车，一天收到两封邮件。

**工作流只能有 `workflow_dispatch:`。** 要改运行时间，去 cron-job.org 改。

### 2. 凭证只能从环境变量读

```python
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", LOCAL_PASSWORD)
```

- `LOCAL_PASSWORD` 是本地调试占位符，**提交时必须保持占位符状态**
- 仓库是 public 的，任何提交进去的凭证等同于公开
- 真实凭证只在三个地方：GitHub Secrets（`GMAIL_APP_PASSWORD` / `SENDER_EMAIL` / `RECIPIENT_EMAIL`）、cron-job.org 的 header 配置、用户本人的密码管理器
- 不要在代码、注释、commit message、issue 里写任何凭证

### 3. `screen_history.csv` 由 CI 维护

跨运行的状态持久化全靠它（CI 本身无状态）。只保留最近 2 个日期。手动编辑会让第二天的"新增/移出"对比出错。

### 4. 阈值方向

```python
THRESHOLD = 0.20
...
if deviation <= -THRESHOLD:     # 低于年线 20% 及以上
```

是 `<= -0.20`，不是 `>= -0.20`。这个方向被改错过一次。

### 5. 出错必须非零退出

邮件发送失败、App Password 仍是占位符、价格数据一只都没下载到（Yahoo 不可用）时，脚本 `sys.exit(1)`，Actions 标红。以前只是 `return`，exit 0，Actions 显示绿色，邮件没发出去也没人知道。不要改回去。历史提交步骤是 `if: always()`，失败时名单快照照常提交。

---

## 数据源的坑

### yfinance 返回 MultiIndex 列

批量下载时列是多层索引，直接取 `df["Close"]` 会拿到空数据：

```python
if isinstance(data.columns, pd.MultiIndex):
    closes = data["Close"]
    for t in closes.columns:
        ...
```

### 两道长度门槛

```python
if len(s) >= 30:          # batch_download_closes：不足 30 天的直接丢弃
    out[t] = s

ma_days = min(200, len(series))    # 主循环：不足 200 天就用短均线，不跳过
```

这两行决定了新上市股票和刚做过公司行为的股票的行为。CTVA 事件（见下）就卡在这里。

### RSI 必须用严格播种

不要改回 `ewm(com=13, adjust=False)`。那个版本在 300 天数据上看起来对，但在 Wilder 原著的 33 期序列上算出 33.7，正确答案是 37.8。差别在于前 14 期要用**简单均值播种**再递推。

当前实现对 Wilder 原始序列和 200 组随机序列零误差。

### 罗素 1000 用缓存，不要改成网页抓取优先

```python
sources = [
    ("本地缓存(iShares官方)",  _r1k_from_cache),    # ← 必须排第一
    ("维基百科(表格)",         _r1k_from_wikipedia),
    ...
]
```

网页抓取同一份代码在不同网络环境下时好时坏（实测维基页面在某些网络下返回的 HTML 里 nasdaq/nyse 链接数为 0）。缓存是唯一可靠来源。

每一级必须返回 ≥500 只才算成功，非缓存来源成功后会覆写缓存（所以 CI 也会提交 `russell1000_cache.csv`）。

注意：`_r1k_from_ishares` 和 `_r1k_from_wikipedia` 在脚本里各定义了两次，Python 以**后面**那个为准。改这两个函数时改后面的定义。

### 代码符号转换

iShares 的写法和 Yahoo 不一样，转换表在脚本里：

```
BRKB → BRK-B    BFB → BF-B    LENB → LEN-B    UHALB → UHAL-B
```

### iShares `.xls` 其实是 XML

解析前要转义裸 `&`，否则 `ET.fromstring` 报 `not well-formed`：

```python
raw = re.sub(r'&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', raw)
```

---

## 公司行为（corporate action）护栏

### 起因

2026-10-07 实例：Corteva (CTVA) 在 10/1 把种子业务分拆为 Vylor (VYLR)，股价从 \$77.65 掉到 \$11.92。脚本拿分拆后的 \$14.45 去除一条用分拆前 \$70–80 算出的年线，得到 **−81.25%**，占据名单第一位。这是纯粹的假信号——股东手里多了价值约 \$73 的 VYLR。第二天数据源修正历史后它自动消失了，但下一个分拆的公司会重演。

### 现行规则（2026-10-24 上线，用户看过试发邮件后选定）

```python
GAP_THRESHOLD = 0.40
...
gap = detect_gap(series, ma_days)   # 只在 deviation <= -THRESHOLD 的股票上检查
```

- 均线窗口（最近 `ma_days` 根，通常 200）内出现**单日跌幅 ≥ 40%** 的股票，移出主名单，进 `suspect_df`
- 只看下跌：上跳只会压低均线，不会制造"低于年线"的假信号；窗口外的跳水不影响 MA，不算
- 疑似名单**不进**统计卡片、`screen_history.csv`、日度对比；昨天在主名单、今天进了疑似名单的，也不算"移出"
- 邮件里在主表下方单独列出（琥珀色区块），Excel 里是单独的 `疑似公司行为` 工作表

### 权衡

真实暴跌（财报爆雷、FDA 否决、诉讼败诉）单日也能跌 40%+，也会被隔离，而且会在疑似区块里待满约 200 个交易日，直到跳水移出均线窗口。信号不会丢（每天都在邮件里展示），只是不进主名单和对比。首次试跑（10/23 数据）隔离了 PRIM、CAR 两只，主名单 115 → 113。以后要缩短，可以把 `detect_gap` 的窗口改成最近 N 个交易日。

---

## 定期维护

| 事项 | 周期 | 操作 |
|---|---|---|
| 罗素 1000 缓存 | 每年 6 月底 | 指数重新编制后，从 iShares 重下 IWB 持仓，转成 `russell1000_cache.csv` 提交 |
| 成分股硬编码列表 | 每季度 | `SPX_TICKERS` / `NDX_TICKERS` / `DJIA_TICKERS` / `DJTA_TICKERS` / `DJUA_TICKERS` 都是硬编码 |
| GitHub PAT | 到期前 | 过期后 cron-job.org 触发返回 401 |
| Gmail App Password | 主密码变更后 | 会失效，要在 Secrets 里更新 |

缓存超过 **400 天**时脚本会在邮件里自动打过期警告。

---

## 本地运行

```bash
py -m pip install -r requirements.txt
py daily_screener_email.py
```

需要先在 `LOCAL_PASSWORD` 填 App Password。**跑完改回占位符再提交。**

用户环境是 Windows，桌面路径是 `C:\Users\Pingtao Tang\OneDrive\桌面`（OneDrive 接管 + 中文目录名）。`pip` 要写成 `py -m pip`。

---

## 手动触发一次

GitHub → Actions → "Daily Stock Screener" → Run workflow → `main` 分支。约 3–5 分钟后收到邮件。

---

## 约定

- **改动直接推 `main`**：用户已授权。用户提出修改（例如改参数、加筛选条件）时，改好、本地验证通过后直接推 `main`，当天 18:00 那次运行就生效。推之前先 `git pull` 拿到 CI 当天提交的 `screen_history.csv`
- **界面语言是中文**：列名、邮件正文、日志输出、commit message 都用中文
- **Excel 列名**：`股票代码 | 公司名称 | 所属指数 | 收盘价(USD) | 当日涨跌% | MA200(USD) | 偏离年线% | RSI(14)`；`疑似公司行为` 工作表再加 `跳水日期 | 单日跌幅%`
- **邮件标题**：`SPX 500、NDX、DJCA和罗素1000指数偏离年线20%以上_{y}年{m}月{d}日`
- **主题色**：`1F3864`（深蓝），用于表头填充和邮件标题栏
- **观察名单**（当前只有 SOXL）每天无条件展示，但**不进入筛选统计和日度对比**——靠 `if ticker not in index_set: continue` 实现
- **Excel 自动筛选**：`auto_filter.ref` 必须覆盖全部数据行（`A1:{last_col}{max_row}`），只写表头会导致下拉箭头点不动；同时要取消所有合并单元格
- **工作表名不能含 `/`**，openpyxl 会直接报错，用 `&` 替代

---

## 改动时的自检

- [ ] 没有在 yml 里加 `schedule:`
- [ ] `LOCAL_PASSWORD` 还是占位符
- [ ] 改了 `run_screener()` 返回值的话，`main()` 和 `send_email()` 同步改了
- [ ] 改了筛选条件的话，方向是 `<= -THRESHOLD`
- [ ] 改了列的话，Excel 和邮件 HTML 两边都改了
- [ ] 改了观察名单的话，`WATCHLIST` 和 `WATCHLIST_NAMES` 两个都改了
- [ ] 出错时仍然 `sys.exit(1)`（邮件失败、价格数据全部下载失败）

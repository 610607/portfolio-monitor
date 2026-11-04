# portfolio-monitor

按 Notion 里写的规则检查持仓：行情、技术指标和估值全部在本地计算，只输出触发项和一张指标快照。
供 Claude 计划任务「持仓监控」在美股收盘后调用：Claude 只负责取持仓、查新闻和写推送，计算交给脚本，
以免每天把几千个价格搬进对话。

仓库里没有账户数据和密钥。持仓、净值和规则在运行时由调用方写进输入文件。

## 运行

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="portfolio-monitor 你的邮箱@example.com"   # SEC 要求写联系邮箱，否则返回 403
python -m monitor run input.json --out result.json   # 打印精简结果，完整结果写入 result.json
python -m monitor check input.json                   # 只检查规则写法（不联网）
python -m monitor eval PEP "RSI(14)" "PE" "分位(PE, 5)"   # 看某个标的的指标，调规则时用
python -m monitor names [--talib]                    # 列出全部指标
python -m pytest                                     # 离线测试
```

## 数据来源

| 数据 | 来源 | 说明 |
| --- | --- | --- |
| 日线（10 年）、分红、拆股 | [yfinance](https://github.com/ranaroussi/yfinance)（Yahoo Finance） | 收盘价按拆股调整、不按分红调整，与 IBKR 图表一致。非官方接口，偶尔限流或失灵，失败时重试一次 |
| EPS、营收、股数、净资产 | [SEC EDGAR XBRL companyfacts](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | 只有向 SEC 申报 10-Q/10-K 的公司有；ETF 和按 20-F 申报的外国公司没有 |
| 技术指标 | [TA-Lib](https://ta-lib.org/)（Python 封装 [TA-Lib/ta-lib-python](https://github.com/TA-Lib/ta-lib-python)） | 237 个函数原样调用，不改写 |
| 持仓、成本、净值、上限 | 调用方从 IBKR 和 Notion 取来写进输入 | |

基本面按时点取值：每个数字取最早一次申报的值，从申报日起才算已知。缺的季度用累计数相减（例如 Q4 = 全年 − 前三季）。
EPS 是 GAAP 稀释 EPS，会受一次性项目影响；不用公司自己的「调整后 EPS」，因为没有可追溯的结构化来源。

## 规则写法

一行一条，用竖线分栏：

- 标的库「监控条件」：`组 | 条件 | 动作 [| 每天]`
- 投资政策规则块：`范围 | 组 | 条件 | 动作 [| 每天]`；范围写 `全部`、类别名（核心指数、股息、个股、债券、现金储备、其他）、代码，或 `组合`

组只能是 `加仓`、`减仓或停买`、`复核`、`信息`。

**什么时候报**：默认「新成立才报」，即今天成立、上一交易日不成立；这样同一个状态不会每天重复。
最后一栏写 `每天` 则今天成立就报，适合本身就是单日事件的条件（单日跌幅、创新低）。

**条件**：

- 比较：`< <= > >= == !=`（也可写 `≤ ≥ ≠ =`），可以连写 `30 < RSI(14) < 70`
- 逻辑：`且` `或` `非`（也可写 `and or not`），括号分组
- 百分数：`-10%` 就是 `-0.1`
- 运算：`+ - * /`
- 人工核对项：`收盘 < 123 且 [没有新利空]`。方括号里的内容脚本不判断，只在触发时列为「待核对」，由 Claude 查新闻后确认。只能用「且」接在最外层

**动作**里可以用 `{表达式:格式}` 算数，例如 `约卖出 ${市值 - 上限 * 净值:,.0f}`。

示例见 [examples/policy_rules.txt](examples/policy_rules.txt) 和 [examples/input.example.json](examples/input.example.json)（数字只为演示）。

## 指标

`python -m monitor names` 列出全部。主要有：

- **价格**：`收盘` `单日` `涨跌(n)` `距高点(n)` `距低点(n)` `距52周高` `创52周新低` `波动率(n)` `成交量`
- **TA-Lib**：按原名调用，参数顺序和默认值同 TA-Lib，例如 `RSI(14)` `SMA(200)` `EMA(50)` `ADX(14)` `ATR(14)` `CCI(20)` `MFI(14)` `OBV` `WILLR(14)`。
  多输出的写 `函数_输出名`，例如 `MACD_macdhist` `BBANDS_lowerband(20, 2, 2)` `STOCH_slowk`。
  中文别名：`均线(n)` `指数均线(n)` `MACD线` `MACD信号` `MACD柱` `布林上轨` `布林中轨` `布林下轨`
- **估值和基本面**（SEC）：`EPS_TTM` `PE` `营收_TTM` `PS` `PB` `总市值` `营收同比` `EPS同比`
- **股息**（yfinance）：`每股股息_TTM` `股息率`（过去一年实际派发）`派息率`
- **持仓**（来自输入）：`持股` `成本` `相对成本` `市值` `市值("代码")` `净值` `占净值` `投入` `投入占比` `上限` `按市值计` `上限占比` `大类占比("类别")`
- **函数**：`分位(x, 年数)` 历史百分位、`前值(x, n)` `最大(x, n)` `最小(x, n)` `平均(x, n)`

持仓类指标只有今天和上一交易日有意义（上一交易日的持股和净值由今天的成交倒推），不要放进 `平均`、`分位` 这类看长期历史的函数。

某个指标今天没有值时（例如亏损公司的 PE、ETF 的 PE、上市不满一年的分位），条件按不成立处理，并在「提示」里说明；
持仓类没有值（没持有、没设上限）视为不适用，不提示。

新增指标：在 `monitor/indicators.py` 里用 `@metric(...)` 注册一个函数，返回按交易日对齐的 `pandas.Series`。TA-Lib 已有的不用加。

## 输入

```jsonc
{
  "date": "2026-11-03",          // 美东日期
  "calendar": "VT",              // 用它的最新日线判断今天是否交易日
  "nav": 10000, "nav_prev": 9950, // 净值；前一天 = 净值 − 当日盈亏
  "rules": "范围 | 组 | 条件 | 动作 …",   // 投资政策里的规则块，原文
  "snapshot": ["收盘", "RSI(14)"],        // 可选：快照列，默认见 engine.DEFAULT_SNAPSHOT
  "targets": [{
    "symbol": "PEP", "category": "个股", "status": "持有",
    "position": 4, "avg_cost": 140.0,                 // 未持有的不填
    "buy_qty_today": 0, "buy_cost_today": 0, "sell_qty_today": 0,  // 今天的成交，用来倒推前一天
    "cap_pct": 4, "cap_basis": "市值",                // 标的库的仓位上限和口径
    "conditions": "组 | 条件 | 动作 …",                // 标的库「监控条件」，原文
    "yahoo": "BRK-B"                                  // 可选：Yahoo 代码和 IBKR 不同时
  }]
}
```

## 输出

`result.json`：

- `trading_day`：日历标的的最新日线是不是 `date`
- `triggers`：组、代码、条件、动作（占位已算好）、待核对项、条件里每个指标今天的值
- `errors`：规则写错或无法计算；`notes`：数据缺失、行情过旧等提示
- `categories`：各类别今天的市值和占净值
- `dividends`：近 10 个自然日内的除息（每股金额、是否低于此前三次的最小值）
- `snapshot`：每个标的的快照列

条件只按白名单语法解析（Python `ast`），不执行任何代码。

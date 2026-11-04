"""指标：内置指标 + TA-Lib 全部函数。

每个指标返回按交易日对齐的 pandas.Series（或标量）。条件里引用指标名即可，
TA-Lib 的函数按原名使用（例如 RSI(14)、ADX(14)、BBANDS_upperband(20,2,2)）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from .fundamentals import Fundamentals

TRADING_DAYS = 252


@dataclass
class Metric:
    names: tuple[str, ...]
    func: Callable[..., Any]
    desc: str
    group: str
    percent: bool = False          # 快照里按百分比显示
    portfolio: bool = False        # 组合层面也能用


REGISTRY: dict[str, Metric] = {}
METRICS: list[Metric] = []


def metric(*names: str, desc: str, group: str, percent: bool = False, portfolio: bool = False):
    def deco(func):
        m = Metric(names, func, desc, group, percent, portfolio)
        METRICS.append(m)
        for n in names:
            REGISTRY[n] = m
        return func
    return deco


# TA-Lib 的中文别名
ALIASES = {
    "均线": ("SMA", None), "指数均线": ("EMA", None),
    "MACD线": ("MACD", "macd"), "MACD信号": ("MACD", "macdsignal"), "MACD柱": ("MACD", "macdhist"),
    "布林上轨": ("BBANDS", "upperband"), "布林中轨": ("BBANDS", "middleband"), "布林下轨": ("BBANDS", "lowerband"),
}


def talib_names() -> list[str]:
    import talib
    return list(talib.get_functions())


def resolve_talib(ident: str) -> tuple[str, str | None] | None:
    """把名字解析成 (TA-Lib 函数, 输出名)；不是 TA-Lib 函数返回 None。"""
    if ident in ALIASES:
        return ALIASES[ident]
    import talib
    funcs = set(talib.get_functions())
    up = ident.upper()
    if up in funcs:
        return up, None
    if "_" in ident:
        fname, out = ident.split("_", 1)
        if fname.upper() in funcs:
            return fname.upper(), out.lower()
    return None


# ------------------------------------------------------------------ 上下文

@dataclass
class Position:
    position: float = float("nan")      # 今天收盘后持股
    avg_cost: float = float("nan")
    buy_qty_today: float = 0.0
    buy_cost_today: float = 0.0
    sell_qty_today: float = 0.0
    cap: float = float("nan")           # 仓位上限（小数，0.04 = 4%）
    cap_basis: str = "市值"              # 市值 / 投入


@dataclass
class Portfolio:
    nav: float = float("nan")
    nav_prev: float = float("nan")
    calendar: pd.DatetimeIndex | None = None
    targets: dict[str, "TargetContext"] = field(default_factory=dict)

    def nav_series(self, index: pd.DatetimeIndex) -> pd.Series:
        prev = self.nav_prev if not math.isnan(self.nav_prev) else self.nav
        s = pd.Series(prev, index=index, dtype="float64")
        if len(s):
            s.iloc[-1] = self.nav
        return s

    def market_value(self, symbol: str, index: pd.DatetimeIndex) -> pd.Series:
        t = self.targets.get(symbol.upper())
        if t is None:
            raise KeyError(f"没有标的 {symbol}")
        return t.metric("市值").reindex(index, method="ffill")

    def category_share(self, category: str, index: pd.DatetimeIndex) -> pd.Series:
        total = pd.Series(0.0, index=index)
        found = False
        for t in self.targets.values():
            if t.category == category:
                mv = t.metric("市值").reindex(index, method="ffill").fillna(0.0)
                total = total + mv
                found = True
        if not found:
            return pd.Series(0.0, index=index)
        return total / self.nav_series(index)


class TargetContext:
    """一个标的的求值环境。"""

    def __init__(self, symbol: str, bars: pd.DataFrame, *, category: str = "", status: str = "",
                 position: Position | None = None, portfolio: Portfolio | None = None,
                 fundamentals: Fundamentals | None = None):
        self.symbol = symbol.upper()
        self.bars = bars
        self.index = bars.index
        self.category = category
        self.status = status
        self.pos = position or Position()
        self.portfolio = portfolio or Portfolio()
        self.fund = fundamentals
        self._cache: dict[tuple, Any] = {}
        self._ohlcv = bars[["open", "high", "low", "close", "volume"]].astype("float64")

    # 求值器接口
    def name(self, ident: str) -> Any:
        return self.call(ident, [], {})

    def call(self, ident: str, args: list, kwargs: dict) -> Any:
        key = (ident, tuple(_hashable(a) for a in args), tuple(sorted((k, _hashable(v)) for k, v in kwargs.items())))
        if key in self._cache:
            return self._cache[key]
        m = REGISTRY.get(ident)
        if m is not None:
            value = m.func(self, *args, **kwargs)
        else:
            ta = resolve_talib(ident)
            if ta is None:
                raise NameError(f"不认识的指标：{ident}")
            value = self._talib(ta[0], ta[1], args, kwargs)
        self._cache[key] = value
        return value

    def metric(self, ident: str, *args) -> Any:
        return self.call(ident, list(args), {})

    def _talib(self, fname: str, output: str | None, args: list, kwargs: dict) -> pd.Series:
        from talib import abstract
        f = abstract.Function(fname)
        res = f(self._ohlcv, *args, **kwargs)
        if isinstance(res, pd.DataFrame):
            if output is None:
                output = res.columns[0]
            if output not in res.columns:
                raise NameError(f"{fname} 没有输出 {output}，可选：{', '.join(res.columns)}")
            res = res[output]
        elif output is not None:
            raise NameError(f"{fname} 只有一个输出，不用写 _{output}")
        return pd.Series(np.asarray(res, dtype="float64"), index=self.index)

    def series(self, value: Any) -> pd.Series:
        if isinstance(value, pd.Series):
            return value
        return pd.Series(float(value) if value is not None else float("nan"), index=self.index)


def _hashable(v: Any) -> Any:
    if isinstance(v, pd.Series):
        return ("series", id(v))
    return v


def _nan_if_nonpositive(s: pd.Series) -> pd.Series:
    return s.where(s > 0)


# ------------------------------------------------------------------ 价格

@metric("收盘", "close", desc="收盘价（按拆股调整，不按分红调整）", group="价格")
def _close(ctx):
    return ctx.bars["close"]


@metric("开盘", "open", desc="开盘价", group="价格")
def _open(ctx):
    return ctx.bars["open"]


@metric("最高", "high", desc="最高价", group="价格")
def _high(ctx):
    return ctx.bars["high"]


@metric("最低", "low", desc="最低价", group="价格")
def _low(ctx):
    return ctx.bars["low"]


@metric("成交量", "volume", desc="成交量（股）", group="价格")
def _volume(ctx):
    return ctx.bars["volume"].astype("float64")


@metric("单日", desc="单日涨跌 = 今日收盘 / 前一交易日收盘 − 1", group="价格", percent=True)
def _day(ctx):
    return ctx.metric("收盘").pct_change(fill_method=None)


@metric("涨跌", desc="涨跌(n) = 收盘 / n 个交易日前收盘 − 1", group="价格", percent=True)
def _change(ctx, n=1):
    return ctx.metric("收盘").pct_change(int(n), fill_method=None)


@metric("距高点", desc="距高点(n) = 收盘 / 近 n 个交易日（含今天）最高收盘 − 1", group="价格", percent=True)
def _from_high(ctx, n=TRADING_DAYS):
    c = ctx.metric("收盘")
    return c / c.rolling(int(n), min_periods=1).max() - 1


@metric("距低点", desc="距低点(n) = 收盘 / 近 n 个交易日（含今天）最低收盘 − 1", group="价格", percent=True)
def _from_low(ctx, n=TRADING_DAYS):
    c = ctx.metric("收盘")
    return c / c.rolling(int(n), min_periods=1).min() - 1


@metric("距52周高", desc="= 距高点(252)", group="价格", percent=True)
def _from_52w_high(ctx):
    return ctx.metric("距高点", TRADING_DAYS)


@metric("创52周新低", desc="收盘低于此前 252 个交易日的最低收盘，且之前 20 个交易日内没有出现过（真/假）", group="价格")
def _new_52w_low(ctx):
    c = ctx.metric("收盘")
    prior_min = c.shift(1).rolling(TRADING_DAYS, min_periods=20).min()
    new_low = (c < prior_min).fillna(False)
    recent = new_low.shift(1).rolling(20, min_periods=1).max().fillna(0).astype(bool)
    return new_low & ~recent


@metric("波动率", desc="波动率(n) = 近 n 日对数收益率标准差 × √252（年化）", group="价格", percent=True)
def _vol(ctx, n=20):
    r = np.log(ctx.metric("收盘")).diff()
    return r.rolling(int(n)).std() * math.sqrt(TRADING_DAYS)


# ------------------------------------------------------------------ 通用函数

@metric("分位", desc="分位(x, 年数=5)：x 今天的值在过去若干年里的百分位（0–1，pandas rolling rank）",
        group="函数", percent=True)
def _pct_rank(ctx, x, years=5):
    s = ctx.series(x)
    w = max(int(round(float(years) * TRADING_DAYS)), 2)
    return s.rolling(w, min_periods=min(w, TRADING_DAYS)).rank(pct=True)


@metric("前值", desc="前值(x, n=1)：x 在 n 个交易日前的值", group="函数")
def _shift(ctx, x, n=1):
    return ctx.series(x).shift(int(n))


@metric("最大", desc="最大(x, n)：x 近 n 个交易日的最大值", group="函数")
def _rmax(ctx, x, n):
    return ctx.series(x).rolling(int(n), min_periods=1).max()


@metric("最小", desc="最小(x, n)：x 近 n 个交易日的最小值", group="函数")
def _rmin(ctx, x, n):
    return ctx.series(x).rolling(int(n), min_periods=1).min()


@metric("平均", desc="平均(x, n)：x 近 n 个交易日的平均值", group="函数")
def _rmean(ctx, x, n):
    return ctx.series(x).rolling(int(n)).mean()


# ------------------------------------------------------------------ 持仓与组合

def _two_point(ctx, today: float, prev: float) -> pd.Series:
    s = pd.Series(prev, index=ctx.index, dtype="float64")
    if len(s):
        s.iloc[-1] = today
    return s


@metric("持股", desc="持股数（今天收盘后；之前各天按今天的成交倒推）", group="持仓")
def _shares(ctx):
    p = ctx.pos
    prev = p.position - p.buy_qty_today + p.sell_qty_today
    return _two_point(ctx, p.position, prev)


@metric("成本", desc="平均成本价（未持有为空）", group="持仓")
def _cost(ctx):
    return ctx.pos.avg_cost


@metric("相对成本", desc="收盘 / 成本 − 1", group="持仓", percent=True)
def _vs_cost(ctx):
    return ctx.metric("收盘") / ctx.pos.avg_cost - 1


@metric("市值", desc="市值 = 持股 × 收盘；市值(\"代码\") 取另一个标的的市值", group="持仓", portfolio=True)
def _mv(ctx, symbol=None):
    if symbol is not None:
        return ctx.portfolio.market_value(str(symbol), ctx.index)
    return ctx.metric("持股") * ctx.metric("收盘")


@metric("净值", desc="账户净值（前一天用 净值 − 当日盈亏 近似）", group="持仓", portfolio=True)
def _nav(ctx):
    return ctx.portfolio.nav_series(ctx.index)


@metric("占净值", desc="市值 / 净值", group="持仓", percent=True)
def _weight(ctx):
    return ctx.metric("市值") / ctx.metric("净值")


@metric("投入", desc="持仓成本 = 持股 × 平均成本；前一天扣除今天买入的成交额", group="持仓")
def _invested(ctx):
    p = ctx.pos
    today = p.position * p.avg_cost
    return _two_point(ctx, today, today - p.buy_cost_today)


@metric("投入占比", desc="投入 / 净值", group="持仓", percent=True)
def _invested_weight(ctx):
    return ctx.metric("投入") / ctx.metric("净值")


@metric("上限", desc="标的库的仓位上限（小数；没填为空）", group="持仓", percent=True)
def _cap(ctx):
    return ctx.pos.cap


@metric("按市值计", desc="上限口径是否为市值（真/假）", group="持仓")
def _cap_by_mv(ctx):
    return ctx.pos.cap_basis != "投入"


@metric("上限占比", desc="上限口径为投入时 = 投入占比，否则 = 占净值", group="持仓", percent=True)
def _cap_weight(ctx):
    return ctx.metric("投入占比") if ctx.pos.cap_basis == "投入" else ctx.metric("占净值")


@metric("大类占比", desc="大类占比(\"类别\")：该类别全部持仓市值 / 净值", group="持仓", percent=True, portfolio=True)
def _category_share(ctx, category):
    return ctx.portfolio.category_share(str(category), ctx.index)


# ------------------------------------------------------------------ 基本面（SEC）

def _fund(ctx) -> Fundamentals:
    if ctx.fund is None:
        raise NameError("这个环境没有基本面数据")
    return ctx.fund


@metric("EPS_TTM", "每股收益", desc="稀释每股收益，最近四季之和（GAAP，SEC 申报日起可用，按拆股调整）", group="基本面")
def _eps(ctx):
    return _fund(ctx).eps_ttm()


@metric("PE", "市盈率", desc="收盘 / EPS_TTM；EPS ≤ 0 时为空", group="基本面")
def _pe(ctx):
    return ctx.metric("收盘") / _nan_if_nonpositive(ctx.metric("EPS_TTM"))


@metric("营收_TTM", desc="营业收入，最近四季之和（SEC）", group="基本面")
def _rev(ctx):
    return _fund(ctx).revenue_ttm()


@metric("稀释股数", desc="最近一季的稀释加权平均股数（SEC，按拆股调整）", group="基本面")
def _dil_shares(ctx):
    return _fund(ctx).diluted_shares()


@metric("总市值", desc="收盘 × 稀释股数", group="基本面")
def _mcap(ctx):
    return ctx.metric("收盘") * ctx.metric("稀释股数")


@metric("PS", "市销率", desc="总市值 / 营收_TTM", group="基本面")
def _ps(ctx):
    return ctx.metric("总市值") / _nan_if_nonpositive(ctx.metric("营收_TTM"))


@metric("净资产", desc="股东权益（最近一期资产负债表，SEC）", group="基本面")
def _equity(ctx):
    return _fund(ctx).equity()


@metric("PB", "市净率", desc="总市值 / 净资产；净资产 ≤ 0 时为空", group="基本面")
def _pb(ctx):
    return ctx.metric("总市值") / _nan_if_nonpositive(ctx.metric("净资产"))


@metric("营收同比", desc="营收_TTM / 一年前（252 个交易日前）的营收_TTM − 1", group="基本面", percent=True)
def _rev_yoy(ctx):
    s = ctx.metric("营收_TTM")
    return s / _nan_if_nonpositive(s.shift(TRADING_DAYS)) - 1


@metric("EPS同比", desc="EPS_TTM / 一年前的 EPS_TTM − 1；一年前 ≤ 0 时为空", group="基本面", percent=True)
def _eps_yoy(ctx):
    s = ctx.metric("EPS_TTM")
    return s / _nan_if_nonpositive(s.shift(TRADING_DAYS)) - 1


@metric("每股股息_TTM", desc="过去 365 天按除息日计的每股现金分红之和（yfinance）", group="股息")
def _dps(ctx):
    d = ctx.bars["dividends"].fillna(0.0)
    return d.rolling("365D").sum()


@metric("股息率", desc="每股股息_TTM / 收盘（过去一年实际派发，不是预测）", group="股息", percent=True)
def _yield(ctx):
    return ctx.metric("每股股息_TTM") / ctx.metric("收盘")


@metric("派息率", desc="每股股息_TTM / EPS_TTM；EPS ≤ 0 时为空", group="股息", percent=True)
def _payout(ctx):
    return ctx.metric("每股股息_TTM") / _nan_if_nonpositive(ctx.metric("EPS_TTM"))


# ------------------------------------------------------------------ 组合层面的求值环境

class PortfolioContext:
    """范围为「组合」的规则：只能用标了 portfolio 的指标，日期以日历标的为准。"""

    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        self.index = portfolio.calendar
        self.symbol = "组合"

    def name(self, ident: str) -> Any:
        return self.call(ident, [], {})

    def call(self, ident: str, args: list, kwargs: dict) -> Any:
        if ident == "大类占比":
            return self.portfolio.category_share(str(args[0]), self.index)
        if ident == "市值":
            if not args:
                raise NameError("组合规则里要写 市值(\"代码\")")
            return self.portfolio.market_value(str(args[0]), self.index)
        if ident == "净值":
            return self.portfolio.nav_series(self.index)
        m = REGISTRY.get(ident)
        if m is not None and m.group == "函数":
            return m.func(self, *args, **kwargs)
        raise NameError(f"组合规则里不能用 {ident}")

    def series(self, value: Any) -> pd.Series:
        if isinstance(value, pd.Series):
            return value
        return pd.Series(float(value), index=self.index)


def is_percent(text: str) -> bool:
    """快照显示用：最外层的指标是不是比例。"""
    head = text.strip().split("(", 1)[0].strip()
    m = REGISTRY.get(head)
    return bool(m and m.percent)

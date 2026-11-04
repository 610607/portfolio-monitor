"""基本面数据：SEC EDGAR XBRL（companyfacts）。

只用公开、可追溯的数据：每个数字都来自公司向 SEC 申报的 10-Q / 10-K。
时点一致：每个事实取「最早申报的那一次」，只从申报日起才算已知，
避免用到当时还没公布、或后来重述的数字。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}

# 概念候选：按顺序取第一个有数据的；同一期间多个候选都有值时取靠前的
EPS_CONCEPTS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
REVENUE_CONCEPTS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenuesNetOfInterestExpense",
    "SalesRevenueNet",
]
SHARES_CONCEPTS = ["WeightedAverageNumberOfDilutedSharesOutstanding"]
EQUITY_CONCEPTS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]


@dataclass
class Fact:
    start: pd.Timestamp | None
    end: pd.Timestamp
    val: float
    filed: pd.Timestamp

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


class SecClient:
    """取 SEC 数据；同一次运行内缓存，另可落盘缓存 12 小时。

    SEC 要求请求头 User-Agent 写明使用者和联系邮箱（例如「portfolio-monitor 某人@example.com」），
    否则返回 403。由环境变量 SEC_USER_AGENT 提供，代码里不写死。
    """

    def __init__(self, user_agent: str | None = None, cache_dir: str | None = None, session=None):
        import requests

        self.ua = user_agent or os.environ.get("SEC_USER_AGENT") or ""
        self.session = session or requests.Session()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._mem: dict[str, dict] = {}
        self._last = 0.0

    def _get_json(self, url: str, key: str) -> dict:
        if key in self._mem:
            return self._mem[key]
        if "@" not in self.ua:
            raise RuntimeError("没有设置 SEC_USER_AGENT（SEC 要求请求头里写联系邮箱）")
        path =self.cache_dir / f"{key}.json" if self.cache_dir else None
        if path and path.exists() and time.time() - path.stat().st_mtime < 12 * 3600:
            data = json.loads(path.read_text())
        else:
            wait = 0.15 - (time.time() - self._last)   # SEC 要求每秒不超过 10 次
            if wait > 0:
                time.sleep(wait)
            r = self.session.get(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip"}, timeout=30)
            self._last = time.time()
            r.raise_for_status()
            data = r.json()
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
        self._mem[key] = data
        return data

    def cik(self, symbol: str) -> int | None:
        table = self._get_json(SEC_TICKERS_URL, "company_tickers")
        sym = symbol.upper().replace(" ", "-").replace(".", "-")
        for row in table.values():
            if row["ticker"].upper() == sym:
                return int(row["cik_str"])
        return None

    def companyfacts(self, cik: int) -> dict:
        return self._get_json(SEC_FACTS_URL.format(cik=cik), f"facts_{cik}")


def point_in_time_facts(companyfacts: dict, concepts: list[str], taxonomy: str = "us-gaap") -> list[Fact]:
    """合并候选概念，每个 (起, 止) 期间取最早申报的值。"""
    best: dict[tuple, tuple[int, Fact]] = {}
    tax = companyfacts.get("facts", {}).get(taxonomy, {})
    for rank, concept in enumerate(concepts):
        units = tax.get(concept, {}).get("units", {})
        for rows in units.values():
            for r in rows:
                if r.get("form") not in FORMS or r.get("val") is None:
                    continue
                f = Fact(
                    start=pd.Timestamp(r["start"]) if r.get("start") else None,
                    end=pd.Timestamp(r["end"]),
                    val=float(r["val"]),
                    filed=pd.Timestamp(r["filed"]),
                )
                k = (f.start, f.end)
                cur = best.get(k)
                if cur is None or (rank, f.filed) < (cur[0], cur[1].filed):
                    best[k] = (rank, f)
    return sorted((f for _, f in best.values()), key=lambda f: (f.end, f.filed))


def split_factor(splits: pd.Series | None, when: pd.Timestamp) -> float:
    """when 之后发生的拆股累计倍数（yfinance 的价格和股息已按拆股调整）。"""
    if splits is None or splits.empty:
        return 1.0
    after = splits[(splits.index > when) & (splits > 0)]
    return float(after.prod()) if len(after) else 1.0


def adjust_for_splits(facts: list[Fact], splits: pd.Series | None, per_share: bool) -> list[Fact]:
    out = []
    for f in facts:
        k = split_factor(splits, f.filed)
        val = f.val / k if per_share else f.val * k
        out.append(Fact(f.start, f.end, val, f.filed))
    return out


def quarterly_values(facts: list[Fact]) -> list[Fact]:
    """从 3 个月、半年、9 个月、全年的累计数里得到单季数值。

    单季直接申报的优先；缺的季度用同一起点的相邻累计数相减（例如 Q4 = 全年 − 前三季）。
    """
    quarters: dict[pd.Timestamp, Fact] = {}
    for f in facts:
        if f.days is not None and 80 <= f.days <= 120:
            quarters.setdefault(f.end, f)
    by_start: dict[pd.Timestamp, list[Fact]] = {}
    for f in facts:
        if f.start is not None and f.days is not None and 80 <= f.days <= 380:
            by_start.setdefault(f.start, []).append(f)
    for start, group in by_start.items():
        group.sort(key=lambda f: f.end)
        for prev, cur in zip(group, group[1:]):
            gap = (cur.end - prev.end).days
            if 80 <= gap <= 120 and cur.end not in quarters:
                quarters[cur.end] = Fact(prev.end, cur.end, cur.val - prev.val, max(prev.filed, cur.filed))
    return sorted(quarters.values(), key=lambda f: f.end)


def ttm_points(facts: list[Fact]) -> list[Fact]:
    """最近四个连续季度之和（TTM）。可用日期取四季中最晚的申报日。"""
    q = quarterly_values(facts)
    out = []
    for i in range(3, len(q)):
        window = q[i - 3: i + 1]
        if all(80 <= (b.end - a.end).days <= 120 for a, b in zip(window, window[1:])):
            out.append(Fact(window[0].start, window[-1].end, sum(f.val for f in window),
                            max(f.filed for f in window)))
    return out


def latest_quarter_points(facts: list[Fact]) -> list[Fact]:
    """单季数值本身（例如季度加权平均股数）。"""
    return quarterly_values(facts)


def instant_points(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if f.start is None]


def asof_series(points: list[Fact], index: pd.DatetimeIndex) -> pd.Series:
    """把「申报日起可用」的数值铺到每个交易日：每天取当时已申报的最新一期。"""
    if not points:
        return pd.Series(float("nan"), index=index)
    df = pd.DataFrame({"filed": pd.to_datetime([p.filed for p in points]).astype("datetime64[ns]"),
                       "end": [p.end for p in points], "val": [p.val for p in points]})
    # 同一天申报多期时，取期末最新的
    df = df.sort_values(["filed", "end"]).drop_duplicates("filed", keep="last")
    # 申报日早于某个更晚期末的，不能覆盖后来的新数据：按申报日顺序保留期末单调递增的
    df["max_end"] = df["end"].cummax()
    df = df[df["end"] >= df["max_end"]]
    left = pd.DataFrame({"date": pd.DatetimeIndex(index).astype("datetime64[ns]")})
    merged = pd.merge_asof(left, df[["filed", "val"]], left_on="date", right_on="filed", direction="backward")
    return pd.Series(merged["val"].to_numpy(), index=index, dtype="float64")


class Fundamentals:
    """一个标的的基本面序列（与行情的交易日对齐）。"""

    def __init__(self, sec: SecClient | None, symbol: str, index: pd.DatetimeIndex, splits: pd.Series | None):
        self.sec, self.symbol, self.index, self.splits = sec, symbol, index, splits
        self._facts: dict | None = None
        self._loaded = False
        self.note: str | None = None
        self._cache: dict[str, pd.Series] = {}

    def _companyfacts(self) -> dict | None:
        if not self._loaded:
            self._loaded = True
            if self.sec is None:
                self.note = "没有配置 SEC 数据源"
                return None
            try:
                cik = self.sec.cik(self.symbol)
                if cik is None:
                    self.note = "SEC 没有这个代码（ETF 或非美国申报公司）"
                    return None
                self._facts = self.sec.companyfacts(cik)
            except Exception as e:  # 网络或数据问题：基本面记为无值，不影响其他指标
                self.note = f"SEC 数据取不到：{e}"
        return self._facts

    def _series(self, key: str, build) -> pd.Series:
        if key not in self._cache:
            cf = self._companyfacts()
            self._cache[key] = build(cf) if cf else pd.Series(float("nan"), index=self.index)
        return self._cache[key]

    def eps_ttm(self) -> pd.Series:
        return self._series("eps", lambda cf: asof_series(
            ttm_points(adjust_for_splits(point_in_time_facts(cf, EPS_CONCEPTS), self.splits, per_share=True)),
            self.index))

    def revenue_ttm(self) -> pd.Series:
        return self._series("rev", lambda cf: asof_series(
            ttm_points(point_in_time_facts(cf, REVENUE_CONCEPTS)), self.index))

    def diluted_shares(self) -> pd.Series:
        return self._series("shares", lambda cf: asof_series(
            latest_quarter_points(adjust_for_splits(point_in_time_facts(cf, SHARES_CONCEPTS), self.splits,
                                                    per_share=False)), self.index))

    def equity(self) -> pd.Series:
        return self._series("equity", lambda cf: asof_series(
            instant_points(point_in_time_facts(cf, EQUITY_CONCEPTS)), self.index))

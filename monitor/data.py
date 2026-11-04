"""行情：yfinance（Yahoo Finance 的日线，含分红和拆股）。"""
from __future__ import annotations

import time

import pandas as pd

COLUMNS = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
           "Dividends": "dividends", "Stock Splits": "splits"}


class DataError(RuntimeError):
    pass


def yahoo_symbol(symbol: str) -> str:
    """IBKR 代码 → Yahoo 代码（BRK B → BRK-B）。"""
    return symbol.strip().upper().replace(" ", "-").replace(".", "-")


def fetch_bars(symbol: str, yahoo: str | None = None, period: str = "10y", retries: int = 1) -> pd.DataFrame:
    """日线：收盘价按拆股调整、不按分红调整（与 IBKR 图表一致）；dividends/splits 列为当天的分红和拆股。"""
    import yfinance as yf

    ysym = yahoo or yahoo_symbol(symbol)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            h = yf.Ticker(ysym).history(period=period, interval="1d", auto_adjust=False, actions=True)
            if h is None or h.empty:
                raise DataError(f"{ysym} 没有数据")
            for col in ("Dividends", "Stock Splits"):
                if col not in h.columns:
                    h[col] = 0.0
            df = h.rename(columns=COLUMNS)[list(COLUMNS.values())].copy()
            df.index = pd.DatetimeIndex(pd.to_datetime([d.date() for d in df.index])).astype("datetime64[ns]")
            df = df[~df.index.duplicated(keep="last")].sort_index()
            df = df.dropna(subset=["close"])
            return df
        except Exception as e:  # yfinance 偶尔限流或失灵，重试一次
            last_err = e
            if attempt < retries:
                time.sleep(3)
    raise DataError(f"{ysym} 行情取不到：{last_err}")


def splits_of(bars: pd.DataFrame) -> pd.Series:
    s = bars.get("splits")
    if s is None:
        return pd.Series(dtype="float64")
    return s[s.fillna(0) > 0].astype("float64")

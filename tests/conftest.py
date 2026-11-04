import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_bars(closes, start="2025-01-02", dividends=None):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype="float64")
    df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                       "volume": 1_000_000.0, "dividends": 0.0, "splits": 0.0}, index=idx)
    if dividends:
        for d, v in dividends.items():
            df.loc[pd.Timestamp(d), "dividends"] = v
    return df


@pytest.fixture
def walk():
    rng = np.random.default_rng(7)
    return 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))


@pytest.fixture
def bars_factory():
    return make_bars

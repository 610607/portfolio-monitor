import json
from pathlib import Path

import pandas as pd

from monitor.fundamentals import (EPS_CONCEPTS, Fact, adjust_for_splits, asof_series, point_in_time_facts,
                                  quarterly_values, ttm_points)

FIX = json.loads((Path(__file__).parent / "fixtures" / "pep_eps_facts.json").read_text())


def test_pep_quarters_and_ttm():
    facts = point_in_time_facts(FIX, EPS_CONCEPTS)
    q = {f.end.date().isoformat(): round(f.val, 2) for f in quarterly_values(facts)}
    # 10-Q 直接申报的单季
    assert q["2026-09-05"] == 2.23 and q["2026-06-13"] == 2.18 and q["2026-03-21"] == 1.70
    # Q4 = 全年 6.00 − 前三季累计 4.15
    assert q["2025-12-27"] == 1.85
    ttm = ttm_points(facts)
    last = ttm[-1]
    assert last.end == pd.Timestamp("2026-09-05")
    assert round(last.val, 2) == 7.96          # 1.85 + 1.70 + 2.18 + 2.23
    assert last.filed == pd.Timestamp("2026-10-08")


def test_point_in_time_uses_filing_date():
    facts = point_in_time_facts(FIX, EPS_CONCEPTS)
    idx = pd.DatetimeIndex(["2026-10-07", "2026-10-08", "2026-10-09"])
    s = asof_series(ttm_points(facts), idx)
    assert s.iloc[0] != s.iloc[2]               # 10-Q 申报前后 TTM 不同
    assert round(s.iloc[1], 2) == 7.96


def test_split_adjustment():
    facts = [Fact(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-03-31"), 4.0, pd.Timestamp("2024-04-20"))]
    splits = pd.Series([2.0], index=[pd.Timestamp("2024-06-10")])
    assert adjust_for_splits(facts, splits, per_share=True)[0].val == 2.0
    assert adjust_for_splits(facts, splits, per_share=False)[0].val == 8.0
    later = [Fact(None, pd.Timestamp("2024-06-30"), 4.0, pd.Timestamp("2024-07-20"))]
    assert adjust_for_splits(later, splits, per_share=True)[0].val == 4.0

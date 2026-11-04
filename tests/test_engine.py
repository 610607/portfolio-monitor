import numpy as np
import pandas as pd
import talib
from conftest import make_bars

from monitor import engine
from monitor.dsl import evaluate_text
from monitor.indicators import TargetContext


def ctx_for(closes, **kw):
    return TargetContext("TST", make_bars(closes), **kw)


def test_talib_passthrough_matches_direct_call(walk):
    ctx = ctx_for(walk)
    ours = evaluate_text("RSI(14)", ctx)
    direct = talib.RSI(np.asarray(walk, dtype="float64"), timeperiod=14)
    assert np.allclose(ours.to_numpy(), direct, equal_nan=True)
    hist = evaluate_text("MACD柱", ctx)
    _, _, h = talib.MACD(np.asarray(walk, dtype="float64"), 12, 26, 9)
    assert np.allclose(hist.to_numpy(), h, equal_nan=True)
    upper = evaluate_text("BBANDS_upperband(20, 2, 2)", ctx)
    u, _, _ = talib.BBANDS(np.asarray(walk, dtype="float64"), 20, 2, 2)
    assert np.allclose(upper.to_numpy(), u, equal_nan=True)


def test_drawdown_and_new_low():
    closes = [100.0] * 30 + [90.0] + [95.0] * 5 + [89.0]
    ctx = ctx_for(closes)
    dd = evaluate_text("距52周高", ctx)
    assert abs(dd.iloc[30] - (-0.10)) < 1e-12
    low = evaluate_text("创52周新低", ctx)
    assert bool(low.iloc[30]) is True
    # 6 天后再创新低，但 20 个交易日内已出现过，不再算
    assert bool(low.iloc[-1]) is False


def test_percentile_rank():
    closes = list(range(1, 301))
    ctx = ctx_for(closes)
    p = evaluate_text("分位(收盘, 1)", ctx)
    assert p.iloc[-1] == 1.0
    assert np.isnan(p.iloc[100])          # 不足一年


def fake_fetch(table):
    def fetch(symbol, yahoo=None):
        return table[symbol]
    return fetch


def base_input(**kw):
    inp = {"date": "2025-02-10", "calendar": "VT", "nav": 10000, "nav_prev": 10000, "targets": []}
    inp.update(kw)
    return inp


def test_edge_vs_level_and_actions():
    n = 28
    vt = make_bars([100.0] * (n - 2) + [91.0, 89.0])           # 倒数第二天跌破 -9%，最后一天 -11%
    sgov = make_bars([100.0] * n)
    pep = make_bars([130.0] * (n - 1) + [122.0])
    table = {"VT": vt, "SGOV": sgov, "PEP": pep}
    date = str(vt.index[-1].date())
    inp = base_input(date=date, targets=[
        {"symbol": "VT", "category": "核心指数", "position": 10},
        {"symbol": "SGOV", "category": "现金储备", "position": 30},
        {"symbol": "PEP", "category": "个股", "position": 5, "avg_cost": 140,
         "conditions": "加仓 | 收盘 < 123 且 [无新利空] | 提前补齐"},
    ], rules="""
VT | 加仓 | 距52周高 <= -10% | 第一档：动用 SGOV 的 1/3，约 ${市值("SGOV")/3:,.0f}
个股 | 复核 | 单日 <= -5% | 复核论点 | 每天
核心指数 | 信息 | 距52周高 <= -5% | 回撤跨过 -5%
组合 | 减仓或停买 | 大类占比("个股") > 5% | 按第三节先用定投资金调整
""")
    res = engine.run(inp, fetch=fake_fetch(table))
    assert res["trading_day"] and not res["errors"]
    got = {(t["group"], t["symbol"], t["condition"]) for t in res["triggers"]}
    # VT 今天首次 <= -10%：报；<= -5% 昨天已成立：不报
    assert ("加仓", "VT", "距52周高 <= -10%") in got
    assert ("信息", "VT", "距52周高 <= -5%") not in got
    vt_trig = next(t for t in res["triggers"] if t["symbol"] == "VT")
    assert vt_trig["action"].endswith("约 $1,000")
    # PEP 今天跌破 123：带待核对项
    pep_trig = next(t for t in res["triggers"] if t["group"] == "加仓" and t["symbol"] == "PEP")
    assert pep_trig["checks"] == ["无新利空"]
    assert pep_trig["values"]["收盘"] == "122.00"
    # 单日 -6.15%，每天模式
    assert ("复核", "PEP", "单日 <= -5%") in got
    # 组合：PEP 市值 610 / 10000 = 6.1%，昨天 650 / 10000 = 6.5% → 昨天已成立，不报
    assert not any(t["symbol"] == "组合" for t in res["triggers"])
    assert res["triggers"][0]["symbol"] == "VT"


def test_caps_cost_basis_and_errors():
    n = 10
    rk = make_bars([10.0] * (n - 1) + [10.5])
    table = {"VT": make_bars([100.0] * n), "RKLB": rk}
    date = str(rk.index[-1].date())
    inp = base_input(date=date, nav=1000, nav_prev=990, targets=[
        {"symbol": "VT", "category": "核心指数", "position": 1},
        {"symbol": "RKLB", "category": "个股", "position": 11, "avg_cost": 9.5, "buy_qty_today": 1,
         "buy_cost_today": 10, "cap_pct": 10, "cap_basis": "投入",
         "conditions": "信息 | 不存在的指标 > 1 | x"},
    ], rules="""
全部 | 减仓或停买 | 上限占比 > 上限 | 按第六节停止加仓
全部 | 减仓或停买 | 按市值计 且 占净值 > 上限 * 1.25 | 减回上限
""")
    res = engine.run(inp, fetch=fake_fetch(table))
    # 投入：今天 11 × 9.5 = 104.5 / 1000 = 10.45% > 10%；昨天 94.5 / 990 = 9.5% → 首次超过
    groups = [(t["symbol"], t["action"]) for t in res["triggers"]]
    assert ("RKLB", "按第六节停止加仓") in groups
    assert ("RKLB", "减回上限") not in groups          # 投入口径不按市值减仓
    assert any("不认识的指标" in e for e in res["errors"])


def test_missing_fundamentals_give_note_not_trigger():
    n = 10
    table = {"VT": make_bars([100.0] * n)}
    date = str(table["VT"].index[-1].date())
    inp = base_input(date=date, targets=[{"symbol": "VT", "category": "核心指数", "conditions": "信息 | PE < 20 | x"}])
    res = engine.run(inp, fetch=fake_fetch(table), sec=None)
    assert not res["triggers"]
    assert any("PE 今天没有值" in n for n in res["notes"]) and len(res["notes"]) == 1


def test_dividend_yield_trailing():
    closes = [50.0] * 300
    bars = make_bars(closes, dividends={"2025-03-03": 0.5, "2025-06-02": 0.5, "2025-09-01": 0.5})
    ctx = TargetContext("O", bars)
    y = evaluate_text("股息率", ctx)
    assert abs(y.iloc[-1] - 1.5 / 50) < 1e-12


def test_market_closed_skips_rules():
    vt = make_bars([100.0] * 9 + [80.0])
    inp = base_input(date=str((vt.index[-1] + pd.Timedelta(days=1)).date()),
                     targets=[{"symbol": "VT", "category": "核心指数", "position": 1}],
                     rules="VT | 加仓 | 单日 <= -10% | x | 每天")
    res = engine.run(inp, fetch=fake_fetch({"VT": vt}))
    assert res["trading_day"] is False
    assert res["triggers"] == []                       # 不重复报上一个交易日的触发
    assert res["snapshot"]["rows"]["VT"]["收盘"] == 80.0


def test_recent_dividends_and_cut_flag():
    bars = make_bars([50.0] * 300, dividends={"2025-03-03": 0.5, "2025-06-02": 0.52, "2025-09-01": 0.51,
                                              "2025-12-01": 0.40})
    vt = make_bars([100.0] * 300)
    date = "2025-12-05"
    inp = base_input(date=date, targets=[{"symbol": "VT", "category": "核心指数"},
                                         {"symbol": "O", "category": "个股", "position": 10, "avg_cost": 50}])
    res = engine.run(inp, fetch=fake_fetch({"VT": vt, "O": bars}))
    assert res["dividends"] == [{"symbol": "O", "ex_date": "2025-12-01", "amount": 0.4, "prev3_min": 0.5,
                                 "below_prev3": True}]
    assert res["categories"]["个股"]["market_value"] == 500.0


def test_same_condition_in_two_groups_kept_once():
    vt = make_bars([100.0] * 20 + [89.0])
    inp = base_input(date=str(vt.index[-1].date()), targets=[{"symbol": "VT", "category": "核心指数"}],
                     rules="核心指数 | 信息 | 距52周高 <= -10% | 回撤跨过 −10%\nVT | 加仓 | 距52周高 <= -10% | 大盘第一档")
    res = engine.run(inp, fetch=fake_fetch({"VT": vt}))
    assert [(t["group"], t["action"]) for t in res["triggers"]] == [("加仓", "大盘第一档")]

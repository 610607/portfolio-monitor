import pandas as pd
import pytest

from monitor.dsl import Evaluator, RuleError, compile_condition, parse_rules


class Env:
    def __init__(self, **values):
        self.values = values

    def name(self, ident):
        return self.values[ident]

    def call(self, ident, args, kwargs):
        return self.values[ident](*args, **kwargs)


def ev(text, env):
    expr, tree, _ = compile_condition(text)
    return Evaluator(env, expr).evaluate(tree)


def test_chinese_words_percent_and_fullwidth():
    expr, _, _ = compile_condition("距52周高 ≤ -10% 且 （单日 ＜ -3％ 或 非 持有）")
    assert "and" in expr and "or" in expr and "not" in expr
    assert "(10/100)" in expr and "<=" in expr


def test_single_equals_becomes_comparison():
    expr, _, _ = compile_condition("持股 = 0")
    assert "==" in expr


def test_series_logic_and_chained_compare():
    idx = pd.RangeIndex(3)
    env = Env(a=pd.Series([1.0, 5.0, 9.0], index=idx), b=pd.Series([0.0, 0.0, 1.0], index=idx))
    r = ev("2 < a < 8 且 b == 0", env)
    assert list(r) == [False, True, False]
    r = ev("a > 8 或 b > 0", env)
    assert list(r) == [False, False, True]
    r = ev("非 (a > 4)", env)
    assert list(r) == [True, False, False]


def test_nan_compares_false():
    env = Env(x=pd.Series([float("nan"), 1.0]))
    assert list(ev("x < 5", env)) == [False, True]
    assert list(ev("非 (x < 5)", env)) == [True, False]


def test_check_brackets_only_top_level_and():
    _, _, checks = compile_condition("收盘 < 123 且 [无新利空]")
    assert checks == ["无新利空"]
    with pytest.raises(RuleError):
        compile_condition("收盘 < 123 或 [无新利空]")
    with pytest.raises(RuleError):
        compile_condition("[无新利空]")


@pytest.mark.parametrize("bad", [
    "__import__('os').system('ls')",
    "收盘.real > 1",
    "(lambda: 1)() > 0",
    "收盘[0] > 1",
    "'abc' > 1",
])
def test_rejects_unsafe_syntax(bad):
    with pytest.raises(RuleError):
        compile_condition(bad)


def test_parse_rules_formats_and_errors():
    text = """
    # 注释
    加仓 | 收盘 < 123 且 [无新利空] | 提前补齐
    复核 | 单日 <= -8% | 复核论点 | 每天
    买入 | 收盘 < 1 | x
    信息 | 收盘 <
    """
    rules, errors = parse_rules(text, with_scope=False, source="t")
    assert [r.group for r in rules] == ["加仓", "复核"]
    assert rules[1].mode == "level" and rules[0].mode == "edge"
    assert len(errors) == 2


def test_parse_policy_rules_need_scope():
    rules, errors = parse_rules("个股 | 复核 | 单日 <= -8% | 复核 | 每天\n组合 | 信息 | 大类占比(\"股息\") > 20% | x",
                                with_scope=True, source="p")
    assert not errors
    assert rules[0].scope == "个股" and rules[1].scope == "组合"

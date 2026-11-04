"""条件语言：把 Notion 里写的规则行解析成可求值的表达式。

规则行格式
    标的库「监控条件」：组 | 条件 | 动作 [| 每天]
    投资政策规则块：    范围 | 组 | 条件 | 动作 [| 每天]

条件用 Python 自带的解析器（ast）读成语法树，再按白名单逐个节点求值；
不调用 eval/exec，属性访问、下标、lambda、import 之类一律拒绝。
"""
from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

GROUPS = ("加仓", "减仓或停买", "复核", "信息")
MODES = {"": "edge", "新成立": "edge", "每天": "level"}

_CHAR_MAP = {
    "（": "(", "）": ")", "，": ",", "＜": "<", "＞": ">", "≤": "<=", "≥": ">=",
    "≠": "!=", "＝": "=", "％": "%", "－": "-", "＋": "+", "×": "*", "÷": "/",
    "“": '"', "”": '"', "‘": "'", "’": "'", "　": " ", "｜": "|", "【": "[", "】": "]",
}
_WORDS = [("并且", " and "), ("或者", " or "), ("且", " and "), ("或", " or "), ("非", " not ")]
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_SINGLE_EQ = re.compile(r"(?<![<>!=])=(?!=)")
_CHECK = re.compile(r"\[([^\[\]]+)\]")
_CHECK_NAME = "__check{}"

_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Lt, ast.LtE, ast.Gt,
    ast.GtE, ast.Eq, ast.NotEq, ast.Call, ast.Name, ast.Load, ast.Constant, ast.keyword,
)


class RuleError(ValueError):
    """规则写法有误。"""


@dataclass
class Rule:
    group: str
    condition: str                  # 原文
    action: str
    mode: str = "edge"              # edge：今天成立且上一交易日不成立才报；level：今天成立就报
    scope: str | None = None        # 投资政策规则的范围：全部 / 类别 / 代码 / 组合
    source: str = ""
    checks: list[str] = field(default_factory=list)   # 方括号里的人工核对项
    expr: str = ""
    tree: ast.Expression | None = None


def normalize(text: str) -> str:
    for a, b in _CHAR_MAP.items():
        text = text.replace(a, b)
    return text


def compile_condition(condition: str) -> tuple[str, ast.Expression, list[str]]:
    """把条件原文转成 Python 表达式和语法树；返回 (表达式, 语法树, 人工核对项)。"""
    s = normalize(condition).strip()
    if not s:
        raise RuleError("条件为空")
    checks: list[str] = []

    def _take(m: re.Match) -> str:
        checks.append(m.group(1).strip())
        return " " + _CHECK_NAME.format(len(checks) - 1) + " "

    s = _CHECK.sub(_take, s)
    for word, rep in _WORDS:
        s = s.replace(word, rep)
    s = _PERCENT.sub(r"(\1/100)", s)
    s = _SINGLE_EQ.sub("==", s).strip()
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise RuleError(f"语法错误：{condition}") from e
    _validate(tree, len(checks))
    return s, tree, checks


def _validate(tree: ast.Expression, n_checks: int) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise RuleError(f"不支持的写法：{type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise RuleError("函数名只能是指标名")
            for kw in node.keywords:
                if kw.arg is None:
                    raise RuleError("不支持 ** 参数")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool, str)):
            raise RuleError(f"不支持的常量：{node.value!r}")
    # 字符串只能作函数参数
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if not isinstance(node, (ast.Call, ast.keyword)):
                    raise RuleError("文字只能写在函数参数里，例如 大类占比(\"股息\")")
    if n_checks:
        body = tree.body
        top = body.values if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And) else [body]
        names = {_CHECK_NAME.format(i) for i in range(n_checks)}
        top_names = {v.id for v in top if isinstance(v, ast.Name)}
        if names - top_names:
            raise RuleError("方括号核对项只能用「且」接在条件最外层")
        if all(isinstance(v, ast.Name) and v.id in names for v in top):
            raise RuleError("条件不能只有方括号核对项")


def parse_rules(text: str | list[str] | None, *, with_scope: bool, source: str) -> tuple[list[Rule], list[str]]:
    """逐行解析规则；返回 (规则列表, 错误列表)。空行和 # 开头的行忽略。"""
    if not text:
        return [], []
    lines = text if isinstance(text, list) else text.splitlines()
    rules: list[Rule] = []
    errors: list[str] = []
    for raw in lines:
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue
        # 只按竖线分栏；全角符号的替换只用在条件栏，动作栏原样保留
        parts = [p.strip() for p in re.split(r"[|｜]", line)]
        try:
            rules.append(_parse_line(parts, with_scope, source))
        except RuleError as e:
            errors.append(f"{source}：{line} → {e}")
    return rules, errors


def _parse_line(parts: list[str], with_scope: bool, source: str) -> Rule:
    need = 4 if with_scope else 3
    if len(parts) not in (need - 1, need, need + 1):
        fmt = "范围 | 组 | 条件 | 动作 [| 每天]" if with_scope else "组 | 条件 | 动作 [| 每天]"
        raise RuleError(f"字段数不对，格式是 {fmt}")
    scope = parts.pop(0) if with_scope else None
    if with_scope and not scope:
        raise RuleError("范围为空")
    group = parts[0]
    if group not in GROUPS:
        raise RuleError(f"组只能是 {' / '.join(GROUPS)}")
    condition = parts[1] if len(parts) > 1 else ""
    action = parts[2] if len(parts) > 2 else ""
    mode_word = parts[3] if len(parts) > 3 else ""
    if mode_word not in MODES:
        raise RuleError("最后一栏只能是「每天」或留空")
    expr, tree, checks = compile_condition(condition)
    return Rule(group=group, condition=condition, action=action, mode=MODES[mode_word],
                scope=scope, source=source, checks=checks, expr=expr, tree=tree)


# ---------------------------------------------------------------- 求值

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
}
_CMPOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


def as_bool(x: Any) -> Any:
    if isinstance(x, pd.Series):
        return x.astype("boolean").fillna(False).astype(bool)
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return False
    return bool(x)


class Evaluator:
    """在一个求值环境里计算语法树。

    env 需要提供：
        name(ident) -> 值           无参数指标或常量
        call(ident, args, kwargs) -> 值
    值可以是 pandas.Series（按日期对齐）或标量。
    """

    def __init__(self, env: Any, source: str = ""):
        self.env = env
        self.source = source
        self.terms: dict[str, Any] = {}   # 条件里用到的指标原文 → 值（序列或标量）

    def evaluate(self, tree: ast.Expression) -> Any:
        return self._eval(tree.body)

    def _record(self, node: ast.AST, value: Any) -> None:
        seg = ast.get_source_segment(self.source, node) if self.source else None
        if seg and not seg.startswith("__check"):
            self.terms[seg] = value

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id.startswith("__check"):
                return True
            value = self.env.name(node.id)
            self._record(node, value)
            return value
        if isinstance(node, ast.Call):
            args = [self._eval(a) for a in node.args]
            kwargs = {k.arg: self._eval(k.value) for k in node.keywords}
            value = self.env.call(node.func.id, args, kwargs)
            self._record(node, value)
            return value
        if isinstance(node, ast.BoolOp):
            vals = [as_bool(self._eval(v)) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                b = as_bool(v)
                return ~b if isinstance(b, pd.Series) else (not b)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left), self._eval(node.right)
            try:
                return _BINOPS[type(node.op)](a, b)
            except ZeroDivisionError:
                return float("nan")
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            out = None
            for op, comp in zip(node.ops, node.comparators):
                right = self._eval(comp)
                r = as_bool(_CMPOPS[type(op)](left, right))
                out = r if out is None else (out & r)
                left = right
            return out
        raise RuleError(f"不支持的写法：{type(node).__name__}")


def evaluate_text(text: str, env: Any) -> Any:
    """求一个表达式（不要求是条件），用于动作里的 {占位} 和指标快照。"""
    expr, tree, _ = compile_condition(text)
    return Evaluator(env, expr).evaluate(tree)

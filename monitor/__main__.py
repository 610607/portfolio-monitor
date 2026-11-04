"""命令行入口。

    python -m monitor run input.json [--out result.json] [--cache DIR]
    python -m monitor eval PEP "RSI(14)" "PE" "分位(PE, 5)"
    python -m monitor check input.json
    python -m monitor names [--talib]
"""
from __future__ import annotations

import argparse
import json
import sys

from . import engine
from .dsl import parse_rules
from .indicators import ALIASES, METRICS, talib_names


def _sec(cache: str | None):
    from .fundamentals import SecClient
    return SecClient(cache_dir=cache)


def cmd_run(a) -> int:
    inp = json.load(open(a.input, encoding="utf-8"))
    res = engine.run(inp, sec=_sec(a.cache))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    print(engine.render_markdown(res))
    return 0


def cmd_eval(a) -> int:
    from .data import fetch_bars, splits_of
    from .dsl import evaluate_text
    from .fundamentals import Fundamentals
    from .indicators import TargetContext

    bars = fetch_bars(a.symbol)
    ctx = TargetContext(a.symbol, bars, fundamentals=Fundamentals(_sec(a.cache), a.symbol.upper(), bars.index,
                                                                    splits_of(bars)))
    d0, d1 = bars.index[-1].date(), bars.index[-2].date()
    print(f"{a.symbol.upper()}  {d1} → {d0}")
    for expr in a.exprs:
        try:
            v = evaluate_text(expr, ctx)
            print(f"  {expr}: {engine.fmt(expr, engine.at(v, -2))} → {engine.fmt(expr, engine.at(v, -1))}")
        except Exception as e:
            print(f"  {expr}: 无法计算（{e}）")
    if ctx.fund and ctx.fund.note:
        print(f"  基本面：{ctx.fund.note}")
    return 0


def cmd_check(a) -> int:
    inp = json.load(open(a.input, encoding="utf-8"))
    rules, errors = parse_rules(inp.get("rules"), with_scope=True, source="投资政策")
    for t in inp.get("targets", []):
        r, e = parse_rules(t.get("conditions"), with_scope=False, source=f"标的库 {t['symbol']}")
        rules += r
        errors += e
    for r in rules:
        bad = engine.unknown_names(r)
        if bad:
            errors.append(f"{r.source}：「{r.condition}」里有不认识的指标：{'、'.join(bad)}")
    print(f"共 {len(rules)} 条规则，{len(errors)} 个问题")
    for e in errors:
        print(f"- {e}")
    return 1 if errors else 0


def cmd_names(a) -> int:
    group = None
    for m in METRICS:
        if m.group != group:
            group = m.group
            print(f"\n## {group}")
        print(f"- {' / '.join(m.names)}：{m.desc}")
    print("\n## TA-Lib 中文别名")
    for k, (f, o) in ALIASES.items():
        print(f"- {k} = {f}{'_' + o if o else ''}")
    names = talib_names()
    print(f"\n## TA-Lib（{len(names)} 个函数，按原名使用，多输出写 函数_输出名）")
    if a.talib:
        from talib import abstract
        for n in names:
            info = abstract.Function(n).info
            params = ", ".join(f"{k}={v}" for k, v in info["parameters"].items())
            outs = info["output_names"]
            out = "" if len(outs) == 1 else f"  输出：{', '.join(outs)}"
            print(f"- {n}({params})：{info['display_name']}{out}")
    else:
        print("、".join(names))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="按输入文件跑一次监控")
    r.add_argument("input")
    r.add_argument("--out")
    r.add_argument("--cache", help="SEC 数据缓存目录")
    r.set_defaults(func=cmd_run)
    e = sub.add_parser("eval", help="看某个标的的指标值")
    e.add_argument("symbol")
    e.add_argument("exprs", nargs="+")
    e.add_argument("--cache")
    e.set_defaults(func=cmd_eval)
    c = sub.add_parser("check", help="只检查规则写法")
    c.add_argument("input")
    c.set_defaults(func=cmd_check)
    n = sub.add_parser("names", help="列出可用指标")
    n.add_argument("--talib", action="store_true", help="列出 TA-Lib 每个函数的参数和输出")
    n.set_defaults(func=cmd_names)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())

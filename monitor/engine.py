"""一次监控：取数 → 算指标 → 判断规则 → 输出触发项和指标快照。"""
from __future__ import annotations

import ast
import math
import re
from typing import Any, Callable

import pandas as pd

from .data import fetch_bars as default_fetch, splits_of
from .dsl import GROUPS, Evaluator, Rule, RuleError, evaluate_text, parse_rules
from .fundamentals import Fundamentals, SecClient
from .indicators import (REGISTRY, Portfolio, PortfolioContext, Position, TargetContext, is_percent,
                         resolve_talib)

DEFAULT_SNAPSHOT = ["收盘", "单日", "距52周高", "市值", "占净值", "相对成本", "RSI(14)", "PE", "分位(PE, 5)", "股息率"]
_PLACEHOLDER = re.compile(r"\{([^{}:]+)(?::([^{}]+))?\}")


def _num(x: Any, default: float = float("nan")) -> float:
    try:
        return float(x) if x is not None and x != "" else default
    except (TypeError, ValueError):
        return default


def at(value: Any, i: int = -1) -> Any:
    if isinstance(value, pd.Series):
        if len(value) < abs(i):
            return float("nan")
        v = value.iloc[i]
        return bool(v) if str(value.dtype) == "bool" else v
    return value


def is_missing(v: Any) -> bool:
    return v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v))


def is_holding_term(term: str) -> bool:
    head = term.strip().split("(", 1)[0].strip()
    m = REGISTRY.get(head)
    return bool(m and m.group == "持仓")


def truthy(v: Any) -> bool:
    return False if is_missing(v) else bool(v)


def fmt(term: str, v: Any, spec: str | None = None) -> str:
    if is_missing(v):
        return "—"
    if isinstance(v, (bool,)) or type(v).__name__ == "bool_":
        return "是" if v else "否"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if spec:
        return format(v, spec)
    if is_percent(term):
        return f"{v:.2%}"
    return f"{v:,.2f}"


def unknown_names(rule_or_expr: Rule | str) -> list[str]:
    """静态检查：条件里有没有不认识的指标名。"""
    if isinstance(rule_or_expr, Rule):
        tree = rule_or_expr.tree
    else:
        from .dsl import compile_condition
        tree = compile_condition(rule_or_expr)[1]
    bad = []
    for node in ast.walk(tree):
        ident = node.func.id if isinstance(node, ast.Call) else node.id if isinstance(node, ast.Name) else None
        if ident is None or ident.startswith("__check"):
            continue
        if ident not in REGISTRY and resolve_talib(ident) is None:
            bad.append(ident)
    return sorted(set(bad))


class Run:
    def __init__(self, inp: dict, fetch: Callable = default_fetch, sec: SecClient | None = None):
        self.inp = inp
        self.fetch = fetch
        self.sec = sec
        self.date = pd.Timestamp(inp["date"])
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.triggers: list[dict] = []
        self.portfolio = Portfolio(nav=_num(inp.get("nav")), nav_prev=_num(inp.get("nav_prev")))
        self.contexts: dict[str, TargetContext] = {}
        self.target_rules: dict[str, list[Rule]] = {}

    # ---------------------------------------------------------------- 取数
    def load(self) -> None:
        for t in self.inp.get("targets", []):
            sym = str(t["symbol"]).upper()
            rules, errs = parse_rules(t.get("conditions"), with_scope=False, source=f"标的库 {sym}")
            self.errors += errs
            self.target_rules[sym] = rules
            try:
                bars = self.fetch(sym, t.get("yahoo"))
            except Exception as e:
                self.errors.append(f"{sym}：{e}")
                continue
            bars = bars[bars.index <= self.date]
            if len(bars) < 2:
                self.errors.append(f"{sym}：行情不足两个交易日")
                continue
            cap_pct = _num(t.get("cap_pct"))
            pos = Position(
                position=_num(t.get("position")), avg_cost=_num(t.get("avg_cost")),
                buy_qty_today=_num(t.get("buy_qty_today"), 0.0), buy_cost_today=_num(t.get("buy_cost_today"), 0.0),
                sell_qty_today=_num(t.get("sell_qty_today"), 0.0),
                cap=cap_pct / 100 if not math.isnan(cap_pct) else float("nan"),
                cap_basis=t.get("cap_basis") or "市值",
            )
            fund = Fundamentals(self.sec, sym, bars.index, splits_of(bars))
            ctx = TargetContext(sym, bars, category=t.get("category", ""), status=t.get("status", ""),
                                position=pos, portfolio=self.portfolio, fundamentals=fund)
            self.contexts[sym] = ctx
            self.portfolio.targets[sym] = ctx

    # ---------------------------------------------------------------- 判断
    def evaluate(self) -> None:
        policy, errs = parse_rules(self.inp.get("rules"), with_scope=True, source="投资政策")
        self.errors += errs
        known_scopes = {"全部", "组合"} | {c.category for c in self.contexts.values()} | set(self.contexts)
        for r in policy:
            if r.scope not in known_scopes:
                self.notes.append(f"投资政策规则的范围「{r.scope}」没有对应的标的：{r.condition}")
        for sym, ctx in self.contexts.items():
            rules = [r for r in policy if r.scope in ("全部", ctx.category, sym)] + self.target_rules.get(sym, [])
            for r in rules:
                self._check(r, ctx, sym)
        if self.portfolio.calendar is not None:
            pctx = PortfolioContext(self.portfolio)
            for r in policy:
                if r.scope == "组合":
                    self._check(r, pctx, "组合")

    def _check(self, rule: Rule, env: Any, sym: str) -> None:
        ev = Evaluator(env, rule.expr)
        try:
            res = ev.evaluate(rule.tree)
        except Exception as e:  # 不认识的指标、数据不够、参数不对：记为规则问题，不影响其他规则
            self.errors.append(f"{sym}：「{rule.condition}」（{rule.source}）无法计算：{e}")
            return
        today, prev = truthy(at(res, -1)), truthy(at(res, -2))
        values = {term: at(v, -1) for term, v in ev.terms.items()}
        # 持仓类（上限、成本、占净值……）没有值只说明不适用，比如没持有、没设上限；数据类没有值才提示
        missing = [term for term, v in values.items() if is_missing(v) and not is_holding_term(term)]
        if missing:
            fund = getattr(env, "fund", None)
            why = f"（{fund.note}）" if fund is not None and fund.note else ""
            self.notes.append(f"{sym}：「{rule.condition}」无法判断，{'、'.join(missing)} 今天没有值{why}")
        if not today or (rule.mode == "edge" and prev):
            return
        self.triggers.append({
            "group": rule.group, "symbol": sym, "condition": rule.condition,
            "action": self._render(rule.action, env, sym),
            "checks": rule.checks, "mode": "每天" if rule.mode == "level" else "新成立",
            "source": rule.source,
            "values": {term: fmt(term, v) for term, v in values.items()},
        })

    def _render(self, action: str, env: Any, sym: str) -> str:
        def rep(m: re.Match) -> str:
            expr, spec = m.group(1), m.group(2)
            try:
                return fmt(expr, at(evaluate_text(expr, env), -1), spec)
            except Exception as e:
                self.errors.append(f"{sym}：动作里的「{{{expr}}}」无法计算：{e}")
                return "{?}"
        return _PLACEHOLDER.sub(rep, action or "")

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict:
        cols = self.inp.get("snapshot") or DEFAULT_SNAPSHOT
        rows = {}
        for sym, ctx in self.contexts.items():
            row = {}
            for c in cols:
                try:
                    v = at(evaluate_text(c, ctx), -1)
                except Exception:
                    v = float("nan")
                row[c] = None if is_missing(v) else (bool(v) if type(v).__name__ in ("bool", "bool_") else float(v))
            rows[sym] = row
        return {"columns": cols, "rows": rows}

    def categories(self) -> dict:
        """各类别今天的市值和占净值（持股 × 收盘）。"""
        out: dict[str, dict] = {}
        nav = self.portfolio.nav
        for ctx in self.contexts.values():
            mv = at(ctx.metric("市值"), -1)
            if is_missing(mv) or not ctx.category:
                continue
            c = out.setdefault(ctx.category, {"market_value": 0.0})
            c["market_value"] += float(mv)
        for c in out.values():
            c["share"] = c["market_value"] / nav if nav and not math.isnan(nav) else None
        return out

    def dividends(self, days: int = 10) -> list[dict]:
        """除息日在今天往前 days 个自然日内的现金分红（yfinance 按除息日记录；同日多笔已合并）。"""
        out = []
        start = self.date - pd.Timedelta(days=days)
        for sym, ctx in self.contexts.items():
            d = ctx.bars["dividends"].fillna(0.0)
            d = d[d > 0]
            for ex, amt in d[(d.index > start) & (d.index <= self.date)].items():
                prev = d[d.index < ex].iloc[-3:]
                out.append({
                    "symbol": sym, "ex_date": str(ex.date()), "amount": round(float(amt), 6),
                    "prev3_min": round(float(prev.min()), 6) if len(prev) == 3 else None,
                    "below_prev3": bool(len(prev) == 3 and amt < prev.min() - 1e-9),
                })
        return out

    # ---------------------------------------------------------------- 主流程
    def run(self) -> dict:
        self.load()
        cal_sym = str(self.inp.get("calendar") or "VT").upper()
        cal = self.contexts.get(cal_sym) or next(iter(self.contexts.values()), None)
        if cal is None:
            return self._result(None, False)
        if cal.symbol != cal_sym:
            self.notes.append(f"日历标的 {cal_sym} 没有行情，改用 {cal.symbol} 判断交易日")
        self.portfolio.calendar = cal.index
        last = cal.index[-1]
        trading_day = bool(last == self.date)
        stale = [s for s, c in self.contexts.items() if c.index[-1] < last]
        for s in stale:
            self.notes.append(f"{s}：最新行情停在 {self.contexts[s].index[-1].date()}，比 {cal.symbol} 旧")
        if trading_day:
            self.evaluate()
        else:  # 休市：最后两根日线就是上一次运行看过的，再判断会重复报
            self.notes.append(f"{self.date.date()} 没有新日线（休市），未判断规则；快照用 {last.date()} 收盘")
        return self._result(last, trading_day)

    def _result(self, last, trading_day: bool) -> dict:
        order = {g: i for i, g in enumerate(GROUPS)}
        cal_sym = str(self.inp.get("calendar") or "VT").upper()
        self.triggers.sort(key=lambda t: (order[t["group"]], t["symbol"] != cal_sym, t["symbol"]))
        # 同一标的、同一条件被几组规则同时命中（例如 VT 既是大盘信号又是核心指数），只留排在前面的组
        seen: set[tuple[str, str]] = set()
        kept = []
        for t in self.triggers:
            key = (t["symbol"], t["condition"].replace(" ", ""))
            if key not in seen:
                seen.add(key)
                kept.append(t)
        self.triggers = kept
        empty = last is None
        return {
            "date": str(self.date.date()),
            "data_date": None if empty else str(last.date()),
            "trading_day": trading_day,
            "triggers": self.triggers,
            "errors": list(dict.fromkeys(self.errors)),
            "notes": list(dict.fromkeys(self.notes)),
            "categories": {} if empty else self.categories(),
            "dividends": [] if empty else self.dividends(),
            "snapshot": {"columns": [], "rows": {}} if empty else self.snapshot(),
        }


def run(inp: dict, fetch: Callable = default_fetch, sec: SecClient | None = None) -> dict:
    return Run(inp, fetch=fetch, sec=sec).run()


def render_markdown(res: dict) -> str:
    out = [f"# 监控结果 {res['date']}"]
    if res["data_date"] is None:
        out.append("没有取到任何行情。")
    else:
        day = "交易日" if res["trading_day"] else "非交易日（今天没有新日线）"
        out.append(f"行情截至 {res['data_date']}，{day}。")
    trig = res["triggers"]
    if trig:
        counts = " · ".join(f"{g} {sum(t['group'] == g for t in trig)}" for g in GROUPS
                            if any(t["group"] == g for t in trig))
        out.append(f"触发：{counts}")
        for g in GROUPS:
            items = [t for t in trig if t["group"] == g]
            if not items:
                continue
            out.append(f"\n## {g}")
            for t in items:
                vals = " · ".join(f"{k} {v}" for k, v in t["values"].items())
                line = f"- {t['symbol']}｜{t['condition']}｜{t['action'] or '政策未规定动作'}｜{vals}"
                if t["checks"]:
                    line += f"｜待核对：{'、'.join(t['checks'])}"
                out.append(line)
    else:
        out.append("今日无触发。")
    if res["errors"]:
        out.append("\n## 规则或数据问题")
        out += [f"- {e}" for e in res["errors"]]
    if res["notes"]:
        out.append("\n## 提示")
        out += [f"- {n}" for n in res["notes"]]
    if res.get("dividends"):
        out.append("\n## 近 10 天除息")
        for d in res["dividends"]:
            cut = "，低于此前三次的最小值" if d["below_prev3"] else ""
            out.append(f"- {d['symbol']} {d['ex_date']} 每股 {d['amount']}{cut}")
    if res.get("categories"):
        out.append("\n## 各类别")
        for name, c in res["categories"].items():
            share = "—" if c["share"] is None else f"{c['share']:.2%}"
            out.append(f"- {name}：市值 {c['market_value']:,.2f}，占净值 {share}")
    snap = res["snapshot"]
    if snap["rows"]:
        cols = snap["columns"]
        out.append("\n## 指标快照")
        out.append("| 代码 | " + " | ".join(cols) + " |")
        out.append("|" + " --- |" * (len(cols) + 1))
        for sym, row in snap["rows"].items():
            out.append(f"| {sym} | " + " | ".join(fmt(c, row[c]) for c in cols) + " |")
    return "\n".join(out)

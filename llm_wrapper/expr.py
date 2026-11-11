"""Safe condition language for the policy.

Supported: field paths (``metadata.role``, ``request.system``), string/number/bool/None
literals, lists, ``and``/``or``/``not``, comparisons (``== != < <= > >= in``, ``not in``)
and ``<field> matches '<regex>'``. Nothing else parses: no calls, subscripts, lambdas,
comprehensions or arithmetic. A missing field is MISSING and every comparison on it is
false, so a rule about a field the request does not carry simply does not match.

``matches`` uses google-re2 when installed (linear time, cannot hang). Without it the
stdlib ``re`` is used and patterns are length-capped; install the ``re2`` extra for
untrusted policy authors.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from functools import lru_cache
from typing import Any

try:  # pragma: no cover - optional dependency
    import re2 as _re_engine  # type: ignore

    RE2 = True
except ImportError:  # pragma: no cover
    _re_engine = re
    RE2 = False

MAX_PATTERN_LEN = 500


class ExprError(ValueError):
    pass


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def _rewrite_matches(src: str) -> str:
    """Turn the ``matches`` keyword into ``@`` (MatMult) outside string literals."""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError) as exc:
        raise ExprError(f"cannot tokenize: {exc}") from exc
    for tok in toks:
        if tok.type == tokenize.NAME and tok.string == "matches":
            tok = tok._replace(string="@")
        out.append(tok)
    return tokenize.untokenize(out)


_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Name, ast.Attribute, ast.Constant, ast.List, ast.Tuple, ast.Load,
    ast.BinOp, ast.MatMult, ast.USub,
)


@lru_cache(maxsize=1024)
def compile_expr(src: str) -> ast.Expression:
    try:
        tree = ast.parse(_rewrite_matches(src).strip(), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error in {src!r}: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"{type(node).__name__} is not allowed in conditions: {src!r}")
        if isinstance(node, ast.BinOp) and not isinstance(node.op, ast.MatMult):
            raise ExprError(f"arithmetic is not allowed in conditions: {src!r}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            if not isinstance(node.operand, ast.Constant):
                raise ExprError(f"unary minus only on numbers: {src!r}")
        if isinstance(node, ast.BinOp):
            if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
                raise ExprError(f"'matches' needs a string literal pattern: {src!r}")
            _regex(node.right.value)  # validate now
    return tree


@lru_cache(maxsize=1024)
def _regex(pattern: str):
    if not RE2 and len(pattern) > MAX_PATTERN_LEN:
        raise ExprError(f"pattern longer than {MAX_PATTERN_LEN} chars (install google-re2)")
    try:
        return _re_engine.compile(pattern)
    except Exception as exc:  # re.error / re2.error
        raise ExprError(f"bad regex {pattern!r}: {exc}") from exc


def _lookup(ctx: dict[str, Any], node: ast.AST) -> Any:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        raise ExprError("field paths must be names joined by dots")
    parts.append(node.id)
    cur: Any = ctx
    for p in reversed(parts):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return MISSING
    return cur


def _cmp(op: ast.cmpop, a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return False
    try:
        if isinstance(op, ast.Eq):
            return a == b
        if isinstance(op, ast.NotEq):
            return a != b
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        if isinstance(op, ast.GtE):
            return a >= b
        if isinstance(op, ast.In):
            return a in b
        if isinstance(op, ast.NotIn):
            return a not in b
    except TypeError:
        return False
    raise ExprError(f"unsupported operator {type(op).__name__}")


def _eval(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -node.operand.value  # type: ignore[attr-defined]
        return not bool(_eval(node.operand, ctx))
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, ctx) for e in node.elts]
    if isinstance(node, (ast.Name, ast.Attribute)):
        if isinstance(node, ast.Name) and node.id in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[node.id]
        return _lookup(ctx, node)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_eval(v, ctx)) for v in node.values)
        return any(bool(_eval(v, ctx)) for v in node.values)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, ctx)
            if not _cmp(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):  # matches
        value = _eval(node.left, ctx)
        if not isinstance(value, str):
            return False
        return _regex(node.right.value).search(value) is not None  # type: ignore[attr-defined]
    raise ExprError(f"unsupported node {type(node).__name__}")


def evaluate(src: str, ctx: dict[str, Any]) -> bool:
    return bool(_eval(compile_expr(src), ctx))

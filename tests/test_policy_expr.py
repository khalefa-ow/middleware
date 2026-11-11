import pytest

from llm_wrapper.expr import ExprError, evaluate
from llm_wrapper.policy import PolicyError, load_policy, merge, policy_from_dict

CTX = {"metadata": {"role": "planner", "n": 3}, "request": {"system": "Decide whether this task", "chars": 900}}


@pytest.mark.parametrize("src,want", [
    ("metadata.role == 'planner'", True),
    ("metadata.role in ['coder', 'repair']", False),
    ("request.system matches '^Decide'", True),
    ("request.chars > 500 and not metadata.missing", True),
    ("metadata.missing == 'x'", False),
    ("metadata.missing != 'x'", False),       # missing field: every comparison is false
    ("metadata.n >= 3 or request.chars < 0", True),
    ("'matches' == 'matches'", True),          # keyword inside strings is untouched
])
def test_eval(src, want):
    assert evaluate(src, CTX) is want


@pytest.mark.parametrize("src", [
    "__import__('os')", "metadata.role.upper()", "metadata['role']", "1 + 2 == 3",
    "[x for x in metadata]", "request.system matches metadata.role", "lambda: 1",
])
def test_rejects_unsafe(src):
    with pytest.raises(ExprError):
        evaluate(src, CTX)


def test_self_call_rejected():
    with pytest.raises(Exception, match="call the wrapper itself"):
        policy_from_dict({"models": {"default": "wrapper/auto"}})


def test_bad_route_expr_rejected():
    with pytest.raises(Exception, match="not allowed"):
        policy_from_dict({"models": {"default": "m"}, "routes": [{"id": "r", "when": "open('x')", "model": "m"}]})


def test_unknown_key_rejected():
    with pytest.raises(Exception):
        policy_from_dict({"models": {"default": "m"}, "privcy": {}})


def test_merge_semantics():
    base = {"a": {"x": 1, "y": 2}, "l": [1, 2], "routes": [{"id": "r1", "model": "a"}, {"id": "r2", "model": "b"}]}
    over = {"a": {"y": 3}, "l": [9], "routes": [{"id": "r2", "model": "c"}, {"id": "r3", "model": "d"}]}
    m = merge(base, over)
    assert m["a"] == {"x": 1, "y": 3} and m["l"] == [9]
    assert [(r["id"], r["model"]) for r in m["routes"]] == [("r1", "a"), ("r2", "c"), ("r3", "d")]


def test_layers_and_secrets(tmp_path, monkeypatch):
    (tmp_path / "base.yaml").write_text("models: {default: m1}\nroutes:\n  - {id: r, when: 'true', model: m2}\n")
    over = tmp_path / "env.yaml"
    over.write_text("routes:\n  - {id: r, when: 'true', model: '${ENV:ROUTE_MODEL}'}\n")
    with pytest.raises(PolicyError, match="ROUTE_MODEL"):
        load_policy(tmp_path / "base.yaml", [over])
    monkeypatch.setenv("ROUTE_MODEL", "m3")
    lp = load_policy(tmp_path / "base.yaml", [over])
    assert lp.policy.routes[0].model == "m3"
    assert "m3" not in lp.effective_yaml  # stored policy keeps the reference, not the secret


def test_starter_policy_valid():
    from llm_wrapper.cli import STARTER
    assert load_policy(STARTER).policy.models.default

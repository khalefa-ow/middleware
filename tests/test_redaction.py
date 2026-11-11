import pytest

from llm_wrapper.privacy import Detector, RedactionReport, RestoreReport, StreamRestorer, Vault, restore_text
from llm_wrapper.privacy.egress import redact_text
from llm_wrapper.privacy.patterns import luhn_ok

KEY = bytes(range(32))
D = Detector(["email", "phone", "card", "ssn", "api_key"])


def kinds(text, det=D):
    return [(m.type, m.value) for m in det.find(text)]


def test_email_ssn_key():
    t = "mail jane.doe@example.com ssn 123-45-6789 key sk-ant-abcdefghijklmnopqrstuvwx1234"
    assert [k for k, _ in kinds(t)] == ["EMAIL", "SSN", "SECRET"]


@pytest.mark.parametrize("text", [
    "Q8: 2084531280550275 Q9: 2084531280550280",   # sums in program output (seen in fastlanes traces)
    "order 4111111111111111 shipped",               # Luhn-valid, Visa prefix, but no card context
    "timestamp 1793939196233 and id 5555555555554444x",
])
def test_card_false_positives(text):
    assert not [k for k, _ in kinds(text) if k == "CARD"]


@pytest.mark.parametrize("text", [
    "card 4111111111111111",
    "pay with 4111 1111 1111 1111 today",
    "visa: 4012-8888-8888-1881",
])
def test_card_true_positives(text):
    assert [k for k, _ in kinds(text)] == ["CARD"]


def test_luhn():
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


def test_phone_needs_context():
    assert kinds("call me at 4155550100") == [("PHONE", "4155550100")]
    assert kinds("row count 4155550100") == []
    assert [k for k, _ in kinds("+1 415 555 0100 or (415) 555-0100")] == ["PHONE", "PHONE"]


def test_code_blocks_and_allow_patterns():
    t = "see ```x = 'a@b.co'``` and mail a@b.co; ORD-5551234567 call"
    det = Detector(["email", "phone"], allow=[r"ORD-\d+"])
    assert [v for _, v in kinds(t, det)] == ["a@b.co"]


def test_vault_values_replaced_everywhere_even_in_code():
    v = Vault(KEY, "s1")
    out = redact_text("mail a@b.co\n```send('a@b.co')```", v, D, RedactionReport())
    assert "a@b.co" not in out and out.count("<EMAIL_") == 2


def test_placeholders_stable_in_scope_unlinkable_across():
    a1, a2, b = Vault(KEY, "conv-a"), Vault(KEY, "conv-a"), Vault(KEY, "conv-b")
    p1 = a1.placeholder_for("x@y.com", "EMAIL")
    assert p1 == a2.placeholder_for("x@y.com", "EMAIL")
    assert p1 != b.placeholder_for("x@y.com", "EMAIL")
    assert len(p1) == len("<EMAIL_>") + 8


def test_collision_grows_suffix():
    v = Vault(KEY, "s")
    p = v.placeholder_for("a@b.co", "EMAIL")
    # forge a collision: another value already owns the 8-char placeholder
    from llm_wrapper.privacy.vault import Entry
    v.by_placeholder.clear(); v.by_value.clear()
    v.by_placeholder[p] = Entry(p, "other@x.io", "EMAIL", "caller_input", "email")
    p2 = v.placeholder_for("a@b.co", "EMAIL")
    assert p2 != p and p2.startswith(p[:-1]) and len(p2) == len(p) + 4


def test_tolerant_restore_only_known_ids():
    v = Vault(KEY, "s")
    p = v.placeholder_for("a@b.co", "EMAIL")
    suffix = p[len("<EMAIL_"):-1]
    rr = RestoreReport()
    text = f"{p} / EMAIL_{suffix} / <email_{suffix.upper()}> / <EMAIL_deadbeef>"
    out = restore_text(text, v, "input_only", rr)
    assert out == "a@b.co / a@b.co / a@b.co / <EMAIL_deadbeef>"
    assert rr.restored == 3 and rr.unknown == ["<EMAIL_deadbeef>"]


def test_input_only_masks_other_origins():
    v = Vault(KEY, "s")
    p = v.placeholder_for("secret@corp.io", "EMAIL", origin="agent:crm")
    rr = RestoreReport()
    assert restore_text(f"x {p}", v, "input_only", rr) == f"x {p}" and rr.masked == 1
    assert restore_text(f"x {p}", v, "all", RestoreReport()) == "x secret@corp.io"


def test_stream_restorer_splits_placeholder():
    v = Vault(KEY, "s")
    p = v.placeholder_for("a@b.co", "EMAIL")
    text = f"Hello {p}, bye"
    r = StreamRestorer(v, "input_only")
    out = "".join(r.feed(text[i:i + 2]) for i in range(0, len(text), 2)) + r.flush()
    assert out == "Hello a@b.co, bye"


@pytest.mark.parametrize("text", [
    "82-\n83-\n84-@pytest.mark.parametrize(\n",          # grep -n context output (public SWE traces)
    "1. Testing basic time.time() call: Current time: 1757012345.2",   # epoch timestamp after "call"
    "re.VERBOSE), list('-+0123456789')",                  # character class in code
    "nvidia-cudnn-cu12==9.1.0.70 (from torch)",           # version string, not an IP
])
def test_public_trace_false_positives(text):
    det = Detector(["email", "phone", "card", "ssn", "api_key", "ipv4"])
    assert det.find(text) == []


def test_email_still_found_in_git_log():
    assert kinds("Author: Jane Dev <jane.dev@gmail.com>") == [("EMAIL", "jane.dev@gmail.com")]
    assert kinds("phone: 4155550100") == [("PHONE", "4155550100")]

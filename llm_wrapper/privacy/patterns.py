"""Detectors for private values.

Each detector yields (start, end, type, pattern_id). Detectors aim for low false
positives: cards must pass Luhn, phone numbers need a "+", a separator layout, or a
nearby word such as "phone"/"call"/"tel", and bare digit runs are never redacted on
their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass(frozen=True, slots=True)
class Match:
    start: int
    end: int
    type: str
    pattern_id: str
    value: str


# local part needs a letter and may not end in "." or "-" (rules out grep/line-number output
# such as "84-@pytest.mark.parametrize")
EMAIL = re.compile(r"(?<![\w.+-])(?=[A-Za-z0-9._%+-]*[A-Za-z][A-Za-z0-9._%+-]*@)"
                   r"[A-Za-z0-9._%+-]{0,63}[A-Za-z0-9_%+]@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}(?![\w-])")
SSN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
PHONE_INTL = re.compile(r"""(?<![\w+\-"'])""" r"\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{2,4}(?:[ .-]?\d{2,4}){1,4}(?!\d)")
PHONE_US = re.compile(r"(?<![\d-])(?:\(\d{3}\)\s?|\d{3}[.-])\d{3}[.-]\d{4}(?![\d-])")
# bare numbers: NANP shape only (area code 2-9, optional leading 1), so Unix timestamps
# (1.6e9-1.9e9) and other counters are not phone numbers even next to "call"
PHONE_BARE = re.compile(r"(?<![\d.])(?:1?[2-9]\d{2}[2-9]\d{6})(?![\d.])")
_SEQUENTIAL = "01234567890123456789"
PHONE_CONTEXT = re.compile(r"(?i)\b(phone|mobile|cell|call|tel|telephone|fax|whatsapp)\b")
API_KEY = re.compile(
    r"(?<![\w-])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})(?![\w-])"
)
IPV4 = re.compile(r"(?<![\w.=+/-])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.-])")


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _simple(rx: re.Pattern, type_: str, pid: str) -> Callable[[str], Iterator[Match]]:
    def det(text: str) -> Iterator[Match]:
        for m in rx.finditer(text):
            yield Match(m.start(), m.end(), type_, pid, m.group(0))
    return det


CARD_IIN = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|6(?:011|5|4[4-9])|35)")
CARD_GROUPED = re.compile(r"^\d{4}([ -])\d{4}\1\d{4}\1\d{1,7}$|^\d{4}([ -])\d{6}\2\d{5}$")
CARD_CONTEXT = re.compile(r"(?i)\b(card|credit|debit|visa|mastercard|amex|cc|pan)\b")


def _cards(text: str) -> Iterator[Match]:
    """Luhn alone is not enough: ~10% of random digit runs pass it (e.g. large sums in
    program output). A match needs a card prefix and either card-style grouping
    (4-4-4-4 / 4-6-5) or a card word just before it."""
    for m in CARD.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if not (13 <= len(digits) <= 19 and luhn_ok(digits) and len(set(digits)) > 1 and CARD_IIN.match(digits)):
            continue
        grouped = CARD_GROUPED.match(raw) is not None
        context = CARD_CONTEXT.search(text[max(0, m.start() - 30):m.start()]) is not None
        if grouped or context:
            yield Match(m.start(), m.end(), "CARD", "card", raw)


def _phones(text: str) -> Iterator[Match]:
    seen: set[tuple[int, int]] = set()
    for rx in (PHONE_INTL, PHONE_US):
        for m in rx.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if 7 <= len(digits) <= 15 and digits not in _SEQUENTIAL and len(set(digits)) > 2:
                seen.add((m.start(), m.end()))
                yield Match(m.start(), m.end(), "PHONE", "phone", m.group(0))
    for m in PHONE_BARE.finditer(text):
        if any(s <= m.start() < e for s, e in seen):
            continue
        window = text[max(0, m.start() - 30): m.start()]
        if PHONE_CONTEXT.search(window):
            yield Match(m.start(), m.end(), "PHONE", "phone_context", m.group(0))


BUILTIN: dict[str, Callable[[str], Iterator[Match]]] = {
    "email": _simple(EMAIL, "EMAIL", "email"),
    "ssn": _simple(SSN, "SSN", "ssn"),
    "card": _cards,
    "phone": _phones,
    "api_key": _simple(API_KEY, "SECRET", "api_key"),
    "ipv4": _simple(IPV4, "IP", "ipv4"),
}

CODE_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)


class Detector:
    def __init__(self, names: list[str], custom: list[tuple[str, str, str]] = (),
                 allow: list[str] = (), skip_code_blocks: bool = True):
        unknown = [n for n in names if n not in BUILTIN]
        if unknown:
            raise ValueError(f"unknown privacy patterns: {unknown} (known: {sorted(BUILTIN)})")
        self.detectors = [BUILTIN[n] for n in names]
        for pid, type_, rx in custom:
            self.detectors.append(_simple(re.compile(rx), type_, pid))
        self.allow = [re.compile(a) for a in allow]
        self.skip_code_blocks = skip_code_blocks

    def find(self, text: str) -> list[Match]:
        if not text:
            return []
        blocked: list[tuple[int, int]] = []
        if self.skip_code_blocks:
            blocked += [(m.start(), m.end()) for m in CODE_FENCE.finditer(text)]
        for a in self.allow:
            blocked += [(m.start(), m.end()) for m in a.finditer(text)]
        found: list[Match] = []
        for det in self.detectors:
            for m in det(text):
                if any(s < m.end and m.start < e for s, e in blocked):
                    continue
                found.append(m)
        # resolve overlaps: earliest start, then longest
        found.sort(key=lambda m: (m.start, -(m.end - m.start)))
        out: list[Match] = []
        last_end = -1
        for m in found:
            if m.start >= last_end:
                out.append(m)
                last_end = m.end
        return out

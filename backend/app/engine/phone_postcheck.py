"""Small, model-only phone checks; no numbering-plan validation.

The 7–15 digit limit deliberately includes country prefixes and extensions.
Explicit dates, currency amounts and Italian CAP fields are not phones.
"""
import re

from . import formats
from .phones import _EXTENSION
from .source_text import SourceText


# No digit limit here: a model fragment inside an overlong number must not
# escape the check. Newlines and source-value boundaries stop reconstruction.
_NUMBER = re.compile(r"\d+(?:[ \t\u00a0\u202f.()/\-]+\d+)*")
_DATE_CUE = re.compile(
    r"(?<!\w)(?:data(?: di nascita| fattura| di emissione| di scadenza)?"
    r"|nat[oa] il|date(?: of birth)?|birth date|invoice date|dob"
    r"|date de naissance|geburtsdatum|datum|fecha(?: de nacimiento)?"
    r"|geboortedatum)[ \t\u00a0\u202f:=\-]*$", re.I)
_CAP_CUE = re.compile(r"(?<!\w)CAP[ \t\u00a0\u202f.:=\-]*$", re.I)


def scan_phone_context(text):
    """Scan once, keeping numeric reconstruction inside each source value."""
    numbers, blocked = [], []
    spans = text.spans if isinstance(text, SourceText) else ((0, len(text)),)
    previous_end = 0
    for start, end in spans:
        value = text[start:end]
        # Synthetic field labels may precede a source value. Include that gap
        # as context, but never the preceding cell's actual contents.
        context_start = previous_end
        previous_end = end
        for match in _NUMBER.finditer(value):
            right = match.end()
            extension = _EXTENSION.match(value, right)
            if extension:
                right = extension.end()
            numbers.append((start + match.start(), start + right))
            if (len(match.group()) == 5 and match.group().isdecimal()
                    and _CAP_CUE.search(text[max(context_start, start + match.start() - 80):
                                            start + match.start()])):
                blocked.append((start + match.start(), start + match.end()))
        for match in formats.AMOUNT_RX.finditer(value):
            if formats.amount_ok(match.group()):
                blocked.append((start + match.start(), start + match.end()))
        for match in formats.DATE_RX.finditer(value):
            left = start + match.start()
            if (formats.date_ok(match.group())
                    and _DATE_CUE.search(text[max(context_start, left - 80):left])):
                blocked.append((left, start + match.end()))
    return numbers, blocked


def clean_phone(entity, text, context):
    """Reject clear mistakes, recovering complete numeric edge fragments."""
    numbers, blocked = context
    digits = list(re.finditer(r"\d", text[entity["start"]:entity["end"]]))
    if not digits:
        return None
    start = entity["start"] + digits[0].start()
    end = entity["start"] + digits[-1].end()
    # Expand the numeric edges, not arbitrary neighbouring values. In
    # particular, a short fragment of a 16-digit sequence is still rejected.
    for left, right in numbers:
        if left <= start < right or left < end <= right:
            start, end = min(start, left), max(end, right)
    if not 7 <= sum(ch.isdecimal() for ch in text[start:end]) <= 15:
        return None
    if any(left <= start and end <= right for left, right in blocked):
        return None
    return {**entity, "start": start, "end": end, "validated": False}

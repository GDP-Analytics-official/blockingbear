"""Unicode-aware text matching without changing stored values or offsets.

Canonical keys are separate from literal document searches. In particular,
casefolding a pattern and searching the original text loses German sharp s.
Secret values always use the exact, case-sensitive matching path.
"""

from functools import wraps
import re
import unicodedata

HSPACE = r"[ \t\u00a0\u202f]"
WORD = r"\w\u0300-\u036f"
PLACEHOLDER_SOURCE = r"\[[A-Z0-9_]+_[0-9]+\]"
PLACEHOLDER_RE = re.compile(PLACEHOLDER_SOURCE)


def canonical(value):
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def literal(value, between=""):
    """Escaped ordinary text, accepting NFC/NFD and German full case folding.

    Alternatives operate directly on the original text, so Match offsets stay
    valid for PDF glyphs, XML runs and Python string slices. `between` is used
    by document matching to tolerate line-end hyphenation between characters.
    This function must not be used for passwords, keys or other exact values.
    """
    value = unicodedata.normalize("NFC", value)
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c in "ßẞ" or value[i:i + 2].lower() == "ss":
            out.append(r"(?:[ßẞ]|s" + between + "s)")
            i += 1 if c in "ßẞ" else 2
            continue
        forms = [re.escape(c)]
        decomposed = unicodedata.normalize("NFD", c)
        if decomposed != c:
            forms.append(re.escape(decomposed))
        out.append("(?:" + "|".join(forms) + ")" if len(forms) > 1 else forms[0])
        i += 1
    return between.join(out)


def exact_pattern(value):
    if not value:
        return None
    left = rf"(?:(?<![{WORD}])|(?<=-p)|(?<=-P))" if value[0].isalnum() or value[0] == "_" or unicodedata.combining(value[0]) else ""
    right = rf"(?![{WORD}])" if value[-1].isalnum() or value[-1] == "_" or unicodedata.combining(value[-1]) else ""
    return re.compile(left + re.escape(value) + right)


def contains_literal(text, value, placeholder):
    """Independent residual check using string search, not redaction regexes.

    Ordinary text uses canonical equality; exact values keep every character.
    Word and decimal boundaries avoid finding a name inside another name or
    a small integer inside an unrelated amount.
    """
    from .detectors import EXACT_SPAN_LABELS
    exact = placeholder.strip("[]").rsplit("_", 1)[0] in EXACT_SPAN_LABELS
    haystack, needle = (text, value) if exact else (canonical(text), canonical(value))
    if not needle:
        return False
    def word(c):
        return c.isalnum() or c == "_" or bool(unicodedata.combining(c))
    start = haystack.find(needle)
    while start >= 0:
        end = start + len(needle)
        left = start == 0 or not word(needle[0]) or not word(haystack[start - 1])
        if exact and haystack[max(0, start - 2):start] in {"-p", "-P"}:
            left = True
        right = end == len(haystack) or not word(needle[-1]) or not word(haystack[end])
        if not exact:
            left = left and not (needle[0].isdigit() and start >= 2
                                and haystack[start - 1] in ".," and haystack[start - 2].isdigit())
            right = right and not (needle[-1].isdigit() and end + 1 < len(haystack)
                                  and haystack[end] in ".," and haystack[end + 1].isdigit())
        if left and right:
            return True
        start = haystack.find(needle, start + 1)
    return False


def _scan_view(text):
    """NFC and horizontal spaces for classification; map back to raw clusters."""
    if text.isascii():
        return text, None
    translation = {0x00a0: " ", 0x202f: " "}
    normalized = unicodedata.normalize("NFC", text).translate(translation)
    if normalized == text:
        return text, None
    parts, starts, ends, i = [], [], [], 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        cluster = unicodedata.normalize("NFC", text[i:j]).translate(translation)
        parts.append(cluster)
        starts.extend([i] * len(cluster))
        ends.extend([j] * len(cluster))
        i = j
    return "".join(parts), (starts, ends)


def normalized_detector(fn):
    """Use normalized cues while returning spans over the untouched input.

    Detectors only return labels and offsets, never normalized secret values.
    Nested detectors are inexpensive: an already normalized scan view is reused.
    """
    @wraps(fn)
    def detect(text):
        view, offsets = _scan_view(text)
        entities = fn(view)
        if offsets is not None:
            starts, ends = offsets
            for entity in entities:
                entity["start"] = starts[entity["start"]]
                entity["end"] = ends[entity["end"] - 1]
        return entities
    return detect

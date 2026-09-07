"""Candidate index for large Excel mappings.

An Aho-Corasick index finds a superset of possible matches in linear time.
The existing value patterns still decide boundaries and exact spelling. Thus
whole-cell table optimization does not give embedded values different privacy
semantics, and a 40k-value column does not require 40k regexes per cell.
"""
from collections import deque
from functools import lru_cache
import unicodedata

from .pdf_export import _value_pattern
from .text_patterns import canonical


def _key(text):
    text = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in text if c.isalnum()).replace("ı", "i")


class ValueIndex:
    def __init__(self, items):
        from .detectors import EXACT_SPAN_LABELS
        self.items = list(items)
        self.whole, self.exact_whole = {}, {}
        for ph, value in self.items:
            if ph.strip("[]").rsplit("_", 1)[0] in EXACT_SPAN_LABELS:
                self.exact_whole.setdefault(value, ph)
            else:
                self.whole.setdefault(canonical(value), ph)
        self.next, self.fail, self.ends = [{}], [0], [[]]
        self.fallback = []
        for i, (_ph, value) in enumerate(self.items):
            key = _key(value)
            if not key:
                self.fallback.append(i)
                continue
            state = 0
            for char in key:
                if char not in self.next[state]:
                    self.next[state][char] = len(self.next)
                    self.next.append({})
                    self.fail.append(0)
                    self.ends.append([])
                state = self.next[state][char]
            self.ends[state].append(i)
        queue = deque(self.next[0].values())
        while queue:
            state = queue.popleft()
            for char, child in self.next[state].items():
                queue.append(child)
                back = self.fail[state]
                while back and char not in self.next[back]:
                    back = self.fail[back]
                self.fail[child] = self.next[back].get(char, 0)
                self.ends[child].extend(self.ends[self.fail[child]])
        # Per-instance caches have bounded lifetime and memory.
        self.pattern = lru_cache(maxsize=4096)(self._pattern)

    def _pattern(self, index):
        ph, value = self.items[index]
        return _value_pattern(value, ph)

    def candidates(self, text):
        found, state = set(self.fallback), 0
        for char in _key(text):
            while state and char not in self.next[state]:
                state = self.fail[state]
            state = self.next[state].get(char, 0)
            found.update(self.ends[state])
        # Longest first, consistently with the document redactors.
        for i in sorted(found, key=lambda j: -len(self.items[j][1])):
            pattern = self.pattern(i)
            if pattern is not None:
                ph, value = self.items[i]
                yield ph, value, pattern

    def matches(self, text):
        return [(ph, value) for ph, value, pat in self.candidates(text)
                if pat.search(text)]

    def replace(self, text):
        ph = self.exact_whole.get(text, self.whole.get(canonical(text)))
        if ph is not None:
            return ph, {ph: 1}
        matches, claimed = [], []
        for ph, _value, pat in self.candidates(text):
            for m in pat.finditer(text):
                if any(m.start() < e and m.end() > s for s, e in claimed):
                    continue
                claimed.append(m.span())
                matches.append((m.start(), m.end(), ph))
        hits = {}
        for start, end, ph in sorted(matches, reverse=True):
            text = text[:start] + ph + text[end:]
            hits[ph] = hits.get(ph, 0) + 1
        return text, hits


class CellValues(dict):
    """Whole-cell lookup plus the same values indexed for embedded matches."""
    index = None


def patterns(usable, exact, text):
    index = getattr(exact, "index", None)
    if index is None:
        return usable
    return sorted([*usable, *index.candidates(text)], key=lambda row: -len(row[1]))

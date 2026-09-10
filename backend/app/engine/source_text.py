"""Analysis text with explicit boundaries between independent source values.

Separators and repeated labels provide model context but are not document
content. A literal separator inside a source value remains ordinary data.
"""
from bisect import bisect_right


class SourceText(str):
    def __new__(cls, text, spans=None):
        obj = super().__new__(cls, text)
        obj.spans = tuple(spans if spans is not None else [(0, len(text))])
        obj._ends = tuple(end for _, end in obj.spans)
        return obj

    @classmethod
    def join(cls, separator, values):
        parts, spans, offset = [], [], 0
        for value in values:
            if parts:
                parts.append(separator)
                offset += len(separator)
            parts.append(value)
            spans.extend((offset + start, offset + end)
                         for start, end in getattr(value, "spans", [(0, len(value))])
                         if start < end)
            offset += len(value)
        return cls("".join(parts), spans)

    @classmethod
    def labeled(cls, label, value):
        prefix = f"{label}: "
        return cls(prefix + value, [(len(prefix), len(prefix) + len(value))])

    def intersections(self, start, end):
        for i in range(bisect_right(self._ends, start), len(self.spans)):
            left, right = self.spans[i]
            if left >= end:
                break
            yield i, max(left, start), min(right, end)

    def contains_span(self, start, end):
        return any(left == start and right == end
                   for _, left, right in self.intersections(start, end))

    def values(self):
        return (self[start:end] for start, end in self.spans)

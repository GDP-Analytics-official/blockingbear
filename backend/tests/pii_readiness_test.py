"""Readiness PII: una pipeline costruita non basta, serve un vero forward.

Il test usa pipeline finte e non carica torch né il checkpoint.

Uso:  python backend/tests/pii_readiness_test.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from app.engine.core import PiiEngine  # noqa: E402


class WorkingPipeline:
    def __init__(self):
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        return [[] for _ in texts]


class BrokenFirstForward:
    def __call__(self, texts):
        raise RuntimeError("Failed to find C compiler")


def engine_with(pipeline):
    engine = PiiEngine("unused-by-this-test")
    engine._nlp = pipeline
    return engine


def main():
    good_pipeline = WorkingPipeline()
    good = engine_with(good_pipeline)
    assert not good.loaded, "costruire/caricare la pipeline non deve dare ready"
    assert good.load() is good_pipeline
    assert not good.loaded, "load() senza forward non deve dare ready"
    good.warmup()
    assert good.loaded, "un forward concluso deve dare ready"
    assert good_pipeline.calls == 1

    broken = engine_with(BrokenFirstForward())
    try:
        broken.warmup()
    except RuntimeError as exc:
        assert "C compiler" in str(exc)
    else:
        raise AssertionError("il forward guasto doveva propagare l'errore")
    assert not broken.loaded, "un forward fallito non deve mai dare ready"

    print("PII READINESS: 6 PASS, 0 FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Conservative page extraction regressions, using synthetic HTML only.

Run in a backend container:
    python tests/browser_extraction_test.py
    python tests/browser_extraction_test.py --camofox

The optional browser pass uses the configured Camofox service with a separate
diagnostic user and disposable about:blank tabs; it never visits fixture URLs.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.openrouter import browser  # noqa: E402

BASE = "https://example.org/news/release.html"
CASES = [
    ("article in a page-wide form", """
      <form><main><article><header><h1>Annual results</h1>
      <p>Published 2026-02-13</p></header><p>Revenue: 100 million.</p>
      <aside>Figures exclude discontinued operations.</aside>
      <footer>Source: audited accounts.</footer></article></main></form>
    """, ["Annual results", "2026-02-13", "Revenue: 100 million.",
          "Figures exclude discontinued operations.", "Source: audited accounts."], []),
    ("multiple articles", """
      <section><article><h2>First company</h2><p>Revenue: 100 million.</p></article>
      <article><h2>Second company</h2><p>Revenue: 200 million.</p></article></section>
    """, ["First company", "100 million", "Second company", "200 million"], []),
    ("unique main", """
      <nav>Global menu</nav><main><h1>Useful title</h1>
      <article>First article</article><article>Second article</article></main>
    """, ["Useful title", "First article", "Second article"], ["Global menu"]),
    ("multiple mains", "<main>First region</main><main>Second region</main>",
     ["First region", "Second region"], []),
    ("empty main", "<main> </main><article>Article outside empty main</article>",
     ["Article outside empty main"], []),
    ("hidden main", "<main hidden>Hidden main text</main><p>Visible body text</p>",
     ["Visible body text"], ["Hidden main text"]),
    ("role main", '<nav>Global menu</nav><div role="main">Main landmark text</div>',
     ["Main landmark text"], ["Global menu"]),
    ("navigation directory", '<nav><a href="/report.pdf">Annual report 2025</a></nav>',
     ["Annual report 2025", "https://example.org/report.pdf"], []),
    ("FAQ buttons", "<main><button>What does it cost?</button><p>20 euros per month.</p></main>",
     ["What does it cost?", "20 euros per month."], []),
    ("visible ARIA-hidden text", '<main><p aria-hidden="true">Visible revenue 100</p></main>',
     ["Visible revenue 100"], []),
    ("hidden overridden by CSS", '<main><p hidden style="display:block">Visible override</p></main>',
     ["Visible override"], []),
    ("CSS visibility and link children", """
      <style>.concealed {display:none}</style><main><p>Visible paragraph</p>
      <a href="/report.pdf">Report<span class="concealed">HIDDEN LINK TEXT</span></a>
      <p class="concealed">HIDDEN PARAGRAPH</p></main>
    """, ["Visible paragraph", "https://example.org/report.pdf"],
     ["HIDDEN LINK TEXT", "HIDDEN PARAGRAPH"]),
    ("SVG labels", '<main><svg width="300" height="40"><title>Revenue chart</title>'
     '<text x="0" y="20">Revenue: 100 million.</text></svg></main>',
     ["Revenue: 100 million."], []),
    ("table boundaries", """
      <main><table><tr><th>Metric</th><th>2025</th><th>2024</th></tr>
        <tr><td>Revenue</td><td>100</td><td>90</td></tr>
        <tr><td>Profit</td><td></td><td>-5</td></tr></table></main>
    """, ["Metric\t2025\t2024", "Revenue\t100\t90", "Profit\t\t-5"], []),
    ("relative links", '<main><a href="../report.pdf">PDF report</a> '
     '<a href="https://other.example.org/">Other source</a></main>',
     ["[PDF report](https://example.org/report.pdf)",
      "[Other source](https://other.example.org/)"], []),
    ("non-content elements", '<main><p>Real article</p><script type="application/json">'
     '{"unused":"SCRIPT PAYLOAD"}</script><template>TEMPLATE PAYLOAD</template></main>',
     ["Real article"], ["SCRIPT PAYLOAD", "TEMPLATE PAYLOAD"]),
]


def assert_content(name, text, required, forbidden=()):
    for value in required:
        assert value in text, f"{name}: missing {value!r} in {text!r}"
    for value in forbidden:
        assert value not in text, f"{name}: unexpected {value!r} in {text!r}"
    print(f"PASS {name}", flush=True)


def static_tests():
    for name, html, required, _ in CASES:
        title, text = browser._html_to_text("<title>Fixture title</title>" + html, BASE)
        assert title == "Fixture title", f"{name}: document title changed to {title!r}"
        # Static HTML has no rendered visibility or main selection. Its
        # fallback deliberately preserves more text than the browser.
        forbidden = ["SCRIPT PAYLOAD", "TEMPLATE PAYLOAD"]
        assert_content("static: " + name, text, required, forbidden)
    _, text = browser._html_to_text("<main><noscript>Static alternative</noscript></main>")
    assert_content("static: noscript alternative", text, ["Static alternative"])
    _, text = browser._html_to_text("<style>UNUSED STYLE</style><p>Article after style</p>")
    assert_content("static: styles excluded", text, ["Article after style"], ["UNUSED STYLE"])
    _, text = browser._html_to_text(
        '<a href="http://[broken">Broken link</a><p>Following paragraph</p>', BASE)
    assert_content("static: malformed link", text, ["Broken link", "Following paragraph"])
    _, text = browser._html_to_text('<a id="section">Named anchor</a>', BASE)
    assert_content("static: named anchor", text, ["Named anchor"], [BASE])


def browser_tests():
    assert browser.CAMOFOX_URL, "--camofox requires an existing configured Camofox service"
    browser._USER = "extraction-regression-" + uuid.uuid4().hex
    browser._keeper = None
    try:
        browser._ensure()
        with browser._Tab() as tab:
            head = '<base href="' + BASE + '"><title>Fixture title</title>'
            for name, html, required, forbidden in CASES:
                tab.evaluate("document.head.innerHTML = " + json.dumps(head) + ";"
                             "document.body.innerHTML = " + json.dumps(html) + "; 'ready'")
                page = browser._extract(tab, 120000)
                assert page["title"] == "Fixture title"
                assert page["total"] == len(page["text"])
                assert_content("browser: " + name, page["text"], required, forbidden)

            html = '<main><a href="/report.pdf">Annual report</a><p>' + "x" * 500 + '</p></main>'
            tab.evaluate("document.body.innerHTML = " + json.dumps(html) + "; 'ready'")
            complete = browser._extract(tab, 120000)
            shorter = browser._extract(tab, 30)
            assert shorter["total"] == complete["total"], "retry changed the source length"
            assert shorter["text"] == complete["text"][:30], "retry changed link markup"
            assert shorter["total"] > len(shorter["text"])
            print("PASS browser: repeated extraction and truncation", flush=True)
    finally:
        if browser._keeper:
            browser._http("DELETE", f"/tabs/{browser._keeper}", {"userId": browser._USER})
            browser._keeper = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camofox", action="store_true")
    args = parser.parse_args()
    static_tests()
    if args.camofox:
        browser_tests()
    print("Page extraction regressions passed.")

#!/usr/bin/env python3
"""Acceptance tests for the search-facing copy of the static site (zero deps).

Covers the <title> and <h1> of website/index.html only:

  * the <title> names the category and at least two clients,
  * every client named in the <title> is corroborated by the page's rendered
    body copy, so a title cannot vouch for a client the body never shows,
  * the <title> stays short enough not to be truncated in a SERP snippet,
  * the single <h1 id="hero-title"> says what the product actually is (memory).

Run from the repo root:
    python3 tests/website/test_search_terms.py
    python3 -m unittest discover -s tests/website

Locates the published site relative to this file (repo_root/website), never by
absolute path. No network access.
"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "website" / "index.html"

PRODUCT_NAME = "neoBrain"  # case-sensitive: the brand is camel-cased
HERO_ID = "hero-title"

# Accepted category phrases for the <title>. The page's own copy uses only
# "durable memory" (body prose), "coding agents" (body prose) and "memory
# daemon" (meta description); "agent memory", "persistent memory" and
# "memory layer" are accepted as equivalent category labels even though the
# page never uses those exact phrases. Vocabulary, not a claim about the page.
CATEGORY_TERMS = (
    "agent memory",
    "persistent memory",
    "memory layer",
    "memory daemon",
    "durable memory",
    "coding agents",
)

# Vocabulary of AI coding clients a <title> may name. A client may only be
# named in the title if the page body substantiates it, and
# test_title_names_category_and_two_clients enforces exactly that: every
# client matched in the title must also appear in body_copy() — the rendered
# <body> text. Corroboration must come from rendered body copy, never from
# <head> metadata (description, og:*, twitter:title) or the title itself.
# Being in this tuple is not evidence that the product supports the client.
CLIENT_NAMES = (
    "Claude Code",
    "Cursor",
    "Gemini CLI",
    "OpenCode",
)
MIN_CLIENTS = 2

# Hard ceiling for a Google snippet; 60-65 is the normal truncation point.
MAX_TITLE_LENGTH = 70

# The <h1> must say the product stores memories, in whatever voice.
MEMORY_TERMS = ("memory", "memories", "remember")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def read_text(path):
    return path.read_text(encoding="utf-8")


def collapse_whitespace(value):
    return re.sub(r"\s+", " ", value).strip()


class _VisibleTextParser(HTMLParser):
    """Collects rendered text, skipping <script>/<style> contents."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


def body_copy(page_text):
    """Rendered text of the <body> only.

    Everything in <head> is discarded first: no <meta> content, no JSON-LD,
    no <link>. That matters because og:title/twitter:title mirror <title>
    verbatim, so counting <head> metadata would let a title corroborate
    itself. <script>/<style> bodies are skipped too, so identifiers such as
    ``cursor: pointer``, ``.t-cursor`` or ``clearCursor()`` cannot pass as
    support for the Cursor editor. What remains is what a reader actually
    sees: the page's own prose.
    """
    match = _BODY_OPEN_RE.search(page_text)
    if not match:
        return ""
    parser = _VisibleTextParser()
    parser.feed(page_text[match.end():])
    parser.close()
    return collapse_whitespace(" ".join(parser.parts))


class _TitleAndHeadingsParser(HTMLParser):
    """Collects the <title> text and every <h1> (attrs + rendered text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titles = []
        self.h1s = []
        self._title_parts = None
        self._h1_parts = None
        self._h1_attrs = None

    def handle_starttag(self, tag, attrs):
        if tag == "title" and self._title_parts is None:
            self._title_parts = []
        elif tag == "h1":
            self._h1_parts = []
            self._h1_attrs = {key.lower(): (value or "") for key, value in attrs}

    def handle_endtag(self, tag):
        if tag == "title" and self._title_parts is not None:
            self.titles.append(collapse_whitespace("".join(self._title_parts)))
            self._title_parts = None
        elif tag == "h1" and self._h1_parts is not None:
            self.h1s.append(
                {
                    "attrs": self._h1_attrs,
                    "text": collapse_whitespace("".join(self._h1_parts)),
                }
            )
            self._h1_parts = None

    def handle_data(self, data):
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._h1_parts is not None:
            self._h1_parts.append(data)


def parse_page(page_text):
    parser = _TitleAndHeadingsParser()
    parser.feed(page_text)
    parser.close()
    return parser


def matched_terms(text, terms):
    """Case-insensitive distinct substring matches, in list order."""
    lowered = text.lower()
    return [term for term in terms if term.lower() in lowered]


# --------------------------------------------------------------------------
# criterion 1-2: the <title> is a usable search snippet
# --------------------------------------------------------------------------
class TitleTests(unittest.TestCase):
    def _page_and_title(self):
        self.assertTrue(
            INDEX_HTML.is_file(), "website/index.html is missing"
        )
        page = read_text(INDEX_HTML)
        parser = parse_page(page)
        self.assertEqual(
            len(parser.titles),
            1,
            "website/index.html must have exactly one <title>, found %d"
            % len(parser.titles),
        )
        title = parser.titles[0]
        self.assertTrue(title, "the <title> is empty")
        return page, title

    def test_title_names_category_and_two_clients(self):
        page, title = self._page_and_title()

        self.assertIn(
            PRODUCT_NAME,
            title,
            "the <title> must contain the product name %r in exactly that "
            "casing; got %r" % (PRODUCT_NAME, title),
        )

        categories = matched_terms(title, CATEGORY_TERMS)
        self.assertTrue(
            categories,
            "the <title> names no category term, so it cannot rank for what "
            "people search; expected at least one of %r, got %r"
            % (list(CATEGORY_TERMS), title),
        )

        clients = matched_terms(title, CLIENT_NAMES)
        self.assertGreaterEqual(
            len(clients),
            MIN_CLIENTS,
            "the <title> must name at least %d supported clients (%s) so the "
            "page can surface against client-branded queries; found %d (%r) "
            "in %r"
            % (
                MIN_CLIENTS,
                ", ".join(CLIENT_NAMES),
                len(clients),
                clients,
                title,
            ),
        )

        # A title may only claim a client the rendered body substantiates.
        # Matching is case-sensitive: these are proper nouns, and lower-case
        # code tokens ("cursor: pointer") must not count as support.
        copy = body_copy(page)
        unsubstantiated = [
            client for client in clients if client not in copy
        ]
        self.assertEqual(
            unsubstantiated,
            [],
            "the <title> names %s, but the rendered <body> copy never "
            "mentions %s; a client may only be named in the title if the "
            "page body substantiates it — <head> metadata and the title "
            "itself do not count, and neither do matches inside "
            "JavaScript/CSS identifiers (e.g. 'cursor: pointer', "
            "'clearCursor()', '.t-cursor')"
            % (
                unsubstantiated,
                " or ".join(unsubstantiated),
            ),
        )

    def test_title_stays_within_snippet_length(self):
        # Guard: the shipped title is already short, so this passes today.
        # It fails if new copy pushes the title past the SERP ceiling.
        _, title = self._page_and_title()
        self.assertLessEqual(
            len(title),
            MAX_TITLE_LENGTH,
            "the <title> is %d characters, over the %d-character snippet "
            "ceiling and will be truncated: %r"
            % (len(title), MAX_TITLE_LENGTH, title),
        )


# --------------------------------------------------------------------------
# criterion 3: the hero heading says the product is about memory
# --------------------------------------------------------------------------
class HeroHeadingTests(unittest.TestCase):
    def _hero(self):
        self.assertTrue(
            INDEX_HTML.is_file(), "website/index.html is missing"
        )
        parser = parse_page(read_text(INDEX_HTML))
        self.assertEqual(
            len(parser.h1s),
            1,
            "website/index.html must have exactly one <h1>, found %d"
            % len(parser.h1s),
        )
        hero = parser.h1s[0]
        self.assertEqual(
            hero["attrs"].get("id"),
            HERO_ID,
            "the single <h1> must remain the hero heading with id=%r, got "
            "attrs %r" % (HERO_ID, hero["attrs"]),
        )
        self.assertTrue(
            hero["text"],
            "the hero <h1> renders no visible text",
        )
        self.assertGreaterEqual(
            len(hero["text"]),
            10,
            "the hero <h1> is too short to be readable copy: %r"
            % hero["text"],
        )
        return hero["text"]

    def test_hero_h1_carries_a_memory_term(self):
        heading = self._hero()
        terms = matched_terms(heading, MEMORY_TERMS)
        self.assertTrue(
            terms,
            "the hero <h1> says nothing about memory, so the page never "
            "states what the product is in its most important heading; "
            "expected one of %r, got %r"
            % (list(MEMORY_TERMS), heading),
        )


if __name__ == "__main__":
    unittest.main()

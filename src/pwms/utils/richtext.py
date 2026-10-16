"""Rich text: a small allowlist sanitiser for the agenda (and similar) fields.

The meeting agenda is typed in a dependency-free WYSIWYG editor
(``static/js/rich-text.js``) whose content is submitted as HTML. That HTML is
attacker-controlled input the moment it reaches the server, so it is never
trusted on the way *in*: it is stored as written and sanitised on the way *out*,
by :func:`render_rich_text`, which every template that displays it goes through.

The sanitiser is an allowlist built on the standard library's
:class:`html.parser.HTMLParser`: only a fixed set of inline/block tags survives,
only ``href``/``title``/``target``/``rel`` attributes on a link survive (and only
with an ``http`` / ``https`` / ``mailto`` URL, or a relative/anchor one), and
everything else — scripts, event handlers, styles, iframes, ``javascript:`` URLs —
is dropped. Text is always re-escaped, so a field can never inject markup however
it was stored.

It is intentionally minimal rather than a general-purpose HTML sanitiser: the
editor only ever produces a small vocabulary, so a small allowlist is enough and
there is no third-party sanitising dependency to keep patched.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
from urllib.parse import urlparse

from django.utils.html import escape, strip_tags
from django.utils.safestring import mark_safe

#: Tags the editor can produce and the display may keep.
ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "sub",
        "sup",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "blockquote",
        "a",
    }
)

#: Attributes kept, per tag. Nothing else survives.
ALLOWED_ATTRIBUTES = {"a": frozenset({"href", "title", "target", "rel"})}

#: Void elements (no closing tag).
VOID_TAGS = frozenset({"br"})

#: Tags whose *content* is dropped entirely (not just the tag), so a
#: ``<script>alert(1)</script>`` leaves no stray text behind.
DROP_CONTENT_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "noscript",
        "template",
        "svg",
        "math",
    }
)

#: URL schemes an ``href`` may use (relative URLs and ``#anchors`` are also kept).
ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})

#: Block-level tags that mean "this is HTML, not plain text", used to decide
#: whether bare newlines should become ``<br>``.
_BLOCK_MARKERS = (
    "<p",
    "<div",
    "<ul",
    "<ol",
    "<li",
    "<h1",
    "<h2",
    "<h3",
    "<h4",
    "<blockquote",
    "<br",
)


def _safe_href(value: str | None) -> str | None:
    """Return ``value`` when it is a safe link target, else ``None``.

    Relative paths (``/pwms/...``) and in-page anchors (``#section``) are fine;
    anything with a scheme must use ``http``, ``https`` or ``mailto``. A
    protocol-relative ``//host`` link is rejected because its scheme is implicit.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    if candidate.startswith(("/", "#")):
        return candidate
    parsed = urlparse(candidate)
    if parsed.scheme:
        return candidate if parsed.scheme.lower() in ALLOWED_SCHEMES else None
    if candidate.startswith("//"):
        return None
    # A scheme-less, non-relative value (e.g. ``example.com``) is left as-is.
    return candidate


class _Sanitizer(HTMLParser):
    """Rebuild permitted markup and escape everything else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []
        self._drop: list[str] = []

    # -- helpers ------------------------------------------------------------
    def _attributes(self, tag: str, attrs) -> str:
        allowed = ALLOWED_ATTRIBUTES.get(tag, frozenset())
        rendered = []
        for name, value in attrs:
            if name not in allowed:
                continue
            if name == "href":
                href = _safe_href(value)
                if href is None:
                    continue
                rendered.append(f' href="{escape(href)}"')
            else:
                rendered.append(f' {name}="{escape(value or "")}"')
        return "".join(rendered)

    def result(self) -> str:
        # Close any tags the source left open, so the fragment is well-formed.
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)

    # -- HTMLParser hooks ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self._drop:
            self._drop.append(tag)
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return
        self._out.append(f"<{tag}{self._attributes(tag, attrs)}>")
        if tag not in VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._drop:
            if tag in self._drop:
                while self._drop:
                    if self._drop.pop() == tag:
                        break
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self._open:
            # Close any tags left open inside this one first, to stay balanced.
            while self._open:
                opened = self._open.pop()
                self._out.append(f"</{opened}>")
                if opened == tag:
                    break

    def handle_data(self, data):
        if self._drop:
            return
        self._out.append(escape(data))

    def handle_comment(self, data):
        # Comments carry no display value and are a classic hiding place.
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return


def sanitize_html(value: str | None) -> str:
    """Return ``value`` reduced to the safe markup allowlist (escaped)."""
    if not value:
        return ""
    parser = _Sanitizer()
    parser.feed(value)
    parser.close()
    return parser.result()


def render_rich_text(value: str | None) -> str:
    """Sanitised HTML for display, marked safe.

    Bare newlines in content that carries no block tags (plain text typed into
    the field outside the editor, e.g. by an administrator) become ``<br>`` so a
    hand-written agenda keeps its line breaks.
    """
    rendered = sanitize_html(value)
    if rendered and not any(marker in rendered for marker in _BLOCK_MARKERS):
        rendered = rendered.replace("\n", "<br>")
    return mark_safe(rendered)


def rich_text_to_text(value: str | None) -> str:
    """The plain text of a rich-text value (for PDF/plain-text renderings)."""
    return html.unescape(strip_tags(sanitize_html(value)))

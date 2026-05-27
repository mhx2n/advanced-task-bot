import re
import html

# Characters/markup we strip so Telegram plain text stays clean and
# LaTeX/markdown noise is removed.
_STRIP_INLINE = re.compile(r"[`*_~#>|]")
_LATEX_BLOCK = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_LATEX_INLINE = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]|\$(.+?)\$", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    if not text:
        return ""
    t = text
    t = _LATEX_BLOCK.sub("", t)
    t = _LATEX_INLINE.sub(lambda m: next((g for g in m.groups() if g), ""), t)
    t = _HTML_TAG.sub("", t)
    t = _STRIP_INLINE.sub("", t)
    # Remove leftover markdown list bullets
    t = re.sub(r"^\s*[-+]\s+", "• ", t, flags=re.MULTILINE)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def chunk_text(text: str, limit: int = 4000):
    text = text or ""
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            yield buf
            buf = ""
        buf += line
    if buf:
        yield buf


def escape_html(s: str) -> str:
    return html.escape(s or "", quote=False)

import re
import html

# Strict cleaner — strips ALL markup. Used for safe plain text broadcast.
_STRIP_INLINE = re.compile(r"[`*_~#>|]")
_LATEX_BLOCK = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_LATEX_INLINE = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]|\$(.+?)\$", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")
_CODE_FENCE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")


def clean_text(text: str) -> str:
    """Aggressive cleaner — strips markdown/code/HTML/LaTeX."""
    if not text:
        return ""
    t = text
    t = _LATEX_BLOCK.sub("", t)
    t = _LATEX_INLINE.sub(lambda m: next((g for g in m.groups() if g), ""), t)
    t = _HTML_TAG.sub("", t)
    t = _STRIP_INLINE.sub("", t)
    t = re.sub(r"^\s*[-+]\s+", "• ", t, flags=re.MULTILINE)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def format_ai_answer(text: str) -> str:
    """Convert AI output to Telegram-safe HTML while preserving code blocks."""
    if not text:
        return ""
    blocks = []

    def _stash(m):
        lang = (m.group(1) or "").strip()
        code = m.group(2).rstrip()
        idx = len(blocks)
        blocks.append((lang, code))
        return f"\x00CODE{idx}\x00"

    t = _CODE_FENCE.sub(_stash, text)
    t = _LATEX_BLOCK.sub("", t)
    t = _LATEX_INLINE.sub(lambda m: next((g for g in m.groups() if g), ""), t)
    t = html.escape(_HTML_TAG.sub("", t), quote=False)
    t = re.sub(r"^\s*[-+]\s+", "• ", t, flags=re.MULTILINE)
    t = _MULTI_NL.sub("\n\n", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*", r"<b>\1</b>", t)
    t = re.sub(r"__(.+?)__", r"<u>\1</u>", t)
    t = re.sub(r"(?<!_)_(?!\s)(.+?)(?<!\s)_", r"<i>\1</i>", t)
    t = _INLINE_CODE.sub(lambda m: f"<code>{html.escape(m.group(1), quote=False)}</code>", t)
    for i, (lang, code) in enumerate(blocks):
        safe_code = html.escape(code.replace("```", "''' "), quote=False)
        block = f"<pre><code class=\"language-{lang}\">{safe_code}</code></pre>" if lang else f"<pre>{safe_code}</pre>"
        t = t.replace(f"\x00CODE{i}\x00", block)
    return t.strip()


def chunk_text(text: str, limit: int = 3800):
    """Split keeping code fences intact when possible."""
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


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

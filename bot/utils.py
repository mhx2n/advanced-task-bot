import re
import html

# Strict cleaner — strips ALL markup. Used for safe plain text broadcast.
_STRIP_INLINE = re.compile(r"[`*_~#>|]")
_LATEX_BLOCK = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_LATEX_INLINE = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]|\$(.+?)\$", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")
_CODE_FENCE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)


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
    """Mira-bot-style: preserve ```code blocks``` and basic markdown.
    Strips LaTeX/HTML but KEEPS code fences so Telegram renders them nicely.
    Returns text safe to send with parse_mode='Markdown' (legacy)."""
    if not text:
        return ""
    # Extract code blocks first to protect them
    blocks = []

    def _stash(m):
        lang = (m.group(1) or "").strip()
        code = m.group(2).rstrip()
        idx = len(blocks)
        blocks.append((lang, code))
        return f"\x00CODE{idx}\x00"

    t = _CODE_FENCE.sub(_stash, text)
    # Clean LaTeX & HTML outside code
    t = _LATEX_BLOCK.sub("", t)
    t = _LATEX_INLINE.sub(lambda m: next((g for g in m.groups() if g), ""), t)
    t = _HTML_TAG.sub("", t)
    # Convert lone markdown list bullets to •
    t = re.sub(r"^\s*[-+]\s+", "• ", t, flags=re.MULTILINE)
    # Escape stray backticks (single) so Markdown doesn't break — but keep inline `x`
    # legacy Markdown only treats `code`, *bold*, _italic_. We'll leave those.
    t = _MULTI_NL.sub("\n\n", t)
    # Restore code blocks
    for i, (lang, code) in enumerate(blocks):
        # Escape backticks inside code so the fence isn't broken
        safe_code = code.replace("```", "''' ")
        fence = f"```{lang}\n{safe_code}\n```" if lang else f"```\n{safe_code}\n```"
        t = t.replace(f"\x00CODE{i}\x00", fence)
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

"""Decide whether a QQ reply is text, voice, or both.

These functions do not import AstrBot. The plugin calls them from its own
hooks and does not change any other plugin's messages.
"""

from __future__ import annotations

import re

try:
    from .speech import MAX_SPEECH_CHARS, prepare_speech_text
except ImportError as exc:  # Pytest loads this file from the plugin directory.
    if "known parent package" not in str(exc):
        raise
    from speech import MAX_SPEECH_CHARS, prepare_speech_text

MODES = ("both", "voice", "smart")
FALLBACKS = ("both", "text")
VOICE_MARKER = "<voice-only/>"
DEFAULT_KEYWORDS = "语音,说给我听,读给我听,念给我听,voice"
VOICE_HINT = (
    "若这条回复更适合听而不是读（短聊、问候、安慰或朗读），"
    "请在回复最开头单独写 <voice-only/>。"
    "事实、列表、链接、代码和较长的回答不要加这个标记。"
    " If this reply is better heard than read (a short chat line, a greeting, "
    "comfort, or reading aloud), start it with <voice-only/>. "
    "Do not use the marker for facts, lists, links, code, or long answers."
)

_MARKER = re.compile(r"<voice-only\s*/>", re.IGNORECASE)
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_URL = re.compile(r"https?://\S+")
_MARKDOWN_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\)")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_NEWLINE = re.compile(r" *\n *")


def fully_speakable(text: str) -> bool:
    """True when the voice would say the whole reply, without cutting it."""
    raw = text or ""
    if (
        _CODE_FENCE.search(raw)
        or _INLINE_CODE.search(raw)
        or _URL.search(raw)
        or _MARKDOWN_LINK.search(raw)
    ):
        return False
    spoken = prepare_speech_text(raw, limit=max(len(raw), MAX_SPEECH_CHARS) + 1)
    return bool(spoken) and len(spoken) <= MAX_SPEECH_CHARS


def strip_marker(text: str) -> tuple[str, bool]:
    """Remove ``<voice-only/>`` wherever it sits. The bool says one was found."""
    raw = text or ""
    cleaned, count = _MARKER.subn(" ", raw)
    if not count:
        return raw, False
    cleaned = _SPACES.sub(" ", cleaned)
    cleaned = _SPACE_NEWLINE.sub("\n", cleaned)
    return cleaned.strip(), True


def keyword_hit(user_text: str, keywords: str) -> bool:
    """True when the user's message contains one configured phrase."""
    haystack = (user_text or "").casefold()
    if not haystack:
        return False
    for phrase in re.split(r"[,，]", keywords or ""):
        token = phrase.strip()
        if token and token.casefold() in haystack:
            return True
    return False


def choose(
    mode: str,
    text: str,
    *,
    agent: bool,
    user_voice: bool,
    user_text: str,
    keywords: str,
    fallback: str,
) -> str:
    """Return ``both``, ``voice``, or ``text`` for one reply."""
    selected = mode if mode in MODES else "both"
    if selected == "both":
        return "both"
    voice = "voice" if fully_speakable(text) else "both"
    if selected == "voice":
        return voice
    quiet = fallback if fallback in FALLBACKS else "both"
    signaled = bool(agent) or bool(user_voice) or keyword_hit(user_text, keywords)
    return voice if signaled else quiet

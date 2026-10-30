"""Reply-mode decisions stay pure: no AstrBot, no network."""

from reply_mode import (
    DEFAULT_KEYWORDS,
    choose,
    fully_speakable,
    keyword_hit,
    strip_marker,
)


def test_fully_speakable_rejects_text_the_voice_would_drop() -> None:
    assert fully_speakable("**你好**")
    assert not fully_speakable("看 https://example.com/a")
    assert not fully_speakable("用 `code` 这个词")
    assert not fully_speakable("```py\nprint(1)\n```")
    assert not fully_speakable("[链接](https://example.com)")
    assert not fully_speakable("你好。" * 200)
    assert not fully_speakable("```code```")


def test_strip_marker_finds_the_token_in_any_position() -> None:
    assert strip_marker("你好") == ("你好", False)
    assert strip_marker("<voice-only/>你好") == ("你好", True)
    assert strip_marker("前文 <voice-only/> 后文") == ("前文 后文", True)
    assert strip_marker("听完了 <Voice-Only/>") == ("听完了", True)
    assert strip_marker("<voice-only/>\n第二行") == ("第二行", True)


def test_choose_uses_one_smart_signal_or_the_fallback() -> None:
    assert choose(
        "both",
        "你好",
        agent=True,
        user_voice=True,
        user_text="语音",
        keywords=DEFAULT_KEYWORDS,
        fallback="text",
    ) == "both"
    assert choose(
        "voice",
        "你好",
        agent=False,
        user_voice=False,
        user_text="",
        keywords="",
        fallback="text",
    ) == "voice"
    assert choose(
        "voice",
        "看 https://example.com",
        agent=False,
        user_voice=False,
        user_text="",
        keywords="",
        fallback="text",
    ) == "both"
    quiet = {
        "mode": "smart",
        "text": "你好",
        "agent": False,
        "user_voice": False,
        "user_text": "今天吃什么",
        "keywords": DEFAULT_KEYWORDS,
    }
    assert choose(**quiet, fallback="text") == "text"
    assert choose(**quiet, fallback="both") == "both"
    assert choose(**{**quiet, "agent": True}, fallback="text") == "voice"
    assert choose(**{**quiet, "user_voice": True}, fallback="text") == "voice"
    assert choose(**{**quiet, "user_text": "请读给我听"}, fallback="text") == "voice"
    assert keyword_hit("Please use your Voice", DEFAULT_KEYWORDS)
    assert choose(
        "smart",
        "看 https://example.com",
        agent=True,
        user_voice=False,
        user_text="",
        keywords=DEFAULT_KEYWORDS,
        fallback="text",
    ) == "both"

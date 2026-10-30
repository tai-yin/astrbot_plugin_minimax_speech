"""MiniMax synchronous speech and the WAV contract used by QQ voice.

AstrBot 4.29.3's QQ official adapter (WebSocket and webhook share
``QQOfficialMessageEvent``) resolves a ``Record`` with
``MediaResolver(..., target_format="tencent_silk")``. That path requires a
readable WAV, encodes it with ``pysilk`` (``tencent=True``), and uploads the
Silk bytes to ``POST /v2/users/{openid}/files`` or
``POST /v2/groups/{group_openid}/files`` with ``file_type=3``. The send call
then uses ``msg_type=7`` and ``media.file_info``.

The OneBot adapter (``aiocqhttp``, including NapCat) does not use that
upload. It base64-encodes the same WAV via ``Record.convert_to_base64``
(``target_format="wav"``) and sends a record segment. NapCat then encodes
Silk itself. The file must stay WAV: a Silk file here would be decoded as
WAV by that adapter.

This module asks MiniMax T2A v2 for 24 kHz mono 16-bit PCM, which is a rate
the Silk encoder accepts without resampling, and wraps it in a WAV file.
"""

from __future__ import annotations

import io
import re
import wave
from typing import Any, NamedTuple
from urllib.parse import urlparse

import aiohttp

DEFAULT_API_BASE = "https://api.minimax.io"
# The voice catalog differs by host. Values are the origins sent to MiniMax.
API_BASES = (
    ("https://api.minimax.io", "International"),
    ("https://api.minimax.cn", "China"),
)
DEFAULT_VOICE_ID = "male-qn-qingse"
# T2A v2 model ids. HD and Turbo share a price tier on each host.
SPEECH_MODELS = (
    ("speech-2.8-hd", "Speech 2.8 HD", "Higher quality"),
    ("speech-2.8-turbo", "Speech 2.8 Turbo", "Faster and lower cost"),
    ("speech-2.6-hd", "Speech 2.6 HD", "Previous HD"),
    ("speech-2.6-turbo", "Speech 2.6 Turbo", "Previous Turbo"),
    ("speech-02-hd", "Speech 02 HD", "Older HD"),
    ("speech-02-turbo", "Speech 02 Turbo", "Older Turbo"),
    ("speech-01-hd", "Speech 01 HD", "Oldest HD"),
    ("speech-01-turbo", "Speech 01 Turbo", "Oldest Turbo"),
)
SPEECH_MODEL = SPEECH_MODELS[0][0]
# 24 kHz is in both the MiniMax audio_setting list and the Silk rates
# AstrBot 4.29.3 accepts: 8000, 12000, 16000, 24000, 32000, 48000.
QQ_VOICE_SAMPLE_RATE = 24000
QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
# OneBot v11. NapCat, Lagrange, and LLOneBot all use this AstrBot adapter.
AIOCQHTTP_PLATFORM = "aiocqhttp"
SPEECH_PLATFORMS = QQ_OFFICIAL_PLATFORMS | frozenset({AIOCQHTTP_PLATFORM})
# Keeps a voice reply inside a short QQ voice bubble. The text message is
# not truncated; only the spoken copy is.
MAX_SPEECH_CHARS = 400

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_MARK = re.compile(r"[#>*_~]+")
_EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]+")
_WHITESPACE = re.compile(r"\s+")


class SpeechAudio(NamedTuple):
    """Decoded MiniMax audio plus the character count the response reported."""

    data: bytes
    usage_characters: int | None
    media_type: str


class VoiceChoice(NamedTuple):
    """One selectable MiniMax voice.

    MiniMax does not return language or gender. Language comes from the
    voice id prefix used in the system-voice catalog. Gender is inferred
    from the id, name, and description.
    """

    voice_id: str
    label: str
    subtitle: str
    language: str
    gender: str


class MinimaxSpeechError(Exception):
    """MiniMax returned an error or audio this plugin cannot turn into WAV."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        trace_id: str = "",
    ) -> None:
        self.status_code = status_code
        self.trace_id = trace_id
        super().__init__(message)


def is_qq_official_platform(platform_name: str) -> bool:
    return platform_name in QQ_OFFICIAL_PLATFORMS


def is_speech_platform(platform_name: str) -> bool:
    """True for QQ official bots and the OneBot ``aiocqhttp`` adapter."""
    return platform_name in SPEECH_PLATFORMS


def normalize_api_base(url: str) -> str:
    """Accept a host or a full T2A URL and return the origin only."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return DEFAULT_API_BASE
    lowered = base.lower()
    for suffix in ("/v1/t2a_v2", "/v1"):
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/") or DEFAULT_API_BASE


def apply_api_base_dropdown(schema: dict[str, Any], current: str) -> None:
    """Show the MiniMax hosts as a dropdown.

    A saved host that is not one of the presets stays selectable. The voice
    list itself is loaded from the selected host when the plugin reloads.
    """
    field = schema.get("api_base")
    if not isinstance(field, dict):
        return
    options = [url for url, _label in API_BASES]
    labels = [label for _url, label in API_BASES]
    subtitles = list(options)
    selected = normalize_api_base(current)
    if selected not in options:
        options.append(selected)
        labels.append(selected)
        subtitles.append("Current selection")
    field["options"] = options
    field["labels"] = labels
    field["subtitles"] = subtitles


def prepare_speech_text(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """Turn a bot reply into a short string that is safe to read aloud."""
    cleaned = _CODE_FENCE.sub(" ", text or "")
    cleaned = _MD_IMAGE.sub(" ", cleaned)
    cleaned = _MD_LINK.sub(r"\1", cleaned)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _MD_MARK.sub("", cleaned)
    cleaned = _EMOJI.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if len(cleaned) > limit:
        clipped = cleaned[:limit]
        cut = max((clipped.rfind(mark) for mark in "。！？!?；;"), default=-1)
        cleaned = clipped[: cut + 1] if cut >= limit // 2 else clipped
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in cleaned):
        return ""
    return cleaned


def normalize_speech_model(model: str) -> str:
    """Return a documented T2A model id, or the default HD model."""
    chosen = (model or "").strip()
    if chosen in {model_id for model_id, _label, _subtitle in SPEECH_MODELS}:
        return chosen
    return SPEECH_MODEL


def build_t2a_payload(
    text: str,
    voice_id: str,
    model: str = SPEECH_MODEL,
    audio_format: str = "pcm",
) -> dict[str, Any]:
    """Build the non-streaming T2A v2 body from the current MiniMax HTTP spec."""
    return {
        "model": normalize_speech_model(model),
        "text": text,
        "stream": False,
        "output_format": "hex",
        "language_boost": "auto",
        "voice_setting": {
            "voice_id": voice_id,
            "speed": 1,
            "vol": 1,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": QQ_VOICE_SAMPLE_RATE,
            "format": audio_format,
            "channel": 1,
        },
    }


def decode_audio_field(encoded: str) -> bytes:
    """Decode T2A ``data.audio``. The spec uses hex; some gateways send base64."""
    payload = (encoded or "").strip()
    if not payload:
        raise MinimaxSpeechError("MiniMax response did not include audio")
    if len(payload) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in payload):
        try:
            return bytes.fromhex(payload)
        except ValueError:
            pass
    try:
        import base64

        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise MinimaxSpeechError(
            "MiniMax audio field was neither hex nor base64"
        ) from exc


def pcm_to_wav(pcm: bytes, sample_rate: int = QQ_VOICE_SAMPLE_RATE) -> bytes:
    """Wrap signed 16-bit little-endian mono PCM in a WAV container."""
    if sample_rate <= 0:
        raise MinimaxSpeechError(f"invalid sample rate: {sample_rate}")
    if not pcm or len(pcm) % 2 != 0:
        raise MinimaxSpeechError("PCM audio is empty or not 16-bit aligned")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def wav_info(wav_bytes: bytes) -> tuple[int, int, int, int]:
    """Return ``(channels, sample_width, sample_rate, frame_count)``."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        return (
            wav_file.getnchannels(),
            wav_file.getsampwidth(),
            wav_file.getframerate(),
            wav_file.getnframes(),
        )


def usage_characters_from(payload: dict[str, Any]) -> int | None:
    """Read ``extra_info.usage_characters`` when MiniMax billed the call."""
    extra = payload.get("extra_info") or {}
    if not isinstance(extra, dict):
        return None
    raw = extra.get("usage_characters")
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def raise_for_base_resp(payload: dict[str, Any], fallback: str) -> None:
    """Raise when MiniMax reports a non-zero ``base_resp.status_code``."""
    base = payload.get("base_resp") or {}
    status_code = base.get("status_code", 0)
    if status_code in (0, None):
        return
    message = base.get("status_msg") or fallback
    raise MinimaxSpeechError(
        str(message),
        status_code=int(status_code),
        trace_id=str(payload.get("trace_id") or ""),
    )


_VOICE_BUCKETS = (
    ("system_voice", "System voice"),
    ("voice_cloning", "Cloned voice"),
    ("voice_generation", "Generated voice"),
)
# Longest prefix first so "Chinese (Mandarin)" wins over a shorter match.
_LANGUAGE_PREFIXES = (
    "Chinese (Mandarin)",
    "Arabic",
    "Cantonese",
    "Czech",
    "Dutch",
    "English",
    "Finnish",
    "French",
    "German",
    "Greek",
    "Hindi",
    "Indonesian",
    "Italian",
    "Japanese",
    "Korean",
    "Polish",
    "Portuguese",
    "Romanian",
    "Russian",
    "Spanish",
    "Thai",
    "Turkish",
    "Ukrainian",
    "Vietnamese",
)
_FEMALE_TOKENS = (
    "girlfriend",
    "hostess",
    "princess",
    "maiden",
    "woman",
    "queen",
    "lady",
    "aunt",
    "antie",
    "girl",
    "sister",
    "female",
    "wife",
    "miss",
    "(f)",
    "女",
)
_MALE_TOKENS = (
    "boyfriend",
    "gentleman",
    "husband",
    "brother",
    "butler",
    "knight",
    "bloke",
    "male",
    "boy",
    "guy",
    "man",
    "(m)",
    "男",
)
ANY = "any"
GENDER_LABELS = {
    ANY: "Any",
    "female": "Female",
    "male": "Male",
    "unknown": "Unspecified",
}
SELECTOR_GENDERS = (ANY, "female", "male", "unknown")
VOICE_ROW_HINT = "Changes with the language and gender above."


def selector_languages() -> tuple[str, ...]:
    """Languages the voice row can filter on, including Any and Other."""
    return (ANY, *_LANGUAGE_PREFIXES, "Other")


def selector_key(language: str, gender: str) -> str:
    """Config key for the voice list shown for one language and gender.

    ``any`` / ``any`` stays ``voice_id`` so older configs keep working.
    """
    if language == ANY and gender == ANY:
        return "voice_id"
    slug = re.sub(r"[^a-z0-9]+", "_", language.lower()).strip("_") or "other"
    return f"voice_{slug}_{gender}"


def iter_voice_selectors() -> tuple[tuple[str, str, str], ...]:
    """``(language, gender, config key)`` for every voice dropdown."""
    return tuple(
        (language, gender, selector_key(language, gender))
        for language in selector_languages()
        for gender in SELECTOR_GENDERS
    )


def voice_selector_schema() -> dict[str, dict[str, Any]]:
    """Static dropdowns. AstrBot shows the one whose condition matches.

    The dashboard applies ``condition`` to the form as it is edited, so
    changing language or gender swaps the visible voice list immediately.
    One shared ``voice_id`` list cannot do that.
    """
    fields: dict[str, dict[str, Any]] = {}
    for language, gender, key in iter_voice_selectors():
        fields[key] = {
            "description": "Voice",
            "type": "string",
            "hint": VOICE_ROW_HINT,
            "options": [DEFAULT_VOICE_ID],
            "labels": [DEFAULT_VOICE_ID],
            "default": DEFAULT_VOICE_ID if key == "voice_id" else "",
            "condition": {"language": language, "gender": gender},
        }
    return fields


def _voice_subtitle(item: dict[str, Any], fallback: str) -> str:
    description = item.get("description")
    if isinstance(description, list):
        text = next(
            (str(part).strip() for part in description if str(part).strip()), ""
        )
    elif isinstance(description, str):
        text = description.strip()
    else:
        text = ""
    subtitle = text or fallback
    return subtitle if len(subtitle) <= 160 else subtitle[:157] + "..."


def voice_language(voice_id: str) -> str:
    """Return the catalog language prefix, or ``Other`` when there is none."""
    for prefix in _LANGUAGE_PREFIXES:
        if voice_id == prefix or voice_id.startswith((prefix + "_", prefix + " ")):
            return prefix
    return "Other"


def _has_token(haystack: str, token: str) -> bool:
    if token in {"女", "男", "(f)", "(m)"}:
        return token in haystack
    start = 0
    while True:
        index = haystack.find(token, start)
        if index < 0:
            return False
        after_index = index + len(token)
        after = haystack[after_index] if after_index < len(haystack) else ""
        # "man" is the start of "mandarin" and the end of "woman".
        if after.isalpha():
            start = index + 1
            continue
        if token == "man" and haystack[max(0, index - 2) : index] in {"wo", "hu"}:
            start = index + 1
            continue
        if token == "male" and haystack[max(0, index - 2) : index] == "fe":
            start = index + 1
            continue
        return True


def voice_gender(*parts: str) -> str:
    """Infer female, male, or unknown. ``female`` is not treated as male."""
    haystack = " ".join(part for part in parts if part).lower()
    female = any(_has_token(haystack, token) for token in _FEMALE_TOKENS)
    male = any(_has_token(haystack, token) for token in _MALE_TOKENS)
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return "unknown"


def voices_from_response(payload: dict[str, Any]) -> list[VoiceChoice]:
    """Read ``POST /v1/get_voice`` with ``voice_type=all``.

    System voices use ``voice_name`` as the label. Cloned and generated voices
    usually have no name, so the label is the ``voice_id``.
    """
    raise_for_base_resp(payload, "MiniMax voice list failed")
    choices: list[VoiceChoice] = []
    seen: set[str] = set()

    def add(item: object, fallback: str) -> None:
        if not isinstance(item, dict):
            return
        voice_id = str(item.get("voice_id") or "").strip()
        if not voice_id or voice_id in seen:
            return
        seen.add(voice_id)
        label = str(item.get("voice_name") or "").strip() or voice_id
        subtitle = _voice_subtitle(item, fallback)
        choices.append(
            VoiceChoice(
                voice_id,
                label,
                subtitle,
                voice_language(voice_id),
                voice_gender(voice_id, label, subtitle),
            )
        )

    for key, fallback in _VOICE_BUCKETS:
        for item in payload.get(key) or []:
            add(item, fallback)
    return choices


def voice_buckets(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Voice ids in each ``get_voice`` bucket. System voices stay separate."""
    raise_for_base_resp(payload, "MiniMax voice list failed")
    found: dict[str, list[str]] = {}
    for key, _fallback in _VOICE_BUCKETS:
        ids: list[str] = []
        seen: set[str] = set()
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            voice_id = str(item.get("voice_id") or "").strip()
            if not voice_id or voice_id in seen:
                continue
            seen.add(voice_id)
            ids.append(voice_id)
        found[key] = ids
    return found


def reply_voice_updates(voice_id: str, language: str, gender: str) -> dict[str, str]:
    """Config writes that make ``voice_id`` the QQ reply voice.

    When the voice matches the current language and gender, only that
    voice list changes. Otherwise the filters move to the voice's own
    language and gender so the dashboard shows the list that holds it.
    """
    language = (language or ANY).strip() or ANY
    gender = (gender or ANY).strip() or ANY
    choice = VoiceChoice(
        voice_id,
        voice_id,
        "",
        voice_language(voice_id),
        voice_gender(voice_id),
    )
    updates: dict[str, str] = {}
    if not _matches(choice, language, gender):
        language = choice.language
        gender = choice.gender
        updates["language"] = language
        updates["gender"] = gender
    updates[selector_key(language, gender)] = voice_id
    return updates


def _set_dropdown(
    schema: dict[str, Any],
    key: str,
    options: list[str],
    labels: list[str],
    subtitles: list[str] | None = None,
) -> None:
    field = schema.get(key)
    if not isinstance(field, dict):
        return
    field["options"] = options
    field["labels"] = labels
    if subtitles is not None:
        field["subtitles"] = subtitles


def _matches(voice: VoiceChoice, language: str, gender: str) -> bool:
    if language != ANY and voice.language != language:
        return False
    return gender == ANY or voice.gender == gender


def _install_selector_fields(schema: dict[str, Any]) -> None:
    """Keep every voice list in the schema, directly under gender."""
    templates = voice_selector_schema()
    passthrough = [
        (key, value) for key, value in schema.items() if key not in templates
    ]
    ordered: dict[str, Any] = {}
    inserted = False
    for key, value in passthrough:
        ordered[key] = value
        if key != "gender":
            continue
        inserted = True
        for sel_key, template in templates.items():
            current = schema.get(sel_key)
            ordered[sel_key] = current if isinstance(current, dict) else dict(template)
    if not inserted:
        for sel_key, template in templates.items():
            current = schema.get(sel_key)
            ordered[sel_key] = current if isinstance(current, dict) else dict(template)
    schema.clear()
    schema.update(ordered)


def _remember_choice(options: list[str], saved_value: str, preferred: str) -> str:
    if saved_value in options:
        return saved_value
    if preferred in options:
        return preferred
    return options[0]


def apply_voice_dropdown(
    schema: dict[str, Any],
    voices: list[VoiceChoice],
    *,
    language: str,
    gender: str,
    current_voice_id: str,
    saved_values: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Fill language, gender, and one voice dropdown per filter pair.

    AstrBot cannot filter the options of one dropdown from another. Each
    pair has its own list, and ``condition`` shows only the matching list
    while the form is being edited. Returns the voice id for the active
    pair, and config values that should be stored for lists whose saved
    id is no longer valid.
    """
    language = language or ANY
    gender = gender or ANY
    saved = saved_values or {}
    languages = [ANY]
    seen_languages = {ANY}
    for voice in voices:
        if voice.language not in seen_languages:
            seen_languages.add(voice.language)
            languages.append(voice.language)
    if language not in seen_languages:
        languages.append(language)
    languages.sort(key=lambda item: (item != ANY, item == "Other", item.lower()))
    _install_selector_fields(schema)
    language_field = schema.get("language")
    if isinstance(language_field, dict):
        language_field["hint"] = (
            "The voice list updates when this changes. Taken from the voice id prefix."
        )
    _set_dropdown(
        schema,
        "language",
        languages,
        ["Any" if item == ANY else item for item in languages],
    )
    gender_options = list(SELECTOR_GENDERS)
    if gender not in gender_options:
        gender_options.append(gender)
    gender_field = schema.get("gender")
    if isinstance(gender_field, dict):
        gender_field["hint"] = (
            "The voice list updates when this changes. Inferred from the voice name."
        )
    _set_dropdown(
        schema,
        "gender",
        gender_options,
        [GENDER_LABELS.get(item, item) for item in gender_options],
    )

    preferred = (current_voice_id or "").strip()
    updates: dict[str, str] = {}
    active = preferred or DEFAULT_VOICE_ID
    for sel_language, sel_gender, key in iter_voice_selectors():
        field = schema.get(key)
        if not isinstance(field, dict):
            continue
        filtered = [
            voice for voice in voices if _matches(voice, sel_language, sel_gender)
        ]
        field["condition"] = {"language": sel_language, "gender": sel_gender}
        field["invisible"] = False
        saved_value = str(saved.get(key) or "").strip()
        if not voices:
            fallback = saved_value or preferred or DEFAULT_VOICE_ID
            field["options"] = [fallback]
            field["labels"] = [fallback]
            field["subtitles"] = [
                "Save an API key, then reopen this page to load voices."
            ]
            field["hint"] = VOICE_ROW_HINT
            chosen = fallback
        elif not filtered:
            field["options"] = []
            field["labels"] = []
            field["subtitles"] = []
            field["hint"] = "No voices match this language and gender."
            chosen = ""
        else:
            field["options"] = [voice.voice_id for voice in filtered]
            field["labels"] = [voice.label for voice in filtered]
            field["subtitles"] = [voice.subtitle for voice in filtered]
            field["hint"] = VOICE_ROW_HINT
            chosen = _remember_choice(field["options"], saved_value, preferred)
        if chosen and chosen != saved_value:
            updates[key] = chosen
        if sel_language == language and sel_gender == gender and chosen:
            active = chosen
    return active, updates


def wav_bytes_from_response(payload: dict[str, Any]) -> bytes:
    """Validate a T2A JSON body and return 16-bit mono PCM WAV bytes."""
    raise_for_base_resp(payload, "MiniMax speech request failed")
    data = payload.get("data") or {}
    raw = decode_audio_field(str(data.get("audio") or ""))
    extra = payload.get("extra_info") or {}
    audio_format = str(extra.get("audio_format") or "pcm").lower()
    sample_rate = int(extra.get("audio_sample_rate") or QQ_VOICE_SAMPLE_RATE)
    if raw[:4] == b"RIFF" or audio_format == "wav":
        channels, width, rate, frames = wav_info(raw)
        if channels != 1 or width != 2 or frames <= 0:
            raise MinimaxSpeechError(
                "MiniMax WAV is not 16-bit mono PCM",
                trace_id=str(payload.get("trace_id") or ""),
            )
        if rate != sample_rate and sample_rate > 0:
            # Trust the container. AstrBot resamples unsupported rates.
            pass
        return raw
    if audio_format not in ("pcm", ""):
        raise MinimaxSpeechError(
            f"MiniMax returned {audio_format}, but QQ voice needs PCM WAV",
            trace_id=str(payload.get("trace_id") or ""),
        )
    return pcm_to_wav(raw, sample_rate)


class SpeechClient:
    """Small async client for ``POST /v1/t2a_v2``."""

    def __init__(
        self,
        api_key: str,
        api_base: str = DEFAULT_API_BASE,
        *,
        timeout_seconds: float = 60,
    ) -> None:
        self.api_key = api_key
        self.api_base = normalize_api_base(api_base)
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # No default Content-Type: JSON posts set their own, and file
            # uploads need multipart.
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._session

    async def _read_json(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status
        try:
            body = await response.json(content_type=None)
        except ValueError as exc:
            raise MinimaxSpeechError(
                f"MiniMax response was not JSON (HTTP {status})"
            ) from exc
        if not isinstance(body, dict):
            raise MinimaxSpeechError("MiniMax response was not a JSON object")
        if status >= 400:
            base = body.get("base_resp") or {}
            raise MinimaxSpeechError(
                str(base.get("status_msg") or f"HTTP {status}"),
                status_code=int(base.get("status_code") or status),
                trace_id=str(body.get("trace_id") or ""),
            )
        return body

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = await self._session_get()
        try:
            async with session.post(f"{self.api_base}{path}", json=payload) as response:
                return await self._read_json(response)
        except aiohttp.ClientError as exc:
            raise MinimaxSpeechError(f"MiniMax request failed: {exc}") from exc

    async def get_voice_catalog(self) -> dict[str, Any]:
        """Return the raw ``POST /v1/get_voice`` body."""
        return await self._post("/v1/get_voice", {"voice_type": "all"})

    async def list_voices(self) -> list[VoiceChoice]:
        """Return every voice id on this MiniMax account."""
        return voices_from_response(await self.get_voice_catalog())

    async def upload_file(self, filename: str, content: bytes, purpose: str) -> int:
        """Upload one audio file. ``purpose`` is ``voice_clone`` or ``prompt_audio``."""
        if purpose not in {"voice_clone", "prompt_audio"}:
            raise MinimaxSpeechError(f"unsupported upload purpose: {purpose}")
        form = aiohttp.FormData()
        form.add_field("purpose", purpose)
        form.add_field(
            "file",
            content,
            filename=filename or "audio.mp3",
            content_type="application/octet-stream",
        )
        session = await self._session_get()
        try:
            async with session.post(
                f"{self.api_base}/v1/files/upload",
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                body = await self._read_json(response)
        except aiohttp.ClientError as exc:
            raise MinimaxSpeechError(f"MiniMax upload failed: {exc}") from exc
        raise_for_base_resp(body, "MiniMax file upload failed")
        file_info = body.get("file") or {}
        file_id = file_info.get("file_id") if isinstance(file_info, dict) else None
        if file_id is None:
            raise MinimaxSpeechError("MiniMax upload did not return file_id")
        return int(file_id)

    async def clone_voice(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/voice_clone``. The caller builds the documented body."""
        body = await self._post("/v1/voice_clone", payload)
        raise_for_base_resp(body, "MiniMax voice clone failed")
        return body

    async def design_voice(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/voice_design``."""
        body = await self._post("/v1/voice_design", payload)
        raise_for_base_resp(body, "MiniMax voice design failed")
        return body

    async def delete_voice(self, voice_id: str, voice_type: str) -> dict[str, Any]:
        """Delete a cloned or designed voice. The id cannot be reused."""
        body = await self._post(
            "/v1/delete_voice",
            {"voice_type": voice_type, "voice_id": voice_id},
        )
        raise_for_base_resp(body, "MiniMax voice delete failed")
        return body

    async def fetch_audio(self, url: str, *, limit: int = 25 * 1024 * 1024) -> bytes:
        """Download a clone preview URL. Only http and https are accepted."""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MinimaxSpeechError("MiniMax preview URL was not http(s)")
        session = await self._session_get()
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                if response.status >= 400:
                    raise MinimaxSpeechError(
                        f"MiniMax preview download failed (HTTP {response.status})"
                    )
                data = await response.content.read(limit + 1)
        except aiohttp.ClientError as exc:
            raise MinimaxSpeechError(f"MiniMax preview download failed: {exc}") from exc
        if len(data) > limit:
            raise MinimaxSpeechError("MiniMax preview audio was too large")
        if not data:
            raise MinimaxSpeechError("MiniMax preview audio was empty")
        return data

    async def synthesize(
        self, text: str, voice_id: str, model: str = SPEECH_MODEL
    ) -> bytes:
        spoken = prepare_speech_text(text)
        if not spoken:
            raise MinimaxSpeechError("nothing left to speak after cleaning the text")
        audio = await self.synthesize_audio(
            spoken,
            voice_id,
            model,
            audio_format="pcm",
            limit=len(spoken),
        )
        return audio.data

    async def synthesize_audio(
        self,
        text: str,
        voice_id: str,
        model: str = SPEECH_MODEL,
        *,
        audio_format: str = "mp3",
        limit: int = 1000,
    ) -> SpeechAudio:
        """Synthesize speech and keep MiniMax's billed character count.

        ``pcm`` comes back as WAV for QQ. ``mp3`` is the bytes MiniMax
        returned, for playback in the MiniMax voice studio.
        """
        spoken = prepare_speech_text(text, limit=limit)
        if not spoken:
            raise MinimaxSpeechError("nothing left to speak after cleaning the text")
        chosen = audio_format if audio_format in {"pcm", "mp3"} else "mp3"
        body = await self._post(
            "/v1/t2a_v2",
            build_t2a_payload(
                spoken,
                voice_id or DEFAULT_VOICE_ID,
                normalize_speech_model(model),
                chosen,
            ),
        )
        usage = usage_characters_from(body)
        if chosen == "pcm":
            return SpeechAudio(wav_bytes_from_response(body), usage, "audio/wav")
        raise_for_base_resp(body, "MiniMax speech request failed")
        data = body.get("data") or {}
        raw = decode_audio_field(str(data.get("audio") or ""))
        return SpeechAudio(raw, usage, "audio/mpeg")

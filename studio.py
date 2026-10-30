"""MiniMax voice studio ledger, price estimates, and error text.

Draft clones and designs are not listed by MiniMax until they are used, so
this module remembers them per API host. Preview audio is cached here and
sent to the page as base64 because the dashboard bridge only returns JSON.

List prices were checked on 2026-10-30:

- https://platform.minimax.io/docs/guides/pricing-paygo
- https://platform.minimax.cn/docs/pricing/overview

speech-01 is not given its own row; it uses the HD or Turbo tier.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from .speech import (
        API_BASES,
        SPEECH_MODELS,
        MinimaxSpeechError,
        decode_audio_field,
        normalize_api_base,
        normalize_speech_model,
        usage_characters_from,
        voice_buckets,
        voice_gender,
        voice_language,
        voices_from_response,
    )
except ImportError as exc:  # Pytest loads this file from the plugin directory.
    if "known parent package" not in str(exc):
        raise
    from speech import (
        API_BASES,
        SPEECH_MODELS,
        MinimaxSpeechError,
        decode_audio_field,
        normalize_api_base,
        normalize_speech_model,
        usage_characters_from,
        voice_buckets,
        voice_gender,
        voice_language,
        voices_from_response,
    )

log = logging.getLogger("astrbot_plugin_minimax_speech.studio")

PLUGIN_NAME = "astrbot_plugin_minimax_speech"
DRAFT_TTL_SECONDS = 168 * 60 * 60
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
SOURCE_MIN_SECONDS = 10.0
SOURCE_MAX_SECONDS = 5 * 60.0
PROMPT_MAX_SECONDS = 8.0
CLONE_TEXT_MAX = 1000
DESIGN_PREVIEW_MAX = 500
DESIGN_PROMPT_MAX = 2000
TEXT_VALIDATION_MAX = 200
KEEP_TEXT = "你好，这是保存音色的试听。"
PRICES_CHECKED = "2026-10-30"
# First character is a letter, last is not "-" or "_", length 8 to 256.
_VOICE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]$")
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
_BOOST = re.compile(r"^[A-Za-z][A-Za-z0-9 ,()_-]{0,63}$")
_AUDIO_SUFFIXES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav"}

STUDIO_ROUTES = (
    ("state", "state", ["GET"], "MiniMax voice studio state"),
    ("upload/source", "upload_source", ["POST"], "Upload clone source audio"),
    ("upload/prompt", "upload_prompt", ["POST"], "Upload clone example clip"),
    ("clone", "clone", ["POST"], "Clone a voice"),
    ("design", "design", ["POST"], "Design a voice"),
    ("takes/<voice_id>/audio", "take_audio", ["GET"], "Cached take preview"),
    ("audition", "audition", ["POST"], "Audition a sentence"),
    ("keep", "keep", ["POST"], "Keep a draft voice"),
    ("use", "use", ["POST"], "Use a voice for QQ replies"),
    ("delete", "delete", ["POST"], "Delete a voice"),
)

_INT_PRICING = "https://platform.minimax.io/docs/guides/pricing-paygo"
_CN_PRICING = "https://platform.minimax.cn/docs/pricing/overview"
_INT_BILLING = "https://platform.minimax.io/user-center/payment"
_CN_BILLING = "https://platform.minimaxi.com/user-center/payment"


class StudioError(Exception):
    """A voice-studio request that should not be sent to MiniMax."""

    def __init__(
        self,
        en: str,
        zh: str,
        *,
        status: int = 400,
        code: str = "invalid",
    ) -> None:
        self.en = en
        self.zh = zh
        self.http_status = status
        self.code = code
        super().__init__(en)

    def text(self, locale: str) -> str:
        if normalize_locale(locale) == "zh-CN":
            return self.zh
        return self.en


def normalize_locale(value: str) -> str:
    return "zh-CN" if str(value or "").lower().startswith("zh") else "en-US"


def _pair(en: str, zh: str) -> tuple[str, str]:
    return (en, zh)


_AUTH = _pair(
    "The API key was rejected. Check that it was issued for this host.",
    "API 密钥被拒绝。请确认它是为当前服务器签发的。",
)
_BALANCE = _pair(
    "The account balance or credit is too low.",
    "账户余额或积分不足。",
)
_AUDIO = _pair(
    "The audio length or quality was rejected. Use a clear clip within the limits.",
    "音频时长或音质未通过。请换一段清晰、时长符合要求的音频。",
)
_MATCH = _pair(
    "The audio did not match the transcript.",
    "音频和文字对不上。",
)
_CONTENT = _pair(
    "The content was rejected. Try different text or audio.",
    "内容未通过审核。请更换文字或音频。",
)
_BUSY = _pair(
    "MiniMax is busy. Wait a moment and try again.",
    "MiniMax 正忙。请稍后再试。",
)
_ERROR_TEXT = {
    1004: _AUTH,
    2049: _AUTH,
    1008: _BALANCE,
    2053: _BALANCE,
    2038: _pair(
        "Voice cloning needs individual or enterprise verification on this account.",
        "语音克隆需要先完成个人或企业认证。",
    ),
    2039: _pair(
        "That voice id is already used. Deleted ids cannot be reused.",
        "这个音色 ID 已经用过。删除后的 ID 不能再用。",
    ),
    2037: _AUDIO,
    2048: _AUDIO,
    2051: _AUDIO,
    1043: _MATCH,
    1044: _MATCH,
    1026: _CONTENT,
    1040: _CONTENT,
    1046: _pair(
        "The description was rejected because it names a real person.",
        "声音描述因为涉及真实人物被拒绝。",
    ),
    2052: _pair(
        "This account has no free voice slots. Delete a cloned voice or upgrade.",
        "音色名额已满。请删除一个克隆音色，或升级套餐。",
    ),
    1002: _BUSY,
    1045: _BUSY,
    2013: _pair(
        "MiniMax rejected the request. Check the audio and the voice id.",
        "MiniMax 拒绝了这次请求。请检查音频和音色 ID。",
    ),
    2054: _pair(
        "That voice no longer exists.",
        "这个音色已经不存在。",
    ),
}


def minimax_message(exc: MinimaxSpeechError, locale: str) -> str:
    """Plain-language text for a MiniMax status code."""
    pair = _ERROR_TEXT.get(exc.status_code or 0)
    if pair is not None:
        message = pair[1] if normalize_locale(locale) == "zh-CN" else pair[0]
    else:
        message = str(exc) or "MiniMax request failed"
    if exc.status_code:
        return f"{message} ({exc.status_code})"
    return message


def is_china_host(api_base: str) -> bool:
    host = normalize_api_base(api_base).split("://", 1)[-1].split("/", 1)[0]
    host = host.lower().split("@")[-1].split(":", 1)[0]
    return host.endswith(("minimax.cn", "minimaxi.com"))


def host_label(api_base: str) -> str:
    selected = normalize_api_base(api_base)
    for url, label in API_BASES:
        if selected == url:
            return label
    return selected


class _HostPrice:
    def __init__(
        self,
        *,
        currency: str,
        han_weight: int,
        hd_per_million: float,
        turbo_per_million: float,
        design_preview_per_million: float,
        clone_fee: float,
        design_fee: float,
        pricing_url: str,
        billing_url: str,
    ) -> None:
        self.currency = currency
        self.han_weight = han_weight
        self.hd_per_million = hd_per_million
        self.turbo_per_million = turbo_per_million
        self.design_preview_per_million = design_preview_per_million
        self.clone_fee = clone_fee
        self.design_fee = design_fee
        self.pricing_url = pricing_url
        self.billing_url = billing_url


_CHINA = _HostPrice(
    currency="CNY",
    han_weight=2,
    hd_per_million=350.0,
    turbo_per_million=200.0,
    design_preview_per_million=200.0,
    clone_fee=9.9,
    design_fee=9.9,
    pricing_url=_CN_PRICING,
    billing_url=_CN_BILLING,
)
_INTERNATIONAL = _HostPrice(
    currency="USD",
    han_weight=1,
    hd_per_million=100.0,
    turbo_per_million=60.0,
    design_preview_per_million=60.0,
    clone_fee=1.5,
    design_fee=3.0,
    pricing_url=_INT_PRICING,
    billing_url=_INT_BILLING,
)


def host_prices(api_base: str) -> _HostPrice:
    if is_china_host(api_base):
        return _CHINA
    return _INTERNATIONAL


def billed_characters(text: str, han_weight: int) -> int:
    """Count characters the way the published price page describes.

    On the China host a Han character counts as 2. The international page
    does not publish a rule, so this estimate counts every character as 1.
    """
    if han_weight == 1:
        return len(text or "")
    total = 0
    for char in text or "":
        total += 2 if "\u4e00" <= char <= "\u9fff" else 1
    return total


def _per_million(price: _HostPrice, model: str, kind: str) -> float:
    if kind == "design_preview":
        return price.design_preview_per_million
    if "turbo" in (model or ""):
        return price.turbo_per_million
    return price.hd_per_million


def quote_text(text: str, api_base: str, model: str, kind: str) -> dict[str, Any]:
    """Estimate the character charge for one piece of text."""
    price = host_prices(api_base)
    per_million = _per_million(price, model, kind)
    characters = billed_characters(text, price.han_weight)
    return {
        "characters": characters,
        "per_million": per_million,
        "amount": characters * per_million / 1_000_000,
        "currency": price.currency,
        "han_weight": price.han_weight,
    }


def one_time_fee(api_base: str, kind: str) -> float:
    price = host_prices(api_base)
    if kind == "design":
        return price.design_fee
    return price.clone_fee


def format_money(amount: float, currency: str, *, approximate: bool = False) -> str:
    symbol = "¥" if currency == "CNY" else "$"
    if amount == 0:
        body = f"{symbol}0.00"
    elif abs(amount) < 0.01:
        body = f"{symbol}{amount:.4f}"
    else:
        body = f"{symbol}{amount:.2f}"
    if approximate:
        return f"≈ {body}"
    return body


def pricing_public(api_base: str) -> dict[str, Any]:
    price = host_prices(api_base)
    rates = {
        model_id: (
            price.turbo_per_million if "turbo" in model_id else price.hd_per_million
        )
        for model_id, _label, _subtitle in SPEECH_MODELS
    }
    return {
        "checked": PRICES_CHECKED,
        "currency": price.currency,
        "han_weight": price.han_weight,
        "clone_fee": price.clone_fee,
        "design_fee": price.design_fee,
        "design_preview_per_million": price.design_preview_per_million,
        "speech_per_million": rates,
        "pricing_url": price.pricing_url,
        "billing_url": price.billing_url,
    }


def model_choices() -> list[dict[str, str]]:
    return [
        {"id": model_id, "label": label, "subtitle": subtitle}
        for model_id, label, subtitle in SPEECH_MODELS
    ]


def validate_voice_id(voice_id: str) -> str:
    chosen = (voice_id or "").strip()
    if not _VOICE_ID.fullmatch(chosen):
        raise StudioError(
            "Voice id must be 8 to 256 characters, start with a letter, "
            "and use only letters, digits, - and _. It cannot end with - or _.",
            "音色 ID 需要 8 到 256 个字符，以字母开头，只能包含字母、数字、- 和 _，"
            "并且不能以 - 或 _ 结尾。",
            code="voice_id",
        )
    return chosen


def suggest_voice_id(kind: str) -> str:
    prefix = "Design" if kind == "design" else "Clone"
    return prefix + uuid.uuid4().hex[:12]


def _missing_key() -> StudioError:
    return StudioError(
        "Add an API key in this plugin's settings, then reload the page.",
        "请先在插件设置里填写 API 密钥，然后重新打开这个页面。",
        code="missing_key",
    )


def _fee_required(api_base: str, kind: str) -> StudioError:
    label = format_money(one_time_fee(api_base, kind), host_prices(api_base).currency)
    return StudioError(
        f"This voice is still a draft. Confirm to pay the {label} one-time fee.",
        f"这个音色还是草稿。确认后才会收取 {label} 的一次性费用。",
        status=409,
        code="fee_required",
    )


def _sniff_audio(data: bytes) -> tuple[str, str]:
    if data[:4] == b"RIFF":
        return ".wav", "audio/wav"
    if data[:3] == b"ID3" or (
        len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    ):
        return ".mp3", "audio/mpeg"
    return ".mp3", "audio/mpeg"


def _flag(value: object) -> bool:
    return value is True


class VoiceStudio:
    """Per-host takes, cached previews, and the actions behind the page."""

    def __init__(
        self, data_dir: Path, *, now: Callable[[], float] | None = None
    ) -> None:
        self.data_dir = Path(data_dir)
        self._now = now or time.time
        self._lock = threading.Lock()

    def save_upload(
        self,
        kind: str,
        filename: str,
        content: bytes,
        *,
        limit: int = MAX_UPLOAD_BYTES,
    ) -> dict[str, Any]:
        if kind not in {"source", "prompt"}:
            raise StudioError("Unknown upload.", "未知的上传类型。")
        name = Path(filename or "").name or f"audio{'.wav' if kind == 'source' else '.mp3'}"
        suffix = Path(name).suffix.lower()
        if suffix not in _AUDIO_SUFFIXES:
            raise StudioError(
                "Use an mp3, m4a, or wav file.",
                "请使用 mp3、m4a 或 wav 文件。",
                code="file_type",
            )
        if not content:
            raise StudioError("The audio file is empty.", "音频文件是空的。")
        if len(content) > limit:
            raise StudioError(
                "The audio file is larger than 20 MB.",
                "音频文件超过 20 MB。",
                code="file_size",
            )
        self._sweep_uploads()
        upload_id = uuid.uuid4().hex
        folder = self.data_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{upload_id}{suffix}").write_bytes(content)
        (folder / f"{upload_id}.json").write_text(
            json.dumps({"name": name[:120], "suffix": suffix, "kind": kind}),
            encoding="utf-8",
        )
        return {"upload_id": upload_id, "filename": name, "bytes": len(content)}

    async def state(
        self,
        client: Any,
        api_base: str,
        model: str,
        reply_voice_id: str,
        locale: str,
    ) -> dict[str, Any]:
        base = {
            "has_key": client is not None,
            "api_base": normalize_api_base(api_base),
            "host_label": host_label(api_base),
            "model": normalize_speech_model(model),
            "pricing": pricing_public(api_base),
            "models": model_choices(),
            "keep_text": KEEP_TEXT,
            "suggested_clone_id": suggest_voice_id("clone"),
            "suggested_design_id": suggest_voice_id("design"),
        }
        if client is None:
            return base
        remote: dict[str, Any] | None = None
        warning = ""
        try:
            remote = await client.get_voice_catalog()
            voice_buckets(remote)
        except MinimaxSpeechError as exc:
            warning = minimax_message(exc, locale)
            remote = None

        def apply(host: dict[str, Any]) -> dict[str, Any]:
            self._sync_remote(host, remote)
            return {
                **base,
                "reply_voice_id": reply_voice_id,
                "spend": self._spend(host),
                "takes": self._public_takes(host, api_base, remote),
                "voices": self._system_voices(remote),
                "warning": warning,
            }

        return self._mutate(api_base, apply)

    async def clone(
        self, client: Any, api_base: str, model: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        if client is None:
            raise _missing_key()
        voice_id = validate_voice_id(str(fields.get("voice_id") or ""))
        self._reject_known(api_base, voice_id)
        text = str(fields.get("text") or "").strip()
        if len(text) > CLONE_TEXT_MAX:
            raise StudioError(
                "Preview text is longer than 1000 characters.",
                "试听文字超过 1000 个字符。",
            )
        source_id = str(fields.get("source_id") or "").strip()
        prompt_id = str(fields.get("prompt_id") or "").strip()
        prompt_text = str(fields.get("prompt_text") or "").strip()
        if not source_id:
            raise StudioError("Choose the source audio.", "请选择要克隆的音频。")
        if bool(prompt_id) != bool(prompt_text):
            raise StudioError(
                "The example clip and its transcript must be sent together.",
                "示例音频和它的文字需要一起填写。",
                code="prompt_pair",
            )
        self._require_duration("source", fields.get("source_seconds"))
        if prompt_id:
            self._require_duration("prompt", fields.get("prompt_seconds"))
        validation = str(fields.get("text_validation") or "").strip()
        if len(validation) > TEXT_VALIDATION_MAX:
            raise StudioError(
                "The expected transcript is longer than 200 characters.",
                "预期文字超过 200 个字符。",
            )
        accuracy = self._accuracy(fields.get("accuracy"))
        boost = self._boost(str(fields.get("language_boost") or ""))
        source_bytes, source_name = self._read_upload(source_id, "source")
        source_file_id = await client.upload_file(
            source_name, source_bytes, "voice_clone"
        )
        self._delete_upload(source_id)
        prompt_file_id = None
        if prompt_id:
            prompt_bytes, prompt_name = self._read_upload(prompt_id, "prompt")
            prompt_file_id = await client.upload_file(
                prompt_name, prompt_bytes, "prompt_audio"
            )
            self._delete_upload(prompt_id)
        payload: dict[str, Any] = {"file_id": source_file_id, "voice_id": voice_id}
        if text:
            payload["text"] = text
            payload["model"] = normalize_speech_model(model)
        if prompt_file_id is not None:
            payload["clone_prompt"] = {
                "prompt_audio": prompt_file_id,
                "prompt_text": prompt_text,
            }
        if boost:
            payload["language_boost"] = boost
        if validation:
            payload["text_validation"] = validation
            if accuracy is not None:
                payload["accuracy"] = accuracy
        if _flag(fields.get("need_noise_reduction")):
            payload["need_noise_reduction"] = True
        if _flag(fields.get("need_volume_normalization")):
            payload["need_volume_normalization"] = True
        body = await client.clone_voice(payload)
        audio = b""
        demo = str(body.get("demo_audio") or "").strip()
        if demo:
            try:
                audio = await client.fetch_audio(demo)
            except MinimaxSpeechError:
                log.warning("clone preview could not be downloaded")
        return self._record_preview(
            api_base,
            kind="clone",
            voice_id=voice_id,
            label=source_name,
            preview_text=text,
            prompt=prompt_text,
            audio=audio,
            usage=usage_characters_from(body),
            quote_kind="clone_preview",
            model=model,
            sensitive=bool(body.get("input_sensitive")),
        )

    async def design(
        self, client: Any, api_base: str, model: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        if client is None:
            raise _missing_key()
        prompt = str(fields.get("prompt") or "").strip()
        preview = str(fields.get("preview_text") or "").strip()
        if not prompt:
            raise StudioError("Describe the voice.", "请填写声音描述。")
        if len(prompt) > DESIGN_PROMPT_MAX:
            raise StudioError(
                "The description is longer than 2000 characters.",
                "声音描述超过 2000 个字符。",
            )
        if not preview:
            raise StudioError("Preview text is required.", "请填写试听文字。")
        if len(preview) > DESIGN_PREVIEW_MAX:
            raise StudioError(
                "Preview text is longer than 500 characters.",
                "试听文字超过 500 个字符。",
            )
        requested = str(fields.get("voice_id") or "").strip()
        if requested:
            voice_id = validate_voice_id(requested)
            self._reject_known(api_base, voice_id)
        else:
            voice_id = suggest_voice_id("design")
            while self._peek(api_base, voice_id):
                voice_id = suggest_voice_id("design")
        body = await client.design_voice(
            {"prompt": prompt, "preview_text": preview, "voice_id": voice_id}
        )
        returned = str(body.get("voice_id") or "").strip() or voice_id
        trial = str(body.get("trial_audio") or "").strip()
        audio = b""
        if trial:
            try:
                audio = decode_audio_field(trial)
            except MinimaxSpeechError:
                log.warning("design preview audio could not be decoded")
        return self._record_preview(
            api_base,
            kind="design",
            voice_id=returned,
            label=prompt[:80],
            preview_text=preview,
            prompt=prompt,
            audio=audio,
            usage=usage_characters_from(body),
            quote_kind="design_preview",
            model=model,
            sensitive=bool(body.get("input_sensitive")),
        )

    def read_audio(self, api_base: str, voice_id: str) -> dict[str, str]:
        take = self._peek(api_base, voice_id)
        name = str((take or {}).get("audio_file") or "")
        if not take or not name or "/" in name or "\\" in name:
            raise StudioError(
                "This take has no preview yet.",
                "这个音色还没有试听。",
                status=404,
                code="no_audio",
            )
        path = self.data_dir / "audio" / name
        if not path.is_file():
            raise StudioError(
                "The preview file is missing.",
                "试听文件不见了。",
                status=404,
                code="no_audio",
            )
        return {
            "audio_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "media_type": str(take.get("media_type") or "audio/mpeg"),
        }

    async def audition(
        self,
        client: Any,
        api_base: str,
        model: str,
        fields: dict[str, Any],
        refresh: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        if client is None:
            raise _missing_key()
        voice_id = str(fields.get("voice_id") or "").strip()
        if not voice_id:
            raise StudioError("Choose a voice.", "请选择音色。")
        text = str(fields.get("text") or "").strip()
        if not text:
            raise StudioError("Enter a sentence to play.", "请输入要试听的句子。")
        if len(text) > CLONE_TEXT_MAX:
            raise StudioError(
                "That sentence is longer than 1000 characters.",
                "这句话超过 1000 个字符。",
            )
        chosen_model = normalize_speech_model(str(fields.get("model") or model))
        take = self._peek(api_base, voice_id)
        if take and take.get("status") in {"expired", "deleted"}:
            raise StudioError(
                "This voice is no longer available.",
                "这个音色已经不能使用。",
            )
        if take and take.get("status") == "draft" and not _flag(fields.get("confirm_fee")):
            raise _fee_required(api_base, str(take.get("kind") or "clone"))
        audio = await client.synthesize_audio(
            text, voice_id, chosen_model, audio_format="mp3", limit=CLONE_TEXT_MAX
        )
        kept = False
        if take and take.get("status") == "draft":
            kept = True

            def apply_keep(host: dict[str, Any]) -> None:
                current = host["takes"].get(voice_id)
                if not isinstance(current, dict):
                    return
                self._charge_fee(current, api_base)
                self._charge_characters(
                    current, text, api_base, chosen_model, "speech", audio.usage_characters
                )

            self._mutate(api_base, apply_keep)
            await self._refresh(refresh)
        else:
            quote = quote_text(text, api_base, chosen_model, "speech")
            amount = self._amount(quote, audio.usage_characters)

            def apply_spend(host: dict[str, Any]) -> None:
                current = host["takes"].get(voice_id)
                if isinstance(current, dict):
                    current["spent"] = round(float(current.get("spent") or 0) + amount, 6)
                else:
                    host["extra_spent"] = round(
                        float(host.get("extra_spent") or 0) + amount, 6
                    )

            self._mutate(api_base, apply_spend)
        stored = self._peek(api_base, voice_id)
        return {
            "audio_base64": base64.b64encode(audio.data).decode("ascii"),
            "media_type": audio.media_type,
            "usage_characters": audio.usage_characters,
            "kept": kept,
            "spend": self._spend_of(api_base),
            "take": self._public_take(stored, api_base) if stored else None,
        }

    async def keep(
        self,
        client: Any,
        api_base: str,
        model: str,
        fields: dict[str, Any],
        refresh: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        if client is None:
            raise _missing_key()
        voice_id = str(fields.get("voice_id") or "").strip()
        take = self._peek(api_base, voice_id)
        if not take:
            raise StudioError("That take is not on this page.", "页面上没有这个音色。")
        if take.get("status") == "kept":
            raise StudioError("This voice is already saved.", "这个音色已经保存。")
        if take.get("status") != "draft":
            raise StudioError(
                "This draft has expired. Clone or design it again.",
                "这个草稿已过期。请重新克隆或设计。",
            )
        if not _flag(fields.get("confirm_fee")):
            raise _fee_required(api_base, str(take.get("kind") or "clone"))
        chosen = normalize_speech_model(model)
        audio = await client.synthesize_audio(
            KEEP_TEXT, voice_id, chosen, audio_format="mp3", limit=len(KEEP_TEXT)
        )

        def apply(host: dict[str, Any]) -> dict[str, Any]:
            current = host["takes"].get(voice_id)
            if not isinstance(current, dict):
                raise StudioError("That take is not on this page.", "页面上没有这个音色。")
            self._charge_fee(current, api_base)
            self._charge_characters(
                current, KEEP_TEXT, api_base, chosen, "speech", audio.usage_characters
            )
            if not current.get("audio_file"):
                name, media = self._store_audio(api_base, voice_id, audio.data)
                current["audio_file"] = name
                current["media_type"] = media
            return self._public_take(current, api_base)

        public = self._mutate(api_base, apply)
        await self._refresh(refresh)
        return {
            "take": public,
            "spend": self._spend_of(api_base),
            "audio_base64": base64.b64encode(audio.data).decode("ascii"),
            "media_type": audio.media_type,
            "usage_characters": audio.usage_characters,
        }

    def assert_usable(self, api_base: str, voice_id: str) -> None:
        take = self._peek(api_base, voice_id)
        if take is None:
            return
        if take.get("status") == "draft":
            raise StudioError(
                "Keep this voice before using it for replies.",
                "请先保存这个音色，再用于回复。",
                code="keep_first",
            )
        if take.get("status") != "kept":
            raise StudioError(
                "This voice is no longer available.",
                "这个音色已经不能使用。",
            )

    async def remove(
        self, client: Any, api_base: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        voice_id = str(fields.get("voice_id") or "").strip()
        if not voice_id:
            raise StudioError("Choose a voice.", "请选择音色。")
        take = self._peek(api_base, voice_id)
        if take and take.get("status") in {"deleted", "expired", "draft"}:
            # A draft preview is not in MiniMax's voice list yet. delete_voice
            # answers 2013 for that id, so the row is only removed here.
            preview_only = take.get("status") == "draft"

            def clear(host: dict[str, Any]) -> None:
                host["takes"].pop(voice_id, None)

            self._mutate(api_base, clear)
            return {
                "removed": True,
                "preview_only": preview_only,
                "take": None,
                "spend": self._spend_of(api_base),
            }
        if client is None:
            raise _missing_key()
        kind = str(take.get("kind") if take else fields.get("kind") or "")
        if kind not in {"clone", "design"}:
            raise StudioError(
                "Only a cloned or designed voice can be deleted.",
                "只能删除克隆或设计的音色。",
            )
        voice_type = "voice_cloning" if kind == "clone" else "voice_generation"
        try:
            await client.delete_voice(voice_id, voice_type)
        except MinimaxSpeechError as exc:
            if exc.status_code == 2013:
                raise StudioError(
                    "MiniMax rejected this delete. Save the voice on this host first, then try again.",
                    "MiniMax 拒绝了这次删除。请先在当前服务器上保存这个音色，然后再试。",
                    code="delete_rejected",
                ) from exc
            if exc.status_code != 2054:
                raise

        def mark(host: dict[str, Any]) -> dict[str, Any]:
            current = host["takes"].get(voice_id)
            if not isinstance(current, dict):
                current = {
                    "voice_id": voice_id,
                    "kind": kind,
                    "created_at": self._now(),
                    "label": voice_id,
                    "preview_text": "",
                    "prompt": "",
                    "status": "deleted",
                    "discovered": True,
                    "audio_file": "",
                    "media_type": "",
                    "spent": 0.0,
                    "fee_paid": False,
                    "fee_amount": None,
                    "preview_characters": None,
                    "preview_exact": False,
                }
                host["takes"][voice_id] = current
            current["status"] = "deleted"
            return self._public_take(current, api_base)

        public = self._mutate(api_base, mark)
        return {"removed": False, "take": public, "spend": self._spend_of(api_base)}

    def _record_preview(
        self,
        api_base: str,
        *,
        kind: str,
        voice_id: str,
        label: str,
        preview_text: str,
        prompt: str,
        audio: bytes,
        usage: int | None,
        quote_kind: str,
        model: str,
        sensitive: bool,
    ) -> dict[str, Any]:
        audio_file = ""
        media_type = ""
        if audio:
            audio_file, media_type = self._store_audio(api_base, voice_id, audio)
        quote = (
            quote_text(preview_text, api_base, model, quote_kind) if preview_text else None
        )
        characters = usage if usage is not None else (quote or {}).get("characters")
        amount = 0.0 if quote is None else self._amount(quote, usage)

        def apply(host: dict[str, Any]) -> dict[str, Any]:
            take = {
                "voice_id": voice_id,
                "kind": kind,
                "created_at": self._now(),
                "label": label,
                "preview_text": preview_text,
                "prompt": prompt,
                "status": "draft",
                "discovered": False,
                "audio_file": audio_file,
                "media_type": media_type,
                "spent": round(amount, 6),
                "fee_paid": False,
                "fee_amount": None,
                "preview_characters": characters,
                "preview_exact": usage is not None,
            }
            host["takes"][voice_id] = take
            return self._public_take(take, api_base)

        public = self._mutate(api_base, apply)
        result = {
            "take": public,
            "spend": self._spend_of(api_base),
            "usage_characters": usage,
            "sensitive": sensitive,
        }
        if audio:
            result["audio_base64"] = base64.b64encode(audio).decode("ascii")
            result["media_type"] = media_type
        return result

    def _charge_fee(self, take: dict[str, Any], api_base: str) -> None:
        if take.get("fee_paid"):
            return
        fee = one_time_fee(api_base, str(take.get("kind") or "clone"))
        take["fee_paid"] = True
        take["fee_amount"] = fee
        take["status"] = "kept"
        take["kept_at"] = self._now()
        take["spent"] = round(float(take.get("spent") or 0) + fee, 6)

    def _charge_characters(
        self,
        take: dict[str, Any],
        text: str,
        api_base: str,
        model: str,
        kind: str,
        usage: int | None,
    ) -> None:
        quote = quote_text(text, api_base, model, kind)
        take["spent"] = round(
            float(take.get("spent") or 0) + self._amount(quote, usage), 6
        )

    @staticmethod
    def _amount(quote: dict[str, Any], usage: int | None) -> float:
        characters = usage if usage is not None else int(quote["characters"])
        return characters * float(quote["per_million"]) / 1_000_000

    def _store_audio(self, api_base: str, voice_id: str, data: bytes) -> tuple[str, str]:
        suffix, media = _sniff_audio(data)
        digest = hashlib.sha256(
            f"{normalize_api_base(api_base)}\n{voice_id}".encode()
        ).hexdigest()[:24]
        name = f"{digest}{suffix}"
        folder = self.data_dir / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return name, media

    def _sync_remote(self, host: dict[str, Any], remote: dict[str, Any] | None) -> None:
        if remote is None:
            return
        buckets = voice_buckets(remote)
        cloning = set(buckets.get("voice_cloning") or [])
        generation = set(buckets.get("voice_generation") or [])
        now = self._now()
        for take in host["takes"].values():
            if not isinstance(take, dict) or take.get("status") == "deleted":
                continue
            listed = (
                take.get("kind") == "clone" and take.get("voice_id") in cloning
            ) or (
                take.get("kind") == "design" and take.get("voice_id") in generation
            )
            if listed:
                if take.get("status") != "kept":
                    take["status"] = "kept"
                    take["kept_at"] = take.get("kept_at") or now
            elif (
                take.get("status") == "draft"
                and now - float(take.get("created_at") or 0) >= DRAFT_TTL_SECONDS
            ):
                take["status"] = "expired"

    def _public_takes(
        self,
        host: dict[str, Any],
        api_base: str,
        remote: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        takes = [
            self._public_take(take, api_base)
            for take in host["takes"].values()
            if isinstance(take, dict)
        ]
        known = {str(item["voice_id"]) for item in takes}
        if remote is not None:
            buckets = voice_buckets(remote)
            for voice_id in buckets.get("voice_cloning") or []:
                if voice_id not in known:
                    takes.append(self._virtual(voice_id, "clone", api_base))
            for voice_id in buckets.get("voice_generation") or []:
                if voice_id not in known and voice_id not in {
                    item["voice_id"] for item in takes
                }:
                    takes.append(self._virtual(voice_id, "design", api_base))
        takes.sort(key=lambda item: (item["created_at"] == 0, -float(item["created_at"])))
        return takes

    def _virtual(self, voice_id: str, kind: str, api_base: str) -> dict[str, Any]:
        return self._public_take(
            {
                "voice_id": voice_id,
                "kind": kind,
                "created_at": 0,
                "label": voice_id,
                "preview_text": "",
                "status": "kept",
                "discovered": True,
                "audio_file": "",
                "fee_paid": False,
                "fee_amount": None,
                "preview_characters": None,
                "preview_exact": False,
            },
            api_base,
        )

    def _public_take(self, take: dict[str, Any] | None, api_base: str) -> dict[str, Any]:
        if not take:
            return {}
        status = str(take.get("status") or "draft")
        created = float(take.get("created_at") or 0)
        days_left = None
        if status == "draft":
            remaining = created + DRAFT_TTL_SECONDS - self._now()
            days_left = max(1, math.ceil(remaining / 86400)) if remaining > 0 else 0
        currency = host_prices(api_base).currency
        fee = take.get("fee_amount")
        return {
            "voice_id": take.get("voice_id") or "",
            "kind": take.get("kind") or "clone",
            "created_at": created,
            "label": take.get("label") or take.get("voice_id") or "",
            "preview_text": take.get("preview_text") or "",
            "prompt": take.get("prompt") or "",
            "status": status,
            "days_left": days_left,
            "has_audio": bool(take.get("audio_file")),
            "media_type": take.get("media_type") or "",
            "preview_characters": take.get("preview_characters"),
            "preview_exact": bool(take.get("preview_exact")),
            "fee_amount": fee,
            "fee_label": (
                format_money(float(fee), currency) if isinstance(fee, (int, float)) else ""
            ),
            "fee_paid": bool(take.get("fee_paid")),
            "discovered": bool(take.get("discovered")),
            "language": voice_language(str(take.get("voice_id") or "")),
            "gender": voice_gender(str(take.get("voice_id") or ""), str(take.get("label") or "")),
        }

    @staticmethod
    def _system_voices(remote: dict[str, Any] | None) -> list[dict[str, str]]:
        if remote is None:
            return []
        custom = set()
        try:
            buckets = voice_buckets(remote)
        except MinimaxSpeechError:
            return []
        custom.update(buckets.get("voice_cloning") or [])
        custom.update(buckets.get("voice_generation") or [])
        voices = []
        for voice in voices_from_response(remote):
            if voice.voice_id in custom:
                continue
            voices.append({"voice_id": voice.voice_id, "label": voice.label})
        voices.sort(key=lambda item: item["label"].lower())
        return voices

    def _reject_known(self, api_base: str, voice_id: str) -> None:
        if self._peek(api_base, voice_id):
            raise StudioError(
                "That voice id is already used on this page. Deleted ids cannot be reused.",
                "这个音色 ID 在本页已经用过。删除后的 ID 不能再用。",
                code="voice_id",
            )

    def _require_duration(self, kind: str, seconds: object) -> None:
        if seconds is None or seconds == "":
            raise StudioError(
                "The page could not read the audio length.",
                "页面没有读到音频时长。",
                code="duration",
            )
        try:
            value = float(seconds)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise StudioError(
                "The audio length is not a number.",
                "音频时长不是数字。",
                code="duration",
            ) from exc
        if kind == "source" and not (
            SOURCE_MIN_SECONDS <= value <= SOURCE_MAX_SECONDS
        ):
            raise StudioError(
                "Source audio must be between 10 seconds and 5 minutes.",
                "原始音频需要在 10 秒到 5 分钟之间。",
                code="duration",
            )
        if kind == "prompt" and not (0 < value < PROMPT_MAX_SECONDS):
            raise StudioError(
                "The example clip must be shorter than 8 seconds.",
                "示例音频需要短于 8 秒。",
                code="duration",
            )

    @staticmethod
    def _accuracy(value: object) -> float | None:
        if value is None or value == "":
            return None
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise StudioError(
                "The match threshold must be between 0 and 1.",
                "匹配阈值需要在 0 到 1 之间。",
            ) from exc
        if not 0 <= number <= 1:
            raise StudioError(
                "The match threshold must be between 0 and 1.",
                "匹配阈值需要在 0 到 1 之间。",
            )
        if number == 0:
            return None
        return number

    @staticmethod
    def _boost(value: str) -> str:
        chosen = value.strip()
        if not chosen:
            return ""
        if not _BOOST.fullmatch(chosen):
            raise StudioError(
                "Language boost contains characters MiniMax would reject.",
                "语言增强里有不能提交的字符。",
            )
        return chosen

    def _read_upload(self, upload_id: str, kind: str) -> tuple[bytes, str]:
        if not _UPLOAD_ID.fullmatch(upload_id or ""):
            raise StudioError("The upload expired. Choose the file again.", "上传已失效。请重新选择文件。")
        meta_path = self.data_dir / "uploads" / f"{upload_id}.json"
        if not meta_path.is_file():
            raise StudioError("The upload expired. Choose the file again.", "上传已失效。请重新选择文件。")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("kind") != kind:
            raise StudioError("The upload expired. Choose the file again.", "上传已失效。请重新选择文件。")
        suffix = str(meta.get("suffix") or "")
        path = self.data_dir / "uploads" / f"{upload_id}{suffix}"
        if not path.is_file():
            raise StudioError("The upload expired. Choose the file again.", "上传已失效。请重新选择文件。")
        return path.read_bytes(), str(meta.get("name") or path.name)

    def _delete_upload(self, upload_id: str) -> None:
        folder = self.data_dir / "uploads"
        if not folder.is_dir():
            return
        for path in folder.glob(f"{upload_id}*"):
            path.unlink(missing_ok=True)

    def _sweep_uploads(self) -> None:
        folder = self.data_dir / "uploads"
        if not folder.is_dir():
            return
        cutoff = self._now() - 86400
        for path in folder.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _peek(self, api_base: str, voice_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read_unlocked()
            host = data.get("hosts", {}).get(normalize_api_base(api_base), {})
            take = (host.get("takes") or {}).get(voice_id) if isinstance(host, dict) else None
            if isinstance(take, dict):
                return dict(take)
        return None

    def _spend_of(self, api_base: str) -> float:
        with self._lock:
            data = self._read_unlocked()
            host = data.get("hosts", {}).get(normalize_api_base(api_base), {})
            if not isinstance(host, dict):
                return 0.0
            return self._spend(host)

    @staticmethod
    def _spend(host: dict[str, Any]) -> float:
        total = float(host.get("extra_spent") or 0)
        for take in (host.get("takes") or {}).values():
            if isinstance(take, dict):
                total += float(take.get("spent") or 0)
        return round(total, 6)

    def _mutate(self, api_base: str, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            data = self._read_unlocked()
            hosts = data.setdefault("hosts", {})
            key = normalize_api_base(api_base)
            host = hosts.get(key)
            if not isinstance(host, dict):
                host = {"takes": {}, "extra_spent": 0.0}
                hosts[key] = host
            if not isinstance(host.get("takes"), dict):
                host["takes"] = {}
            host.setdefault("extra_spent", 0.0)
            result = fn(host)
            self._write_unlocked(data)
            return result

    def _ledger_path(self) -> Path:
        return self.data_dir / "ledger.json"

    def _read_unlocked(self) -> dict[str, Any]:
        path = self._ledger_path()
        if not path.is_file():
            return {"hosts": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("MiniMax voice studio ledger was unreadable")
            return {"hosts": {}}
        if not isinstance(data, dict):
            return {"hosts": {}}
        data.setdefault("hosts", {})
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        path = self._ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    async def _refresh(self, refresh: Callable[[], Any] | None) -> None:
        if refresh is None:
            return
        try:
            result = refresh()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            log.exception("voice list refresh failed after saving a voice")

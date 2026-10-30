"""Speak QQ replies with MiniMax.

The plugin runs on the QQ Open Platform adapters (``qq_official`` and
``qq_official_webhook``) and on OneBot (``aiocqhttp``, including NapCat).
It appends a 24 kHz PCM WAV ``Record``. The official adapter encodes that
file as Tencent Silk. The OneBot adapter sends the WAV; NapCat encodes Silk.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.api.star import Context, Star

try:
    from .reply_mode import DEFAULT_KEYWORDS, VOICE_HINT, choose, strip_marker
    from .speech import (
        DEFAULT_VOICE_ID,
        MinimaxSpeechError,
        SpeechClient,
        apply_api_base_dropdown,
        apply_voice_dropdown,
        is_speech_platform,
        iter_voice_selectors,
        normalize_api_base,
        normalize_speech_model,
        prepare_speech_text,
        reply_voice_updates,
        selector_key,
    )
    from .studio import (
        PLUGIN_NAME,
        STUDIO_ROUTES,
        StudioError,
        VoiceStudio,
        minimax_message,
        normalize_locale,
    )
except ImportError as exc:  # AstrBot loads main.py both as a package and by path.
    if "known parent package" not in str(exc):
        raise
    from reply_mode import DEFAULT_KEYWORDS, VOICE_HINT, choose, strip_marker
    from speech import (
        DEFAULT_VOICE_ID,
        MinimaxSpeechError,
        SpeechClient,
        apply_api_base_dropdown,
        apply_voice_dropdown,
        is_speech_platform,
        iter_voice_selectors,
        normalize_api_base,
        normalize_speech_model,
        prepare_speech_text,
        reply_voice_updates,
        selector_key,
    )
    from studio import (
        PLUGIN_NAME,
        STUDIO_ROUTES,
        StudioError,
        VoiceStudio,
        minimax_message,
        normalize_locale,
    )

# A voice or file already in the reply is left alone. An image, such as a
# meme, still gets a voice beside it.
_BLOCKING_MEDIA = (Video, File, Record)


def _web():
    from astrbot.api.web import error_response, json_response, request

    return error_response, json_response, request


def _request_locale(request_obj: object, body: dict | None) -> str:
    if isinstance(body, dict) and body.get("locale"):
        return normalize_locale(str(body.get("locale") or ""))
    query_locale = ""
    headers = getattr(request_obj, "headers", None)
    query = getattr(request_obj, "query", None)
    if query is not None:
        query_locale = str(query.get("locale", "") or "")
    if query_locale:
        return normalize_locale(query_locale)
    header = ""
    if headers is not None:
        header = str(headers.get("accept-language", "") or "")
    return normalize_locale(header)


class Main(Star):
    """Attach a MiniMax voice to QQ official and OneBot replies."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: SpeechClient | None = None
        self._handled_events: set[int] = set()

    async def initialize(self) -> None:
        """Load account voices and open the voice studio page."""
        self._register_studio_routes()
        await self.refresh_voice_choices()

    async def refresh_voice_choices(self) -> None:
        schema = getattr(self.config, "schema", None)
        if not isinstance(schema, dict):
            return
        api_key, voice_id, api_base, _model = self._settings()
        language = str(self.config.get("language") or "any").strip() or "any"
        gender = str(self.config.get("gender") or "any").strip() or "any"
        voices: list = []
        if api_key:
            try:
                client = await self._client_for(api_key, api_base)
                voices = await client.list_voices()
            except MinimaxSpeechError as exc:
                logger.warning("[minimax-speech] voice list unavailable: %s", exc)
        apply_api_base_dropdown(schema, api_base)
        saved = {
            key: str(self.config.get(key) or "")
            for _language, _gender, key in iter_voice_selectors()
        }
        selected, updates = apply_voice_dropdown(
            schema,
            voices,
            language=language,
            gender=gender,
            current_voice_id=voice_id,
            saved_values=saved,
        )
        changed = False
        for key, value in updates.items():
            if str(self.config.get(key) or "") != value:
                self.config[key] = value
                changed = True
        if changed:
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
        if voices:
            logger.info(
                "[minimax-speech] loaded %s voices from %s, %s match %s / %s (%s)",
                len(voices),
                api_base,
                len(
                    schema.get(selector_key(language, gender), {}).get("options") or []
                ),
                language,
                gender,
                selected,
            )

    async def terminate(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _settings(self) -> tuple[str, str, str, str]:
        api_key = str(self.config.get("api_key") or "").strip()
        language = str(self.config.get("language") or "any").strip() or "any"
        gender = str(self.config.get("gender") or "any").strip() or "any"
        voice_id = str(self.config.get(selector_key(language, gender)) or "").strip()
        if not voice_id:
            voice_id = (
                str(self.config.get("voice_id") or "").strip() or DEFAULT_VOICE_ID
            )
        api_base = normalize_api_base(str(self.config.get("api_base") or ""))
        model = normalize_speech_model(str(self.config.get("model") or ""))
        return api_key, voice_id, api_base, model

    async def _client_for(self, api_key: str, api_base: str) -> SpeechClient:
        current = self._client
        if (
            current is None
            or current.api_key != api_key
            or current.api_base != api_base
        ):
            if current is not None:
                await current.close()
            self._client = SpeechClient(api_key, api_base)
        return self._client

    async def _write_voice(self, text: str) -> str | None:
        api_key, voice_id, api_base, model = self._settings()
        spoken = prepare_speech_text(text)
        if not api_key or not spoken:
            return None
        client = await self._client_for(api_key, api_base)
        wav_bytes = await client.synthesize(spoken, voice_id, model)
        directory = Path(self._audio_dir())
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}.wav"
        path.write_bytes(wav_bytes)
        return str(path)

    @staticmethod
    def _audio_dir() -> Path:
        import tempfile

        return Path(tempfile.gettempdir()) / "astrbot_plugin_minimax_speech"

    def _voice_studio(self) -> VoiceStudio:
        studio = getattr(self, "_studio", None)
        if studio is None:
            folder = self._studio_dir()
            folder.mkdir(parents=True, exist_ok=True)
            studio = VoiceStudio(folder)
            self._studio = studio
        return studio

    @staticmethod
    def _studio_dir() -> Path:
        import tempfile

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            root = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001
            root = Path(tempfile.gettempdir())
        return root / PLUGIN_NAME

    def _register_studio_routes(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return
        handlers = {
            "state": self.studio_state,
            "upload_source": self.studio_upload_source,
            "upload_prompt": self.studio_upload_prompt,
            "clone": self.studio_clone,
            "design": self.studio_design,
            "take_audio": self.studio_take_audio,
            "audition": self.studio_audition,
            "keep": self.studio_keep,
            "use": self.studio_use,
            "delete": self.studio_delete,
        }
        for path, name, methods, desc in STUDIO_ROUTES:
            register(f"/{PLUGIN_NAME}/{path}", handlers[name], methods, desc)

    async def _studio_settings(self):
        api_key, voice_id, api_base, model = self._settings()
        client = None
        if api_key:
            client = await self._client_for(api_key, api_base)
        return client, voice_id, api_base, model

    def _apply_reply_voice(self, voice_id: str) -> None:
        language = str(self.config.get("language") or "any").strip() or "any"
        gender = str(self.config.get("gender") or "any").strip() or "any"
        for key, value in reply_voice_updates(voice_id, language, gender).items():
            self.config[key] = value
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    async def _studio_action(self, fn):
        error_response, json_response, request = _web()
        body = await request.json(default={})
        if not isinstance(body, dict):
            body = {}
        locale = _request_locale(request, body)
        try:
            client, voice_id, api_base, model = await self._studio_settings()
            payload = await fn(client, api_base, model, voice_id, body, locale)
        except StudioError as exc:
            return error_response(
                exc.text(locale),
                status_code=exc.http_status,
                data={"code": exc.code},
            )
        except MinimaxSpeechError as exc:
            return error_response(
                minimax_message(exc, locale),
                status_code=502,
                data={"code": exc.status_code},
            )
        except Exception:  # noqa: BLE001
            logger.exception("[minimax-speech] voice studio failed")
            message = "语音工作室出错了。" if locale == "zh-CN" else "Voice studio failed."
            return error_response(message, status_code=500)
        return json_response(payload)

    async def studio_state(self):
        async def run(client, api_base, model, voice_id, _body, locale):
            return await self._voice_studio().state(
                client, api_base, model, voice_id, locale
            )

        return await self._studio_action(run)

    async def studio_upload_source(self):
        return await self._studio_upload("source")

    async def studio_upload_prompt(self):
        return await self._studio_upload("prompt")

    async def _studio_upload(self, kind: str):
        error_response, json_response, request = _web()
        locale = _request_locale(request, None)
        try:
            files = await request.files()
            upload = files.get("file")
            if upload is None:
                raise StudioError("Choose an audio file.", "请选择音频文件。")
            content = await upload.read()
            payload = self._voice_studio().save_upload(
                kind, upload.filename or "audio.bin", content
            )
        except StudioError as exc:
            return error_response(
                exc.text(locale),
                status_code=exc.http_status,
                data={"code": exc.code},
            )
        except Exception:  # noqa: BLE001
            logger.exception("[minimax-speech] voice studio upload failed")
            message = "上传失败。" if locale == "zh-CN" else "Upload failed."
            return error_response(message, status_code=500)
        return json_response(payload)

    async def studio_clone(self):
        async def run(client, api_base, model, _voice_id, body, _locale):
            return await self._voice_studio().clone(client, api_base, model, body)

        return await self._studio_action(run)

    async def studio_design(self):
        async def run(client, api_base, model, _voice_id, body, _locale):
            return await self._voice_studio().design(client, api_base, model, body)

        return await self._studio_action(run)

    async def studio_take_audio(self, voice_id: str = ""):
        error_response, json_response, request = _web()
        locale = _request_locale(request, None)
        try:
            _client, _reply, api_base, _model = await self._studio_settings()
            payload = self._voice_studio().read_audio(api_base, voice_id)
        except StudioError as exc:
            return error_response(
                exc.text(locale),
                status_code=exc.http_status,
                data={"code": exc.code},
            )
        except Exception:  # noqa: BLE001
            logger.exception("[minimax-speech] voice studio audio failed")
            message = "试听读取失败。" if locale == "zh-CN" else "Could not read the preview."
            return error_response(message, status_code=500)
        return json_response(payload)

    async def studio_audition(self):
        async def run(client, api_base, model, _voice_id, body, _locale):
            return await self._voice_studio().audition(
                client,
                api_base,
                model,
                body,
                refresh=self.refresh_voice_choices,
            )

        return await self._studio_action(run)

    async def studio_keep(self):
        async def run(client, api_base, model, _voice_id, body, _locale):
            return await self._voice_studio().keep(
                client,
                api_base,
                model,
                body,
                refresh=self.refresh_voice_choices,
            )

        return await self._studio_action(run)

    async def studio_use(self):
        async def run(_client, api_base, _model, _voice_id, body, _locale):
            voice_id = str(body.get("voice_id") or "").strip()
            if not voice_id:
                raise StudioError("Choose a voice.", "请选择音色。")
            self._voice_studio().assert_usable(api_base, voice_id)
            self._apply_reply_voice(voice_id)
            await self.refresh_voice_choices()
            return {"voice_id": voice_id, "reply_voice_id": voice_id}

        return await self._studio_action(run)

    async def studio_delete(self):
        async def run(client, api_base, _model, _voice_id, body, _locale):
            payload = await self._voice_studio().remove(client, api_base, body)
            await self.refresh_voice_choices()
            return payload

        return await self._studio_action(run)

    def _reply_mode(self) -> str:
        mode = str(self.config.get("reply_mode") or "both").strip()
        return mode if mode in {"both", "voice", "smart"} else "both"

    def _smart_fallback(self) -> str:
        fallback = str(self.config.get("smart_fallback") or "both").strip()
        return fallback if fallback in {"both", "text"} else "both"

    def _enabled(self, key: str, default: bool) -> bool:
        value = self.config.get(key)
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _keywords(self) -> str:
        raw = self.config.get("smart_keywords")
        if raw is None:
            return DEFAULT_KEYWORDS
        return str(raw)

    @staticmethod
    def _incoming(event: AstrMessageEvent) -> list:
        message = getattr(getattr(event, "message_obj", None), "message", None)
        return list(message) if message else []

    def _user_text(self, event: AstrMessageEvent) -> str:
        parts = [
            component.text or ""
            for component in self._incoming(event)
            if isinstance(component, Plain)
        ]
        if parts:
            return "".join(parts)
        return str(getattr(event, "message_str", "") or "")

    def _user_sent_voice(self, event: AstrMessageEvent) -> bool:
        return any(isinstance(component, Record) for component in self._incoming(event))

    @staticmethod
    def _content_type_name(result: object) -> str:
        content_type = getattr(result, "result_content_type", None)
        return str(getattr(content_type, "name", content_type) or "")

    def _remember(self, event: AstrMessageEvent) -> None:
        self._handled_events.add(id(event))
        if len(self._handled_events) > 200:
            self._handled_events.clear()
            self._handled_events.add(id(event))

    @filter.command("tts")
    async def tts_command(self, event: AstrMessageEvent):
        """把后面的文字合成为一条 QQ 语音。"""
        self._remember(event)
        if not is_speech_platform(event.get_platform_name() or ""):
            yield event.plain_result(
                "语音只在 QQ 官方机器人（含 Webhook）和 OneBot（aiocqhttp，含 NapCat）里发送。"
            )
            return
        raw = (event.message_str or "").strip()
        if raw.startswith("/tts"):
            raw = raw[len("/tts") :].strip()
        elif raw == "tts" or raw.startswith("tts "):
            raw = raw[len("tts") :].strip()
        if not raw:
            yield event.plain_result("用法：/tts 要朗读的文字")
            return
        spoken = prepare_speech_text(raw)
        if not spoken:
            yield event.plain_result("没有可以朗读的文字。")
            return
        try:
            wav_path = await self._write_voice(raw)
        except MinimaxSpeechError as exc:
            logger.error("[minimax-speech] /tts failed: %s", exc)
            yield event.plain_result(f"语音合成失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            # Keep the command from crashing the pipeline on unexpected I/O errors.
            logger.exception("[minimax-speech] /tts failed")
            yield event.plain_result(f"语音合成失败：{exc}")
            return
        if not wav_path:
            yield event.plain_result("请先在插件配置里填写 MiniMax API Key。")
            return
        if self._reply_mode() == "both":
            yield event.chain_result(
                [Plain(spoken), Record(file=wav_path, url=wav_path)]
            )
        else:
            yield event.chain_result([Record(file=wav_path, url=wav_path)])

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """Ask the model to mark replies that should be heard, on supported QQ chats."""
        if self._reply_mode() != "smart" or not self._enabled("smart_agent", True):
            return
        if not is_speech_platform(event.get_platform_name() or ""):
            return
        prompt = str(getattr(req, "system_prompt", "") or "")
        if "<voice-only/>" in prompt:
            return
        req.system_prompt = f"{prompt}\n{VOICE_HINT}".strip()

    @filter.on_llm_response(priority=100000)
    async def on_llm_response(self, event: AstrMessageEvent, response) -> None:
        """Remove the voice marker before other plugins read the model reply."""
        if not is_speech_platform(event.get_platform_name() or ""):
            return
        found = False
        chain = getattr(getattr(response, "result_chain", None), "chain", None)
        if chain:
            for component in chain:
                if not isinstance(component, Plain) or not component.text:
                    continue
                cleaned, hit = strip_marker(component.text)
                if hit:
                    component.text = cleaned
                    found = True
        else:
            cleaned, hit = strip_marker(
                str(getattr(response, "completion_text", "") or "")
            )
            if hit:
                response.completion_text = cleaned
                found = True
        if found:
            event.set_extra("minimax_speech_voice_only", True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """Speak an LLM reply on a supported QQ bot, without touching other plugins."""
        if id(event) in self._handled_events:
            self._handled_events.discard(id(event))
            return
        if not is_speech_platform(event.get_platform_name() or ""):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain or any(isinstance(component, _BLOCKING_MEDIA) for component in chain):
            return
        content_type = self._content_type_name(result)
        streaming = content_type in {"STREAMING_RESULT", "STREAMING_FINISH"}
        is_llm = getattr(result, "is_llm_result", None)
        if not streaming and not (callable(is_llm) and is_llm()):
            return
        marked = self._strip_marker(chain)
        if marked and not chain:
            return
        mode = self._reply_mode()
        if streaming:
            if mode == "both":
                await self._attach_voice(event, chain, drop_text=False)
            return
        getter = getattr(event, "get_extra", None)
        agent = marked or (
            bool(getter("minimax_speech_voice_only")) if callable(getter) else False
        )
        text = "".join(
            component.text or "" for component in chain if isinstance(component, Plain)
        )
        decision = choose(
            mode,
            text,
            agent=agent and self._enabled("smart_agent", True),
            user_voice=(
                self._enabled("smart_mirror_voice", True) and self._user_sent_voice(event)
            ),
            user_text=self._user_text(event),
            keywords=self._keywords(),
            fallback=self._smart_fallback(),
        )
        if decision == "text":
            return
        await self._attach_voice(event, chain, drop_text=decision == "voice")

    @staticmethod
    def _strip_marker(chain: list) -> bool:
        found = False
        for component in chain:
            if not isinstance(component, Plain) or not component.text:
                continue
            cleaned, hit = strip_marker(component.text)
            if hit:
                component.text = cleaned
                found = True
        if found:
            chain[:] = [
                component
                for component in chain
                if not isinstance(component, Plain) or (component.text or "").strip()
            ]
        return found

    def _platform_settings(self, event: AstrMessageEvent) -> dict:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return {}
        umo = getattr(event, "unified_msg_origin", None)
        try:
            config = getter(umo=umo) if umo else getter()
        except Exception:  # noqa: BLE001
            return {}
        if not hasattr(config, "get"):
            return {}
        settings = config.get("platform_settings")
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def _message_type_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_type", None)
        if not callable(getter):
            return ""
        value = getter()
        return str(getattr(value, "name", value) or "")

    def _keep_quote(self, event: AstrMessageEvent, chain: list) -> None:
        """Add the quote AstrBot would add after this hook.

        AstrBot quotes a reply only when every part is text or an image. The
        voice record appended below would otherwise remove that quote.
        """
        allowed = (Plain, Image, At, Reply)
        if any(not isinstance(component, allowed) for component in chain):
            return
        settings = self._platform_settings(event)
        if not settings:
            return
        message_type = self._message_type_name(event)
        if (
            settings.get("reply_with_mention")
            and message_type
            and message_type != "FRIEND_MESSAGE"
            and not any(isinstance(component, At) for component in chain)
        ):
            sender_id = getattr(event, "get_sender_id", None)
            sender_name = getattr(event, "get_sender_name", None)
            chain.insert(
                0,
                At(
                    qq=str(sender_id() or "") if callable(sender_id) else "",
                    name=str(sender_name() or "") if callable(sender_name) else "",
                ),
            )
            if len(chain) > 1 and isinstance(chain[1], Plain):
                chain[1].text = "\n" + (chain[1].text or "")
        if settings.get("reply_with_quote") and not any(
            isinstance(component, Reply) for component in chain
        ):
            message_id = getattr(
                getattr(event, "message_obj", None), "message_id", None
            )
            if message_id:
                chain.insert(0, Reply(id=str(message_id)))

    async def _attach_voice(
        self, event: AstrMessageEvent, chain: list, *, drop_text: bool
    ) -> None:
        text = "".join(
            component.text or "" for component in chain if isinstance(component, Plain)
        )
        try:
            wav_path = await self._write_voice(text)
        except MinimaxSpeechError as exc:
            logger.error(
                "[minimax-speech] synthesis failed, sending text only: %s", exc
            )
            return
        except Exception:  # noqa: BLE001
            # A failed voice must leave the original text reply in place.
            logger.exception("[minimax-speech] synthesis failed, sending text only")
            return
        if not wav_path:
            return
        if drop_text:
            chain[:] = [
                component for component in chain if not isinstance(component, Plain)
            ]
        self._keep_quote(event, chain)
        chain.append(Record(file=wav_path, url=wav_path))
        logger.info("[minimax-speech] attached voice")

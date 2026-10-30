"""POC checks for MiniMax WAV output and the QQ official voice contract.

The Silk test follows AstrBot v4.29.3 ``wav_to_tencent_silk``: read 16-bit PCM
from the WAV and call ``pysilk.encode(..., tencent=True)``. That is the bytes
``QQOfficialMessageEvent`` uploads with ``file_type=3``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import socket
import sys
import types
import wave
from io import BytesIO
from pathlib import Path

import pytest
from aiohttp import web

from speech import (
    DEFAULT_VOICE_ID,
    QQ_OFFICIAL_PLATFORMS,
    QQ_VOICE_SAMPLE_RATE,
    SPEECH_MODEL,
    SPEECH_MODELS,
    SPEECH_PLATFORMS,
    MinimaxSpeechError,
    SpeechClient,
    apply_api_base_dropdown,
    apply_voice_dropdown,
    build_t2a_payload,
    decode_audio_field,
    is_qq_official_platform,
    is_speech_platform,
    iter_voice_selectors,
    normalize_api_base,
    normalize_speech_model,
    pcm_to_wav,
    prepare_speech_text,
    selector_key,
    voice_gender,
    voice_language,
    voice_selector_schema,
    voices_from_response,
    wav_bytes_from_response,
    wav_info,
)


def _tone_pcm(seconds: float = 0.4, rate: int = QQ_VOICE_SAMPLE_RATE) -> bytes:
    frames = int(rate * seconds)
    pcm = bytearray()
    for index in range(frames):
        sample = int(8000 * math.sin(2 * math.pi * 440 * index / rate))
        pcm += int(sample).to_bytes(2, "little", signed=True)
    return bytes(pcm)


def _t2a_body(pcm: bytes, *, status_code: int = 0, audio_format: str = "pcm") -> dict:
    return {
        "data": {"audio": pcm.hex(), "status": 2},
        "extra_info": {
            "audio_format": audio_format,
            "audio_sample_rate": QQ_VOICE_SAMPLE_RATE,
            "audio_length": 400,
        },
        "trace_id": "trace-poc",
        "base_resp": {"status_code": status_code, "status_msg": "success"},
    }


def test_speech_text_strips_markdown_and_limits_length() -> None:
    raw = "你好，**世界**。看 https://example.com/a 和 `code`。\n```py\nprint(1)\n```"
    assert prepare_speech_text(raw) == "你好，世界。看 和 code。"
    long_text = "这是一句很长的话。" * 80
    spoken = prepare_speech_text(long_text, limit=40)
    assert len(spoken) <= 40
    assert spoken.endswith("。")
    assert prepare_speech_text("```code``` https://example.com") == ""
    assert prepare_speech_text("***") == ""


def test_payload_matches_minimax_t2a_v2() -> None:
    payload = build_t2a_payload("你好", DEFAULT_VOICE_ID)
    assert payload["model"] == SPEECH_MODEL
    assert build_t2a_payload("你好", DEFAULT_VOICE_ID, "speech-2.8-turbo")["model"] == (
        "speech-2.8-turbo"
    )
    assert normalize_speech_model("nope") == SPEECH_MODEL
    schema = json.loads(Path("_conf_schema.json").read_text())
    assert schema["model"]["options"] == [
        model_id for model_id, _label, _hint in SPEECH_MODELS
    ]
    assert schema["model"]["default"] == SPEECH_MODEL
    assert payload["stream"] is False
    assert payload["output_format"] == "hex"
    assert payload["voice_setting"]["voice_id"] == DEFAULT_VOICE_ID
    assert payload["audio_setting"] == {
        "sample_rate": 24000,
        "format": "pcm",
        "channel": 1,
    }


def test_dashboard_locales_cover_config_text() -> None:
    root = Path(".astrbot-plugin/i18n")
    zh = json.loads((root / "zh-CN.json").read_text())
    en = json.loads((root / "en-US.json").read_text())
    assert zh["metadata"]["display_name"] == "MiniMax 语音"
    assert en["metadata"]["display_name"] == "MiniMax Speech"
    assert zh["config"]["api_base"]["labels"] == ["国际", "中国"]
    assert en["config"]["api_base"]["labels"] == ["International", "China"]
    assert len(zh["config"]["model"]["subtitles"]) == len(SPEECH_MODELS)
    for _language, _gender, key in iter_voice_selectors():
        assert zh["config"][key]["description"] == "音色"
        assert en["config"][key]["description"] == "Voice"


def test_api_base_drops_pasted_endpoint() -> None:
    assert normalize_api_base("") == "https://api.minimax.io"
    assert normalize_api_base("https://api.minimax.io/v1/t2a_v2") == (
        "https://api.minimax.io"
    )
    assert normalize_api_base("https://api-bj.minimaxi.com/v1/") == (
        "https://api-bj.minimaxi.com"
    )
    schema = json.loads(Path("_conf_schema.json").read_text())
    assert schema["api_base"]["options"] == [
        "https://api.minimax.io",
        "https://api.minimax.cn",
    ]
    assert schema["api_base"]["labels"] == ["International", "China"]
    apply_api_base_dropdown(schema, "https://api-bj.minimaxi.com/v1/")
    assert schema["api_base"]["options"][-1] == "https://api-bj.minimaxi.com"
    assert schema["api_base"]["labels"][-1] == "https://api-bj.minimaxi.com"


def test_pcm_wav_is_qq_silk_input_format() -> None:
    wav_bytes = pcm_to_wav(_tone_pcm(), QQ_VOICE_SAMPLE_RATE)
    channels, width, rate, frames = wav_info(wav_bytes)
    assert wav_bytes[:4] == b"RIFF"
    assert channels == 1
    assert width == 2
    assert rate == 24000
    assert frames > 0
    with pytest.raises(MinimaxSpeechError):
        pcm_to_wav(b"\x00", 24000)


def test_response_decoder_accepts_hex_and_base64() -> None:
    pcm = _tone_pcm(0.05)
    wav_bytes = wav_bytes_from_response(_t2a_body(pcm))
    assert wav_info(wav_bytes)[:3] == (1, 2, 24000)

    body = _t2a_body(pcm)
    body["data"]["audio"] = base64.b64encode(pcm).decode()
    # base64 of arbitrary PCM is not valid hex, so the fallback must be used.
    assert decode_audio_field(body["data"]["audio"]) == pcm
    assert wav_info(wav_bytes_from_response(body))[3] > 0

    with pytest.raises(MinimaxSpeechError) as error:
        wav_bytes_from_response(_t2a_body(pcm, status_code=1004))
    assert error.value.status_code == 1004
    assert error.value.trace_id == "trace-poc"

    mp3_body = _t2a_body(b"\x00\x00", audio_format="mp3")
    with pytest.raises(MinimaxSpeechError, match="PCM WAV"):
        wav_bytes_from_response(mp3_body)


def test_voice_list_becomes_a_dropdown() -> None:
    payload = {
        "system_voice": [
            {
                "voice_id": "Chinese (Mandarin)_News_Anchor",
                "voice_name": "新闻女声",
                "description": ["一位专业新闻主播"],
            },
            {
                "voice_id": "Chinese (Mandarin)_Reliable_Executive",
                "voice_name": "沉稳高管",
                "description": ["中年男性"],
            },
        ],
        "voice_cloning": [{"voice_id": "clone-1", "description": []}],
        "voice_generation": [{"voice_id": "ttv-1", "description": ["designed"]}],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }
    voices = voices_from_response(payload)
    assert [voice.voice_id for voice in voices] == [
        "Chinese (Mandarin)_News_Anchor",
        "Chinese (Mandarin)_Reliable_Executive",
        "clone-1",
        "ttv-1",
    ]
    assert voices[0].label == "新闻女声"
    assert voices[0].language == "Chinese (Mandarin)"
    assert voices[0].gender == "female"
    assert voices[1].gender == "male"
    assert voice_language("male-qn-qingse") == "Other"
    assert voice_gender("male-qn-qingse") == "male"
    assert voice_gender("female-tianmei") == "female"
    assert voice_language("Arrogant_Miss") == "Other"
    assert voice_gender("Arrogant_Miss") == "female"
    assert voice_gender("Jovialman") == "male"
    assert voice_gender("Chinese (Mandarin)_News_Anchor", "新闻女声") == "female"
    assert voices[2].language == "Other"
    assert voices[2].gender == "unknown"
    assert voices[2].subtitle == "Cloned voice"
    schema = json.loads(Path("_conf_schema.json").read_text())
    female_key = selector_key("Chinese (Mandarin)", "female")
    selected, updates = apply_voice_dropdown(
        schema,
        voices,
        language="Chinese (Mandarin)",
        gender="female",
        current_voice_id="male-qn-qingse",
        saved_values={"voice_id": "male-qn-qingse"},
    )
    assert selected == "Chinese (Mandarin)_News_Anchor"
    assert updates[female_key] == "Chinese (Mandarin)_News_Anchor"
    assert schema["voice_id"]["condition"] == {"language": "any", "gender": "any"}
    assert schema["voice_id"]["options"] == [voice.voice_id for voice in voices]
    assert schema[female_key]["options"] == ["Chinese (Mandarin)_News_Anchor"]
    assert schema[female_key]["condition"] == {
        "language": "Chinese (Mandarin)",
        "gender": "female",
    }
    assert schema["language"]["options"][0] == "any"
    assert "Chinese (Mandarin)" in schema["language"]["options"]
    assert schema["gender"]["labels"] == ["Any", "Female", "Male", "Unspecified"]
    assert set(voice_selector_schema()) <= set(
        json.loads(Path("_conf_schema.json").read_text())
    )
    with pytest.raises(MinimaxSpeechError):
        voices_from_response({"base_resp": {"status_code": 1004, "status_msg": "no"}})


def test_qq_official_platforms_include_webhook() -> None:
    assert is_qq_official_platform("qq_official")
    assert is_qq_official_platform("qq_official_webhook")
    assert not is_qq_official_platform("aiocqhttp")
    assert QQ_OFFICIAL_PLATFORMS == {"qq_official", "qq_official_webhook"}
    assert is_speech_platform("qq_official")
    assert is_speech_platform("qq_official_webhook")
    assert is_speech_platform("aiocqhttp")
    assert not is_speech_platform("telegram")
    assert SPEECH_PLATFORMS == QQ_OFFICIAL_PLATFORMS | {"aiocqhttp"}


def test_wav_encodes_to_tencent_silk() -> None:
    """Mirror AstrBot v4.29.3 wav_to_tencent_silk and the QQ voice header."""
    pysilk = pytest.importorskip("pysilk")
    wav_bytes = pcm_to_wav(_tone_pcm(), QQ_VOICE_SAMPLE_RATE)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        pcm_data = wav_file.readframes(wav_file.getnframes())
    assert (channels, sample_width, rate) == (1, 2, 24000)
    output = BytesIO()
    pysilk.encode(BytesIO(pcm_data), output, rate, rate, tencent=True)
    silk = output.getvalue()
    assert silk.startswith(b"\x02#!SILK_V3")
    duration = len(pcm_data) / (2 * rate)
    assert duration > 0


class _Server:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status_code = 0
        self.http_status = 200
        self.pcm = _tone_pcm(0.05)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "path": request.path,
                "authorization": request.headers.get("Authorization"),
                "json": await request.json(),
            }
        )
        if request.path == "/v1/get_voice":
            return web.json_response(
                {
                    "system_voice": [
                        {
                            "voice_id": "Chinese (Mandarin)_News_Anchor",
                            "voice_name": "新闻女声",
                            "description": ["主播"],
                        }
                    ],
                    "voice_cloning": [],
                    "voice_generation": [],
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }
            )
        body = _t2a_body(self.pcm, status_code=self.status_code)
        if self.status_code:
            body["base_resp"]["status_msg"] = "auth failed"
        return web.json_response(body, status=self.http_status)


async def _serve(server: _Server) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/t2a_v2", server.handle)
    app.router.add_post("/v1/get_voice", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    return runner, f"http://127.0.0.1:{port}"


def test_client_posts_t2a_and_returns_wav() -> None:
    async def scenario() -> None:
        server = _Server()
        runner, base = await _serve(server)
        client = SpeechClient("secret-key", base + "/v1/t2a_v2")
        try:
            wav_bytes = await client.synthesize("  你好，世界。  ", "male-qn-qingse")
            assert wav_info(wav_bytes)[:3] == (1, 2, 24000)
            recorded = server.requests[0]
            assert recorded["path"] == "/v1/t2a_v2"
            assert recorded["authorization"] == "Bearer secret-key"
            assert recorded["json"]["text"] == "你好，世界。"
            assert recorded["json"]["audio_setting"]["format"] == "pcm"
            voices = await client.list_voices()
            assert voices[0].voice_id == "Chinese (Mandarin)_News_Anchor"
            assert server.requests[1]["path"] == "/v1/get_voice"
            assert server.requests[1]["json"] == {"voice_type": "all"}

            server.status_code = 1004
            with pytest.raises(MinimaxSpeechError, match="auth failed"):
                await client.synthesize("你好", "male-qn-qingse")
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(scenario())


def _install_astrbot_stub() -> None:
    if "astrbot.api.star" in sys.modules and hasattr(
        sys.modules["astrbot.api.star"], "Star"
    ):
        star = sys.modules["astrbot.api.star"]
        if getattr(star.Star, "__module__", "").startswith("astrbot"):
            return

    def package(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module
        return module

    package("astrbot")
    api = package("astrbot.api")
    event_mod = package("astrbot.api.event")
    components = package("astrbot.api.message_components")
    star_mod = package("astrbot.api.star")
    api.logger = logging.getLogger("minimax-speech-test")
    api.AstrBotConfig = dict

    class _Filter:
        @staticmethod
        def command(_name: str):
            def decorate(fn):
                return fn

            return decorate

        @staticmethod
        def on_decorating_result():
            def decorate(fn):
                return fn

            return decorate

        @staticmethod
        def on_llm_request(*_args, **_kwargs):
            def decorate(fn):
                return fn

            return decorate

        @staticmethod
        def on_llm_response(*_args, **_kwargs):
            def decorate(fn):
                return fn

            return decorate

    class Plain:
        def __init__(self, text: str = "", **_kwargs) -> None:
            self.text = text

    class Record:
        def __init__(self, file: str = "", url: str = "", **_kwargs) -> None:
            self.file = file
            self.url = url

    class Image:
        def __init__(self, file: str = "", **_kwargs) -> None:
            self.file = file

    class At:
        def __init__(self, qq: str = "", name: str = "", **_kwargs) -> None:
            self.qq = qq
            self.name = name

    class Reply:
        def __init__(self, id: str = "", **_kwargs) -> None:
            self.id = id

    class Star:
        def __init__(self, context) -> None:
            self.context = context

    event_mod.filter = _Filter
    event_mod.AstrMessageEvent = object
    components.Plain = Plain
    components.Record = Record
    components.Image = Image
    components.At = At
    components.Reply = Reply
    components.Video = type("Video", (), {})
    components.File = type("File", (), {})
    star_mod.Context = object
    star_mod.Star = Star


def _load_main():
    _install_astrbot_stub()
    sys.modules.pop("main", None)
    import main

    return main


def test_plugin_attaches_voice_for_webhook_and_skips_other_platforms() -> None:
    async def scenario() -> None:
        server = _Server()
        runner, base = await _serve(server)
        main = _load_main()
        plugin = main.Main(
            context=object(),
            config={
                "api_key": "secret-key",
                "voice_id": "male-qn-qingse",
                "api_base": base,
            },
        )

        class Event:
            def __init__(
                self,
                platform: str,
                chain: list,
                message_str: str = "",
                *,
                content_type: str = "LLM_RESULT",
                incoming: list | None = None,
            ) -> None:
                self.platform = platform
                self.message_str = message_str
                self.extras: dict = {}
                self.message_obj = types.SimpleNamespace(message=list(incoming or []))
                self.result = types.SimpleNamespace(
                    chain=chain,
                    result_content_type=content_type,
                    is_llm_result=lambda: content_type == "LLM_RESULT",
                )
                self.sent: list = []

            def set_extra(self, key: str, value) -> None:
                self.extras[key] = value

            def get_extra(self, key: str | None = None, default=None):
                if key is None:
                    return self.extras
                return self.extras.get(key, default)

            def get_platform_name(self) -> str:
                return self.platform

            def get_result(self):
                return self.result

            def plain_result(self, text: str):
                item = ("plain", text)
                self.sent.append(item)
                return item

            def chain_result(self, chain: list):
                item = ("chain", chain)
                self.sent.append(item)
                return item

        try:
            webhook = Event(
                "qq_official_webhook",
                [main.Plain("你好，**世界**。")],
            )
            await plugin.on_decorating_result(webhook)
            assert len(webhook.result.chain) == 2
            record = webhook.result.chain[1]
            assert isinstance(record, main.Record)
            assert record.file == record.url
            assert record.file.endswith(".wav")
            assert wav_info(Path(record.file).read_bytes())[:3] == (1, 2, 24000)
            assert server.requests[0]["json"]["text"] == "你好，世界。"

            telegram = Event("telegram", [main.Plain("你好")])
            await plugin.on_decorating_result(telegram)
            assert len(telegram.result.chain) == 1

            onebot = Event("aiocqhttp", [main.Plain("你好，NapCat。")])
            await plugin.on_decorating_result(onebot)
            assert len(onebot.result.chain) == 2
            onebot_record = onebot.result.chain[1]
            assert isinstance(onebot_record, main.Record)
            assert onebot_record.file == onebot_record.url
            assert onebot_record.file.endswith(".wav")
            assert wav_info(Path(onebot_record.file).read_bytes())[:3] == (1, 2, 24000)
            assert server.requests[-1]["json"]["text"] == "你好，NapCat。"

            onebot_tts = Event("aiocqhttp", [], message_str="/tts 协议端语音")
            yielded = [item async for item in plugin.tts_command(onebot_tts)]
            assert yielded[0][0] == "chain"
            assert isinstance(yielded[0][1][1], main.Record)

            refused = Event("telegram", [], message_str="/tts 不该读")
            yielded = [item async for item in plugin.tts_command(refused)]
            assert yielded[0][0] == "plain"
            assert "aiocqhttp" in yielded[0][1]

            image_reply = Event(
                "qq_official",
                [main.Plain("看图"), main.Image(file="pic.png")],
            )
            await plugin.on_decorating_result(image_reply)
            assert [type(item) for item in image_reply.result.chain] == [
                main.Plain,
                main.Image,
                main.Record,
            ]
            assert server.requests[-1]["json"]["text"] == "看图"

            command = Event("qq_official", [], message_str="/tts 测试语音")
            yielded = [item async for item in plugin.tts_command(command)]
            assert yielded[0][0] == "chain"
            chain = yielded[0][1]
            assert isinstance(chain[0], main.Plain)
            assert isinstance(chain[1], main.Record)
            assert chain[0].text == "测试语音"
            # The command already produced a record, so the hook must not add another.
            command.result.chain = list(chain)
            await plugin.on_decorating_result(command)
            assert len(command.result.chain) == 2

            junk = Event("qq_official", [], message_str="/tts ```code```")
            yielded = [item async for item in plugin.tts_command(junk)]
            assert yielded[0] == ("plain", "没有可以朗读的文字。")

            missing = main.Main(context=object(), config={"api_key": ""})
            empty = Event("qq_official_webhook", [], message_str="你好")
            yielded = [item async for item in missing.tts_command(empty)]
            assert yielded[0] == ("plain", "请先在插件配置里填写 MiniMax API Key。")

            plugin.config["reply_mode"] = "voice"
            spoken = Event("qq_official", [main.Plain("今天天气不错。")])
            await plugin.on_decorating_result(spoken)
            assert len(spoken.result.chain) == 1
            assert isinstance(spoken.result.chain[0], main.Record)

            linked = Event("qq_official", [main.Plain("看 https://example.com/a")])
            await plugin.on_decorating_result(linked)
            assert "https://example.com/a" in linked.result.chain[0].text
            assert isinstance(linked.result.chain[1], main.Record)

            server.status_code = 1004
            failed = Event("qq_official", [main.Plain("合成会失败。")])
            await plugin.on_decorating_result(failed)
            server.status_code = 0
            assert [type(item) for item in failed.result.chain] == [main.Plain]
            assert failed.result.chain[0].text == "合成会失败。"

            calls = len(server.requests)
            command_reply = Event(
                "qq_official",
                [main.Plain("其他插件的命令输出")],
                content_type="GENERAL_RESULT",
            )
            await plugin.on_decorating_result(command_reply)
            assert command_reply.result.chain[0].text == "其他插件的命令输出"
            assert len(command_reply.result.chain) == 1
            assert len(server.requests) == calls

            meme = Event(
                "qq_official",
                [main.Plain(""), main.Image(file="sticker.gif")],
            )
            await plugin.on_decorating_result(meme)
            assert len(meme.result.chain) == 2
            assert isinstance(meme.result.chain[1], main.Image)

            plugin.config["reply_mode"] = "both"
            quoted = Event("qq_official", [main.Plain("接着说。")])
            quoted.message_obj.message_id = "quoted-message"
            quoted.unified_msg_origin = "qq:FriendMessage:1"

            class QuoteContext:
                def get_config(self, umo=None):
                    assert umo == "qq:FriendMessage:1"
                    return {
                        "platform_settings": {
                            "reply_with_quote": True,
                            "reply_with_mention": True,
                        }
                    }

            plugin.context = QuoteContext()
            await plugin.on_decorating_result(quoted)
            assert isinstance(quoted.result.chain[0], main.Reply)
            assert quoted.result.chain[0].id == "quoted-message"
            assert isinstance(quoted.result.chain[1], main.Plain)
            assert quoted.result.chain[1].text == "接着说。"
            assert isinstance(quoted.result.chain[2], main.Record)
            assert server.requests[-1]["json"]["text"] == "接着说。"

            plugin.config["reply_mode"] = "voice"
            stream = Event(
                "qq_official",
                [main.Plain("流式文字还在。")],
                content_type="STREAMING_FINISH",
            )
            await plugin.on_decorating_result(stream)
            assert [item.text for item in stream.result.chain] == ["流式文字还在。"]

            plugin.config["reply_mode"] = "both"
            stream_both = Event(
                "qq_official",
                [main.Plain("流式也带语音。")],
                content_type="STREAMING_FINISH",
            )
            await plugin.on_decorating_result(stream_both)
            assert isinstance(stream_both.result.chain[0], main.Plain)
            assert isinstance(stream_both.result.chain[1], main.Record)

            plugin.config["reply_mode"] = "smart"
            plugin.config["smart_fallback"] = "text"
            calls = len(server.requests)
            quiet = Event("qq_official", [main.Plain("普通回答。")])
            await plugin.on_decorating_result(quiet)
            assert quiet.result.chain[0].text == "普通回答。"
            assert len(server.requests) == calls

            marked = Event("qq_official", [main.Plain("<voice-only/>听我说。")])
            await plugin.on_decorating_result(marked)
            assert len(marked.result.chain) == 1
            assert isinstance(marked.result.chain[0], main.Record)
            assert server.requests[-1]["json"]["text"] == "听我说。"

            heard = Event(
                "qq_official",
                [main.Plain("我听到了。")],
                incoming=[main.Record(file="in.wav")],
            )
            await plugin.on_decorating_result(heard)
            assert isinstance(heard.result.chain[0], main.Record)

            keyword = Event(
                "qq_official",
                [main.Plain("好，读给你听。")],
                message_str="请读给我听",
            )
            await plugin.on_decorating_result(keyword)
            assert isinstance(keyword.result.chain[0], main.Record)

            req = types.SimpleNamespace(system_prompt="你是助手。")
            await plugin.on_llm_request(Event("qq_official", []), req)
            assert "<voice-only/>" in req.system_prompt
            napcat_req = types.SimpleNamespace(system_prompt="你是助手。")
            await plugin.on_llm_request(Event("aiocqhttp", []), napcat_req)
            assert "<voice-only/>" in napcat_req.system_prompt
            again = types.SimpleNamespace(system_prompt=req.system_prompt)
            await plugin.on_llm_request(Event("qq_official", []), again)
            assert again.system_prompt == req.system_prompt

            other_req = types.SimpleNamespace(system_prompt="stay")
            await plugin.on_llm_request(Event("telegram", []), other_req)
            assert other_req.system_prompt == "stay"
            plugin.config["reply_mode"] = "both"
            off = types.SimpleNamespace(system_prompt="stay")
            await plugin.on_llm_request(Event("qq_official", []), off)
            assert off.system_prompt == "stay"

            plugin.config["reply_mode"] = "smart"
            image = main.Image(file="keep.png")
            plain = main.Plain("前文 <voice-only/> 后文")
            response = types.SimpleNamespace(
                result_chain=types.SimpleNamespace(chain=[plain, image]),
                completion_text="前文 <voice-only/> 后文",
            )
            host = Event("qq_official", [])
            await plugin.on_llm_response(host, response)
            assert plain.text == "前文 后文"
            assert response.result_chain.chain[1] is image
            assert host.extras["minimax_speech_voice_only"] is True
            bare = types.SimpleNamespace(
                result_chain=None,
                completion_text="<voice-only/>只有文字",
            )
            await plugin.on_llm_response(Event("qq_official", []), bare)
            assert bare.completion_text == "只有文字"

            plugin.config["reply_mode"] = "voice"
            voice_only = Event("qq_official", [], message_str="/tts 只要声音")
            yielded = [item async for item in plugin.tts_command(voice_only)]
            assert len(yielded[0][1]) == 1
            assert isinstance(yielded[0][1][0], main.Record)
        finally:
            await plugin.terminate()
            await runner.cleanup()

    asyncio.run(scenario())

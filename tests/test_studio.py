"""MiniMax voice studio ledger, clone/design calls, and page copy."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web

from speech import SPEECH_MODELS, MinimaxSpeechError, SpeechClient, reply_voice_updates
from studio import (
    KEEP_TEXT,
    STUDIO_ROUTES,
    StudioError,
    VoiceStudio,
    format_money,
    minimax_message,
    quote_text,
)


def _ok(extra: dict | None = None) -> dict:
    body = {"base_resp": {"status_code": 0, "status_msg": "success"}}
    if extra:
        body.update(extra)
    return body


class _MiniMax:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.custom: list[str] = []

    async def handle(self, request: web.Request) -> web.Response:
        if request.method == "GET":
            self.calls.append({"path": request.path, "method": "GET"})
            return web.Response(body=b"ID3preview")
        if request.path == "/v1/files/upload":
            purpose = None
            filename = None
            size = 0
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "purpose":
                    purpose = await part.text()
                elif part.name == "file":
                    filename = part.filename
                    size = len(await part.read())
            self.calls.append(
                {
                    "path": request.path,
                    "purpose": purpose,
                    "filename": filename,
                    "size": size,
                }
            )
            return web.json_response(_ok({"file": {"file_id": 4100 + len(self.calls)}}))
        payload = await request.json()
        self.calls.append(
            {
                "path": request.path,
                "json": payload,
                "authorization": request.headers.get("Authorization"),
            }
        )
        if request.path == "/v1/get_voice":
            return web.json_response(
                _ok(
                    {
                        "system_voice": [
                            {
                                "voice_id": "Chinese (Mandarin)_News_Anchor",
                                "voice_name": "新闻女声",
                            }
                        ],
                        "voice_cloning": [
                            {"voice_id": voice_id} for voice_id in self.custom
                        ],
                        "voice_generation": [],
                    }
                )
            )
        if request.path == "/v1/voice_clone":
            demo = ""
            extra: dict = {}
            if payload.get("text"):
                demo = f"{request.url.scheme}://{request.url.host}/demo.mp3"
                if request.url.port:
                    demo = (
                        f"{request.url.scheme}://{request.url.host}:{request.url.port}/demo.mp3"
                    )
                extra = {"extra_info": {"usage_characters": 4}}
            return web.json_response(_ok({"demo_audio": demo, **extra}))
        if request.path == "/v1/voice_design":
            return web.json_response(
                _ok(
                    {
                        "voice_id": payload.get("voice_id") or "Designassigned1",
                        "trial_audio": b"ID3design".hex(),
                        "extra_info": {"usage_characters": 6},
                    }
                )
            )
        if request.path == "/v1/delete_voice":
            return web.json_response(_ok({"voice_id": payload.get("voice_id")}))
        if request.path == "/v1/t2a_v2":
            return web.json_response(
                _ok(
                    {
                        "data": {"audio": b"ID3keep".hex(), "status": 2},
                        "extra_info": {
                            "audio_format": "mp3",
                            "usage_characters": 4,
                        },
                    }
                )
            )
        return web.json_response(_ok(), status=404)


async def _serve(server: _MiniMax) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/files/upload", server.handle)
    app.router.add_post("/v1/voice_clone", server.handle)
    app.router.add_post("/v1/voice_design", server.handle)
    app.router.add_post("/v1/delete_voice", server.handle)
    app.router.add_post("/v1/get_voice", server.handle)
    app.router.add_post("/v1/t2a_v2", server.handle)
    app.router.add_get("/demo.mp3", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    return runner, f"http://127.0.0.1:{port}"


def _paths(server: _MiniMax) -> list[str]:
    return [call["path"] for call in server.calls]


def test_prices_and_china_character_weight() -> None:
    china = quote_text("你好A", "https://api.minimax.cn", "speech-2.8-hd", "speech")
    assert china["characters"] == 5
    assert china["per_million"] == 350
    assert china["currency"] == "CNY"
    turbo = quote_text("你好", "https://api.minimax.cn", "speech-2.8-turbo", "speech")
    assert turbo["per_million"] == 200
    assert turbo["amount"] == pytest.approx(4 * 200 / 1_000_000)
    design = quote_text("ab", "https://api.minimax.io", "speech-2.8-hd", "design_preview")
    speech = quote_text("ab", "https://api.minimax.io", "speech-2.8-hd", "speech")
    assert design["characters"] == 2
    assert design["per_million"] == 60
    assert speech["per_million"] == 100
    assert format_money(9.9, "CNY") == "¥9.90"
    assert format_money(0.0014, "CNY", approximate=True) == "≈ ¥0.0014"
    assert quote_text("a", "https://api.minimax.io", "speech-01-turbo", "speech")[
        "per_million"
    ] == 60
    assert {model_id for model_id, _label, _subtitle in SPEECH_MODELS}


def test_error_codes_are_bilingual() -> None:
    zh = minimax_message(MinimaxSpeechError("raw", status_code=2038), "zh-CN")
    en = minimax_message(MinimaxSpeechError("raw", status_code=2038), "en-US")
    assert "认证" in zh
    assert "verification" in en
    assert zh.endswith("(2038)")
    assert "余额" in minimax_message(MinimaxSpeechError("x", status_code=1008), "zh-CN")
    assert "busy" in minimax_message(MinimaxSpeechError("x", status_code=1045), "en-US").lower()


def test_reply_voice_stays_in_the_matching_list() -> None:
    same = reply_voice_updates(
        "Chinese (Mandarin)_Gentle_Lady", "Chinese (Mandarin)", "female"
    )
    assert same == {"voice_chinese_mandarin_female": "Chinese (Mandarin)_Gentle_Lady"}
    switched = reply_voice_updates("male-qn-qingse", "Chinese (Mandarin)", "female")
    assert switched["language"] == "Other"
    assert switched["gender"] == "male"
    assert switched["voice_other_male"] == "male-qn-qingse"


def test_view_locales_cover_the_page() -> None:
    script = Path("views/voices/app.js").read_text()
    keys = set(re.findall(r'(?<![A-Za-z.])text\("([a-z0-9_]+)"', script))
    keys.update(re.findall(r'"(chip_[a-z_]+)"', script))
    keys.update(re.findall(r'"(phrase_[a-z_]+)"', script))
    keys.update(re.findall(r'"(example_\d)"', script))
    assert Path("views/voices/index.html").is_file()
    assert Path("views/voices/style.css").is_file()
    zh = json.loads(Path(".astrbot-plugin/i18n/zh-CN.json").read_text())["views"]["voices"]
    en = json.loads(Path(".astrbot-plugin/i18n/en-US.json").read_text())["views"]["voices"]
    assert zh["title"] == "MiniMax 音色工作室"
    assert en["title"] == "MiniMax Voice studio"
    assert zh["description"]
    assert en["description"]
    assert not keys - set(zh)
    assert set(zh) == set(en)


def test_routes_cover_the_studio_actions() -> None:
    assert [item[0] for item in STUDIO_ROUTES] == [
        "state",
        "upload/source",
        "upload/prompt",
        "clone",
        "design",
        "takes/<voice_id>/audio",
        "audition",
        "keep",
        "use",
        "delete",
    ]


def test_plugin_imports_as_a_package() -> None:
    """AstrBot imports the plugin directory as a package, not as loose modules."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root.parent)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import astrbot_plugin_minimax_speech.reply_mode as reply_mode; "
                "import astrbot_plugin_minimax_speech.studio as studio; "
                "assert reply_mode.choose and studio.PLUGIN_NAME"
            ),
        ],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_upload_clone_design_and_delete_match_the_api(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = _MiniMax()
        runner, base = await _serve(server)
        client = SpeechClient("secret-key", base)
        clock = {"now": 1_700_000_000.0}
        studio = VoiceStudio(tmp_path, now=lambda: clock["now"])
        host = "https://api.minimax.cn"
        try:
            source = studio.save_upload("source", "reading.mp3", b"ID3source")
            prompt = studio.save_upload("prompt", "clip.wav", b"RIFF")
            with pytest.raises(StudioError):
                await studio.clone(
                    client,
                    host,
                    "speech-2.8-hd",
                    {
                        "voice_id": "bad",
                        "source_id": source["upload_id"],
                        "source_seconds": 12,
                    },
                )
            with pytest.raises(StudioError) as too_short:
                await studio.clone(
                    client,
                    host,
                    "speech-2.8-hd",
                    {
                        "voice_id": "Cloneabc12345",
                        "source_id": source["upload_id"],
                        "source_seconds": 3,
                    },
                )
            assert too_short.value.code == "duration"
            with pytest.raises(StudioError) as pair:
                await studio.clone(
                    client,
                    host,
                    "speech-2.8-hd",
                    {
                        "voice_id": "Cloneabc12345",
                        "source_id": source["upload_id"],
                        "source_seconds": 12,
                        "prompt_id": prompt["upload_id"],
                        "prompt_text": "",
                    },
                )
            assert pair.value.code == "prompt_pair"
            assert _paths(server) == []

            cloned = await studio.clone(
                client,
                host,
                "speech-2.8-hd",
                {
                    "voice_id": "Cloneabc12345",
                    "source_id": source["upload_id"],
                    "source_seconds": 12,
                    "prompt_id": prompt["upload_id"],
                    "prompt_text": "hello there",
                    "prompt_seconds": 3.2,
                    "text": "你好",
                },
            )
            uploads = [call for call in server.calls if call["path"] == "/v1/files/upload"]
            assert [call["purpose"] for call in uploads] == ["voice_clone", "prompt_audio"]
            assert uploads[0]["filename"] == "reading.mp3"
            clone_body = next(
                call["json"] for call in server.calls if call["path"] == "/v1/voice_clone"
            )
            assert clone_body["voice_id"] == "Cloneabc12345"
            assert isinstance(clone_body["file_id"], int)
            assert clone_body["clone_prompt"]["prompt_text"] == "hello there"
            assert isinstance(clone_body["clone_prompt"]["prompt_audio"], int)
            assert clone_body["clone_prompt"]["prompt_audio"] != clone_body["file_id"]
            assert clone_body["text"] == "你好"
            assert clone_body["model"] == "speech-2.8-hd"
            assert "need_noise_reduction" not in clone_body
            assert cloned["take"]["status"] == "draft"
            assert cloned["take"]["fee_paid"] is False
            assert cloned["usage_characters"] == 4
            assert base64.b64decode(cloned["audio_base64"]) == b"ID3preview"
            assert list((tmp_path / "uploads").glob("*")) == []

            designed = await studio.design(
                client,
                host,
                "speech-2.8-hd",
                {"prompt": "a calm woman", "preview_text": "你好"},
            )
            design_body = next(
                call["json"] for call in server.calls if call["path"] == "/v1/voice_design"
            )
            assert design_body["prompt"] == "a calm woman"
            assert design_body["preview_text"] == "你好"
            assert designed["take"]["prompt"] == "a calm woman"
            assert designed["take"]["preview_text"] == "你好"
            assert design_body["voice_id"].startswith("Design")
            assert base64.b64decode(designed["audio_base64"]) == b"ID3design"
            assert designed["spend"] == pytest.approx(
                cloned["spend"] + 6 * 200 / 1_000_000
            )

            reloaded = VoiceStudio(tmp_path, now=lambda: clock["now"])
            state = await reloaded.state(
                client, host, "speech-2.8-hd", "male-qn-qingse", "zh-CN"
            )
            ids = {take["voice_id"] for take in state["takes"]}
            assert "Cloneabc12345" in ids
            assert designed["take"]["voice_id"] in ids
            assert state["pricing"]["clone_fee"] == 9.9
            assert state["pricing"]["han_weight"] == 2
            assert state["voices"][0]["voice_id"] == "Chinese (Mandarin)_News_Anchor"

            removed = await studio.remove(
                client,
                host,
                {"voice_id": designed["take"]["voice_id"], "kind": "design"},
            )
            assert not any(call["path"] == "/v1/delete_voice" for call in server.calls)
            assert removed["removed"] is True
            assert removed["preview_only"] is True
            assert removed["take"] is None

            clock["now"] += 8 * 24 * 60 * 60
            expired = await studio.state(client, host, "speech-2.8-hd", "", "en-US")
            aged = next(
                take for take in expired["takes"] if take["voice_id"] == "Cloneabc12345"
            )
            assert aged["status"] == "expired"
            server.custom.append("Cloneabc12345")
            listed = await studio.state(client, host, "speech-2.8-hd", "", "en-US")
            revived = next(
                take for take in listed["takes"] if take["voice_id"] == "Cloneabc12345"
            )
            assert revived["status"] == "kept"
            assert revived["fee_paid"] is False
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_keep_and_confirmed_audition_synthesize_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = _MiniMax()
        runner, base = await _serve(server)
        client = SpeechClient("secret-key", base)
        studio = VoiceStudio(tmp_path)
        host = "https://api.minimax.cn"
        refreshed: list[str] = []

        async def refresh() -> None:
            refreshed.append("yes")

        try:
            source = studio.save_upload("source", "reading.wav", b"RIFFdata")
            await studio.clone(
                client,
                host,
                "speech-2.8-turbo",
                {
                    "voice_id": "Keepvoice01",
                    "source_id": source["upload_id"],
                    "source_seconds": 15,
                },
            )
            with pytest.raises(StudioError) as fee:
                await studio.audition(
                    client,
                    host,
                    "speech-2.8-turbo",
                    {"voice_id": "Keepvoice01", "text": "你好", "confirm_fee": False},
                )
            assert fee.value.code == "fee_required"
            assert "/v1/t2a_v2" not in _paths(server)

            kept = await studio.keep(
                client,
                host,
                "speech-2.8-turbo",
                {"voice_id": "Keepvoice01", "confirm_fee": True},
                refresh=refresh,
            )
            t2a = [call for call in server.calls if call["path"] == "/v1/t2a_v2"]
            assert len(t2a) == 1
            assert t2a[0]["json"]["text"] == KEEP_TEXT
            assert t2a[0]["json"]["voice_setting"]["voice_id"] == "Keepvoice01"
            assert t2a[0]["json"]["audio_setting"]["format"] == "mp3"
            assert t2a[0]["authorization"] == "Bearer secret-key"
            assert kept["take"]["status"] == "kept"
            assert kept["take"]["fee_amount"] == 9.9
            assert kept["usage_characters"] == 4
            assert refreshed == ["yes"]
            assert kept["spend"] == pytest.approx(9.9 + 4 * 200 / 1_000_000)
            studio.assert_usable(host, "Keepvoice01")
            deleted = await studio.remove(
                client, host, {"voice_id": "Keepvoice01", "kind": "clone"}
            )
            assert deleted["take"]["status"] == "deleted"
            assert {
                call["json"]["voice_type"]
                for call in server.calls
                if call["path"] == "/v1/delete_voice"
            } == {"voice_cloning"}

            other = studio.save_upload("source", "other.mp3", b"ID3other")
            await studio.clone(
                client,
                host,
                "speech-2.8-hd",
                {
                    "voice_id": "Audition001",
                    "source_id": other["upload_id"],
                    "source_seconds": 11,
                },
            )
            with pytest.raises(StudioError) as blocked:
                studio.assert_usable(host, "Audition001")
            assert blocked.value.code == "keep_first"
            played = await studio.audition(
                client,
                host,
                "speech-2.8-hd",
                {
                    "voice_id": "Audition001",
                    "text": "你好",
                    "model": "speech-2.8-hd",
                    "confirm_fee": True,
                },
            )
            assert played["kept"] is True
            assert played["take"]["fee_paid"] is True
            studio.assert_usable(host, "Audition001")
            again = await studio.audition(
                client,
                host,
                "speech-2.8-hd",
                {"voice_id": "Audition001", "text": "AB", "confirm_fee": False},
            )
            assert again["kept"] is False
            assert again["spend"] == pytest.approx(played["spend"] + 4 * 350 / 1_000_000)
            assert len([call for call in server.calls if call["path"] == "/v1/t2a_v2"]) == 3
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_rejects_bad_files_before_minimax(tmp_path: Path) -> None:
    studio = VoiceStudio(tmp_path)
    with pytest.raises(StudioError) as wrong:
        studio.save_upload("source", "notes.txt", b"hello")
    assert wrong.value.code == "file_type"
    with pytest.raises(StudioError) as large:
        studio.save_upload("source", "big.mp3", b"1234567890", limit=4)
    assert large.value.code == "file_size"


def test_readme_mentions_the_studio() -> None:
    readme = Path("README.md").read_text()
    assert "MiniMax 音色工作室" in readme
    assert "MiniMax Voice studio" in readme
    assert "7 days" in readme

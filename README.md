# astrbot_plugin_minimax_speech

[中文](#中文) · [English](#english)

## 中文

AstrBot 插件。用 [MiniMax 同步语音合成](https://platform.minimaxi.com/docs/api-reference/speech-t2a-http) 把 QQ 文字回复合成为语音消息。

支持这些适配器：

- `qq_official`（WebSocket）
- `qq_official_webhook`（Webhook）
- `aiocqhttp`（OneBot，含 NapCat）

Webhook 事件类继承同一个发送器，因此两种官方适配器上传语音的方式相同。

### 发送内容

插件调用 MiniMax `POST /v1/t2a_v2`，使用所选语音模型、非流式 hex 输出，以及 24 kHz 单声道 PCM，再包成 WAV `Record`。默认模型是 `speech-2.8-hd`。

AstrBot 4.29.3 随后会：

1. 用 `target_format="tencent_silk"` 解析这条录音。
2. 用 `pysilk`（`tencent=True`）编码，并加上 QQ Silk 文件头。
3. 以语音（`file_type=3`）上传到 `POST /v2/users/{openid}/files` 或 `POST /v2/groups/{group_openid}/files`。
4. 用 `msg_type=7` 和 `media.file_info` 发送。

`aiocqhttp` 把同一条 WAV 转成 base64 发给协议端。NapCat 收到语音段后自己编码成 Silk。插件不在这条路径上再转一次 Silk，否则适配器会把文件当成 WAV 再读一遍。

原文字默认和语音留在同一条消息里。回复方式可以改成只发语音，或按条件只发语音，见下方。带表情图片的文字回复仍会附上语音，引用回复也会保留。已经带语音、视频或文件的回复不改。朗读文本最多 400 字；默认模式下界面上的文字不会被截断。

### 配置

管理面板的语言会切换这些配置项的中文或英文文案。

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `api_key` | 空 | MiniMax Bearer 令牌。留空则不发语音。 |
| `api_base` | `https://api.minimax.io` | 国际或中国服务器。保存后重新打开页面，音色列表才会从该服务器重新加载。 |
| `model` | `speech-2.8-hd` | 语音模型。HD 比 Turbo 贵。 |
| `language` | `any` | 更改后，下方音色列表立刻按该语言切换。 |
| `gender` | `any` | 音色列表按女声、男声或未标明切换。性别从名称推断。 |
| `voice_id` | `male-qn-qingse` | 当前语言和性别下的音色。 |

不发送 GroupId。当前 T2A 接口只使用 Bearer 令牌。

### 回复方式

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `reply_mode` | `both` | `both` 文字加语音，`voice` 仅语音，`smart` 按下面的条件只发语音。 |
| `smart_agent` | 开 | 智能模式下，让模型在适合听的回复开头写 `<voice-only/>`。标记会在其他插件读到回复之前去掉。 |
| `smart_mirror_voice` | 开 | 用户发来语音时，只回语音。 |
| `smart_keywords` | `语音,说给我听,读给我听,念给我听,voice` | 用户消息包含其中一句时，只回语音。 |
| `smart_fallback` | `both` | 智能模式没有触发时：文字加语音，或只发文字。 |

这些规则只作用在上面列出的 QQ 适配器的模型回复上。其他插件的命令回复保持原样。已经带语音、视频或文件的回复不改。表情图片会留在消息里，语音加在后面。

仅语音会在整段都能朗读时去掉文字。回复太长，或带有链接、代码时，文字会留下来，避免把内容念丢。语音合成失败时同样保留原文。

### 用法

把本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_minimax_speech` 并重载插件，然后填写 API 密钥。

- 回复方式为「文字和语音」时，上面列出的 QQ 适配器的模型回复会自动朗读。
- 智能模式只在模型标记、用户发来语音，或命中关键词时只发语音。
- `/tts 要朗读的文字` 按需朗读这段文字。仅语音和智能模式下这条命令只发语音。

### MiniMax 音色工作室

插件详情页里的「MiniMax 音色工作室」可以克隆一段音频，或用文字描述设计一个声音。

克隆和设计先得到一个**草稿**。试听草稿不会收取一次性音色费，但试听文字仍按字符计费。草稿如果 7 天内没有真正拿去合成，MiniMax 会删掉它。

**保存**会用这个音色合成一句短话。这一次会计入一次性费用（中国站克隆和设计都是 ¥9.90；国际站克隆 $1.50、设计 $3），音色才会留在账户里，并出现在上面的音色列表中。**用于回复**才会改 QQ 机器人正在用的声音。

页面上的金额是 2026-10-30 的标价估算。中国站一个汉字计 2 个字符。MiniMax 没有公开余额接口，所以页头的累计金额只统计从本页发出的调用，账单以 MiniMax 控制台为准。

官方适配器需要能导入 `pysilk`（`silk-python`）才能生成语音文件。NapCat 在协议端自己把 WAV 转成 Silk，这条路径不依赖 AstrBot 里的 `pysilk`。流式回复会跳过 `on_decorating_result`。如果自动语音没有附上，请关闭流式输出。

## English

An AstrBot plugin that turns a QQ reply into a voice message with [MiniMax synchronous speech](https://platform.minimax.io/docs/api-reference/speech-t2a-http).

It supports these adapters:

- `qq_official` (WebSocket)
- `qq_official_webhook` (Webhook)
- `aiocqhttp` (OneBot, including NapCat)

The webhook event class subclasses the same sender as the WebSocket adapter, so both official adapters upload voice the same way.

### What it sends

The plugin calls MiniMax `POST /v1/t2a_v2` with the selected speech model, non-streaming hex output, and 24 kHz mono PCM, then wraps that PCM in a WAV `Record`. The default model is `speech-2.8-hd`.

AstrBot 4.29.3 then:

1. Resolves the record with `target_format="tencent_silk"`.
2. Encodes it with `pysilk` (`tencent=True`), which prefixes the QQ Silk header.
3. Uploads it as voice (`file_type=3`) to `POST /v2/users/{openid}/files` or `POST /v2/groups/{group_openid}/files`.
4. Sends `msg_type=7` with `media.file_info`.

`aiocqhttp` base64-encodes that same WAV and sends a record segment. NapCat encodes Silk on its side. The plugin does not encode Silk for this path, because the adapter would then try to read the file as WAV.

The original text stays in the same message by default. Reply mode can send voice alone, or only when a condition matches. A reply that includes a meme image still gets a voice, and a quote of an earlier message stays in place. A reply that already contains a voice, video, or file is left unchanged. Spoken text is capped at 400 characters; in the default mode the visible text is not cut.

### Configuration

The dashboard language switches these labels between Chinese and English.

| Key | Default | Purpose |
| --- | --- | --- |
| `api_key` | empty | MiniMax Bearer token. Empty disables speech. |
| `api_base` | `https://api.minimax.io` | International or China. Save, then reopen the page so the voice list reloads from that host. |
| `model` | `speech-2.8-hd` | Speech model. HD costs more than Turbo. |
| `language` | `any` | The voice row below switches to that language as soon as you change this. |
| `gender` | `any` | The voice row switches to female, male, or unspecified. Inferred from the name. |
| `voice_id` | `male-qn-qingse` | The voice list for the language and gender selected above. |

GroupId is not sent. The current T2A spec authenticates with the Bearer token only.

### Reply mode

| Key | Default | Purpose |
| --- | --- | --- |
| `reply_mode` | `both` | `both` sends text and voice, `voice` sends voice only, `smart` sends voice only when a condition below matches. |
| `smart_agent` | on | In smart mode, the model may start a reply with `<voice-only/>` when it is better heard than read. The marker is removed before other plugins read the reply. |
| `smart_mirror_voice` | on | A voice message from the user gets a voice-only reply. |
| `smart_keywords` | `语音,说给我听,读给我听,念给我听,voice` | A user message containing one of these gets a voice-only reply. |
| `smart_fallback` | `both` | When smart mode does not trigger: text and voice, or text only. |

These rules apply only to model replies on the QQ adapters listed above. Command replies from other plugins are left unchanged, as are replies that already contain a voice, video, or file. A meme image stays in the message, and the voice is added after it.

Voice only drops the text when the whole reply can be spoken. A reply that is too long, or that contains a link or code, keeps its text so nothing is lost. A failed synthesis keeps the text too.

### Usage

Put this repository in AstrBot `data/plugins/astrbot_plugin_minimax_speech` and reload the plugin. Fill in the API key.

- With reply mode set to text and voice, model replies on the QQ adapters listed above are spoken automatically.
- Smart mode sends voice alone only when the model marks the reply, the user sent a voice, or a keyword matches.
- `/tts 要朗读的文字` speaks that text on demand. Voice only and smart mode send the voice without the text.

### MiniMax Voice studio

The plugin detail page opens **MiniMax Voice studio**. Clone a recording, or design a voice from a written description.

Clone and design create a **draft**. Previewing a draft does not charge the one-time voice fee, but the preview text is still billed by the character. MiniMax deletes a draft that is not used for synthesis within 7 days.

**Keep** speaks one short sentence with that voice. That call charges the one-time fee (China: ¥9.90 for either clone or design; international: $1.50 to clone, $3 to design) and saves the voice so it shows up in the voice list above. **Use for replies** is what changes the voice the QQ bot speaks with.

Amounts on the page are estimates from list prices checked on 2026-10-30. On the China host a Chinese character counts as 2. MiniMax has no public balance API, so the total at the top only adds up calls made from this page. The billing console is the record of what was charged.

The official adapter must be able to import `pysilk` (`silk-python`) to build the voice file. NapCat encodes the WAV to Silk itself, so that path does not need `pysilk` inside AstrBot. Streaming replies skip `on_decorating_result`, so turn streaming off if automatic voice does not attach.

## Development test

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest silk-python
.venv/bin/pytest
```

The tests check the T2A payload, WAV layout, a mocked MiniMax response, plugin wiring for the webhook adapter, and that the WAV encodes to the Tencent Silk header AstrBot uploads.

# Lovomo

[简体中文](README.md) · **English**

> Repository: <https://github.com/slpk1ng/Lovomo>

An AI character chat assistant built on a local large language model (LLM) and GPT-SoVITS speech synthesis. It connects to QQ through NapCat (the official QQ bot and WeChat ClawBot are also available as connection types) and ships with a full-featured WebUI console. The code is split by responsibility into the feature modules under `modules/`. **Connections** get their own page in the sidebar: one channel per platform, with WeChat ClawBot logging in by QR scan and the official QQ bot bound by QR scan (the AppID and AppSecret are filled in automatically; login state, receive cursor and session tokens are stored with the connection) and a character optionally bound to one channel. Messages go back through the channel the session came from, and that mapping survives a restart; a session never seen before is matched to its platform by the target format (WeChat openid / @chatroom group) before picking the connection — so scheduled tasks, proactive messages, holiday greetings and the mood diary also work on WeChat and the official QQ bot. The WeChat channel handles text and images (images are downloaded and decrypted for the vision model), and when replies stop arriving the delivery self-check tells apart “the service accepted it” from “it really reached WeChat” (WeChat's context_token expires, so anything that was not delivered — proactive messages and self-checks included — is held and re-sent in order once the user writes again and refreshes the token). The official QQ bot allows at most 5 messages per passive reply, and anything beyond that is sent right after as proactive messages.

## ✨ Features

* **Runs locally**: your data never passes through an external server.
* **AI voice interaction**: realistic text-to-speech synthesis through GPT-SoVITS, or a cloud TTS provider (OpenAI-compatible endpoints or Alibaba Cloud Bailian), with a per-emotion voice (the model and the voice must match each other), voice design from a text description, and voice cloning from one or more uploaded audio clips. Non-dialogue content is dropped before synthesis (`tts_strip_non_dialogue`, on by default): actions, expressions and scene notes in brackets (such as (笑) or [旁白]) and kaomoji stay in the message text while the TTS reads only the lines, and the log lists what was removed. Replies do not have to carry voice every time either: “voice sending mode” can send voice every time, by chance, or only in private chats. A local GPT-SoVITS service can be launched by the program itself ("Auto-start TTS service"), and the reason a start failed is written to `%LOCALAPPDATA%\Lovomo\tts_start.log`.
* **Emotion modelling**: multiple emotions, tone transitions, breathing gaps and crossfades make the voice sound natural.
* **Character customisation**: set the persona, reply style and prompts from the WebUI.
* **Chat management**: multiple sessions, history browsing and one-click deletion; replies can quote the current message, and on request can @ a group member who has spoken in this session, placed where the sentence calls for it instead of always at the very front; an @ is always a real mention (two @ signs written together in a line — "@@someone", "@@QQ number", "@@everyone" — are turned back into a real one, so the person actually gets notified, with a space between the mention and the text that follows; a single @ is always plain text); with “separate sending” on, the chat history keeps one record per message actually sent, and each person's bubbles are coloured differently; the record dialog is enlarged for chat; avatars use the real QQ pictures with the nickname next to its avatar, while sessions whose avatars cannot be fetched (WeChat) show no avatar and keep every message in the same green bubble instead of colour-coding speakers.
* **Poke, recall and forwarded messages**: the character notices being poked and reacts to it, and may poke the other side on her own — the poke is a real poke interaction rather than a "poked you" text message; when she says the wrong thing she recalls that reply, she may send one on purpose just to take it back after you have seen it, and she really recalls it when you explicitly ask her to (only what you type yourself counts — a mention of “recall” inside a quoted or forwarded message is not taken as a request; quoting a message and asking her to recall it recalls that quoted one, together with the voice of the same sentence, and a nudge like “you still haven't recalled it” counts too, and a group admin or the group owner can also have her recall someone else's message while an ordinary member cannot); forwarded chat records are read before replying; she also decides how each reply is sent (as usual with voice, as text only, or one message per character when asked to talk one character at a time, without dropping the sentences after it when that delivery mode shows up on a later sentence); on text-only channels (WeChat, the official QQ bot) the prompt no longer teaches her to @, quote, poke or recall, which those channels cannot do at all; an action the owner asks for, such as a mute or a recall, lands on the right target, and a receipt shows who it was done to and whether it was actually carried out; the default Murasame persona now asks for a more colloquial, detail-aware and self-driven way of speaking, with less assistant-like tone.
* **Log monitoring**: watch the running log in real time; every line starts with a uniform `[hh:mm:ss.mmm][source][level]` prefix (source is `Lovomo` or `插件`, level is `INFO` / `WARN` / `ERROR`, with no brackets around the level name), and lines are coloured by level (the time in green and not bold, INFO white, WARN yellow, ERROR red), to track down problems.
* **Memory freed when the window is closed**: after the window goes to the tray the interface processes are released (about 300MB back to the system), and the next open rebuilds them in roughly 0.5 seconds.

### 🧩 Extended features (each can be enabled and configured independently in the WebUI)

* **Scheduled tasks and proactive messages**: custom interval / daily / weekly greetings; the character starts a topic on its own after a long silence; automatic greetings for holidays, anniversaries and user birthdays; a missed greeting is sent when the program starts after the greeting time (can be turned off); quiet hours and a daily cap keep it from being annoying.
* **To-do reminders**: saying “remind me…” in a chat creates a to-do automatically (regex or LLM extraction), a reminder is sent when it is due, and everything is managed visually in the WebUI. The reminder wording can be generated live by the LLM in character (default) or use a fixed template; if the LLM fails or omits the item, the template is used instead so no reminder is lost. A reminder missed while the program was off is sent once after it comes back online, and one that fails to send (dropped connection) is retried a few times with growing delays before being marked expired instead of sitting there silently.
* **Conversation recall**: the character now remembers details from earlier conversations — unresolved topics are pinned on every turn (having agreed to go to the seaside but not set off yet is no longer forgotten once the chat drifts), and passages relevant to the current topic are retrieved from the history and injected. The lookup can be semantic (embedding, still matches when the wording changes) or character-overlap (lexical, no dependencies and works offline); it falls back to character overlap when embeddings are unavailable. The switch, the lookup mode and the number of passages are all under "Sending and Conversation Memory".
* **Streaming replies**: sentences are synthesised and sent as they are generated, which cuts the wait for the first sentence dramatically. When generation fails, a Chinese notice with the likely cause is sent back into the chat instead of a bare "something went wrong" (the raw error stays in the log and is never turned into speech).
* **Stickers**: local sticker images organised into emotion folders, attached to replies by probability; when the LLM judges an image interesting enough to keep, it can be filed into the matching emotion category automatically (only meme-shaped images are kept — interface or page screenshots and long images are rejected) and sent later on matching emotions (the keep decision rides along with the image-caption call; categorisation and naming use a separate neutral call without persona or mood so the character's current mood does not skew it). “Auto-recognize” next to Choose Image takes a batch of images or a whole folder to the vision model, which decides the emotion folder and a generic name for each one and files it into the matching folder under the sticker directory (folder names go through the same sanitising rules as auto-capture, so no junk folders are created).
* **Multi-character chat**: every character has its own persona and voice (a character that leaves its prompts empty never falls back to the default character's persona); after the bot is @-mentioned in a group, replies are routed by the character name found in the message, and characters can answer each other (group chats require an @ by default, which `group_need_at` turns off; quoting the bot's message counts as an @); forms of address and relationships already established in a group are remembered from the whole history, so after a long conversation another member cannot take them over by asking, claiming or impersonating; speaker labels are numbered from the whole history, so truncating the context with the automatic summary never makes them point at the wrong person.
* **Tool calling (function calling)**: built-in time / calculation / weather / web page reading, plus custom HTTP and command tools; every tool can be authorised separately (user allow-list, call quota).
* **Knowledge base RAG**: uploaded documents are chunked and vectorised automatically (local vector store, no external dependency) and relevant passages are retrieved and injected into answers.
* **User profiles**: nickname, birthday and preferences recorded per QQ number, with automatic LLM extraction and birthday greetings; the nickname defaults to the person's QQ nickname and is never overwritten automatically — only an explicit request in chat (or a manual edit in the WebUI) changes it; the removal fields the extractor may emit are capped per run (at most half of a category), so one bad judgement cannot wipe the whole profile.
* **Relationship progress (visual-novel style)**: affection and relationship nature are tracked per character and per person; affection only builds up slowly through time spent together (with a daily cap) and stands for familiarity (Stranger → Acquaintance → Friend → Close Friend), while Flirting and Lover are decided by whether romantic signals actually show up in the interaction — a purely friendly relationship is never counted as romance however high its affection gets, and a pet name alone never moves it on, and a vague “I'll say yes” does not count as confirming the relationship (she may be agreeing to something else) — only a reply that actually names the relationship records a partner; a relationship can also be undone — when the other side clearly says it is over (or she says so herself) the relationship is dissolved while affection is kept (it stands for familiarity, and breaking up does not turn two people back into strangers), and such a message always gets her own answer instead of being blocked by the mood probability; before the relationship reaches Flirting she does not flirt, does not make suggestive remarks and does not make the first move — the “loves to flirt” side of her persona only applies once the relationship has got there, so a brand-new acquaintance is not dragged straight into ambiguity; whether to accept a confession (or to speak up first) is the character's own decision. The dating difficulty comes in four levels (Easy / Normal / Hard / Very Hard, Normal by default) which is not just how fast affection grows but how self-consistent the character is: the higher the level, the more she has her own goals, boundaries, obsessions and the right to refuse — gifts, sweet talk and long company do not buy her heart, it takes understanding her past, respecting her limits and making choices that fit her logic at the decisive moments, and she may end up not choosing you at all; the level also sets the daily cap and how much the affection gains swing around; the closer the relationship the more proactive the character (stage unlocks), long silences slowly decay affection, naming another character makes her jealous, and she sends anniversary greetings on partner milestones; the earliest relationship nature that may settle a relationship, the partner limit (unlimited by default) and the daily affection cap are configurable, and the User Profiles page shows each person's relationship progress, affection and days together.
* **Companion play**: a small random adventure is thought up by the character each day and comes up naturally in conversation; things the character promises are recorded and fulfilled proactively by the daily companion check (which also runs neglect decay and anniversary greetings; the time is configurable).
* **Dynamic context**: automatic conversation summaries plus topic detection keep long chats from degrading; once a summary is updated the whole conversation is checked once more for a settled relationship, missing profile details and new vocabulary (gaps and missed judgements only, at most once per session every 10 minutes); character lines that slipped into the summary are removed on their own, so one stray line never voids the whole background (the earlier conversation is not lost).
* **Statistics**: message volume, emotion distribution and trend charts, and live LLM/TTS latency monitoring; plus mood/affection history curves and the daily mood diary — every chart's time range follows the one selected under Conversation Statistics by default (each can also be adjusted on its own) and a Total range was added, windows shorter than two days such as Today / Yesterday can still be viewed by hour and drilled down to the minute, and token statistics separate cloud from local inference and break usage down by model.
* **Emotion audio management**: upload and preview each emotion's reference audio in the WebUI, fill in the reference text per clip (one-click speech recognition can generate it), normalise durations in one click, and create or delete emotions in one click. The same speech recognition settings also handle voice messages from users: their audio is transcribed first and then answered (engine, language and model live under “Configuration → More → Speech Recognition”).
* **Data import and export**: back up and restore chat history (a single session or everything as one archive); the configuration can be saved as multiple presets and switched back with one click.
* **Reply judging and mood**: the LLM first decides whether a message deserves a reply; each character has a persistent mood value that rises and falls with the conversation, and a low mood lowers the reply probability according to your configuration (both switchable and tunable in the WebUI); each turn also pulls the mood a little toward its initial value (`mood_regress_rate`, 0.1 by default, 0 = off, so a mood that only gains never parks at the ceiling), and a low mood carried over from a previous day bounces back toward the initial value each day (`mood_daily_recover`, 20 by default), the mood dips late at night, warms up on weekends and sinks slowly in silent periods (judgement only, never persisted), and a short diary entry in the character's voice is written for every session each day from what actually happened there and sent out (missed checks are caught up on launch; the entry is delivered — one per session at a time, and a timed-out send is not retried because it may already have arrived — while the character herself still believes nobody else reads it; an entry that merely copies what was said that day is treated as laziness and rewritten, and the day is skipped if it still copies). Mood also shapes the speaking style directly: slightly low turns cold, very low turns irritable and compresses replies to 1–2 sentences (`mood_style_enabled`, on by default). In group chats the mood is isolated per user, so upsetting the character only lowers the willingness to reply to that person. After the character says she is going to sleep or play a game — something that takes a while — ordinary messages may go unanswered (calling her by name or deliberately disturbing her still wakes her up).
* **WebUI chat console and character archives**: the Chat Console page talks to the character straight from the browser through the exact same judging/mood/affection pipeline as real messages; the config page can pack a character's persona, session memories, affection and mood records into one JSON archive for export, and import one back (optionally overwriting the same key). Keyboard shortcuts: Ctrl+S is Save only, and Esc steps back (close a dialog → leave a plugin page/webui → return to the previous page); the save result appears above the floating save buttons, hovering a column of a statistics line chart lists the value of every curve in it, and long lists such as the mood diary, self-learning terms and user profiles are paged 10 entries at a time. The interface ships in Chinese and English, and log lines are coloured by level (INFO green, WARN yellow, ERROR red).
* **Miscall protection**: a message with no tool trigger word (time / calculation / weather / search / URL, etc.) never enters the tool flow, which cuts down stray tool calls noticeably; the trigger words are configurable in the WebUI; questions only the character herself can answer, such as what her own prompts are or what was said before, no longer go to a web search.
* **Split voice and text sending**: when voice and text are sent separately and the LLM returns one unsegmented paragraph, it is split on `。？！.!?` and each piece is synthesised and sent on its own.
* **Privacy**: an access password can protect the WebUI and chat history, and a second password can be set on top of it — reading chat history, saving, importing or exporting, installing plugins, deleting, publishing and unpublishing all require it again; deleting a session also clears that session's mood, mood diary, promises, proactive state and **its own relationship record** (relationships are tracked per session, so deleting a session takes that record with it instead of leaving it on screen to keep counting); deleting a private chat additionally wipes that user's profile (as if you had never talked), while deleting a group chat leaves the members' own private relationships and profiles untouched.
* **Flood protection**: when a session receives too many messages in a short window the extra ones are ignored, so they cannot occupy the LLM and slow down normal replies.
* **Tray residence**: closing the window hides it to the system tray by default (it leaves both the screen and the taskbar), and the tray context menu offers Open Lovomo / Restart Lovomo / Exit Lovomo; launching the executable again while it is already running just brings the existing window to the front instead of starting a second process.
* **Update checks**: new versions are checked against GitHub periodically and announced in an in-app dialog that also shows the release notes (browser-only access is notified too); past releases can be browsed and downloaded under Plugins → Release History.
* **Plugin system**: install plugins by uploading a `.zip` or from the online market to add features or change the look; plugins can declare a settings form or ship their own HTML feature page and a separate `webui.html` page; every package is statically scanned before install and high-risk operations must be confirmed explicitly; uninstalling can keep the plugin's configuration and data; authors can publish their own plugin to a GitHub market repository in one click.

## 🚀 Quick start

### Direct install

1. Download the latest `Lovomo_Setup.exe`.
2. Run the installer, choose an install path and finish.
3. Double-click the desktop icon to run it.

## 🖥️ WebUI guide

The console window opens automatically after launch (or browse to `http://127.0.0.1:11500`).

- **Configuration**: NapCat, LLM, TTS, character persona and emotion mapping, and the switches of every extended feature; the configuration can be saved as presets and switched at any time.
- **Plugin Market (beta)**: browse and refresh the online market, upload, scan and install plugins, and publish or unpublish them.
- **Plugins**: manage installed plugins — enable, disable, uninstall, pin, open settings/WebUI, check for updates and publish.
- **Chat History**: view and manage all sessions, with batch deletion, import/export and several sort orders; the complete chat log is kept.
- **Emotion Audio**: upload and preview each emotion's reference audio, fill in the reference text per clip, normalise durations and run speech recognition in one click.
- **Scheduled Tasks**: scheduled greetings, holiday / birthday events and to-do reminders.
- **Statistics**: conversation charts and live performance monitoring.
- **Advanced Features**: stickers, tool calling, knowledge base RAG, user profiles.
- **Logs**: fills the whole content area and shows the running state in real time; the checkbox in the top-right corner of the log area switches to the full log.
- **Guide**: the built-in tutorial, arranged as “get it working first, then tune it, then how to investigate problems”.
- **Interface language**: the bottom-left corner of the sidebar switches between Simplified Chinese and English; the interface text and messages change immediately.

> Clicking the **Lovomo** title at the top of the sidebar opens the program's release history, where you can browse and download the program's own releases.

## ⚙️ Configuration

After configuring everything in the WebUI, click “Save Only” (use “Save and Restart TTS” when the TTS service should restart as well); the program writes a `config.json` file and persists the settings. Scheduled tasks, tools, events and similar data live in separate JSON files under `data/`. **`config.json` and `data/` always live in `%LOCALAPPDATA%\Lovomo`** and are not tied to the install directory — reinstalling into another folder or upgrading in place keeps them working (a copy left in the program directory by an older version is moved over on first launch); plugins and publish records live in the same directory too. Uninstalling asks item by item whether to delete the configuration, chat history and plugins, keeping all of them by default.

The “LLM service URL”, “Vision model service URL” and “Custom request body fields (JSON)” fields in the WebUI ship with presets for common model providers — pick one and it fills the value in. When the vision model comes from a different provider than the language model, an extra “Vision model API key” field appears; leaving it empty reuses the LLM key.

### Core options

|Option|Description|
|-|-|
|napcat\_ws\_url|WebSocket address of NapCat|
|llm\_backend|LLM backend type: `ollama` or `openai` (LM Studio, llama.cpp and other OpenAI-compatible services)|
|llm\_base\_url|LLM service address: `http://127.0.0.1:11434` for Ollama, `http://127.0.0.1:1234/v1` for LM Studio|
|llm\_model\_name|Model ID, which must match a model already downloaded or loaded by the service (the WebUI can pick it from a folder or from the service list)|
|tts\_backend|TTS backend: `local` (local GPT-SoVITS) or `cloud` (cloud TTS)|
|client\_base\_url|GPT-SoVITS API address|
|ref\_audio\_root|Root directory of the reference audio|
|model\_dir|TTS model folder|
|streaming\_enabled|Streaming replies|
|scheduler\_enabled|Master switch for scheduled tasks|
|tools\_enabled|Master switch for tool calling|
|rag\_enabled|Knowledge base RAG|
|profiles\_enabled|User profiles|

## ❓ FAQ

**Q: Why does the window not open?**
A: Make sure the Microsoft Edge WebView2 Runtime is installed, and check whether the `webui\_port` in `config.json` is already taken.

**Q: A proactive message or reminder line is sometimes not sent?**
A: When the generated line exceeds “Proactive message / reminder length limit”, it is now cut back at punctuation instead of being discarded whole; it is only discarded when it contains no punctuation at all. Raise that limit if you want longer lines.

**Q: After an upgrade or a rebuild, the WebUI still looks like the old version?**
A: The page now asks the server for updates on every load, so a normal refresh is enough; if it still looks stale, force a refresh once (Ctrl+F5).

**Q: Why are some options on the configuration page missing?**
A: Most fields are **shown conditionally**: the TTS inference details only appear while “Hide advanced GSV parameters” is off, the sampling parameters require “Enable LLM sampling parameters” to be on first, and switches such as the plugin system and software updates require their master switch to be on (they live under “More” at the bottom of the page). This is expected.

**Q: Why did the TTS service not start automatically?**
A: Check that `auto\_start\_tts` is `true` in `config.json` and that the `tts\_start\_script` path is correct.

**Q: Why do proactive messages, to-dos or tool calling not work?**
A: Turn on the matching switch on the Configuration page (`proactive\_enabled`, `todo\_enabled`, `tools\_enabled`, etc.) and finish the related data setup as the page describes.

**Q: Uploading a PDF to RAG fails.**
A: PDF parsing needs an extra dependency: `pip install pypdf`. Plain text, Markdown and code files need nothing extra.

## 📦 Changelog

See [update.en.md](update.en.md).

## 📄 License and disclaimer

### License

This project is released under the **GNU Affero General Public License v3.0 (AGPL-3.0)**; the full text is in [`LICENSE`](LICENSE) at the repository root.

### Disclaimer

This software is intended for personal learning, technical research and entertainment only. It must not be used for anything illegal. The full terms are in [`DISCLAIMER.txt`](DISCLAIMER.txt).

Copyright and portrait rights: make sure the reference audio and voice models you import (such as `.ckpt` and `.pth` files) do not infringe any third party's copyright, portrait rights or voice rights. This repository contains no copyrighted audio material or model weights. Any legal dispute arising from the use of this software is the user's sole responsibility.

## 📞 Contact

- Project home: <https://github.com/slpk1ng/Lovomo>
- Issues / feature requests: <https://github.com/slpk1ng/Lovomo/issues>

GitHub: [Slpk1ng](https://github.com/Slpk1ng)

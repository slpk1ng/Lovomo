# Lovomo

[简体中文](README.md) · **English**

> Repository: <https://github.com/slpk1ng/Lovomo>

An AI character chat assistant built on a local large language model (LLM) and GPT-SoVITS speech synthesis. It connects to QQ through NapCat and ships with a full-featured WebUI console.

## ✨ Features

* **Runs locally**: your data never passes through an external server.
* **AI voice interaction**: realistic text-to-speech synthesis through GPT-SoVITS.
* **Emotion modelling**: multiple emotions, tone transitions, breathing gaps and crossfades make the voice sound natural.
* **Character customisation**: set the persona, reply style and prompts from the WebUI.
* **Chat management**: multiple sessions, history browsing and one-click deletion.
* **Log monitoring**: watch the running log in real time to track down problems.

### 🧩 Extended features (each can be enabled and configured independently in the WebUI)

* **Scheduled tasks and proactive messages**: custom interval / daily / weekly greetings; the character starts a topic on its own after a long silence; automatic greetings for holidays, anniversaries and user birthdays; a missed greeting is sent when the program starts after the greeting time (can be turned off); quiet hours and a daily cap keep it from being annoying.
* **To-do reminders**: saying “remind me…” in a chat creates a to-do automatically (regex or LLM extraction), a reminder is sent when it is due, and everything is managed visually in the WebUI. The reminder wording can be generated live by the LLM in character (default) or use a fixed template; if the LLM fails or omits the item, the template is used instead so no reminder is lost.
* **Streaming replies**: sentences are synthesised and sent as they are generated, which cuts the wait for the first sentence dramatically.
* **Stickers**: local sticker images organised into emotion folders, attached to replies by probability; when the LLM judges an image interesting enough to keep, it can be filed into the matching emotion category automatically and sent later on matching emotions (the keep decision rides along with the image-caption call; categorisation and naming use a separate neutral call without persona or mood so the character's current mood does not skew it).
* **Multi-character chat**: every character has its own persona and voice (a character that leaves its prompts empty never falls back to the default character's persona); after the bot is @-mentioned in a group, replies are routed by the character name found in the message, and characters can answer each other (group chats require an @ by default, which `group_need_at` turns off; quoting the bot's message counts as an @).
* **Tool calling (function calling)**: built-in time / calculation / weather / web page reading, plus custom HTTP and command tools; every tool can be authorised separately (user allow-list, call quota).
* **Knowledge base RAG**: uploaded documents are chunked and vectorised automatically (local vector store, no external dependency) and relevant passages are retrieved and injected into answers.
* **User profiles**: nickname, birthday and preferences recorded per QQ number, with automatic LLM extraction and birthday greetings.
* **Dynamic context**: automatic conversation summaries plus topic detection keep long chats from degrading.
* **Statistics**: message volume, emotion distribution and trend charts, and live LLM/TTS latency monitoring.
* **Emotion audio management**: upload and preview each emotion's reference audio in the WebUI, fill in the reference text per clip (one-click speech recognition can generate it), normalise durations in one click, and create or delete emotions in one click.
* **Data import and export**: back up and restore configuration and chat history (a single session or everything as one archive).
* **Reply judging and mood**: the LLM first decides whether a message deserves a reply; each character has a persistent mood value that rises and falls with the conversation, and a low mood lowers the reply probability according to your configuration (both switchable and tunable in the WebUI). Mood also shapes the speaking style directly: slightly low turns cold, very low turns irritable and compresses replies to 1–2 sentences (`mood_style_enabled`, on by default). In group chats the mood is isolated per user, so upsetting the character only lowers the willingness to reply to that person.
* **Miscall protection**: a message with no tool trigger word (time / calculation / weather / search / URL, etc.) never enters the tool flow, which cuts down stray tool calls noticeably; the trigger words are configurable in the WebUI.
* **Split voice and text sending**: when voice and text are sent separately and the LLM returns one unsegmented paragraph, it is split on `。？！.!?` and each piece is synthesised and sent on its own.
* **Privacy**: an access password can protect the WebUI and chat history, and a second password can be set on top of it — reading chat history, saving, importing or exporting, installing plugins, deleting, publishing and unpublishing all require it again; deleting a session also clears the matching character's mood records.
* **Flood protection**: when a session receives too many messages in a short window the extra ones are ignored, so they cannot occupy the LLM and slow down normal replies.
* **Tray residence**: closing the window hides it to the system tray by default (it leaves both the screen and the taskbar), and the tray context menu offers Open Lovomo / Restart Lovomo / Exit Lovomo; launching the executable again while it is already running just brings the existing window to the front instead of starting a second process.
* **Update checks**: new versions are checked against GitHub periodically and announced in an in-app dialog (browser-only access is notified too).
* **Plugin system**: install plugins by uploading a `.zip` or from the online market to add features or change the look; plugins can declare a settings form or ship their own HTML feature page and a separate `webui.html` page; every package is statically scanned before install and high-risk operations must be confirmed explicitly; uninstalling can keep the plugin's configuration and data; authors can publish their own plugin to a GitHub market repository in one click.

## 🚀 Quick start

### Direct install

1. Download the latest `Lovomo_Setup.exe`.
2. Run the installer, choose an install path and finish.
3. Double-click the desktop icon to run it.

## 🖥️ WebUI guide

The console window opens automatically after launch (or browse to `http://127.0.0.1:11500`).

* **Configuration**: NapCat address, LLM model API, TTS service address, character persona and emotion mapping, and the switches and parameters of every extended feature; configuration can be imported and exported.
* **Plugin Market (beta)**: upload a `.zip` plugin package, open the plugin folder, refresh the online market (official / third-party), with a security scan before every install. Which repository the official market reads is set under “Configuration → Plugin System → Official market repository”, and plugins are served as folders — one `plugins/<category>/<plugin-id>/` folder per plugin in the market repository (the category comes from the plugin manifest's `category`, falling back to “Other”), with entries recorded in `plugins/index.json` (read from the repository's default branch); the toolbar filters by category, with the plugin count after each category, combined with search and sorting. Only when the index has no entries does it fall back to scanning plugin branches, and that branch list is paged in full (more than 100 entries are not missed). When “Source list path” points at a list inside the market repository (one `user/repo` per line), the official market also scans those repositories — an author adds one line to that list and opens a pull request, and the plugin is listed once it is merged, with no write access to the repository needed. On startup the online market is prefetched silently in the background, so opening the page shows the list immediately without delaying startup or interrupting what you are doing; only “Refresh Market” forces a fresh fetch.
* **Plugins**: manage installed plugins (enable / disable / uninstall, optionally keeping configuration and data), pin a card with the button in its top-right corner, click a card with a feature page to open its settings, and use “Open WebUI” for plugins that ship a `webui.html`. **A newly installed plugin is always disabled** — you enable it yourself from this page before it loads (overwriting an existing install keeps the previous switch, so an upgrade never changes your choice). “Publish to market” also lives here — one click writes the plugin into the market repository's `plugins/<category>/<plugin-id>/` folder (a previous category folder is removed in the same commit), updates `plugins/index.json`, and packs the plugin into a zip on the spot as an asset of that version's Release (download and favourite counts come from the Release), with the version tag shaped `<plugin-id>-v<version>`. Whether a version is already published is decided from the current market index (plus version tags) rather than the market page's 30-minute cache, so deleting a plugin directly on GitHub is reflected as soon as you reopen the panel; whatever this machine published or unpublished is also recorded in `publish_state.json` (still valid after a restart, expiring after 24 hours), covering the seconds before the index catches up. A plugin whose version has not moved up shows no publish button and instead says “The current version was already published” (the backend rejects a version that is not newer too, so calling the API by hand gets you nowhere). Published rows also have an “Unpublish” button that removes the market folder, the index entry, and all of that plugin's version tags and releases. A row only ever offers one action — either “Publish”, or “already published” plus “Unpublish”, never both buttons side by side; the panel switches to the new state the moment publishing or unpublishing succeeds (no waiting for another round trip), and the “Publish” button stays unclickable while a push is in flight so the same version cannot be pushed twice.
* **Chat History**: browse and manage all sessions, with batch deletion, import and export; the list can be sorted by character (same character grouped, newest first within the group), by newest conversation, or by oldest conversation — the last two sort globally by time without grouping by character.
* **Emotion Audio**: upload and preview each emotion's reference audio, fill in the reference text per clip, normalise durations and run speech recognition in one click.
* **Scheduled Tasks**: scheduled greetings, holiday / birthday events and to-do reminders.
* **Statistics**: conversation charts and live performance monitoring.
* **Advanced Features**: stickers, tool calling, knowledge base RAG, user profiles.
* **Logs**: fills the whole content area and shows the running state in real time; the checkbox in the top-right corner of the log area switches to the full log. The program log is written to `app.log` (in the program directory, or `%LOCALAPPDATA%\Lovomo` when installed to a read-only directory), and once it exceeds the log size limit (5 MB by default) the oldest entries are dropped.
* **Guide**: the built-in illustrated tutorial, arranged as “get it working first, then tune it, then how to investigate problems”.
* **Interface language**: the bottom-left corner of the sidebar switches between Simplified Chinese and English; the interface text and messages change immediately, and the choice is remembered across page reloads and program restarts.

> Clicking the **Lovomo** title at the top of the sidebar opens the program's release history, where you can browse and download the program's own releases.

## ⚙️ Configuration

After configuring everything in the WebUI, click “Save Only” (use “Save and Restart TTS” when the TTS service should restart as well); the program writes a `config.json` file and persists the settings. Scheduled tasks, tools, events and similar data live in separate JSON files under `data/`.

### Core options

|Option|Description|
|-|-|
|napcat\_ws\_url|WebSocket address of NapCat|
|llm\_backend|LLM backend type: `ollama` or `openai` (LM Studio, llama.cpp and other OpenAI-compatible services)|
|llm\_base\_url|LLM service address: `http://127.0.0.1:11434` for Ollama, `http://127.0.0.1:1234/v1` for LM Studio|
|llm\_model\_name|Model ID, which must match a model already downloaded or loaded by the service (the WebUI can pick it from a folder or from the service list)|
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

**Q: Why are some options on the configuration page missing?**
A: Most fields are **shown conditionally**: the TTS inference details only appear while “Hide advanced GSV parameters” is off, the sampling parameters require “Enable LLM sampling parameters” to be on first, and groups such as the plugin system and software updates require their master switch to be on. This is expected.

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

> To avoid confusion: this repository has **exactly one license**, the `LICENSE` file at the root (the official AGPL-3.0 text).
> The additional disclaimer lives in [`DISCLAIMER.txt`](DISCLAIMER.txt); it is a **terms of use**, not a license.

In short:

- ✅ You may use, modify and distribute this software freely, including commercially.
- ⚠️ **A modified version you distribute must also be open-sourced under AGPL-3.0**, with the copyright notice and a description of your changes preserved.
- ⚠️ **Section 13 (Remote Network Interaction)**: if you modify this software and offer it to others as a network service (for example, hosting a server for other people to use), **you must provide those users with the complete source code of your modified version**. This is the key difference between AGPL and GPL.
- ⚠️ The software is provided “as is”, without warranty of any kind (Sections 15 and 16 of the license).

### Disclaimer

This software is intended for personal learning, technical research and entertainment only. It must not be used for anything illegal. The full terms are in [`DISCLAIMER.txt`](DISCLAIMER.txt).

Copyright and portrait rights: make sure the reference audio and voice models you import (such as `.ckpt` and `.pth` files) do not infringe any third party's copyright, portrait rights or voice rights. This repository contains no copyrighted audio material or model weights. Any legal dispute arising from the use of this software is the user's sole responsibility.

## 📞 Contact

- Project home: <https://github.com/slpk1ng/Lovomo>
- Issues / feature requests: <https://github.com/slpk1ng/Lovomo/issues>

GitHub: [Slpk1ng](https://github.com/Slpk1ng)

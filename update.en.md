[简体中文](update.md) · English

### v1.2.2.0



#### ✨ Added



- Cloud TTS: speech synthesis can switch to a cloud provider (an OpenAI-compatible endpoint or Alibaba Cloud Bailian) by filling in the provider address, API key and model, so GPT-SoVITS no longer has to run locally; one voice can be set as the global default, and voices can also be assigned per emotion in the "Cloud TTS voice map".

- When the vision model and the language model come from different providers, a separate "Vision model API key" can be filled in; leaving it empty falls back to the LLM's key.

- "LLM service address", "Vision model service address" and "Custom request body fields (JSON)" now carry presets for common providers that fill everything in with a single pick; the cloud TTS provider, model and voice carry presets too, with voices grouped by provider and free-text values still allowed.

- Cloud TTS voice design: a voice design window next to "Cloud TTS default voice" creates a custom voice from a text description, with preview, listing and deletion, and fills the new voice into the default voice in one click.

- Cloud TTS voice cloning: the same spot uploads one or more audio clips to clone a voice, merging multiple files automatically, with an optional transcript for better results, listing and deletion, and one-click fill into the default voice.



#### 🛠 Fixed



- Cloud TTS: a transient audio download failure is now retried instead of dropping that sentence's audio; when the voice-cloning file picker allows only one file at a time, selecting again adds to the list.

- Chat history: a session file keeps the complete chat log instead of only the most recent messages, so earlier conversations no longer disappear from "Chat History".

- Chat history: with "By character (newest in group)" selected, the character groups themselves are now ordered by their newest conversation, so the character you talked to most recently comes first.



#### ⚡ Improved



- Once the window is sent to the tray, the interface processes are released immediately (about 200MB back to the system); reopening the window rebuilds and reloads the interface in roughly 0.5 seconds.

- The TTS-related config groups are merged into a single "TTS service and model" group.

- "Export configuration" and "Import configuration" in the top right of the config page are merged into "Switch preset": save the current configuration as a preset on this machine (with an optional note, and deletable), then switch back to any of them in one click.



### v1.2.1.0



#### ✨ Added



- [Experimental] Emotion mimicry: imitates the speaker's intonation, phrasing and pauses.

- Emotion / tone folder names are no longer limited to pinyin; Chinese, English, invented words and punctuation or brackets are all accepted, and matching is case-insensitive.

- Stickers are filed under short, purpose-based names so they can be reused after switching characters; the keep reason and the user-profile rules are filled in dynamically by the active character.

- Voice loudness is normalised consistently; reference audio levels are normalised too.

- The mood value now shapes the speaking style directly: very low turns irritable, slightly low turns cold, and replies are compressed to 1\~2 sentences.

- Sticker sending normalises the longest edge, 400 by default and switchable; animated images are unaffected.

- "Web prefetch" and "Prefetch link limit" were added to the tool-calling configuration page.

- The plugin market is folder-based: one `plugins/<category>/<plugin-id>/` folder per plugin (the category comes from the manifest's `category`, falling back to "Other"), with entries recorded in `plugins/index.json`; one-click publishing writes the folder, updates the index, and packs the plugin into a zip as an asset of that version's Release, which is where the download and favourite counts come from.

- The plugin market toolbar filters by category, with the plugin count after each category, combined with search and sorting.

- The publish panel gained "Unpublish": it removes the market folder, the index entry, and all of that plugin's version tags and releases.

- A second password was added under "WebUI access password": reading chat history, saving, importing or exporting, installing plugins, deleting, publishing and unpublishing all require it again; the unlock duration is configurable, and 0 means it is asked every time.



#### 🛠 Fixed



- Plugin publishing: right after a version is published the UI immediately shows "The current version was already published" instead of allowing a repeated push; the backend rejects a local version that is not newer than the published one, so you cannot go back from 1.0 to 0.1 and scramble the version history.

- Plugin publishing: "published / current version already published / Unpublish button" are now decided from the current market index instead of the market page's 30-minute cache, so deleting a plugin on GitHub stops it from showing as published as soon as you reopen the panel; what this machine published or unpublished is persisted, so a restart still blocks a repeated push of the same version and an unpublished plugin no longer shows as published; a freshly published plugin can be unpublished right away.

- Plugin install: a newly installed plugin is now disabled by default and is enabled by you from the Plugins page (overwriting an existing install keeps the previous switch), instead of starting to run the moment it is installed.

- Publish panel: a row no longer shows both "Publish" and "Unpublish" at once — it offers a single action; after publishing or unpublishing the row switches to the new state right away from the response instead of waiting for the next query (which used to take several seconds); the "Publish" button stays unclickable while a push is in flight, so the same version cannot be pushed twice.

- Stickers: automatic collection by the LLM now files images by the naming style of the sticker root folder, so an existing "撒娇" folder no longer gets a second `sajiao` next to it.

- Stickers: capture now de-duplicates by image content, so an image already in the library is no longer stored twice; leftover empty folders are no longer treated as categories, so captures cannot land in an invisible folder.

- Sticker page: broken thumbnails, undeletable stickers, 404s on filenames with Chinese punctuation, and broken WebP images.

- Characters and sessions: a new session was miscounted as the default character, and after switching characters the default character's name leaked into user profiles; a character with no prompts of its own (such as a blank persona created without a name) still received the default character's persona prompts.

- Emotion folders: names with Chinese punctuation or brackets could not be created, and matching was case-sensitive.

- Mood and judging: the mood value was refreshed before the reply was generated; the switch compatibility layer failed, so the string false was treated as on.

- Greetings and reminders: holiday, birthday and daily scheduled greetings did not carry the history forward and opened out of nowhere; failed to-dos were still marked complete; proactive greetings were not written into the session history.

- Automatic collection: category naming was influenced by mood; supplementary rules overrode hard vetoes; a category was still picked when the classification was uncertain.

- Self-learning: learned meanings were generalised into an abstract "state" and words with a literal sense were read through internet slang; meanings are now written from the literal sense first, and the old prompt is migrated on upgrade.

- State saving: after a read failure several places overwrote the disk with empty data; atomic writes left `.tmp` files behind; concurrent saves overwrote each other.

- User data location: `config.json` and `data/` now live in `%LOCALAPPDATA%\Lovomo` instead of the install folder, so reinstalling into a different folder or upgrading in place keeps working (a copy left in the program folder by an older version is moved over on first launch; if the move cannot complete it stays where it is rather than risking the data). Uninstalling asks separately about the settings, chat history, plugins and local records and keeps all of them by default; "delete" now clears both the program folder and the user folder, so choosing delete no longer silently does nothing, and plugins or publish records no longer survive it.

- Log location: when probing whether the program folder is writable, an antivirus scanner, indexer or cloud-sync client holding the freshly created probe file made the delete fail, which was misread as "folder not writable" and silently redirected the log to the user directory (so `app.log` was missing from the program folder when troubleshooting). The verdict now depends only on the write succeeding; a failed cleanup no longer changes it.

- Plugins, tools, knowledge base, lexicon and sticker index: fixed atomic writes, read guards, boolean strings, default values and empty-data handling.

- TTS / ASR: wrong online detection, expiring timeouts, concurrent overwrites of temporary files, and TTS latency statistics that were always 0.

- WebUI: saving the configuration wiped fields that were not rendered, `success:false` was still reported as success, XSS in plugin publishing, and caching / polling anomalies; the chat list still left grouping gaps between characters when sorted by time.

- Other: weekday string matching in scheduled tasks, plugin market version matching, YAML null values being distorted, login session 0 being ignored, NapCat tokens stored in plain text, statistics showing 0 incorrectly, and more.



#### ⚡ Improved



- Emotion folder scanning is more fault tolerant; a startup warning when the default emotion is missing; a log hint for unrecognised emotions.

- Custom emotion names can now hit sticker categories and profile extraction.

- Old configurations are migrated automatically.

- A summary hint about reference audio quality problems, and poor audio no longer enters the candidate pool.

- When a model's content moderation blocks a request, the reason and the way forward are now stated clearly.





### v1.2.0.0



#### ✨ Added



*\*Plugin system\*\*



- The plugin list supports \*\*pinning\*\*: click "Pin" in the top-right corner of a card and the plugin moves to the front of the Plugins page (the order is stored in the plugin state file and survives a restart).

- Plugins can ship their own `webui.html` (a fixed filename): the card in the plugin list gains an "Open WebUI" button, which opens a standalone page alongside the feature page for status panels and small utilities that have nothing to do with settings.

- The plugin feature page gained a "View README" button, so the documentation shipped with a plugin can be reread at any time.

- Uninstalling a plugin now also asks whether to remove the plugin configuration (`data/settings.json`) and the plugin data (the whole `data/` directory); both are kept by default, so reinstalling the same plugin continues with the previous settings and data.

- A plugin's dropdown options can come from a JSON file in its own directory (`options\_file` in the manifest), so options only known at runtime (such as which recording devices this machine has) can also be a dropdown.

- The official market repository can be changed on the configuration page (`plugin\_market\_repo`); a separate market repository is recommended so plugin branches do not pile up in the program repository. The program repository is organised as: `plugins/sources/<id>/` holds the current version's source, `plugins/packages/` holds local build output, and everything published lives on branches.

- One-click publishing tags the version automatically: `<branch>-v<version>` (for example `lovomo\_plugin\_meow-skin-v1.0.1`). An existing tag is never recreated, so the version history is a series of immutable tags you can download and roll back to at any time.

- A version that has not moved up gets no publish button: when the local version equals the published one, when the local build is still beta while the published one is stable, or when the published version is higher, the button is replaced with "The current version was already published (published vX.Y.Z)". Bump `version` in the manifest and publish again.

- The repository the official market scans, the branch prefix, the fallback index and the third-party market addresses are all gathered under "Configuration → Plugin System"; the branch list is paged in full (GitHub returns at most 100 at a time).



*\*Network and search\*\*



- GitHub accelerator mirrors (`github\_mirrors`): when GitHub cannot be reached directly (plugin market, version information, plugin package downloads), the configured mirrors are tried in order automatically. The configuration page offers one-click "Speed Test and Sort", marking each mirror's latency in green / yellow / red (green ≤600ms, yellow ≤1500ms, red slower or unavailable) and reordering them by speed.

- Hardened safe search: the block list grew substantially (about 185 entries on the normal level and about 230 on the strict level), and editable "Search filter words" and "Search allow-list words" (`web\_search\_block\_words`, `web\_search\_allow\_words`) were added so you can extend it yourself.



*\*Messages and sending\*\*



- Session allow-list (`whitelist\_ids`): once QQ numbers or QQ group numbers are filled in, the bot only responds to those sessions; empty responds to everything.



*\*Repeat guard\*\*



- The repeat-guard thresholds are adjustable: the overlap thresholds against the character's own history and against the user's original wording (`repeat\_guard\_self\_threshold`, `repeat\_guard\_user\_threshold`) can be tuned in the WebUI.



*\*Stickers\*\*



- Four sending modes (`sticker\_send\_mode`): off / random / by emotion / by description. With "by description" the descriptions of the candidate stickers are handed to the model, which picks the one that fits the current line best.

- Stickers kept from image captioning are named after the reason the model gives (duplicate names get a numeric suffix), and the file extension follows the image's real format.



*\*WebUI\*\*



- Log compact mode supports custom hidden fragments (`webui\_log\_hide\_patterns`): noise such as automatic collection or TTS parameters is hidden by substring, and ticking "Show Full Log" prints everything as usual.



#### 🛠 Fixed



*\*Other\*\*



- On a fresh install the whole NapCat connection group was hidden and could not be edited in the WebUI.

- After setting a WebUI password, exporting the configuration and chat history reported "Password required" in the browser.

- Switching away from a WebUI page and back jumped to the top of the page: each page now remembers its own scroll position, and the configuration page restores it once the form has finished refreshing.

- Opening a chat detail now scrolls to the newest message; conversations more than 5 minutes apart get a time separator (today shows only the time, yesterday and the day before carry a relative date, and anything older shows the full date).

- Uninstalling the program did not clean up runtime logs and temporary files: the uninstaller now asks whether to delete config.json and data (chat history and so on), the silent uninstall path used by an in-place upgrade leaves user data alone, and a normal install now goes to a per-user directory instead of writing into Program Files.

- Installing the program into a read-only directory such as Program Files crashed it on startup (log and configuration writes were refused): runtime files are now written to the user directory (`%LOCALAPPDATA%\\Lovomo`) automatically and existing configuration is migrated along with them; printing special symbols in a GBK code page terminal no longer aborts the program.

- The export button did nothing in the desktop window (WebView2 blocks page downloads by default): exporting the configuration and chat history now goes through the system "Save as" dialog, while browser access keeps downloading directly.

- Exported configuration files contained decrypted plaintext keys: exports now always keep the `enc:...` encrypted placeholder form from config.json, and any plaintext key is re-encrypted before export.



#### ⚡ Improved



- The flashing CMD window is hidden when the program exits.

- Proactive message and greeting parameters can be selected in multiple ways.





### v1.2.0.0-beta



#### ✨ Added



*\*Models and LLM\*\*



- Visual model selection in the WebUI: the LLM and image-caption models can be found by clicking "Choose Folder" to scan local models or "Service list" to pull the model IDs from the service, so model identifiers no longer have to be copied by hand; path fields such as the TTS model directory, reference audio and sticker directory all support one-click "Browse".

- Automatically unload the old model after switching (`llm\_auto\_unload\_old`, on by default): once the new model's first call succeeds, the old model that is no longer used is unloaded, so several models do not sit in memory and exhaust VRAM or slow down replies.

- Text cleanup block list (`text\_clean\_blocklist`): specified characters or words in an LLM reply are removed before sending, for both voice and text.

- Search keywords extracted by the LLM (`search\_query\_llm\_extract`): before a prefetch search the model pulls the keywords out of the user's message and drops filler and irrelevant characters; if it fails or times out, rule-based extraction is used instead.

- Emotion rule guidance (`emotion\_guide\_enabled`, on by default): the prompt states when each emotion applies, so intimate, flirtatious and playing-hard-to-get scenes must prefer "shy" and "calm" is only a last resort; custom rules can be appended (`emotion\_guide\_extra`).



*\*Messages and sending\*\*



- Automatic retry on NapCat send timeout (`send\_retry`): when QQ occasionally throws "NTEvent Timeout" and a whole sentence is lost, it is sent once more.

- When voice and text are sent separately, the text is split on `。？！.!?` and each sentence requests TTS on its own, so text and voice correspond one to one.

- Reply judging and mood are isolated per "session + user + character": upsetting the character in a group only lowers the willingness to reply to that person.

- The @-mentioned target is injected into the context so the LLM is less likely to confuse who was mentioned.

- User profiles support replacing or deleting old entries per conversation (`likes\_remove`, `clear\_\*` and so on) so stale information does not linger.

- To-do reminder wording can be LLM-generated or a preset template (`todo\_remind\_mode`), falling back to the template when the model fails so no reminder is lost; the reminder voice can use its own emotion (`todo\_voice\_emotion`).



*\*Voice and repeat guard\*\*



- The image-caption model can use its own endpoint address (`image\_caption\_base\_url`, empty follows the LLM service address), so a cloud vision model and a local chat model can point at different services.

- The repeat guard was split into four independent switches (all on by default): by "when to check" there is "block before the first streaming sentence is sent" (`repeat\_guard\_streaming\_check`) and "validate and regenerate after the whole reply is generated" (`repeat\_guard\_regen\_check`), and by "what to compare against" there is "compare with the character's history" (`repeat\_guard\_compare\_self`) and "compare with the user's original wording" (`repeat\_guard\_compare\_user`). They can be combined freely.

- An upper bound on synthesis duration (`tts\_max\_seconds\_per\_char`, 0.6 seconds per character by default): audio that is clearly too long for the line is treated as abnormal and retried automatically.



*\*System and WebUI\*\*



- API keys are now genuinely encrypted: the key is bound to this machine's hardware to derive the encryption key (HMAC-SHA256 stream encryption with an integrity check), so copying config.json to another computer cannot decrypt it; the WebUI and exported files only show the first and last 4 characters plus the real length (for example `sk-a\*\*\*\*6789（18位）`)



- Tray residence: closing the window minimises to the tray, whose context menu offers Open Lovomo / Restart / Exit.

- Password protection for chat history access (`webui\_password`).

- Flood protection (`anti\_spam\_\*`): too many messages in one session in a short window are ignored, so they do not occupy the LLM.

- New versions are checked against GitHub periodically and announced in an in-app dialog (`update\_check\_\*`).

- TTS troubleshooting log (`tts\_debug\_log`): prints the line, emotion, reference audio and other synthesis details; the character replacement table is configurable in the WebUI (`tts\_char\_map`).

- The program name and current version are shown in the top-left corner of the WebUI; the sidebar menu moved down so it is visually separated from the title.

- The program was renamed to Lovomo and the database file to `lovomo.db` (on first launch the old `ltvm.db` is picked up automatically, so statistics and to-dos are not lost).



#### 🛠 Fixed



*\*Replies and emotions\*\*



- A shy conversation used the calm voice: when the model output an emotion name with Chinese, English or a variant (such as 「害羞」, 「shy」 or 「haixiu。」) it was rejected by strict matching. Aliases are now resolved automatically and only fall back to the default voice when nothing can be resolved.

- Proactive messages read the model's reasoning out loud: the reasoning content is now stripped at all four layers — request, parsing, streaming and sending.

- The LLM mistook its own reply or the speaker label for the message content, and called a sticker the user sent "its own": label normalisation plus image identity rules (`image\_identity\_guard\_enabled`).



*\*Tools and search\*\*



- Safe search let adult content through: only a handful of keywords were filtered, so adult aggregator, gossip and hentai sites (names and domains) slipped past. Site names, domains and the adult term list are now complete and are filtered per level.

- Sending an image with a subjective question triggered a web search: subjective evaluation questions no longer trigger a search (the prompt forbids it explicitly and a model-initiated search of that kind is blocked outright), which also applies when the tool decision runs in LLM mode.

- Search intent detection now ignores the word "search" inside negations and rhetorical questions.

- The LLM called tools when it did not need to: a message with no trigger word no longer enters the tool flow, and the trigger words are configurable in the WebUI.

- Calling the search or web tool returned `400 Bad Request`: the tool\_calls message shape sent to the service was invalid. It is now normalised before sending, and errors carry the upstream's original message.

- Inaccurate search keywords: a whole spoken sentence was used as the query. Negated clauses and request phrases are now dropped so only the search target remains.

- When a reply was rejected for being too similar and regenerated, the same search ran again (wasting 30 seconds or more): prefetched search results are now cached by keyword and reused directly.



*\*TTS and voice\*\*



- The voice was "one sentence / one word short" compared with the text: the cleanup allow-list silently deleted symbols, synthesis parameters were passed in the wrong order, and a mismatch in the number of Chinese and Japanese sentences degraded into synthesising the whole paragraph at once. Cleanup now only performs equivalent replacements and logs them, parameters are self-corrected, and a sentence-count mismatch is merged by character weight so text and voice correspond segment by segment.

- Text was sent one item at a time while the voice stuck together as one piece (short sentences were merged back under separate\_send).

- Printing symbols such as ♪ in a Chinese Windows environment threw UnicodeEncodeError and interrupted synthesis.

- A line's tail dragged on forever: the synthesis engine occasionally repeated the same syllable endlessly (a 30-character line turning into 27 seconds of 「に——」 or 「呜——」). Long drag markers (`——` / `ー`) are now compressed to two before synthesis, and audio clearly too long for the line is discarded and retried — better to send text only for that sentence than to send noise.

- A queued message waited for the previous voice to finish playing before reaching the LLM: the send-side "wait by audio duration" slept even after the last voice clip and did so while holding the session lock. It now waits for the previous clip to finish only before sending the next one, the trailing wait is gone entirely, and each wait uses exactly the duration of the clip that was just sent.

- The model "Service list" returned `400 Bad Request` when the address had never been filled in: `/v1/models` was appended unconditionally. An address that already carries an endpoint suffix is now used as is, listing models tries the candidate addresses one by one, errors list the addresses actually requested together with the upstream reason, and the message points out that the service root address is what should be filled in.



*\*Proactive messages\*\*



- No proactive messages for a long time even outside quiet hours: the scheduled time was recomputed every time, the idle timer was lost on restart, and the daily counter was never persisted. The schedule is now fixed once, the idle timer is restored from the last message, the state is persisted to `data/proactive\_state.json`, and diagnostic logging was added.

- Deleted sessions kept receiving proactive messages: after deleting a conversation the idle timer and daily counter stayed in memory and fired on schedule. Sending now checks that the session memory still exists, and deleting a conversation clears its proactive state at the same time.



*\*Stickers\*\*



- Pure scenery or text-free images were kept by mistake, GIFs were compressed into PNGs, and kept stickers could not be sent: keeping now requires an explicit decision and a concrete reason, GIF and WebP bytes are preserved as is, and the shared pool is synchronised automatically (`sticker\_capture\_any\_pool`).

- Automatic collection from image captioning "did nothing": the decision passed but nothing was written, and every skip path returned silently so the log showed no reason. Collection now reuses the original image bytes already read for captioning (no second download, so a dead QQ image short link no longer fails it), and every skip — minimum interval, daily cap, a switch being off, an unusable image source — states its reason.



*\*Other\*\*



- RAG wrongly reported "embedding model not configured" on a non-Ollama backend.



*\*Stability and data safety\*\*



- Session memory, config.json, mood, profiles, events, scheduled tasks and the other JSON files are all written atomically, so killing the process no longer leaves half-written files; a corrupt file is backed up as `.corrupt` instead of being silently zeroed.

- Fixed a concurrency race where the post-conversation summary task wrote an old session snapshot back over the freshly saved chat history; a user message is now persisted even when the decision is not to reply or generation fails.

- A corrupt config.json is backed up (`.corrupt`) and rebuilt instead of being overwritten with the default configuration.

- Sticker upload gained an image format allow-list and a 10 MB size limit, responses carry nosniff, and session cookies are httponly, closing a stored XSS hole.

- Exported configuration files no longer contain plaintext API keys (keys are exported as the `enc:...` encrypted placeholder and restored on import); side paths that write the configuration, such as saving characters, no longer write keys in plaintext.

- When a streaming reply was truncated, the rescued tail sentence is now sent properly instead of only being written to the history.

- A dead image source in an image-caption request no longer misaligns the following image URLs and contents.

- The per-reply tool call quota is now isolated per reply, so concurrent groups no longer reset or consume each other's quota, and a parameter parsing failure no longer consumes a call.

- web\_fetch fixed broken redirect validation (a 302 could bypass the internal-network guard); DNS validation moved out of the event loop so it no longer stalls everything; custom command tool output is decoded with UTF-8 tolerance; a custom HTTP tool now returns a clear error on a redirect instead of an empty result.

- A failed to-do reminder keeps its pending state instead of being marked complete, and a failed holiday greeting generation no longer marks the greeting as sent.

- Scheduled tasks gained overlap protection: when a callback is slower than the interval the current round is skipped instead of piling up concurrent runs.

- The statistics "today" bucket and the daily charts now share a local-midnight boundary, so 0:00–8:00 no longer counts as yesterday.

- Fixed speed and pitch changes and merge failures caused by mismatched sample rates or channels when merging voice, temporary file leaks on synthesis failure, concurrent messages each waiting 60 seconds when TTS fails to start, and a startup crash when a path option is a single character.

- "Export all chat history" no longer packs `webui\_auth.json` (which holds the WebUI token); importing, exporting and deleting chat history now only touch session memory files and never the feature data in the same directory.

- Write endpoints gained cross-site request blocking (an Origin/Referer that does not match the Host is refused), so a third-party page in the browser can no longer use the cookie to operate the local console; command line and script access is unaffected.

- A single abnormal configuration field (such as `ref\_audio\_root` being `null`) no longer marks the whole config.json as corrupt and resets it; a failed API key decryption keeps the original ciphertext instead of writing an empty string.



#### ⚡ Improved



- Connection failures (unreachable LLM or embedding service) now report the target address and troubleshooting advice, and are probed automatically on startup.

- The WebUI "Save configuration" merges with the default configuration, so fields the interface did not render are no longer dropped.

- The sticker keep decision rides along with the image-caption call (no extra request); mood statistics can expand the records of several sessions; tool testing supports custom JSON parameters; the JSON editor area can be resized.

- Deleting a session also clears the matching character's mood records.

[简体中文](update.md) · English

### v1.2.4.0


#### ✨ Added

- Relationship progress: a character can now be romanced gradually, Galgame style — affection only builds up slowly through long-term company (with a daily cap per person); Flirting and Lover are decided by whether romantic signals actually show up in the interaction, and whether to accept a confession, or to speak up first, is the character's own call. The earliest relationship nature that may settle a relationship, the partner limit (unlimited by default) and the daily affection cap are all configurable, and the User Profiles page shows each person's current relationship progress and affection.

- Mood diary: the statistics panel gained a session filter, so you can read one session's entries only.

- Statistics panel: the mood/affection curves gained a time-range filter — Today and Yesterday are shown by hour, and clicking an hour drills down to that hour's per-minute data.

- Mood diary: an entry is written even on a day when not a single word was exchanged — on days 1/2/3/7/14/30/60/90/180/365 since the last chat (private sessions only).

- Connections: the "NapCat Connection" block on the config page is now a "Connections" block.

- Two new connection types: WeChat ClawBot and the official QQ bot.

- WeChat ClawBot: incoming voice messages are now supported — they are turned into text before the character replies.

- Relationship progress: added a four-level "dating difficulty" (Easy / Normal / Hard / Very Hard, Normal by default), and affection changes now swing with some randomness.

- Companion play: added random adventures, promise tracking, anniversary congratulations, neglect decay, jealousy and stage-unlock behaviours, all driven by a daily companion check.

- Mood now rises and falls with late nights, weekends and long stretches without a chat, and every day a mood diary in the character's own voice — written from that day's real conversations — is written and sent out for each session.

- Voice: added a "strip non-dialogue content" switch (on by default) — actions, expressions and scene notes in brackets (such as (笑) or [旁白]) and kaomoji are no longer spoken and stay in the message text only; the log lists what was removed.

- Mood: added a "mood regression rate" (0.1 by default) that pulls the mood a little toward its initial value every turn. The mood used to only ever climb, so a few compliments pinned it at the ceiling and nothing moved it afterwards no matter what you talked about; now praise lifts it and a long flat stretch slowly drifts it back to the initial value.

- Conversation recall: the character now remembers details from earlier conversations — unresolved topics are pinned on every turn (having agreed to go to the seaside but not set off yet is no longer forgotten once the chat drifts), and passages relevant to the current topic are retrieved from the history and injected. The lookup can be semantic (embedding, still matches when the wording changes) or character-overlap (lexical, no dependencies and works offline); it falls back to character overlap when embeddings are unavailable. The switch, the lookup mode and the number of passages are all under "Sending and Conversation Memory".

- Group management: a character can mute a group member or recall a message someone else sent (both the requester and the bot must be an admin or the group owner).

- Statistics panel: added token usage statistics with Total / Today / Yesterday / Last 7 days / Last 14 days / Last 30 days ranges, drill-down by day, hour and minute like the mood curves, plus a ranking of usage by purpose.

- Quotes: when the message being answered is pushed away by someone else's messages, the reply now quotes that message instead.

- Smart reply: in groups and private chats the bot first judges whether you have finished; if not, it waits a while before answering, and restarts the wait when you send more in the meantime.

- Reply probability: each message can be given a fixed chance of being answered (0.5 by default); when the roll misses, that message is left unanswered.

- Configuration profiles: the whole configuration can be saved under several names, and each connection uses one (default is the main configuration and cannot be deleted).

- Profiles page: a Profiles entry at the top of the anchor navigation on the settings page opens a page where profiles can be created, renamed and deleted, with "Save profile" in the bottom-right corner.

- Official QQ bot: quoting, recalling, images and voice are now supported; one passive reply may contain at most 5 messages, and text is merged into its voice message when that would be exceeded — if it still overflows, the rest is sent right after as proactive messages.

- Official QQ bot: added QR binding — pick "official QQ bot" when creating a connection and scan with mobile QQ to fill in the AppID and AppSecret automatically, instead of copying them from the open platform by hand.

- Profiles page: a "Switch preset" button in the bottom-right corner lets you hop to another configuration profile and keep tweaking it.

- Config page: the dozen or so TTS and reply groups are merged into one "Voice and Reply" group; its sub-groups stay collapsed and hovering the group lists all of them in a tree on the right.

- Local models: added a "Preload local models at startup" switch (on by default) that loads the local models used by the configuration (chat, vision and embedding models; LM Studio, Ollama, llama.cpp and so on) when the program opens, instead of waiting for the first message.


#### 🛠 Fixed

- WeChat ClawBot: fixed incoming voice messages failing to produce text (the silk format has no decoder on this machine, so conversion failed outright).

- Replies: fixed the character copying speaker labels and sticker markers from the context into her lines, which made the whole reply look like malformed output and get discarded.

- Replies: fixed text messages tumbling out one after another when a whole reply carries no voice.

- Mood diary: fixed a diary written in one conversation drifting into another one and bringing things said elsewhere along with it.

- Context background: fixed the summary and topic being generated in the character's voice, which turned the character's mood and subjective judgement into background fed into every later turn — they are now compiled neutrally in the third person, and existing in-character background is no longer injected until it is rewritten.

- Multi-person chats: fixed the character mixing up who a relationship belongs to and treating the other speakers badly — the relationship only records who it is with, and everyone else is still answered politely and warmly.

- Relationship progress: fixed the affection score being read directly as the romance stage — affection only stands for familiarity (Stranger / Acquaintance / Friend / Close Friend), while Flirting and Lover are now decided by whether romantic signals actually show up in the interaction, so a relationship that only ever chats, without ever flirting, is never counted as romance however high its affection gets; the log and the User Profiles page show both the closeness and the relationship nature.

- Relationship progress: fixed a relationship both sides had already settled as lovers staying at Flirting forever without being recorded as partners — settling a relationship no longer depends on a handful of fixed phrasings; a one-sided confession, the other side agreeing to date, or the character speaking up first all count.

- Mood diary: fixed diary entries being missed when the daily check was skipped (the computer was shut down, for instance) and the app was opened again the next day — on launch the last few days are caught up day by day; the diary is also no longer written from the mood numbers and an impression, but from the conversations that actually happened in that session that day, and nothing is written when there is no conversation to base it on.

- Proactive messages: fixed voice synthesis being occupied all at once when greetings, the companion check and idle follow-ups fired together, leaving most messages without voice.

- Holiday/anniversary greetings: fixed the same session receiving the greeting twice (the broadcast session list was not de-duplicated).

- Configuration page: fixed "Hide advanced GSV parameters" hiding the required fields along with everything else.

- Configuration page: the save result no longer appears only at the top of the page; it now shows above the save button in the bottom-right corner, so it is visible when you scroll down to save.

- Mood: fixed the "mood update" log printing the wrong previous value.

- Mood diary: fixed the same session receiving two diary entries in one check (a session now gets at most one per run), and diary entries older than a day are no longer delivered late (they remain visible in the Mood Diary list).

- Mood diary: fixed a timed-out send being retried and the same entry arriving twice — a timeout only means the send receipt never came back, so the entry is neither retried nor queued again.

- Chat console: fixed the mood, affection, promise and other state surviving a conversation reset; clearing now returns to a completely fresh state.

- Plugins: fixed plugins being unable to send messages in WeChat / official QQ bot sessions.

- Release history: fixed the update notes not being rendered as Markdown.

- Context memory: fixed a conversation summary being misjudged as the character's own voice and therefore not injected at all, which lost the earlier conversation along with it.

- User profiles: fixed notes only ever growing, with outdated and duplicated entries left behind forever.

- TTS: fixed "Auto-start TTS service" giving up after 60 seconds with a not-ready message (a cold start often takes longer, and giving up also skipped switching the model weights) — the wait is now 300 seconds and the TTS process's own output goes to `%LOCALAPPDATA%\Lovomo\tts_start.log` so a failed start can be diagnosed.

- Configuration page: fixed the "GitHub accelerator mirrors" box being much narrower than every other input.

- Reminders: fixed reminders missed while the app was closed being merely marked "expired" and never delivered — they are now caught up.

- Time awareness: fixed the character playing along when the user names the wrong time of day.

- Relationship progress: fixed affection being shared as a single copy across every group chat and private chat with the same person — each session now keeps its own, so chatting in a group no longer moves the private chat's affection or relationship progress (existing saves are migrated onto that person's private record instead of resetting to zero).

- Knowledge Base (RAG): fixed the failure log naming llm_base_url as the target endpoint — it now prints the endpoint actually requested and points out that the Embedding endpoint must be filled in when the embedding service does not live at llm_base_url; a search that finds nothing also prints the closest similarity, so tuning rag_min_similarity is no longer guesswork.

- Chat recall: fixed chat recall, promise fulfilment and the daily encounter never taking effect at all — the three modules were never actually wired up at startup, so recall never retrieved a single past topic and promises were never fulfilled; the recall index also failed to save because its vector entries were not plain numbers, and now persists correctly.

- @-mentions: fixed the mention running into the text that follows it with no space in between.

- Model management: fixed the local model being reloaded on every reply, the RAG embedding model being evicted and erroring in a loop, and an unload command window popping up with cloud models when Unload the old model after switching was enabled.

- Emotion tone: fixed a whole sentence falling back to the default tone because the model wrote an emotion outside the list; the prompt now asks for the closest word from the list, and an unrecognised value is resolved from the line itself when possible.

- Repeat guard: fixed replies being judged as repetition and sent back for regeneration when the user explicitly asked to hear something repeated.

- Stats page: message volume and call counts plus the emotion trend are now shown hourly for Today / Yesterday instead of a single point, and any range can be clicked to drill down (range to day to hour to minute).

- Sentence splitting: fixed the piece after a terminal mark carrying a leading comma into the next message (a line starting with a comma showed up in the group).


#### ⚡ Improved

- UI: list pages such as Chat History, Plugin Market, Scheduled Tasks and User Profiles show skeleton placeholders first and fade in when the data arrives.

- UI: all native dialogs are replaced with in-app dialogs.

- Chat history: added search by character name / session ID.

- Mood diary: added search (character name / session ID) and sorting (newest / oldest).

- Plugin market: the search box and sort control now sit on the same row as Market and Category.

- Preset wording: holiday greetings, birthday wishes and scheduled tasks now generate their text in character by default, falling back to the preset wording only if generation fails.

- WebUI: a new "Chat Console" page lets you talk to the character for debugging without opening a chat app; the statistics panel gains mood/affection curves and the mood diary; the config page gains character archive export/import.

- UI: two shortcuts were added — Ctrl+S is the same as "Save only", and Esc goes back level by level (closing a dialog first, then leaving a plugin feature page/webui for the plugin list, and finally returning to the previous page).

- Logs: the level name is coloured on its own — INFO green, WARN yellow, ERROR red — with the body tinted by level, so a line reads "[time] [source] INFO body".

- UI: lists that keep growing — the mood diary, self-learning terms and user profiles — are now paged at 10 entries per page.

- Statistics: hovering over a column of a line chart shows the "colour value" of every curve at that column, listing them all at once when several curves share the column.

- Plugins: one click on "Plugins" in the sidebar expands a list containing only the plugins that ship a webui, and clicking a name jumps straight to that plugin's page; the plugin name on a card works the same way.

- Mood: added a daily recovery — a mood that has sunk to rock bottom slowly climbs back on its own from the next day.

- Update dialog: when a new version is found, this version's release notes are shown alongside.



### v1.2.3.0


#### ✨ Added

- Plugins: a new "Check for plugin updates" lists the plugins that have a newer release in the market, and each one can be downloaded and replaced right in the dialog (an overwrite install that keeps the enabled state and the plugin's data).

- Speech recognition: voice messages from users are transcribed with the settings under "Configuration → More → Speech Recognition" before the character answers.

- Configuration: a new "More" group collects statistics, software updates, the plugin system, volume normalisation and speech recognition; "Switch Preset" gained "Open Folder", which opens the preset directory directly.

- Chat history: different users' bubbles are coloured differently.

- Stickers: a new "One-click recognition" button takes a batch of images or a folder, lets the vision model judge for each image which emotion folder it belongs in and give it a generic name, and files them automatically into the matching folder under the sticker directory (folder names go through the same sanitising rules as manual collecting, so no junk folders are created).



#### 🛠 Fixed

- Speech recognition: fixed ModuleNotFoundError when the audio language is "Auto-detect" (and for Japanese/English/Korean) — the GPT-SoVITS runtime ignores PYTHONPATH, so the script now gets the project root through a bootstrap; a script that produces nothing now reports its exit code and reason instead of a bare "no text recognised".

- Speech synthesis: fixed a sentence being sent as a whole line after only its opening was spoken — when the voiced length is too short, the line is re-synthesised section by section and merged, and if that still fails only the text is sent instead of half a sentence of audio.

- Window close/restart: fixed duplicate processes after repeated open/close cycles, a slow UI rebuild, occasionally needing two clicks to minimise to tray, and the process disappearing when reopening from tray after local TTS was turned off.

- Second password: fixed cases where it still asked for the password again after unlocking.

- Group identity: fixed the character forgetting an already established form of address (who is recognised as what) after a long conversation.

- Group speaker mix-up: fixed the recent history entries sent to the model being renumbered and no longer matching the current speaker's number in the prompt when "automatic conversation summary" was on.

- User profiles: the nickname now defaults to the person's QQ nickname.

- Self-learning: fixed learned meanings being tied to one character and unusable after switching to another; fixed a term you had approved on the page dropping back into "Pending" after a restart.

- Chat history: fixed a reply sent as several separate messages being merged into one whole-paragraph record.

- Statistics: fixed the user column of "Most Active Sessions TOP10" being empty.

- Scheduled tasks: fixed the catch-up of a daily greeting missed that day possibly being sent twice.

- Reasoning output: fixed the full-width thinking markers of R1-style models not being recognised, so the reasoning process could be spoken as dialogue.

- User profiles: fixed raw user text being executed as an extraction instruction.

- Cloud TTS: fixed an abnormally large preview audio being decoded in full and eating memory.

- Speech recognition: fixed a temp directory that could not be deleted being left behind silently.

- Window: fixed the exit-time save and the delayed save overwriting each other.

- Password check: the login password and the second password now use a timing-safe comparison.

- Plugins: when a duplicate command name is shadowed by a later-loaded plugin, the log now says which one is in effect.

- Install/uninstall: installing or uninstalling now closes a running Lovomo first (it requests a normal exit and terminates the process directly if that does not happen within a few seconds), so a locked exe or log file can no longer make an install fail or delete only half of the program; the in-app tutorial was updated accordingly.



#### ⚡ Improved

- Log output: every line now carries the uniform `[hh:mm:ss.mmm][Lovomo/plugin][info/warning/error]` prefix (plugin-originated lines show `[plugin]`).

- Log output: log lines are coloured by level — white for normal output, yellow for warnings, red for errors, so problems stand out at a glance.

- Configuration page: the "currently active character identifier" moved to the top of "Multi-role Configuration"; "Hide advanced GSV parameters" and flood control moved into "More", which now always sits at the very bottom of the page.





### v1.2.2.0



#### ✨ Added



- Cloud TTS: speech synthesis can switch to a cloud provider (an OpenAI-compatible endpoint or Alibaba Cloud Bailian) by filling in the provider address, API key and model, so GPT-SoVITS no longer has to run locally; one voice can be set as the global default, and voices can also be assigned per emotion in the "Cloud TTS voice map".

- When the vision model and the language model are not from the same provider, a separate "Vision model API key" can be filled in; leaving it empty falls back to the LLM's key.

- "LLM service address", "Vision model service address" and "Custom request body fields (JSON)" on the config page carry presets for common providers that fill everything in with a single pick; the cloud TTS provider, model and voice carry presets too, with voices grouped by provider and free-text custom values still allowed.

- Cloud TTS voice design: open the voice design window next to "Cloud TTS default voice" to create a custom voice from a text description, with preview, listing and deletion, and fill the new voice into the default voice in one click.

- Cloud TTS voice cloning: from the same spot, upload one or more audio clips to clone a voice — multiple files are merged automatically, an optional transcript improves the result, voices can be listed and deleted, and a created voice fills the default voice in one click.

- A "Clear interface cache" button was added to the bottom right of the configuration page: when the interface looks unchanged after an update, one click tears down the interface processes, clears the WebView2 cache and reloads the interface.

- Updating is now a choice: when a new version is found you can "update online" from inside the app (mirrors are speed-tested concurrently to pick the fastest, with a progress bar; progress is visible in the log if it runs in the background), or "go to GitHub to update" as before; when the download finishes it asks whether to install now, and installing closes the old process and launches the installer, which is kept until the next "Check for updates" to continue installing.



#### 🛠 Fixed


- Cloud TTS: a transient audio download failure is retried automatically instead of dropping that sentence's voice; when the voice-cloning file picker allows only one file at a time, selecting several times in a row gathers all the audio clips.

- Chat history: fixed conversations longer than 60 messages disappearing from "Chat History".

- Chat history: with "By character (newest in group)" selected, the character groups themselves are now ordered by their newest conversation, so the character you talked to most recently comes first.

- The interface could still be the old one after a program update: the page is no longer kept in the cache, and the WebView2 cache directory is cleared automatically when the version or the interface files change (clearing the cache via the system browser has no effect on it).



#### ⚡ Improved


- Sending the window to the tray releases the interface processes immediately (about 200 MB of memory returned to the system); reopening the window rebuilds and reloads the interface in about 0.5 seconds.

- The TTS-related config groups are merged into a single "TTS service and model" group.

- "Export configuration" and "Import configuration" in the top right of the config page are merged into "Switch Preset": save the current configuration as a preset on this machine (with an optional note, and deletable), then switch back to any of them in one click.





### v1.2.1.0



#### ✨ Added



- [Experimental] Emotion mimicry: imitates the speaker's intonation, phrasing and the like.

- Emotion / tone folder names are no longer limited to pinyin; Chinese, English, invented words, punctuation and brackets are all accepted, and matching is case-insensitive.

- Stickers are filed under short, purpose-based names so they can be reused after switching characters; the keep reason and the user-profile rules are filled in dynamically by the current character.

- Voice loudness is normalised consistently; reference audio levels are handled uniformly.

- The mood value now shapes the speaking style directly: very low turns irritable, slightly low turns cold, and replies are compressed to 1~2 sentences.

- Sticker sending normalises the longest edge, 400 by default and switchable; animated images are unaffected.

- "Web prefetch" and "Prefetch link limit" were added to the tool-calling configuration page.

- The plugin market is organised by folder: one `plugins/<category>/<plugin-id>/` folder per plugin (the category comes from the manifest's `category`, falling back to "Other" when missing), with entries recorded in `plugins/index.json`; one-click publishing writes the folder, updates the index, and packs the plugin into a zip as an asset of that version's Release, which is where the download and favourite counts come from.

- The plugin market toolbar filters by category, with the plugin count after each category, combined with search and sorting.

- The publish panel gained "Unpublish": it removes the market's category folder and index entry, plus all of that plugin's version tags and Releases.

- A second password was added below "WebUI access password": sensitive operations such as reading chat history, saving, importing or exporting, installing plugins, deleting, publishing and unpublishing require it again; the unlock duration is configurable, and 0 means it is asked every time.



#### 🛠 Fixed



- Plugin publishing: right after a version is published the UI immediately shows "The current version was already published" instead of allowing a repeated push; the backend rejects a local version that is not higher than the published one, so you cannot go back from 1.0 to 0.1 and scramble the version history.

- Plugin publishing: "published / current version already published / Unpublish button" are now decided from the current market index instead of the market page's 30-minute cache — delete a plugin on GitHub and it stops showing as published the moment you reopen the panel; what this machine published or unpublished is persisted to disk, so a restart still blocks a repeated push of the same version and an unpublished plugin no longer shows as published; a freshly published plugin can be unpublished right away.

- Plugin install: a newly installed plugin is disabled by default and is enabled by you from the "Plugins" page (an overwrite upgrade keeps the previous switch), instead of starting to run the moment it is installed.

- Publish panel: a row no longer shows both "Publish" and "Unpublish" at once — it offers a single action; after publishing or unpublishing, the row switches to the new state right away from the response instead of waiting for the next query (which used to take several seconds); the "Publish" button stays unclickable while a push is in flight, so the same version cannot be pushed twice.

- Stickers: automatic collection by the LLM now files images by the naming style of the sticker root folder, so an existing "撒娇" folder no longer gets a second `sajiao` created next to it.

- Stickers: collecting de-duplicates by image content, so an image already in the library is no longer stored a second time; leftover empty folders are no longer treated as categories, so captures cannot land in an invisible folder.

- Sticker page: broken thumbnails, stickers that could not be deleted, 404s on filenames with Chinese punctuation, and broken WebP images.

- Characters/sessions: a new session was miscounted as the default character, and after switching characters the default character's name leaked into user profiles; a character with no prompts of its own (such as a blank persona created without a name) still received the default character's persona prompts.

- Emotion folders: names with Chinese punctuation or brackets could not be created, and matching was case-sensitive.

- Mood and judging: the mood value was refreshed before the reply was generated; the switch compatibility layer failed, so a string "false" was treated as on.

- Greetings/reminders: holiday, birthday and daily scheduled greetings did not carry the history forward and opened out of nowhere; failed to-dos were still marked complete; proactive greetings were not written into the session history.

- Automatic collection: category naming was influenced by mood; supplementary rules overrode hard vetoes; a category was still picked even when the classification was uncertain.

- Self-learning: learned meanings were generalised into abstract states, and words with a literal sense were read through their internet-slang meaning; meanings are now written from the literal sense first, and the old prompt is migrated along when upgraded.

- State saving: several places overwrote the disk with empty data after a read failure; atomic writes left `.tmp` files behind; concurrent saves overwrote each other.

- User data location: `config.json` and `data/` now live fixed in `%LOCALAPPDATA%\Lovomo` instead of the install folder — reinstalling into a different folder or upgrading in place keeps everything working (a copy left in the program folder by an older version is moved over in full on first launch; if the move cannot complete it stays where it is, never risking data loss for the sake of moving). Uninstalling asks item by item whether to delete the configuration, chat history, plugins and local records, keeping all of them by default; "delete" now clears both the program folder and the user folder, so choosing delete no longer silently deletes nothing, and plugins or publish records no longer survive forever.

- Log location: when probing whether the program folder is writable, if the freshly created probe file was immediately grabbed by an antivirus scanner, indexer or cloud-sync client and could not be deleted, that was misread as "folder not writable" and the log was silently redirected to the user directory (so `app.log` was missing from the program folder when troubleshooting). The verdict now depends only on whether the write succeeds; a failed cleanup no longer changes it.

- Plugins, tools, knowledge base, lexicon and sticker index: fixed atomic writes, read guards, boolean strings, default values and empty-data handling.

- TTS/ASR: wrong online detection, expiring timeouts, concurrent overwrites of temporary files, and TTS latency statistics that were always 0.

- WebUI: saving the configuration wiped fields that were not rendered, `success:false` was still reported as success, XSS in plugin publishing, and caching/polling anomalies; chat history leaves grouping gaps between characters even when sorted by time.

- Other: weekday string matching in scheduled tasks, plugin market version matching, YAML null values being distorted, login session 0 being ignored, NapCat tokens stored in plain text, statistics showing 0 incorrectly, and more.



#### ⚡ Improved



- Emotion folder scanning is more fault tolerant; a startup warning when the default emotion is missing; a log hint for unrecognised emotions.

- Custom emotion names can now hit sticker categories and profile extraction.

- Old configurations are migrated automatically.

- Reference audio quality problems are collected into a summary hint, and poor audio no longer enters the candidate pool.

- When a model's content moderation blocks a request, the reason and the way forward are stated clearly.






### v1.2.0.0



#### ✨ Added

**Plugin system**

- The plugin list supports **pinning**: click "Pin" in the top-right corner of a card and the pinned plugins move to the front of the "Plugins" page (the order is stored in the plugin state file and survives a restart).

- Plugins can ship their own `webui.html` (a fixed filename): the card in the plugin list gains an "Open WebUI" button, which opens a standalone page alongside the feature page, for status panels, small tools and other interfaces that have nothing to do with settings.

- The plugin feature page gained a "View README" button, so the documentation shipped with a plugin can be reread at any time.

- Uninstalling a plugin now also asks whether to remove the plugin configuration (`data/settings.json`) and the plugin data (the whole `data/` directory); both are kept by default, and keeping them means reinstalling the same plugin continues with the original settings and data.

- A plugin's dropdown options can come from a JSON file in its own directory (`options_file` in the manifest), so options only known at runtime (such as which recording devices this machine has) can also be a dropdown.

- The official market repository can be changed on the configuration page (`plugin_market_repo`); a separate market repository is recommended, so plugin branches do not pile up in the program repository; the program repository's division of labour is "`plugins/sources/<id>/` holds the current version's source, `plugins/packages/` holds local build output, and everything published lives on branches".

- One-click publishing tags the version automatically: `<branch name>-v<version>` (for example `lovomo_plugin_meow-skin-v1.0.1`); an existing tag is never recreated, so the version history is a series of immutable tags you can download and roll back to at any time.

- A version that has not moved up gets no publish button: when the local version equals the published one, when the local build is still beta while the published one is stable, or when the published version is higher, the button is replaced with "The current version was already published (published vX.Y.Z)" — bump `version` in the manifest and publish again.

- The repositories the official market scans, the branch prefix, the fallback index and the third-party market addresses are all gathered under "Configuration → Plugin System"; the branch list is paged in full (GitHub returns at most 100 at a time).


**Network and search**

- GitHub accelerator mirrors (`github_mirrors`): when GitHub cannot be reached directly (plugin market, version information, plugin package downloads), the configured mirrors are tried in order automatically; the configuration page offers one-click "Speed Test and Sort", marking each mirror's latency in green / yellow / red (green ≤600ms, yellow ≤1500ms, red slower or unavailable) and reordering them by speed.

- Hardened safe search: the block list grew substantially (about 185 entries on the normal level and about 230 on the strict level), and editable "Search filter words" and "Search allow-list words" (`web_search_block_words`) (`web_search_allow_words`) were added so you can extend them yourself.


**Messages and sending**

- Session allow-list (`whitelist_ids`): once QQ numbers / QQ group numbers are filled in, the bot only responds to sessions in the allow-list; leaving it empty responds to everything.

**Repeat guard**

- The repeat-guard thresholds are adjustable: the overlap thresholds against the character's own history and against the user's original wording (`repeat_guard_self_threshold`, `repeat_guard_user_threshold`) can be tuned in the WebUI.

**Stickers**

- Four sending modes (`sticker_send_mode`): off / random / by emotion / by description; with "by description", the descriptions of the candidate stickers are handed to the model, which picks the one that fits the current line best.

- Stickers kept from image captioning are named after the reason the model gives (duplicate names get a numeric suffix automatically), and the file extension follows the image's real format.


**WebUI**

- Log compact mode supports custom hidden fragments (`webui_log_hide_patterns`): noise logs such as automatic collection and TTS parameters are hidden by substring; ticking "Show Full Log" prints everything as usual.



#### 🛠 Fixed

**Other**

- On a fresh install, the whole NapCat connection group was hidden and could not be edited in the WebUI.

- After setting a WebUI password, exporting the configuration and chat history reported "Password required" in the browser.

- Switching away from a WebUI page and back jumped to the top of the page: each page now remembers its own scroll position, and the configuration page restores it once the form has finished refreshing.

- Opening a chat detail now jumps to the newest message; conversations more than 5 minutes apart show a time separator (today shows only hours and minutes, yesterday / the day before carry a relative date, and anything older shows the full date).

- Uninstalling the program did not clean up runtime logs and temporary files: the uninstaller now asks whether to delete config.json and data (chat history and so on), the silent uninstall path used by an in-place upgrade leaves user data alone, and a normal install now goes to a per-user directory instead of writing into Program Files.

- Installing the program into a read-only directory such as Program Files crashed it on startup (log / configuration writes were refused): runtime files are now written to the user directory (`%LOCALAPPDATA%\Lovomo`) automatically and existing configuration is migrated along; printing special symbols in a GBK code page terminal no longer aborts the program.

- The export button did nothing in the desktop window (WebView2 blocks page downloads by default): exporting the configuration and chat history now goes through the system "Save as" dialog, while browser access keeps downloading directly.

- Exported configuration files wrote out decrypted plaintext keys: exports now always keep the `enc:...` encrypted placeholder form from config.json, and any plaintext key is re-encrypted before export.



#### ⚡ Improved

- The flashing CMD window is hidden when the program exits.

- Proactive message / greeting parameters can be configured with multiple selections.





### v1.2.0.0-beta

#### ✨ Added

**Models and LLM**

- Visual model selection in the WebUI: the LLM / vision model can be found by clicking "Choose Folder" to scan local models, or "Service list" to pull the model IDs from the server directly, so model identifiers no longer have to be copied by hand; path fields such as the TTS model directory, reference audio and sticker directory all support one-click "Browse".

- The old model is unloaded automatically after switching models (`llm_auto_unload_old`, on by default): once the new model's first call succeeds, the old model that is no longer used is unloaded, so several models do not sit in VRAM and exhaust it or slow down replies.

- Text cleanup block list (`text_clean_blocklist`): specified characters / words appearing in an LLM reply are removed before sending, taking effect on voice and text at the same time.

- Search keywords extracted by the LLM (`search_query_llm_extract`): before a prefetch search, the model extracts the keywords from the user's message and drops filler and irrelevant characters; if it fails or times out, rule-based extraction is used instead.

- Emotion rule guidance (`emotion_guide_enabled`, on by default): the prompt states when each emotion applies, so intimate / flirted-with / playing-hard-to-get scenes must prefer "Shy", and "Calm" is only a last resort; custom rules can be appended (`emotion_guide_extra`).

**Messages and sending**

- NapCat send timeout automatic retry (`send_retry`): when QQ occasionally throws "NTEvent Timeout" and a whole sentence is lost, it is resent once.

- When voice / text are sent separately, the text is force-split on `。？！.!?` and each sentence requests TTS on its own, so text and voice correspond one to one.

- Reply judging and mood are isolated per "session + user + character": if someone upsets the character in a group, only the willingness to reply to that person drops.

- The @-target's context is injected in group chats, reducing the LLM's confusion about who was @-mentioned.

- User profiles support replacing / deleting old entries per conversation (likes_remove, clear_* and so on), so stale information does not linger.

- To-do reminder wording can be LLM-generated or a preset template (`todo_remind_mode`), falling back to the template automatically when the model fails so no reminder is ever lost; the reminder voice can use its own emotion (`todo_voice_emotion`).

**Voice and repeat guard**

- The vision model can have its own endpoint address (`image_caption_base_url`, empty follows the LLM service address), so a cloud vision model and a local chat model can point at different services.

- The repeat guard was split into four independent switches (all on by default): by "when to check" there is "block before the first streaming sentence is sent" (`repeat_guard_streaming_check`) and "validate and regenerate after the whole reply is generated" (`repeat_guard_regen_check`); by "what to compare against" there is "compare with the character's historical replies" (`repeat_guard_compare_self`) and "compare with the user's original wording" (`repeat_guard_compare_user`). They can be combined freely.

- An upper-bound guard on synthesis duration (`tts_max_seconds_per_char`, 0.6 seconds per character by default): audio clearly exceeding the line's magnitude is judged abnormal and retried automatically.

**System and WebUI**

- API keys are now genuinely encrypted: the key is bound to this machine's hardware to derive the encryption key (HMAC-SHA256 stream encryption + integrity check), so config.json copied to another computer cannot decrypt the keys; the WebUI and exported files only show the first and last 4 characters plus the real length (for example `sk-a****6789 (18 chars)`).

- Tray residence: closing the window minimises to the tray, whose right-click menu offers "Open Lovomo / Restart / Exit".

- Password protection for chat history access (`webui_password`).

- Flood-control rate limiting (`anti_spam_*`): too many messages in one session in a short window are ignored automatically, so they do not occupy the LLM.

- Updates are checked from GitHub periodically, and a new version is announced in an in-app dialog (`update_check_*`).

- TTS troubleshooting log (`tts_debug_log`): prints the line, emotion, reference audio and other synthesis details; the character replacement table can be customised in the WebUI (`tts_char_map`).

- The program name and current version are shown in the top-left corner of the WebUI; the sidebar menu moved down as a whole, separating it more clearly from the title area.

- The program was renamed to Lovomo, and the database file was renamed to `lovomo.db` along with it (on first launch the old `ltvm.db` is picked up automatically, so statistics and to-dos are not lost).



#### 🛠 Fixed

**Replies and emotions**

- A shy conversation used the calm voice: when the model output an emotion name with Chinese / English / variants (such as 「害羞」, 「shy」 or 「haixiu。」), it was rejected by strict matching — aliases are now resolved automatically, and only fall back to the default voice when nothing can be resolved.

- Proactive messages read the model's reasoning out loud — reasoning content is now stripped at all four layers: request, parsing, streaming and sending.

- The LLM mistook its own reply or the speaker label for message content, and described a sticker the user sent as "its own" — label normalisation + image identity rules (`image_identity_guard_enabled`).



**Tools and search**

- Safe search let adult content through: only a handful of keywords were filtered, so adult aggregator / gossip / hentai sites (site names and domains) could slip past — site names, domains and the adult term list are now complete, and filtering works per level.

- Sending an image and asking a subjective question triggered a web search: subjective evaluation questions no longer trigger a search (the prompt forbids it explicitly + a model-initiated search of that kind is blocked outright), and it also applies when the tool decision runs in LLM mode.

- Search intent detection was changed to ignore the word "search" inside negated / sceptical contexts.

- The LLM called tools when it did not need to — a message with no trigger word no longer enters the tool flow, and the trigger words are configurable in the WebUI.

- Calling the search / web tool returned `400 Bad Request`: the tool_calls message shape sent to the service was invalid — it is now normalised before sending, and the error carries the server's original message.

- Inaccurate search keywords: a whole spoken sentence was used as the search term — negated clauses and request phrases are dropped, leaving only the search target.

- When a reply was rejected for being too similar and regenerated, the same search was run again (a pointless 30-second wait or more) — prefetched search results are now cached by keyword and reused directly.

**TTS and voice**

- The voice was "one sentence / one word short" compared with the text: the cleanup allow-list silently deleted symbols, synthesis parameters were passed the wrong way round, and a mismatch in the number of Chinese / Japanese sentences degraded into synthesising the whole paragraph at once — cleanup now only performs equivalent replacements and logs them, parameters are self-healed, and a sentence-count mismatch is merged by character weight, so text and voice correspond segment by segment.

- Text was sent item by item while the voice stuck together as one piece (short sentences were merged back under separate_send).

- Printing symbols such as ♪ in a Chinese Windows environment threw UnicodeEncodeError and interrupted synthesis.

- A line's tail dragged on forever: the synthesis engine occasionally repeated the same syllable endlessly (a line of 30-odd characters dragging into 27 seconds of 「に——」 or 「呜——」). Long drag markers (`——` / `ー`) are now compressed to 2 before being sent to synthesis, and audio clearly exceeding the line's magnitude is discarded and retried — better to send text only for that sentence than to send noise.

- The next queued message waited for the previous voice clip to finish playing before reaching the LLM: the send-side "wait by audio duration" used to sleep even after the last voice clip, and did so inside the session lock. It now waits for the previous clip to finish only before sending the next one, the trailing wait is removed entirely, and each wait is taken strictly from the duration of the voice clip that was just sent.

- The model "Service list" returned `400 Bad Request` and the address had never been filled in: the service address was unconditionally appended with `/v1/models`. An address that already carries an endpoint suffix is now used as is, listing models tries the candidate addresses one by one, the error lists the addresses actually requested together with the upstream's reason, and it points out that the service root address is what should be filled in.

**Proactive messages**

- Nothing was sent for a long time even outside quiet hours: the scheduled time was recomputed every time, the idle timer was lost on restart, and the daily counter was never persisted — the schedule is now fixed once, the idle timer is restored from the last message, the state is persisted to `data/proactive_state.json`, and diagnostic logging was added.

- Deleted sessions kept receiving proactive messages: after deleting a conversation, the idle timer and daily counter stayed in memory and fired on schedule. Sending now checks that the session memory still exists, and deleting a conversation clears that session's proactive state at the same time.

**Stickers**

- Meaningless images were kept by mistake, GIFs were compressed into PNGs, and kept stickers could not be sent — keeping now requires an explicit judgement and a concrete reason, GIF/WebP bytes are preserved as is, and the shared pool is synchronised automatically (`sticker_capture_any_pool`).

- Automatic collection from image captioning "did nothing at all": the judgement passed but nothing was written, and every skip path returned silently, so the log showed no reason. Collection now reuses the original image bytes already read during captioning (no second download, so a dead QQ image short link no longer fails it), and every kind of skip — minimum interval / daily cap / a switch being off / an unusable image source — states its reason.

**Other**

- RAG misjudged "embedding model not configured" on non-Ollama backends.

**Stability and data safety**

- Session memory / config.json / mood / profiles / events / scheduled tasks and all the other JSON files are now written atomically, so killing the process no longer produces half-written files; a corrupt file is backed up as `.corrupt` instead of being silently zeroed.

- Fixed a concurrency race where the post-conversation summary task wrote an old session snapshot back and overwrote the freshly saved chat history; the user message is now persisted properly even when the decision is not to reply or generation fails.

- A corrupt config.json is backed up first (`.corrupt`) and then rebuilt, instead of being overwritten directly with the default configuration.

- Sticker upload gained an image format allow-list and a 10 MB size limit, responses carry nosniff, and session cookies are httponly, closing a stored-XSS hole.

- Exported configuration files no longer contain plaintext API keys (keys are exported as the `enc:...` encrypted placeholder and restored automatically on re-import); side paths that write to disk, such as saving characters, no longer write keys in plaintext.

- When a streaming reply was truncated, the rescued tail sentence is now sent properly instead of only being written into the history.

- A dead image source in an image-caption request no longer misaligns the following image URLs and contents.

- The per-reply tool-call quota is now isolated per reply, so multiple groups running concurrently no longer reset or consume each other's quota; a parameter parsing failure no longer consumes an attempt.

- web_fetch fixed broken redirect validation (a 302 could bypass the internal-network guard); DNS validation was moved out of the event loop so it no longer stalls everything; custom command tool output is decoded with UTF-8 tolerance; a custom HTTP tool returns a clear error on a redirect instead of an empty result.

- A failed to-do reminder keeps its pending state instead of being marked complete; a failed holiday greeting generation no longer marks the greeting as sent.

- Scheduled tasks gained overlap protection: when a callback is slower than the interval, the current round is skipped instead of piling up concurrent runs.

- The statistics panel's "today" and the daily charts now share a local-midnight bucket boundary, so 0-8 o'clock no longer counts as yesterday.

- Fixed speed / pitch changes and merge failures caused by mismatched sample rates or channels when merging voice, temporary file leaks on synthesis failure, concurrent messages each waiting 60 seconds when TTS fails to start, and a startup crash when a path configuration was a single character.

- "Export all chat history" no longer packs `webui_auth.json` (which holds the WebUI token); importing, exporting and deleting chat history only touch session memory files, never the feature data in the same directory.

- Write endpoints gained cross-site request blocking (an Origin/Referer inconsistent with the Host is refused), so a third-party page in the browser can no longer use the cookie to operate the local console directly; command-line / script access is unaffected.

- A single abnormal configuration field (such as `ref_audio_root` being `null`) no longer marks the whole config.json as corrupt and resets it; a failed API key decryption keeps the original ciphertext instead of writing an empty string.



#### ⚡ Improved

- Connection-type failures (unreachable LLM / embedding service) report the target address and troubleshooting advice, and are probed automatically on startup.

- The WebUI "Save configuration" merges with the default configuration, so fields the interface did not render are no longer dropped.

- The sticker keep decision rides along with the vision-caption call (zero extra requests); mood statistics can expand several sessions' records; tool testing supports custom JSON parameters; the JSON editor area can be resized.

- Deleting a session also clears the corresponding character's mood records.

- Proactive messages: fixed idle openers, festival greetings, birthday wishes and scheduled messages still being generated with the main configuration when the connection is bound to a config profile.

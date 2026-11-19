(() => {
    'use strict';
    const storageKey = 'lovomo.guide.v2';
    const entry = '#guide-reopen';
    const art = '/webui/guide/guide.webp';
    const step = (selector, zh, en, action = 'read') => ({selector, zh, en, action});
    const modelForm = key => `#app-dialog.active [data-dlg-key="${key}"]`;
    const connection = key => `#connection-modal.active #conn-${key}`;
    const main = [
        {panel: 'models', title: ['配置模型服务', 'Set up a model'],
            intro: ['先建立一个聊天模型服务，之后可在接入方式里为通道指定模型。已有可用服务可以跳过这项。', 'Add a chat model service. You can select it for a channel in Connections. Skip this part if you already have a working service.'],
            open: ['打开模型服务', 'Open Model Services'], steps: [
                step('#model-add-btn', '点击页面上的新建服务，打开服务表单。', 'Click Add Service on the page to open the form.', 'model-form'),
                step(modelForm('kind'), '用途决定服务出现在哪些选择器里。第一次聊天请选择「聊天模型」，其他用途可另建服务。', 'The purpose controls where the service appears. Choose Chat Model for your first conversation.', 'change'),
                step(modelForm('name'), '起一个便于辨认的名称。它只在 Lovomo 内显示。', 'Give the service a name you will recognize in Lovomo.'),
                step(modelForm('base_url'), '粘贴服务商提供的接口地址。本地模型请填正在运行的服务地址。', 'Enter the provider’s API address or the URL of your running local service.'),
                step(modelForm('model'), '填服务商给出的模型 ID，大小写和版本号也要一致。', 'Enter the exact model ID, including case and version.'),
                step('#app-dialog.active #app-dialog-ok', '点击原表单里的创建按钮。新卡片会先留在草稿中。', 'Click Create in the form. The new card is a draft until saved.', 'model-created'),
                step('@model [data-pkey="backend"], @model [data-pkey="protocol"], @model [data-pkey="api_style"]', '选择匹配接口的类型。云端兼容服务通常选 OpenAI，本地 Ollama 选 Ollama。', 'Select the protocol used by your service, such as OpenAI-compatible or Ollama.'),
                step('@model [data-pkey="api_key"]', '需要认证的服务在这里填写密钥。本地服务不需要密钥时可留空。', 'Enter an API key if the service requires one. Leave it blank for local services without authentication.'),
                step('@model .model-fetch-btn, @model [data-pkey="model"]', '核对模型名。支持列表的服务可点「拉取模型」后选择，不支持时直接填模型 ID。', 'Check the model ID. Use Fetch Models when supported, or enter the ID manually.'),
                step('#models-save-btn', '点击页面上的保存全部。保存成功后会自动弹出下一项。', 'Click Save All on the page. The next section opens once saving succeeds.', 'models-saved'),
            ]},
        {panel: 'test-chat', title: ['完成第一次试聊', 'Try your first message'],
            intro: ['用独立测试会话检查角色和模型配置。', 'Check your character and model in an isolated test conversation.'],
            open: ['打开聊天测试', 'Open Chat Console'], steps: [
                step('#test-char-select', '选择要试聊的角色。这里的消息留在独立测试会话。', 'Select a character. Messages here stay in a separate test conversation.', 'change'),
                step('#test-model-select', '按角色自动匹配接入方式，也可以手动选择接入方式或聊天模型。', 'Use the connection matched to the character, or choose a connection or chat model manually.', 'change'),
                step('#test-input', '输入一句「你好」，或写一句你想拿来测试角色的话。', 'Type “Hello” or another message to test the character.', 'input'),
                step('#test-send-btn', '点击原位置的发送按钮。收到回复后自动进入语音引导。', 'Click Send on the page. The voice section opens after a reply arrives.', 'chat-sent'),
            ]},
        {panel: 'system', title: ['试听角色语音', 'Preview the voice'],
            intro: ['语音是可选的。配置好本地或云端语音服务后，可以试听当前角色的音色。', 'Voice is optional. Once a TTS service is configured, preview the active character voice.'],
            open: ['打开语音试听', 'Open Voice Preview'], steps: [
                step('#system-tts-model', '选择合成模型。使用本地服务时，可以在旁边另选参考音色。', 'Choose a synthesis model. For local services with several reference voices, pick a voice next to it.'),
                step('#system-tts-language', '选择要合成的台词语言，不会改变模型或参考音色。', 'Choose the spoken language. This does not change the model or reference voice.', 'change'),
                step('#system-tts-text', '可以输入要试听的文字；留空则使用所选语言的默认台词。', 'Enter your own preview text, or leave it blank to use the default line for the selected language.'),
                step('#system-tts-test', '点击合成并试听。播放控件出现后可重播，也可以继续引导。', 'Click Synthesize and Preview. You can replay the result when the player appears.', 'voice-ready'),
                step('#system-tts-audio', '用这个播放器听听音色和读音，不满意时可以换一种语言再试听。', 'Use this player to check the voice and pronunciation, or preview another language.'),
            ]},
        {panel: 'connections', title: ['接入 QQ 或微信', 'Connect QQ or WeChat'],
            intro: ['选择平台、填写绑定信息，再给通道选角色和模型。只在 Lovomo 内试聊可以跳过。', 'Choose a platform, set up its credentials, then select a character and model for the channel. Skip if you only chat in Lovomo.'],
            open: ['打开接入方式', 'Open Connections'], steps: [
                step('#connection-add-btn', '点击页面上的新建接入方式，打开通道表单。', 'Click Add Connection on the page to open the channel form.', 'connection-form'),
                step(connection('platform'), '选择 NapCat、QQ 官方机器人或微信。后面的绑定字段会随平台变化。', 'Choose NapCat, QQ Official Bot or WeChat. Binding fields depend on the platform.', 'change'),
                step(connection('name'), '给通道起个名称，方便之后辨认不同账号。', 'Give the channel a name so you can tell accounts apart.'),
                step(connection('ws-url'), 'NapCat 请填写 WebSocket 地址。设置了访问令牌时，也填入下面的令牌栏。', 'For NapCat, enter the WebSocket URL and the access token if configured.'),
                step(connection('app-id'), 'QQ 官方机器人请填开放平台的 App ID 和下方密钥，之后在平台侧检查权限。', 'For QQ Official Bot, enter the App ID and secret from the developer console and check its permissions.'),
                step(connection('qr-box'), '微信请用手机扫描二维码，等待登录状态变为成功。二维码过期可以刷新。', 'For WeChat, scan the QR code and wait for confirmation. Refresh it when it expires.'),
                step(connection('profile'), '选择通道使用的配置文件，可按通道区分回复与语音设置。', 'Choose the configuration profile for this channel’s reply and voice behavior.'),
                step(connection('role'), '选择接入后负责回复的角色。', 'Select which character will reply on this channel.'),
                step(connection('model-grid'), '为这条通道指定聊天模型。其他用途可以沿用默认设置。', 'Select the chat model for this channel. Other purposes can use the defaults.'),
                step(connection('tts-backend'), '需要让这个通道单独用本地或云端语音时，在这里切换；否则保持跟随配置文件。', 'Override voice synthesis for this channel only when needed. Otherwise, keep the configuration profile setting.'),
                step(connection('tts-path'), '用本地语音时，可选择这个通道专用的模型目录。留空则沿用默认目录。', 'With local voice synthesis, optionally choose a model folder for this channel. Leave it blank to use the default.'),
                step(connection('save-btn'), '检查设置后点击原表单里的保存按钮。保存成功会完成引导。', 'Check the settings and click Save in the form to finish.', 'connection-saved'),
            ]},
    ];

    const topics = {
        config: [
            step('#anchor-nav .anchor-item:not(.anchor-entry)', '左侧浮窗可快速跳到设置分组。点击高亮分组试试，或继续查看搜索。', 'The floating menu jumps to settings groups. Try the highlighted group or move on to search.', 'click'),
            step('#config-search', '试搜「不回消息」或「语音太慢」。选中搜索结果会定位到对应设置。', 'Search for symptoms like “no reply” or “voice too slow.” Select a result to jump to its setting.'),
            step('#save-only-btn', '保存前会列出改动。普通设置用「仅保存」，需要重启语音时用旁边的选项。可用上方的「撤销上次设置保存」恢复。', 'Review the list of changes before saving. Use Save Only for ordinary settings, and Undo Last Settings Save to revert.'),
            step('#anchor-nav .anchor-entry', '点击浮窗里的「配置文件」打开预设管理，或关闭提示后再回来查看。', 'Click Configuration Profiles in the floating menu to open the manager, or return to it later.'),
        ],
        profiles: [
            step('#profile-editing', '先确认「正在编辑」的是哪个配置文件。保存只影响所选文件。', 'Check which profile you’re editing. Saving affects only that profile.', 'wait'),
            step('#profile-select', '从列表选择配置文件。右下角的「切换预设」也能打开文件列表。', 'Select a profile here or use Switch Profile in the floating actions.'),
            step('#profile-name', '这里用于输入新建或重命名的名称。删除前先看下面的使用情况。', 'Enter a name when creating or renaming a profile. Check its usage before deleting it.'),
            step('#profile-save-btn', '检查文件名后保存。要使用该文件，可去接入方式为通道选用它。', 'Check the profile name before saving. Assign the profile to a channel in Connections.'),
        ],
        models: [step('#models-kind-filter', '按用途筛选服务。新建、修改、删除都先进入草稿，保存全部才会生效。', 'Filter by purpose. Changes remain drafts until you select Save All.')],
        connections: [step('#connection-add-btn', '每条通道可以绑定自己的账号、角色、配置文件和模型。已有通道可在卡片里编辑和自检。', 'Each channel can have its own account, character, profile and model. Edit or test existing channels in their cards.')],
        roles: [step('#roles-list', '展开角色卡编辑人设、音色。卡片里的「隔离试聊」可用同一句对比保存版和草稿版。', 'Expand a character to edit its persona and voice. Isolated Chat compares the saved and draft versions with the same message.')],
        chat: [
            step('#chat-search', '按角色名或会话编号查找聊天记录。', 'Search chat history by character or conversation ID.'),
            step('#panel-chat .file-item, #sort-select', '打开一段会话，再点「角色回忆」查看来源、修改、置顶或忘掉记忆。', 'Open a conversation, then use Character Memories to review sources, edit, pin or forget a memory.'),
        ],
        'test-chat': [step('#test-char-select', '先选角色，再发送测试消息。要对比未保存人设，可以在下方的隔离对比区域试聊。', 'Select a character before sending a test message. Use the isolated comparison below to try an unsaved persona.')],
        system: [
            step('#system-activity-summary', '在这里检查是否收到消息、是否生成了回复，以及平台是否确认。等待补发和结果未知会分别标出。', 'Check whether a message arrived, a reply was generated, and the platform acknowledged delivery. Queued retries and unknown results are shown separately.'),
            step('#system-tts-model', '可切换试听的合成模型和参考音色，输入台词或使用当前语言的默认台词。', 'Choose a synthesis model and reference voice. Enter your own text or use the default line in the selected language.'),
            step('#system-backup-export', '导出备份前确认保存位置。恢复时先预览受影响文件，再确认并安排恢复。', 'Choose where to save your backup. When restoring, preview affected files before scheduling the restore.'),
        ],
        audio: [step('#audio-role-select', '按角色管理情绪音频和参考音频。先选角色与情绪，再上传或试听。', 'Manage emotion clips and reference audio by character. Choose a character and emotion before uploading or previewing.')],
        tasks: [step('#tasks-summary', '查看启用中的定时任务。配置触发时间和对应角色后，再启用任务。', 'Review active scheduled tasks. Set the schedule and character before enabling a task.')],
        stats: [
            step('#stats-range', '用时间区间筛选对话统计。', 'Filter conversation statistics by time range.'),
            step('#perf-grid', '看首条文字和首段语音的等待时间，再对照排队、生成、合成和平台确认耗时，找出慢在哪一段。', 'Compare time to first text and voice with queue, generation, synthesis and acknowledgment times to find the bottleneck.'),
        ],
        plugins: [step('#plugin-market-search', '按名称或标签搜索插件，进入详情查看用途和使用说明。安装后在「插件」页管理。', 'Search by name or tag and open a plugin for instructions. Manage installed plugins under Plugins.')],
        'installed-plugins': [step('#panel-installed-plugins .section-block h3, #plugin-list-body', '在这里启用、配置或移除已安装插件。改动后记得应用。', 'Enable, configure or remove installed plugins here. Apply changes when you finish.')],
        'plugin-page': [step('#plugin-page-body h3, #plugin-apply-btn:not([disabled]), #panel-plugin-page h2', '先核对插件说明和权限，可在这里修改设置，最后保存并重载。', 'Review the plugin instructions and permissions. Edit its settings here, then save and reload.')],
        advanced: [step('#cap-grid .cap-card', '这里汇集知识库、回忆与其他独立工具。按需要打开对应功能。', 'Find the knowledge base, memory and other tools here. Open the feature you need.')],
        logs: [step('#log-full-toggle', '日常排障先看诊断时间线。要查看更完整的运行细节，可开启完整日志。', 'Start with the diagnostics timeline. Enable full logs when you need more runtime detail.')],
        tutorial: [step('#panel-tutorial h3', '需要核对字段和配置示例时，可回来查看使用教程。', 'Return to the tutorial for field descriptions and configuration examples.')],
        'app-releases': [step('#plugin-releases-refresh', '这里可查看程序历史版本。下载不等于安装，切换版本前建议先备份。', 'Browse previous versions here. Downloading does not install a version; back up before switching.')],
    };

    // Follow actual nested view visibility, including modal closes by Escape.
    const contextual = {
        'recall-editor': step('#chat-modal.active #chat-recall-panel:not([hidden]) > p', '可查看回忆的来源时间，也可修改、置顶或忘掉片段。忘掉只移除回忆，不删除聊天记录。', 'Review source times and edit, pin or forget memories. Forgetting removes the memory, not the chat history.'),
        'plugin-doc': step('#plugin-doc-modal.active #plugin-doc-body', '先看插件用途、使用方式和所需权限，再决定是否安装或启用。', 'Review the plugin’s purpose, instructions and permissions before installing or enabling it.'),
        'draft-compare': step('#panel-test-chat.active #test-draft[open] #test-draft-persona', '改好草稿后，输入一句话对比两个版本。满意后可带到角色管理，仍需手动保存。', 'Edit the draft and use one message to compare both versions. You can take the draft to Character Management, where it must still be saved manually.'),
        'advanced-stickers': step('#panel-advanced.active .adv-visible #sticker-category', '按情绪或用途给表情分组，再上传图片。自动识别后也可手动调整。', 'Group stickers by emotion or use, then upload them. You can adjust automatic labels afterward.'),
        'advanced-tools': step('#panel-advanced.active .adv-visible #search-engine-select', '选择搜索服务；下方可以管理和测试角色可用的工具。只启用你了解且信任的工具。', 'Choose a search provider and manage or test tools below. Only enable tools you know and trust.'),
        'advanced-rag': step('#panel-advanced.active .adv-visible #rag-query', '上传资料后用这里测试检索。先到模型服务配好嵌入模型，再启用知识库。', 'Test document retrieval here after uploading. Configure an embedding model before enabling the knowledge base.'),
        'advanced-profiles': step('#panel-advanced.active .adv-visible #add-profile-btn', '可为联系人记录偏好或生日。这里是联系人画像，不是通道配置文件。', 'Record a contact’s preferences or birthday. These are contact profiles, not channel configuration files.'),
        'advanced-learning': step('#panel-advanced.active .adv-visible #terms-role', '先选角色，再核对它的待确认词条，或手动添加词条。不同角色各用自己的词典。', 'Select a character to review pending terms or add your own. Each character has a separate vocabulary.'),
    };

    const text = (zh, en) => document.documentElement.lang === 'en' ? en : zh;
    const copy = pair => text(...pair);
    let state = {status: 'new', done: [], topics: []};
    let authorized = false;
    let navigate;
    let currentPanel;
    let course;
    let target;
    let modelCard = '.role-item:last-child';
    let frame = 0;
    let scrollTarget;
    let bannerKind;
    let openingChapter;
    let opener;
    let ui;

    function persist() {
        try { localStorage.setItem(storageKey, JSON.stringify(state)); } catch { /* Keep progress for this session. */ }
    }

    function readProgress() {
        try {
            const saved = JSON.parse(localStorage.getItem(storageKey));
            if (saved && ['new', 'active', 'skipped', 'complete'].includes(saved.status)) {
                state = {status: saved.status, done: Array.isArray(saved.done) ? saved.done : [],
                    topics: Array.isArray(saved.topics) ? saved.topics : []};
            }
        } catch { /* Use the default progress. */ }
    }

    function pending() {
        return main.find(item => !state.done.includes(item.panel));
    }

    function syncBannerVisibility() {
        ui.banner.hidden = !bannerKind || bannerKind !== state.status || !authorized || ui.dialog.open || !ui.tip.hidden;
    }

    function showBanner(kind) {
        bannerKind = kind;
        syncBannerVisibility();
        if (!kind) return;
        ui.notice.textContent = kind === 'skipped'
            ? text('已跳过。可在「配置文件 → 重新查看新手引导」重开。', 'Tour skipped. Reopen it under Configuration → Restart Quick Start.')
            : text('引导尚未完成。可在「配置文件 → 重新查看新手引导」继续。', 'Tour unfinished. Resume it under Configuration → Restart Quick Start.');
        ui.resume.textContent = kind === 'skipped' ? text('重新打开引导', 'Reopen Tour') : text('继续引导', 'Resume Tour');
        ui.bannerSkip.hidden = kind !== 'active';
        ui.bannerSkip.textContent = skipLabel();
        ui.dismiss.textContent = text('关闭提示', 'Dismiss reminder');
    }

    function hideTip() {
        course = null;
        target = null;
        scrollTarget = null;
        ui.tip.hidden = true;
        ui.spot.hidden = true;
        syncBannerVisibility();
    }

    function revertFocus() {
        if (opener?.getClientRects().length) opener.focus({preventScroll: true});
    }

    const skipLabel = () => text('跳过全部引导', 'Skip All Guides');

    function skipTour() {
        openingChapter = null;
        state.status = 'skipped';
        hideTip();
        if (ui.dialog.open) ui.dialog.close();
        persist();
        showBanner('skipped');
        revertFocus();
    }

    function showOverview(welcome = false) {
        if (!authorized) return;
        showBanner(null);
        hideTip();
        const item = pending();
        if (!item) return;
        ui.dialog.dataset.welcome = String(welcome);
        ui.progress.textContent = welcome ? text('新手引导', 'Quick Start') : `${main.indexOf(item) + 1} / ${main.length}`;
        ui.title.textContent = welcome ? text('你好，我是 Lovomo！', 'Hi, I’m Lovomo!') : copy(item.title);
        ui.intro.textContent = welcome
            ? text('从设置模型到发出第一条测试消息。语音和平台接入可按需设置。', 'Set up a model and send your first test message. Voice and platform setup are optional.')
            : copy(item.intro);
        ui.begin.textContent = welcome ? text('开始引导', 'Start Tour') : copy(item.open);
        ui.skip.textContent = skipLabel();
        ui.skipChapter.hidden = welcome;
        ui.browse.hidden = !welcome || state.topics.includes(currentPanel) || !topics[currentPanel];
        ui.browse.textContent = text('先看看当前页', 'Explore This Page');
        ui.skipChapter.textContent = text('跳过此项', 'Skip This Part');
        if (!ui.dialog.open) ui.dialog.showModal();
        ui.begin.focus();
    }

    function nextChapter() {
        const item = pending();
        if (item) state.done.push(item.panel);
        state.status = pending() ? 'active' : 'complete';
        persist();
        if (pending()) showOverview();
        else {
            hideTip();
            if (ui.dialog.open) ui.dialog.close();
            revertFocus();
        }
    }

    function startOrResume() {
        openingChapter = null;
        opener = document.activeElement;
        const welcome = state.status === 'new' || state.status === 'complete' || state.done.length === 0 && state.status === 'skipped';
        if (state.status === 'complete') state = {status: 'new', done: [], topics: []};
        state.status = 'active';
        persist();
        showOverview(welcome);
    }

    async function openChapter() {
        const item = pending();
        const attempt = openingChapter = {};
        ui.dialog.close();
        try {
            await navigate(item.panel);
            if (openingChapter !== attempt || !authorized || state.status !== 'active') return;
            if (currentPanel !== item.panel) {
                showBanner('active');
                return;
            }
            course = {type: 'main', item, index: 0};
            renderTip(true);
        } finally {
            if (openingChapter === attempt) openingChapter = null;
        }
    }

    function resolve(selector) {
        const expanded = selector.replaceAll('@model', `#models-list ${modelCard}`);
        return Array.from(document.querySelectorAll(expanded)).find(el =>
            el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
    }

    function activeStep() {
        return course?.item.steps[course.index];
    }

    function advance() {
        if (!course) return;
        course.index++;
        renderTip(true);
    }

    function finishTopic() {
        if (course?.type !== 'topic') return;
        state.topics.push(course.item.panel);
        persist();
        hideTip();
    }

    function contextualTip() {
        if (!authorized || state.status === 'skipped' || ui.dialog.open || openingChapter || course?.type === 'main') return;
        if (course?.contextual && !resolve(activeStep().selector)) hideTip();
        if (course?.contextual) return;
        for (const [panel, part] of Object.entries(contextual)) {
            if (state.topics.includes(panel) || !resolve(part.selector)) continue;
            hideTip();
            course = {type: 'topic', contextual: true, item: {panel, steps: [part]}, index: 0};
            renderTip(true);
            return;
        }
    }

    function renderTip(scroll = false) {
        if (!authorized || state.status === 'skipped' || !course) return;
        let part = activeStep();
        while (part && !resolve(part.selector) && part.action === 'read') {
            course.index++;
            part = activeStep();
        }
        if (!part) {
            if (course.type === 'main') nextChapter();
            else finishTopic();
            return;
        }
        target = resolve(part.selector);
        ui.tip.hidden = false;
        syncBannerVisibility();
        ui.tipTitle.textContent = course.type === 'main' ? copy(course.item.title) : text('页面提示', 'Page Tip');
        ui.tipText.textContent = copy([part.zh, part.en]);
        ui.tipNext.hidden = !['read', 'wait', 'change', 'click'].includes(part.action);
        if (part.action === 'input' && target?.value.trim()) ui.tipNext.hidden = false;
        ui.tipNext.textContent = course.index === course.item.steps.length - 1 ? text('知道了', 'Got It') : text('继续', 'Continue');
        ui.tipSkipPart.hidden = course.type !== 'main';
        ui.tipSkipPart.textContent = text('跳过此项', 'Skip This Part');
        ui.tipClose.hidden = course.type !== 'topic';
        ui.tipClose.textContent = text('关闭本页提示', 'Dismiss Tips');
        ui.tipSkipAll.textContent = skipLabel();
        if (target && scroll && scrollTarget !== target) {
            scrollTarget = target;
            const rect = target.getBoundingClientRect();
            if (rect.top < 12 || rect.bottom > innerHeight - 12) target.scrollIntoView({block: 'center', inline: 'nearest'});
        }
        position();
    }

    function position() {
        frame = 0;
        if (!course || ui.tip.hidden) return;
        target = resolve(activeStep().selector);
        const r = target?.getBoundingClientRect();
        const left = Math.max(4, (r?.left ?? 0) - 4);
        const right = Math.min(innerWidth - 4, (r?.right ?? 0) + 4);
        const top = Math.max(4, (r?.top ?? 0) - 4);
        const bottom = Math.min(innerHeight - 4, (r?.bottom ?? 0) + 4);
        const visible = !!r && right > left && bottom > top;
        ui.spot.hidden = !visible;
        if (visible) Object.assign(ui.spot.style, {left: `${left}px`, top: `${top}px`,
            width: `${right - left}px`, height: `${bottom - top}px`});
        const margin = 12;
        const gap = 10;
        const w = ui.tip.offsetWidth;
        const h = ui.tip.offsetHeight;
        const clamp = (n, min, max) => Math.min(Math.max(n, min), Math.max(min, max));
        let x = innerWidth - w - margin;
        let y = innerHeight - h - margin;
        if (visible) {
            const sideY = clamp(top, margin, innerHeight - h - margin);
            if (right + gap + w <= innerWidth - margin) [x, y] = [right + gap, sideY];
            else if (left - gap - w >= margin) [x, y] = [left - gap - w, sideY];
            else {
                x = clamp(left, margin, innerWidth - w - margin);
                if (top - gap - h >= margin) y = top - gap - h;
                else if (bottom + gap + h <= innerHeight - margin) y = bottom + gap;
                else y = top >= innerHeight - bottom ? margin : innerHeight - h - margin;
            }
        }
        Object.assign(ui.tip.style, {left: `${x}px`, top: `${y}px`});
    }

    function schedulePosition() {
        if (course && !frame) frame = requestAnimationFrame(position);
    }

    function panelChanged(panel) {
        if (!ui) return;
        currentPanel = panel;
        if (!authorized || ui.dialog.open || openingChapter) return;
        if (course?.item.panel === panel) return;
        if (course?.type === 'main') showBanner('active');
        if (course?.item.panel === 'config' && panel === 'profiles') finishTopic();
        hideTip();
        if (state.status !== 'skipped' && topics[panel] && !state.topics.includes(panel)) {
            course = {type: 'topic', item: {panel, steps: topics[panel]}, index: 0};
            // Most panels populate controls after their loader resolves. Retry on the real ready hook.
            panelReady(panel);
        }
    }

    function panelReady(panel) {
        if (course?.type !== 'topic' || currentPanel !== panel || course.item.panel !== panel || !ui.tip.hidden) return;
        const first = activeStep();
        if (first.action === 'wait' ? resolve(first.selector) : course.item.steps.some(part => resolve(part.selector))) renderTip(false);
    }

    function signal(name, detail) {
        if (course?.type !== 'main') return;
        if (name === 'model-cancelled' && course.item.panel === 'models' && course.index >= 1 && course.index <= 5) {
            course.index = 0;
            renderTip(true);
        } else if (name === 'connection-cancelled' && course.item.panel === 'connections' && course.index > 0) {
            course.index = 0;
            renderTip(true);
        } else if (name === 'model-created' && course.item.panel === 'models') {
            modelCard = `.role-item[data-draft-index="${detail}"]`;
            course.index = 5;
            advance();
        } else if ((name === 'models-saved' && course.item.panel === 'models' && course.index >= 6)
            || (name === 'chat-sent' && course.item.panel === 'test-chat')
            || (name === 'connection-saved' && course.item.panel === 'connections')) {
            nextChapter();
        } else if (name === 'voice-ready' && course.item.panel === 'system') {
            course.index = course.item.steps.findIndex(part => part.action === 'voice-ready');
            advance();
        } else if (name === activeStep()?.action) {
            advance();
        }
    }

    function setAuthorized(value) {
        if (!ui) return;
        authorized = !!value;
        if (!authorized) {
            openingChapter = null;
            hideTip();
            if (ui.dialog.open) ui.dialog.close();
            showBanner(null);
        }
    }

    function init(options) {
        if (ui) return;
        authorized = options.authorized && !document.getElementById('auth-modal').classList.contains('active');
        navigate = options.navigate;
        currentPanel = options.panel;
        readProgress();
        const host = document.createElement('div');
        host.id = 'guide-ui';
        host.setAttribute('data-i18n-skip', '');
        host.innerHTML = `
            <dialog id="lovomo-guide" aria-labelledby="guide-title" aria-describedby="guide-description">
                <div class="guide-layout">
                    <img class="guide-portrait" src="${art}" alt="" width="608" height="1088" draggable="false">
                    <section class="guide-card">
                        <span id="guide-progress" class="guide-progress"></span>
                        <h2 id="guide-title"></h2><p id="guide-description"></p>
                        <div class="guide-actions">
                            <button id="guide-skip" type="button"></button>
                            <button id="guide-browse" type="button"></button>
                            <button id="guide-skip-chapter" type="button"></button>
                            <button id="guide-begin" class="guide-primary" type="button"></button>
                        </div>
                    </section>
                </div>
            </dialog>
            <div class="guide-spot" aria-hidden="true" hidden></div>
            <aside class="guide-tip" aria-label="Guide" hidden>
                <strong class="guide-tip-title"></strong><p class="guide-tip-text" aria-live="polite"></p>
                <div class="guide-actions"><button class="guide-tip-skip-all" type="button"></button>
                    <button class="guide-tip-close" type="button"></button>
                    <button class="guide-tip-skip-part" type="button"></button>
                    <button class="guide-tip-next guide-primary" type="button"></button></div>
            </aside>
            <aside class="guide-reminder" hidden>
                <span class="guide-notice" role="status"></span>
                <button id="guide-resume" class="guide-primary" type="button"></button>
                <button class="guide-banner-skip" type="button"></button>
                <button class="guide-dismiss" type="button"></button>
            </aside>`;
        document.body.append(host);
        const find = selector => host.querySelector(selector);
        ui = {dialog: find('dialog'), progress: find('#guide-progress'), title: find('#guide-title'), intro: find('#guide-description'),
            begin: find('#guide-begin'), skip: find('#guide-skip'), browse: find('#guide-browse'), skipChapter: find('#guide-skip-chapter'),
            spot: find('.guide-spot'), tip: find('.guide-tip'), tipTitle: find('.guide-tip-title'), tipText: find('.guide-tip-text'),
            tipNext: find('.guide-tip-next'), tipSkipPart: find('.guide-tip-skip-part'), tipClose: find('.guide-tip-close'),
            tipSkipAll: find('.guide-tip-skip-all'), bannerSkip: find('.guide-banner-skip'),
            banner: find('.guide-reminder'), notice: find('.guide-notice'), resume: find('#guide-resume'), dismiss: find('.guide-dismiss')};
        ui.begin.onclick = () => {
            if (ui.dialog.dataset.welcome === 'true') showOverview();
            else return openChapter();
        };
        ui.skip.onclick = skipTour;
        ui.browse.onclick = () => {
            ui.dialog.close();
            panelChanged(currentPanel);
            showBanner('active');
        };
        ui.skipChapter.onclick = nextChapter;
        ui.tipNext.onclick = advance;
        ui.tipSkipPart.onclick = nextChapter;
        ui.tipClose.onclick = finishTopic;
        ui.tipSkipAll.onclick = skipTour;
        ui.bannerSkip.onclick = skipTour;
        ui.resume.onclick = startOrResume;
        ui.dismiss.onclick = () => {
            showBanner(null);
            panelChanged(currentPanel);
        };
        document.querySelector(entry).addEventListener('click', startOrResume);
        ui.dialog.addEventListener('cancel', event => { event.preventDefault(); skipTour(); });
        ui.dialog.addEventListener('keydown', event => {
            if (event.key === 'Escape' || event.ctrlKey || event.metaKey) event.stopPropagation();
        });
        for (const type of ['click', 'change', 'input']) document.addEventListener(type, event => {
            if (activeStep()?.action !== type || !target?.contains(event.target)) return;
            if (type === 'input' && !event.target.value.trim()) return;
            advance();
        });
        window.addEventListener('resize', schedulePosition);
        window.addEventListener('scroll', schedulePosition, true);
        for (const type of ['animationend', 'animationcancel', 'transitionend']) {
            document.addEventListener(type, event => {
                if (target && event.target.contains(target)) schedulePosition();
            });
        }
        new MutationObserver(records => {
            if (!records.some(record => !host.contains(record.target))) return;
            panelReady(currentPanel);
            contextualTip();
            schedulePosition();
        }).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'hidden', 'open']});
        new MutationObserver(() => {
            if (ui.dialog.open) showOverview(ui.dialog.dataset.welcome === 'true');
            else if (course && !ui.tip.hidden) renderTip(false);
            if (bannerKind) showBanner(bannerKind);
        }).observe(document.documentElement, {attributes: true, attributeFilter: ['lang']});
        if (!authorized) return;
        if (state.status === 'new') {
            state.status = 'active';
            persist();
            showOverview(true);
        } else if (state.status === 'active') showBanner('active');
        else if (state.status === 'complete') panelChanged(currentPanel);
    }

    window.lovomoGuide = {init, setAuthorized, panelChanged, panelReady, signal};
})();

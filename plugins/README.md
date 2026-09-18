# Lovomo 插件开发与发布指南

插件可以给 Lovomo 加新功能或换皮肤。这份文档覆盖**从写第一个插件到发布上架**的  
完整流程，重点讲清楚三件事：

1. 插件的元信息（名称 / 版本 / 作者 / 仓库 / 图标）**全部写在 `plugin.yaml` 里**；
2. 市场怎么找到你的插件（仓库分支 + `source_repo`）；
3. **归属校验** —— 只有你自己账号名下的插件才能发布、才能装到别人机器上，     
   防止有人把别人的插件挂到自己仓库上盗用。

装好之后，左侧栏有两个入口，职责分开：

- **「插件市场（测试版）」** 用来装插件 —— 上传 `.zip` 插件包、打开插件目录、    
  刷新在线市场、按「最新发布 / 最多下载 / 最多收藏」排序后一键安装。
- **「插件」** 用来管已装插件 —— 列出**全部**已装插件（含未启用的），    
  每张卡上能直接启用/停用、卸载，右上角还能**置顶**（置顶的排在最前面）；
  声明了 `panel` 的插件还多一个「打开功能页」，插件根目录有 `webui.html` 的多一个「打开webui」。

插件如果声明了 `panel`，在「插件」页点卡片空白处（或点「打开功能页」）就能进入  
它自己的功能页，皮肤设置、功能开关都在那里；功能页里另有「**查看README**」与  
「**查看历史更新**」两个按钮，分别展示插件自带的说明文档与 `update.md` / `update.txt`。

插件还能放一个自己的 `webui.html`（固定文件名），从卡片的「**打开webui**」进入，  
用来做与设置无关的完整页面（状态面板、小工具、图表等），见 1.5b。

程序自己的版本历史在另一个二级页：点左侧栏顶部的 **Lovomo 标题**进入  
「程序历史版本」，里面是程序仓库（`slpk1ng/Lovomo`）的全部 Releases，  
可以挑一个历史版本下载（桌面窗口里会弹系统保存框）。

插件的运行日志统一输出到 WebUI 的「日志输出」页，带 `[插件]` 前缀。

---

## 目录结构

> 上架到市场只需建一条分支，不用改 `index.json`：见「三、发布到市场」。
> `index.json` 是自建索引的兜底路径。

---

## 一、写一个插件

### 1.1 插件目录

插件就是一个文件夹（或打成 .zip），结构如下：

```
my-plugin/
├── plugin.yaml     必需，清单文件（元信息全在这里）
├── main.py         可选，Python 逻辑入口
├── theme.css       可选，皮肤样式
├── webui.html      可选，插件自己的网页界面（卡片上的「打开webui」）
├── ui/panel.html   可选，自带的设置界面（连同它的 css/js）
├── logo.png        可选，列表图标（只认 logo.<主流图片格式>）
├── README.md       可选，安装后弹窗展示，功能页也有「查看README」
└── update.md       可选，「查看历史更新」页里展示
```

打包时会自动跳过 `data/`、`__pycache__/`、`.git/`，以及不在白名单里的文件类型。
白名单覆盖代码与配置（`.py` / `.pyi` / `.json` / `.yaml` / `.toml` / `.ini` …）、
文本与数据（`.md` / `.txt` / `.csv` / `.xml` / `.sqlite` …）、网页与图片、字体、
音视频（`.mp3` / `.wav` / `.flac` / `.mp4` / `.webm` …）以及前端源码
（`.ts` / `.tsx` / `.jsx` / `.scss` / `.wasm` …）；`.exe` / `.dll` / `.pyd` 这类
可执行文件一律不收。


### 1.2 清单：`plugin.yaml`

清单文件名按 **`plugin.yaml` → `plugin.yml` → `plugin.json`** 的顺序取第一个  
存在的（`plugin.json` 只为兼容早期插件保留，新插件请一律用 `plugin.yaml`）。

清单写的是 YAML，解析器内置、不需要额外依赖，支持的语法覆盖清单需要的一切：  
缩进映射、`- ` 序列、行内 `[a, b]` / `{a: b}`、单双引号、`|` / `>` 块标量、  
行尾 `#` 注释。

```yaml
# 我的插件 —— 元信息全部写在这里
id: my-plugin
name: 我的插件
version: 1.0.0
author: 你的名字
github: your-login          # ← 你的 GitHub 用户名（发布归属校验用，强烈建议写）
repo: your-login/my-plugin  # ← 插件所在仓库，格式 用户名/仓库名
homepage: https://github.com/your-login/my-plugin
type: mixed                 # theme / python / mixed
entry: main.py
logo: logo.png
description: 一句话说明这个插件做什么。
panel:
  label: 我的插件
  title: 我的插件 · 设置
skin:
  background: true
  accent: "#7c4dff"
features:
  - key: my_toggle
    label: 我的开关
    type: bool
    default: false
  - key: my_opacity
    label: 透明度
    type: range
    min: 0.2
    max: 1
    step: 0.01
    default: 0.9
```

#### 字段全表

| 字段            | 必需    | 类型  | 说明                                                          |
| ------------- | ----- | --- | ----------------------------------------------------------- |
| `id`          | **是** | 字符串 | 唯一标识，只能用字母、数字、`_`、`-`，必须以字母或数字开头，最长 64 位。**改 id 等于换一个插件**（旧设置不会跟过来）。 |
| `name`        | **是** | 字符串 | 显示名，出现在插件列表与市场卡片上。                                          |
| `version`     | **是** | 字符串 | 版本号，建议语义化（`1.2.3`）。列表与市场卡片上展示。                              |
| `description` | 否     | 字符串 | 说明文字，列表与市场卡片上展示，建议一两句话讲清用途。                                 |
| `author`      | 否     | 字符串 | 显示用的作者名，可以写中文（`爱丽丝`）。**如果它本身就是你的 GitHub 用户名**，也会被当成归属信息之一。  |
| `github`      | 否     | 字符串 | 你的 GitHub 用户名。**发布归属校验的首选字段**，强烈建议写。                        |
| `repo`        | 否     | 字符串 | 插件所在仓库，格式 `用户名/仓库名`。写在这里也会参与归属校验。                           |
| `homepage`    | 否     | 字符串 | 插件主页链接。填了 GitHub 仓库链接时，链接里的用户名同样算归属信息。市场卡片上的「主页」按钮**不用这个字段**，它固定指向你这条分支（自动生成）。 |
| `source_repo` | 否     | 字符串 | 由市场安装时自动写入的来源仓库记录，作者**不需要手写**（手写也不影响）。                      |
| `logo`        | 否     | 字符串 | 图标文件名，默认 `logo.png`，见 1.3。                                  |
| `type`        | 否     | 字符串 | `theme`（纯皮肤）/ `python`（纯功能）/ `mixed`（两者都有）。默认 `python`。     |
| `entry`       | 否     | 字符串 | Python 入口文件名，默认 `main.py`。                                  |
| `tags`        | 否     | 数组  | 市场与列表里显示的标签，最多 8 个，每个 ≤24 字。                                |
| `panel`       | 否     | 映射  | 声明插件自带的功能页，见 1.5。                                           |
| `skin`        | 否     | 映射  | 声明皮肤可调项，见 1.7。                                              |
| `features`    | 否     | 数组  | 声明插件自己提供的可调项，见 1.6。                                         |

> **YAML 小坑**：值里出现 `#` 且前面有空格时会被当成注释
> （`description: 注册 #hello 指令` 只会读到 `注册`）。
> 这种值请用引号包起来：`description: "注册 #hello 指令"`。
> 冒号同理，用引号最保险。

### 1.3 图标：`logo.<主流图片格式>`

列表与市场卡片上的图标按下面的顺序找，**都没有就用插件名首字**（默认图标）：

| 优先级 | 来源                                                                                                      | 说明                                         |
| --- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| 1   | 清单里的 `logo:` 字段                                                                                         | 只认文件名（如 `logo.png`），不能带路径；文件不存在或格式不支持时自动跳过 |
| 2   | `logo.png` / `logo.jpg` / `logo.jpeg` / `logo.webp` / `logo.gif` / `logo.svg` / `logo.ico` / `logo.bmp` | 正式约定，放在插件根目录                               |
| 3   | `icon.png` / `icon.jpg` / `icon.jpeg` / `icon.webp` / `icon.gif` / `icon.svg` / `icon.ico` / `icon.bmp` | 老插件兼容，新插件请用 `logo.*`                       |

名字必须**正好是 `logo`**（大小写不敏感；老插件用 `icon`，见上表第 3 行），  
放在插件根目录；`logo.txt` 这类非图片格式不会被当成图标。建议 128×128 的方形图。

市场里的图标走仓库原始文件链接：`raw.githubusercontent.com/<仓库>/<分支>/<logo 文件名>`，  
所以图标文件必须真的提交到插件分支里。

### 1.4 说明文档

- 安装完成后，主程序会自动读插件根目录的 `readme.md` / `readme.txt`    
  （大小写不敏感）并弹窗展示，**没写就不弹**。注意事项写这里最合适。
- 功能页顶部还有一个「查看README」按钮，随时能再打开这份说明（没有就提示一句）。
- 「查看历史更新」页读 `update.md` / `update.txt`（也认 `changelog.*` /    
  `history.*`），用来写版本变更记录。


### 1.5 功能页：写 `panel`

在清单里写 `panel`，插件列表里这张卡就会出现「有功能页」，  
点卡片空白处即可进入。有两种玩法：

**一、只声明类型，界面自动生成（推荐先从这个开始）**

```yaml
panel:
  label: 喵喵皮肤
  title: 喵喵皮肤 · 外观与追加
```

主程序会按你在 `skin` / `features` 里声明的东西自动渲染出控件 ——  
开关、滑条、下拉、取色器、文件选择都在清单里声明类型即可。

`label` 为空则整段作废（相当于没有功能页）。`title` 省略时用 `label`。

**二、自带界面，完全自己画**

```yaml
panel:
  label: 我的插件
  html: ui/panel.html
```

写上 `html`（zip 内的相对路径）之后，功能页会直接加载你自己的 HTML，  
想画成什么样就什么样。文件不存在时自动退回上面那种自动渲染，  
所以不会变成一个打不开的死页面。

自己的界面里可以用主程序注入的桥接：

```html
<script>
    var api = window.lovomo;   // 页面一解析就有（主程序注入在 <head> 里）
    // api.id / api.name / api.version / api.enabled
    // api.settings            当前设置（普通对象）
    api.save({ volume: 80 })  // 写暂存区：只覆盖你传的键，返回合并后的全量
        .then(function (s) { console.log(s); });   // 自己的界面记得同步刷新
    api.apply();              // 把暂存区落盘并重载插件（= 点「保存并重载」）
    api.asset('img/bg.png');  // 插件 data 目录里的资源 → 可加载的 URL
    api.theme();              // { skinOn, accent } 主程序当前主题口径
    api.toast('已保存');      // 在宿主页面弹一条提示
    api.log('调试信息');      // 写进主程序日志（带 [插件] 前缀）
    api.close();              // 返回插件列表
</script>
```

`api.save()` 只写暂存区，**不会**替你刷新自己的界面：点了自己的开关之后要自己改 DOM 或重画，
否则要等点了「保存并重载」才看到变化（主程序自动渲染的那套功能页不受影响，它点一下就会变）。

桥接脚本由主程序插在页面最前面，所以 `window.lovomo` 在**解析阶段**就能用；  
另外 `DOMContentLoaded` 时会派发一次 `lovomo:ready` 事件，习惯等事件的写法也可以：

```html
<script>
window.addEventListener('lovomo:ready', function () { init(window.lovomo); });
</script>
```

自己的界面跑在独立的分帧里，所以：你的 CSS 不会漏出去污染主程序界面；  
离开功能页时整块被拆掉，你注册的定时器和监听跟着一起消失，  
不会在后台留着。

### 1.5b webui：再给插件一个独立页面

想给插件一个**跟设置无关的完整页面**（状态面板、小工具、图表……），  
就在插件根目录放一个 `webui.html`（**固定这个名字**，不用在清单里声明）。  
列表卡片上会出现「打开webui」，点开是一个独立二级页，里面同样是分帧加载你的页面。

- 和 `panel.html` 用的是同一套桥接（`window.lovomo`）、同一个沙箱、同一个父页面，
  区别只是入口和用途：功能页放设置，webui 放你自己的界面。
- 想同时有两个入口就两个文件都放：卡片上会并排出现「打开功能页」「打开webui」。
- 页面里读自己的数据：插件运行时把 JSON 写进 `data/`，页面用
  `api.asset('status.json')` 取回来即可（`asset()` 的根目录就是插件自己的 `data/`）。
  同目录的 css/js 用 `api/plugins/webui?id=<插件id>&name=xxx.css` 这样的地址引。
- 示例：官方示例插件 `device-status` 的 `plugins/sources/device-status/webui.html`
  就是按这套写的（读 `data/status.json`，展示状态与最近截图，带刷新按钮）。



### 1.6 设置项：声明类型，控件自动出来

`features` 和 `skin.vars` 用的是同一套字段声明，写什么类型就渲染什么控件：

```yaml
- key: volume
  label: 音量
  type: range
  min: 0
  max: 100
  step: 1
  default: 60
  unit: 分贝
```

| `type`        | 控件       | 可用属性                                        |
| ------------- | -------- | ------------------------------------------- |
| `bool`        | 开关（默认类型） | `default`                                   |
| `range`       | 滑条       | `min` `max` `step` `default` `unit`         |
| `number`      | 数字框      | `min` `max` `step` `default` `unit`         |
| `text`        | 单行输入     | `default` `placeholder` `maxlength`         |
| `textarea`    | 多行输入     | `default` `placeholder`                     |
| `password`    | 密码框      | `default` `placeholder`                     |
| `color`       | 取色器      | `default`（`#rgb` / `#rrggbb` / `#rrggbbaa`） |
| `select`      | 下拉       | `options` `default`                         |
| `multiselect` | 多选       | `options` `default`（数组）                     |
| `file`        | 选择文件     | `exts`（允许的后缀）`default`                      |

`options` 可以写成 `["a", "b"]`，也可以是  
`[{"value": "a", "label": "选项 A"}]`。

**选项要运行时才知道（比如本机有哪些录音设备）时用 `options_file`**：  
清单里只写文件路径，插件在运行时把选项写进这个文件，主程序渲染前读它：

```yaml
- key: record_audio_device
  label: 录音设备
  type: select
  default: ""
  options_file: data/audio_devices.json
```

文件内容两种写法都认，跟 `options` 一样：

```json
{"options": ["", {"value": "麦克风阵列 (Realtek(R) Audio)", "label": "麦克风阵列"}]}
```

- 路径相对插件目录，且只能在插件目录内（越界直接忽略）。
- 文件不存在或格式不对时退回清单里写死的 `options`（没写就是空下拉）。
- 下拉的取值会按选项校验，所以插件最好把**当前配置的值**也写进选项里
  （示例插件会把「已配置但这次没枚举到」的设备补进去），否则用户保存时那一项会被丢掉。
- 什么时候写由插件决定：示例插件在加载时（后台线程里跑一次设备枚举）和每次录屏前刷新。

**type 省略时按 `default` 猜**：布尔 → 开关，数字 → 滑条/数字框，  
字符串 → 输入框，数组 → 多选。所以老的 `key: x` + `default: false`  
这种写法继续有效，不用改。写了主程序不认识的类型会退回开关，  
不会让整条字段凭空消失。

每一项都还有 `label`（不写就用 `key`）和 `description`（会显示在控件下方）。

**值存在哪里**：插件私有目录下的 `settings.json`，路径是  
`ctx.data_dir() / "settings.json"`。主程序只负责按声明渲染控件、把值写进去，  
读出来怎么用完全由插件决定：

```python
import json

def _settings(ctx):
    f = ctx.data_dir() / "settings.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
```

用户在功能页点「保存并重载」才会提交，一次改多项不会反复重载插件。


### 1.7 皮肤：写 `skin` + `theme.css`

```yaml
skin:
  background: true
  layout: true
  accent: "#7c4dff"
  label: 喵喵皮肤
```

`background` / `accent` 会渲染出控件（背景图选择器、主色取色器）；  
`layout` 与 `label` 只是清单里的标记位，当前界面不为它们渲染任何控件。

想调别的东西，用 `vars` 声明**任意** CSS 变量，主程序不再限制你能调哪几样：

```yaml
skin:
  vars:
    - name: block-opacity
      label: 区块透明度
      type: range
      min: 0.2
      max: 1
      step: 0.01
      default: 0.92
    - name: radius
      label: 圆角
      type: range
      min: 0
      max: 30
      default: 14
      unit: px
```

每个变量在 CSS 里以 `--skin-<name>` 的形式出现（名字带不带 `skin-`  
前缀都行），在 `theme.css` 里直接取用：

```css
.section-block {
    background: rgba(255, 255, 255, var(--skin-block-opacity, 0.92));
    border-radius: var(--skin-radius, 14px);
}
```

`vars` 支持与 `features` 完全相同的字段类型，所以滑条、下拉、取色器  
都能用。值写进 CSS 前会被收敛（数字夹回范围、颜色只认 hex、  
文本滤掉能突破字符串边界的字符），所以不用担心注入问题。

皮肤样式写在 `theme.css` 里，会被注入到 WebUI 页面。参考  
`sources/meow-skin/theme.css`。

要点：

- 重要属性加 `!important`，否则覆盖不掉主页面自带样式。
- **只改视觉属性**（颜色、圆角、字号、阴影）。不要用 `display:none`    
  藏掉功能按钮 —— 用户会以为程序坏了。
- **底色规则必须挂在 `html.skin-on` 下**，例如    
  `html.skin-on body { background: #fff6f8 !important; }`。    
  默认保留程序原本的背景，只有用户在功能页里主动打开「应用这套外观」，    
  页面才会加上 `skin-on` 类。裸写 `body { background: ... }` 会导致    
  插件一装上就强行改掉用户原来的背景。    
  「应用这套外观」这个开关只在清单声明了 `background` 或 `accent` 时才出现，    
  只写 `vars` 的皮肤没有这个开关，`html.skin-on` 规则也就永远不会生效。
- 不要在自己的 CSS 里重声明 `--skin-*` 变量，那几个变量由主程序按用户    
  设置生成，你的样式排在它后面，写了会覆盖掉用户的选择。
- 给每个 `var()` 写上兜底值，这样用户没设过也能有个合理外观。
- 背景色用浅色，文字保持深色，保证可读性。


### 1.8 加功能：写 `main.py`

```python
def on_load(ctx):
    """插件加载时调用一次，适合在这里注册指令。"""
    ctx.register_command("hello", lambda c, n, a, e: f"你好，{a or '世界'}！")


def on_message(ctx, event):
    """收到消息时调用。返回字符串就会作为回复发出去。
    注意：对每条消息都会触发，务必先判断再返回，否则会刷屏。
    """
    if "喵" in (event or {}).get("text", ""):
        return "喵~"
    return None


def on_unload(ctx):
    """插件被停用/卸载时调用一次。"""
    ctx.log("再见")
```

#### 可用的钩子

| 钩子                                   | 调用时机           | 返回值                     |
| ------------------------------------ | -------------- | ----------------------- |
| `on_load(ctx)`                       | 插件加载时一次        | 忽略                      |
| `on_unload(ctx)`                     | 停用/卸载时一次       | 忽略                      |
| `on_message(ctx, event)`             | 每条消息           | 字符串 = 回复内容，`None` = 不回复 |
| `on_command(ctx, name, args, event)` | 兜底指令分发         | 任意，会被当作回复               |
| `on_reply_done(ctx, info)`           | 一轮 LLM 回复发送完成后 | 忽略                      |

`on_reply_done` 的 `info` 字段：  
`session_type` / `target_id` / `session_id` / `sentences` / `emotions` /  
`role` / `reply` / `sender_id` / `user_text`。

#### ctx 提供的能力

| 方法 / 属性                                                     | 说明                              |
| ----------------------------------------------------------- | ------------------------------- |
| `ctx.log(*args)`                                            | 写日志（进 WebUI「日志输出」页，带 `[插件]` 前缀） |
| `ctx.config`                                                | 当前配置字典，**只读**，改它不会被保存           |
| `ctx.emotions`                                              | 当前角色的情绪表 `{情绪名: {...}}`，取不到是空字典 |
| `ctx.send_text(session_type, target_id, text)`              | 向群/私聊发一条文本                      |
| `ctx.send_message(group_id, text)`                          | 早期签名，等价于向群发文本                   |
| `ctx.send_voice(session_type, target_id, text, emotion="")` | 走主程序 TTS 链路发一条语音，返回是否真的发出           |
| `ctx.register_command(name, fn)`                            | 注册指令，用户发 `#name 参数` 触发          |
| `ctx.data_dir()`                                            | 本插件专属的可写目录（不会被别的插件看到）           |

指令处理函数签名：`fn(ctx, name, args, event) -> str | None`

**异常隔离**：插件代码抛出的异常一律被主程序吞掉并记录到日志，  
不会影响 Lovomo 本身运行。但也别指望「报错了没人管」——  
调试时看「日志输出」页，带 `[插件:插件名]` 前缀。


### 1.9 能用哪些库

1. **Python 标准库**：全部可用。
2. **主程序内置的第三方库**（打包时已带进程序，直接 `import` 即可）：

   | 库                  | 导入名            | 常见用途          |
   | ------------------ | -------------- | ------------- |
   | requests           | `requests`     | 同步 HTTP 请求    |
   | beautifulsoup4     | `bs4`          | HTML 解析       |
   | jinja2             | `jinja2`       | 文本 / 网页模板     |
   | python-dateutil    | `dateutil`     | 自然语言日期解析      |
   | psutil             | `psutil`       | 进程 / 磁盘 / 内存信息 |
   | pygments           | `pygments`     | 语法高亮          |
   | markdown           | `markdown`     | Markdown 转 HTML |
   | qrcode             | `qrcode`       | 生成二维码（配 Pillow） |
   | cryptography       | `cryptography` | 加密 / 签名 / 哈希  |
   | tzdata             | `tzdata`       | 时区数据          |
   | pypdf              | `pypdf`        | 读 PDF 文本       |

   另外主程序自用的 `httpx` / `aiohttp` / `numpy` / `Pillow`（`PIL`）也能直接 import。
3. **插件自带库**：把纯 Python 库（包目录或单个 `.py`）放进插件包根目录，随包一起分发。
   加载插件时插件目录会被加进 `sys.path`，`on_load` / `on_message` 等钩子里都 import 得到，
   卸载时自动摘掉。注意只支持纯 Python 的库；带 C 扩展（`.pyd` / `.so` / `.dll`）的装不了。

---

## 二、测试与打包

1. 把插件目录放到 `%LOCALAPPDATA%\Lovomo\plugins\<插件id>\`，     
   或在 WebUI 的「插件市场」页点「上传插件包 (.zip)」。
2. 系统会先做**安全扫描**，把检测到的风险按高/中/低列出来。
3. 确认后安装，插件立即生效（无需重启 Lovomo）。

打包命令示例：

```bash
cd plugins/sources/my-plugin
zip -r ../../packages/my-plugin.zip .
```

本仓库里有一个现成的打包脚本（会把白名单之外的文件自动排除）：

```bash
python .tmp_test/pack_plugin.py my-plugin      # 不带参数则打包 sources 下全部
```

> 包内可以多套一层文件夹，安装时会自动剥掉；**不支持**绝对路径和 `../`
> 路径穿越（这类条目会被直接丢弃）。
> 限制：包内文件数 ≤ 2000，解压后总大小 ≤ 128MB。

---

## 三、发布到市场

市场的数据源是**分支**，不是索引文件。一条分支 = 一个插件。

### 3.1 先认证 GitHub 身份（Personal Access Token）

「插件」页 →「发布到分支」→ 填 **Personal Access Token** →「保存并校验」。

- 需要勾的权限：经典 Token 勾 **`public_repo`** 即可；    
  细粒度 Token 需要 **Contents: Read and write**（仓库权限）。
- 保存时会调用 `GET /user` 校验 Token 并记下你的 **GitHub 用户名**，    
  用户名是后面所有归属校验的依据。
- **没认证之前，发布列表是隐藏的**：面板上只会显示 Token 输入框和提示，    
  不会列出任何插件，也不会显示发布按钮。
- Token 是密文落盘的（`config.json` 里是 `enc:` / `enc2:` 开头），    
  且只在发布请求的请求头上使用；证书校验失败时**不会**降级成不校验    
  （宁可报错，也不把 Token 交给中间人）。

### 3.2 归属校验：哪些插件才允许发布

认证通过后，主程序会把已装插件分成两类：

| 归属判断                                                       | 结果                            |
| ---------------------------------------------------------- | ----------------------------- |
| 清单里的 `github` / `owner` == 你的用户名                          | ✅ 出现在发布列表里                    |
| 清单里的 `repo` / `source_repo` / `homepage` 是 `你的用户名/仓库名`     | ✅ 同上                          |
| 清单里的 `author` 本身就是你的 GitHub 用户名                            | ✅ 同上                          |
| 清单里的归属是别人（`github: 别人`）                                    | ❌ **不显示**，直接调接口也会被拒（HTTP 403） |
| 清单里一个归属字段都没写                                               | ❌ 不显示                         |

也就是说：**只有你自己账号名下的插件才发得出去**。这条规则在服务端强制执行，  
不是靠前端把按钮藏起来 —— 手工构造 `POST /api/plugins/publish` 一样会被拒：

```
插件「别人的插件」声明的归属是 bob，与当前登录的 GitHub 账号 alice 不符，已拒绝发布。
要发布请先在插件清单（plugin.yaml）里写上自己的 github / repo / homepage。
```

所以新插件第一件事就是在 `plugin.yaml` 里写：

```yaml
author: 你的昵称          # 显示用，可中文
github: your-login        # ← 关键：发布归属
repo: your-login/my-plugin
homepage: https://github.com/your-login/my-plugin
```

### 3.3 用 WebUI 一键发布

认证通过后，「发布到分支」会列出你能发布的插件，每行一个「发布」按钮：

1. 点「发布」→ 填提交信息（留空则用默认的 `Publish <id>`）。
2. 主程序把插件目录（跳过 `data/`、`__pycache__/`）整批推成一个提交，落到     
   `lovomo_plugin_<插件id>` 分支。分支不存在时建成**根提交** —— 分支里只有插件     
   文件，不会把默认分支的仓库内容带进来；分支已存在时以新提交强制覆盖。
3. 成功后提示里会给出分支链接，去「插件市场」点「刷新市场」就能看到它。

发布走 GitHub **Git Data API**：每个文件建一个 blob → 一次建目录树 → 一次建提交 →     
把分支指向该提交。所以**全部文件都在同一个提交里**，分支内容永远以本次发布为准：

- 第一次发布会把全部文件推上去；
- 之后再发布，没改动的文件内容不变，只有改动过的文件内容会变；
- 想发新版本，先改 `plugin.yaml` 里的 `version` 再发布。


### 3.4 手动 git 发布

不想用一键发布，也可以自己推分支。规则就三条：

1. **分支名以前缀开头**（默认 `lovomo_plugin`，可在     
   「配置文件 → 插件系统 → 插件分支前缀」改），例如：
   ```bash
   git checkout --orphan lovomo_plugin_my-plugin
   git rm -rf .                      # 只放插件文件，别把整个仓库带进去
   # 把 plugin.yaml / main.py / theme.css / logo.png / README.md / update.md 放进来
   git add . && git commit -m "my-plugin v1.0.0"
   git push origin lovomo_plugin_my-plugin
   ```
   > ⚠️ **一定要用空分支（`--orphan`）**，不要把 `main` 分叉出来的整仓库当插件分支。   
   > 那种分支的归档里带着整个程序源码，插件系统有「文件数 ≤2000、解压后 ≤128MB」   
   > 的上限，会直接拒绝安装。
2. **`plugin.yaml` 放分支根目录**（不能藏在子目录里，市场和安装都按根目录找；     
   `plugin.yml` / `plugin.json` 也认，但只取优先级最高的那一个）。
3. **建议发一个 Release**：把插件打成 zip 作为资源传上去，tag 用     
   `<分支名>-v<版本>` 或 `<插件id>-v<版本>`（例如     
   `lovomo_plugin_my-plugin-v1.0.0`），并把 `target_commitish` 指向该分支。     
   这样：
   - 用户点「安装」下载的是这个干净的 zip；
   - **下载量** = 该分支名下所有 Release 资源的下载次数之和；
   - **收藏量** = 这些 Release 在 GitHub 上的**点赞数**之和；
   - 「按最新发布」按分支最新提交时间排，「按最多下载 / 最多收藏」按上面两个数排。

没发 Release 也能上架：安装会退而下载整条分支的归档 zip（所以才强调用空分支）。

### 3.5 关于两个数字

| 展示项 | 来源                                       |
| --- | ---------------------------------------- |
| 下载量 | 该分支对应 Release 里所有资源的 `download_count` 之和 |
| 收藏量 | 该分支对应 Release 的 GitHub 点赞（reactions）总数   |
| 更新于 | 该分支最新一次提交的时间（找不到就退回最新 Release 时间）        |

GitHub 的分支本身没有下载数/收藏数，所以这两个数是从 Release 上汇总来的 ——  
**没有发 Release 的插件，两个数字都会是 0**，但依然能正常安装和使用。

> 匿名调用 GitHub API 每小时只有 60 次，所以市场**缓存 30 分钟**；
> 刚创建的 Release 若是数字没变，点「刷新市场」即可。

### 3.6 市场读哪个仓库

官方市场仓库与「程序历史版本」的来源仓库由程序固定（默认都是 `slpk1ng/Lovomo`），  
WebUI 不提供修改入口。WebUI 里能改的是下面三项：

```json
{
  "plugin_market_branch_prefix": "lovomo_plugin",
  "plugin_market_path": "plugins/index.json",
  "plugin_market_thirdparty": ""
}
```

- `plugin_market_branch_prefix`：官方市场只扫名字以此开头的分支；
- `plugin_market_path`：官方市场的**兜底**索引。分支扫描一个插件都没扫到时，才回去读这份    
  JSON（格式为 `{"plugins": [...]}`）；
- `plugin_market_thirdparty`：第三方市场的仓库列表，每行一个「用户名/仓库名」（也可直接粘    
  GitHub 地址）。WebUI 的「插件市场」页把「市场」切到「第三方」时读它，    
  扫分支的规则与官方市场完全一致，但**不读** `plugin_market_path` 兜底索引。

---

## 四、来源校验与防盗用

除了「谁能发布」，还有一道「**这插件到底来自哪**」的校验，专门挡住  
「把别人的插件原样复制到自己的仓库，再挂到市场上冒充作者」这种行为。

### 4.1 市场条目记录 `source_repo`

不管是分支扫描出来的条目，还是兜底 `index.json` 里的条目，  
每条市场数据都带一个 `source_repo` 字段，标明这条插件来自哪个仓库：

| 来源           | `source_repo` 从哪来                                                    |
| ------------ | -------------------------------------------------------------------- |
| 分支扫描         | 直接就是被扫描的那个仓库（如 `slpk1ng/Lovomo`）                                     |
| `index.json` | 先读条目里的 `source_repo`（或 `repo`）；没写就从 `download` 链接的归属推断；再没有就退回索引所在的仓库 |

`index.json` 的条目建议显式写上：

```json
{
  "id": "my-plugin",
  "name": "我的插件",
  "version": "1.0.0",
  "github": "your-login",
  "source_repo": "your-login/my-plugin",
  "download": "https://github.com/your-login/my-plugin/raw/main/packages/my-plugin.zip"
}
```

### 4.2 安装时怎么校验

点「安装」时，主程序会：

1. 下载插件包 → 先跑安全扫描；
2. 校验包内清单的 `id` 与市场条目的 `id` **一致**，不一致直接拒绝     
   （防止「点的是 A，装进来的是 B」）；
3. 校验**来源**：

| 包内清单声明                                                                     | 结果             |
| -------------------------------------------------------------------------- | -------------- |
| 什么都没声明（没有 `github` / `repo` / `source_repo` / `homepage` / 像用户名的 `author`） | ✅ 放行（全新插件无从比对） |
| 声明的仓库与 `source_repo` 完全一致                                                  | ✅ 放行           |
| 声明的归属用户名与 `source_repo` 的仓库主一致                                             | ✅ 放行           |
| 声明了归属，却与 `source_repo` 对不上                                                 | ❌ 拒绝安装         |

被拒绝时的提示：

```
插件声明的来源（bob）与市场仓库 alice/Lovomo 不一致，已拒绝安装。
插件只能从作者本人的仓库安装，如果你就是作者，请在清单里写上自己的 github / repo。
```

4. 装成功后，来源会记一笔（插件目录下 `state.json` 的 `source` 段），     
   在线市场的卡片上会显示「来源 <仓库>」，方便事后追溯。

### 4.3 给插件作者的建议

- **一定要写 `github` 和 `repo`**。不写归属，插件既发不出去，也可能被别人    
  复制走当成自己的作品 —— 校验只能保护"声明了归属"的插件。
- 发布前先 `git log` 确认分支里没有夹带私货（`data/`、密钥、日志）。
- 想证明"这个插件是我做的"，最直接的方式是：在你自己账号的仓库里建插件分支，    
  让别人从**你的仓库**安装。
- 发现有人盗用你的插件：GitHub 上提 DMCA / 举报即可；本程序的校验会在他    
  冒名发布后、别人安装时直接拦下来（来源对不上）。

---

## 五、安全须知（重要）

Python 插件等同于**在你电脑上运行任意代码**。所以：

- 安装前系统会静态扫描，检出以下危险操作并标为**高危**（必须你显式确认才装）：
  | 检测项                                        | 说明              |
  | ------------------------------------------ | --------------- |
  | `shutil.rmtree`                            | 删除整个目录树         |
  | `winreg` / `ctypes.windll` / `_winapi`     | 访问注册表与系统 API    |
  | 连向固定 IP（`connect(('1.2.3.4', ...))`）      | 疑似反弹连接          |
  | 读取 config.json / api_key / 环境变量凭据         | 窃取密钥            |
  | 超长单行（>800 字符）                              | 代码混淆            |
- 中风险（会提示但不阻止）：`os.system` / `subprocess` / `eval` / `exec` /    
  `__import__`、删单个文件、裸建套接字、网络请求、写文件、改 `sys.path`、    
  键鼠钩子、加载原生库、解压到任意路径、Base64 / 十六进制串解码等 ——    
  这些在插件开发里很常见，所以只提示、不拦人。
- 低风险（只列出来，不提示确认）：起后台线程 / 定时器、读环境变量等。
- 只要没有**高危**项就直接放行；有高危项时必须你显式确认才会装上。
- **只安装你信任来源的插件。** 扫描只能发现常见模式，无法保证 100% 检出    
  刻意伪装的恶意代码。归属校验也一样：它拦的是"冒名顶替"，不是"恶意代码"。
- 装错了可以在「插件市场」页卸载，或直接删 `%LOCALAPPDATA%\Lovomo\plugins\<插件id>\`。

---

## 六、常见问题排查


### 发布相关

| 现象                                                | 原因与处理                                                                                                          |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 面板上只看到 Token 输入框，没有插件列表                           | 正常行为：还没认证。填 Token 点「保存并校验」后列表才会出现。                                                                             |
| 认证过了，但列表里没有我要发的插件                                 | 该插件的清单里没有声明你的账号归属。在 `plugin.yaml` 里加 `github: 你的用户名`（或 `repo` / `homepage`），然后刷新面板。面板下方会提示"已隐藏 N 个不属于当前账号的插件"。 |
| 提示「插件 X 声明的归属是 Y，与当前登录的账号 Z 不符」                   | 归属校验拦下了。确认这个插件是不是你自己的；是的话补上 `github: Z`。                                                                       |
| 提示「尚未认证 GitHub 身份」                                | Token 没保存成功，或只存了 Token 没校验出用户名。重新填一次 Token。                                                                    |
| `Token 无效或已过期`                                    | PAT 过期或被撤销，去 GitHub 重新生成。                                                                                      |
| `权限不足，或触发了 GitHub 限流`                             | Token 权限不够（需要 `public_repo` / Contents 读写），或短时间调用太多次。                                                          |
| `仓库 X 不存在或 Token 没有访问权限`                          | Token 没有这个仓库的权限（发布需要 `public_repo` / Contents 读写）。                                                     |
| `仓库 X 的默认分支 main 不存在` / `仓库还是空的`             | 目标仓库还没有任何提交（读不到默认分支的 HEAD），或默认分支被删了。先往仓库里推一个 README。                                                   |
| `网络错误：ConnectError ... CERTIFICATE_VERIFY_FAILED` | 出网被透明拦截（公司代理 / 安全软件 / 加速器），拦截设备的根证书不在系统证书库里。把该根证书导入系统证书库后重试。**发布接口不会降级成"不校验证书"**，因为 Token 就在请求头上。              |
| 发布成功但市场里看不到                                       | 市场有 30 分钟缓存，点「插件市场」→「刷新市场」。                                                                                    |
| 市场一直转圈 / 提示「分支根目录没有 plugin.yaml」或拉取失败（`ConnectError`） | 多半是本机直连 GitHub 不通（关掉加速器后最常见）。到「配置文件 → 插件系统 → GitHub 加速镜像」里点一次「测速并排序」，挑绿的保存即可；市场、版本信息与插件包下载都会走这些镜像。 |

> 所有发布失败都会在「日志输出」页留一行 `[插件发布]` 开头的记录，
> 带动作、URL、HTTP 状态码和返回内容摘要，排查时先看它。

> 加速镜像只用于**匿名读取**（市场扫描、raw 文件、插件包下载、版本信息）。
> 发布要带 Token，永远直连官方 —— 镜像经手就等于把 Token 交给对方。

### 清单与安装相关

| 现象                                            | 原因与处理                                                   |
| --------------------------------------------- | ------------------------------------------------------- |
| 安装时报「插件 id 非法」                                | `plugin.yaml` 的 `id` 只能是字母数字 `_` `-`，且必须以字母或数字开头。       |
| 安装时报「包内插件 id（A）与市场条目（B）不一致」                   | 市场索引里的 `id` 和包内清单的 `id` 对不上，改一致即可。                      |
| 提示「缺少 plugin.yaml / plugin.yml / plugin.json」 | 包里没有清单。会按目录名兜底安装并补一份 `plugin.yaml`，但建议自己补全元信息。          |
| 提示「插件声明的来源与市场仓库不一致」                           | 见 4.2。要么从作者本人的仓库安装，要么让作者补全归属字段。                         |
| 图标不显示                                         | 图标文件没进分支 / 名字不是 `logo.<图片格式>` / `logo:` 字段写成了路径。        |
| `description` 只读到一半                           | 值里有 `#`（前面带空格）被当成注释了，用引号包起来。                            |
| 设置项没出现                                        | `features` / `skin.vars` 的缩进写错了（YAML 对缩进敏感），或 `key` 为空。 |
| 下拉是空的                                         | 写了 `options_file` 但文件还没生成 / 路径不对。见 1.6；文件缺失时会退回清单里写死的 `options`。 |
| 卡片上没有「打开webui」                                 | 插件根目录没有 `webui.html`（文件名固定，大小写敏感）。                     |
| 打开webui 后页面空白或报 `window.lovomo` 未定义            | 页面被当成普通网页打开了。要从卡片点「打开webui」进，桥接是主程序注入的。           |
| 卸载后插件还在列表里                                     | 那是保留了配置/数据的目录（里面有 `.uninstalled` 标记），不是已安装插件；重装同一插件会接着用。 |
| 皮肤一装上就改了背景                                    | 底色规则没挂在 `html.skin-on` 下，见 1.7。                         |

### 关于卸载

卸载时会**同时问两件事**：要不要清除插件配置（`data/settings.json`）、要不要清除插件数据
（整个 `data/` 目录），**默认两个都保留**。两个都选「是」就是彻底删掉插件目录；
选了保留时，插件目录只剩 `data/` 里的那部分，下次装同一个插件会接着用原来的设置与数据。
插件目录里会留一个 `.uninstalled` 标记，主程序据此知道它不算已安装、不再列出来。

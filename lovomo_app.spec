# -*- mode: python ; coding: utf-8 -*-
import os
import pathlib
from PyInstaller.utils.hooks import collect_all, collect_submodules

try:
    ROOT = pathlib.Path(os.path.abspath(__file__)).parent
except NameError:
    try:
        ROOT = pathlib.Path(os.path.abspath(SPEC)).parent
    except NameError:
        ROOT = pathlib.Path.cwd()

# 收集 aiohttp 及所有依赖
aiohttp_datas, aiohttp_bins, aiohttp_hidden = collect_all('aiohttp')

# --- 新增部分：收集 pystray 和 Pillow(PIL) ---
pystray_datas, pystray_bins, pystray_hidden = collect_all('pystray')
pil_datas, pil_bins, pil_hidden = collect_all('PIL')
# ----------------------------------------------

datas = []
binaries = []
hiddenimports = []

datas += aiohttp_datas + pystray_datas + pil_datas
binaries += aiohttp_bins + pystray_bins + pil_bins
hiddenimports += aiohttp_hidden + pystray_hidden + pil_hidden

# 可选：收集 webview 依赖（如遇问题可启用）
# webview_datas, webview_bins, webview_hidden = collect_all('webview')
# datas += webview_datas
# binaries += webview_bins
# hiddenimports += webview_hidden

hiddenimports += [
    'webview.platforms.winforms',
    'napcat',
    'httpx',
    'numpy',
    'multidict',
    'yarl',
    'aiosignal',
    'frozenlist',
    'propcache',
]

# 插件可依赖的第三方库：显式声明并收全子模块，确保打包后插件 import 得到（与 requirements.txt 同步）
PLUGIN_LIBS = [
    'requests',
    'bs4',
    'jinja2',
    'dateutil',
    'psutil',
    'pygments',
    'markdown',
    'qrcode',
    'cryptography',
    'tzdata',
    'pypdf',
]
hiddenimports += PLUGIN_LIBS
for _lib in PLUGIN_LIBS:
    hiddenimports += collect_submodules(_lib)

tzdata_datas, tzdata_bins, tzdata_hidden = collect_all('tzdata')
datas += tzdata_datas
binaries += tzdata_bins
hiddenimports += tzdata_hidden

datas += [
    (str(ROOT / 'webui'), 'webui'),
    (str(ROOT / 'icon.ico'), '.'),
    # AGPL-3.0 第 4 条：分发程序时须一并提供完整协议文本
    (str(ROOT / 'LICENSE'), '.'),
    (str(ROOT / 'DISCLAIMER.txt'), '.'),
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qh3',
             'niquests', 'urllib3_future', 'translators'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Lovomo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False, 
    icon=str(ROOT / 'icon.ico')
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Lovomo',
)
# -*- coding: utf-8 -*-
"""在线更新：安装包识别与清理、镜像测速（先到先得）、流式下载、进度日志不刷屏。

运行: python tests/test_update_online.py      （全通过退出码 0）
"""
import asyncio
import io
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M  # noqa: E402
from modules import updater as U  # noqa: E402
import modules.updater_tools as UT  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  !! {name} 抛异常: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


_HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
_MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
# 更新状态机已搬至 modules/updater_tools.py，部分源码文本断言改查该文件
_UT = (ROOT / "modules" / "updater_tools.py").read_text(encoding="utf-8")
# WebUIServer 类已搬至 modules/webui_server.py，其方法体的源码文本断言改查该文件
_WEBUI = (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 假用户目录：find_ready_installer / cleanup_old_installers 都只看 user_data_dir()
# ---------------------------------------------------------------------------
_FILES = {}


def fake_user_dir():
    tmp = Path(tempfile.mkdtemp(prefix="lovomo_upd_"))
    _FILES["tmp"] = tmp
    return tmp


def plant_installer(root: Path, version: str, size: int = 10):
    d = root / "update"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"Lovomo_Setup_{version}.exe"
    p.write_bytes(b"MZ" + b"x" * max(0, size - 2))
    return p


# ---------------------------------------------------------------------------
# A 安装包命名 / 待装检测 / 清理
# ---------------------------------------------------------------------------
section("A 安装包识别与清理（装过就删、更新的留着）")


def a1():
    """版本号只从约定命名里取。"""
    return (M.parse_installer_version("Lovomo_Setup_1.2.3.0.exe") == "1.2.3.0"
            and M.parse_installer_version("Lovomo_Setup_v1.2.3.exe") == "1.2.3"
            and M.parse_installer_version("Lovomo_Setup.exe") == ""
            and M.parse_installer_version("随便.exe") == "")


def a2():
    """只有「比当前版本新」的安装包才算待装。"""
    root = fake_user_dir()
    saved = UT.user_data_dir
    UT.user_data_dir = lambda: root
    try:
        plant_installer(root, "0.9.0.0")
        if M.find_ready_installer():
            return False
        plant_installer(root, "99.0.0.0", size=64)
        ready = M.find_ready_installer()
        return (ready.get("version") == "99.0.0.0"
                and ready.get("size") == 64
                and Path(ready.get("path", "")).is_file()
                and "from modules.updater import APP_VERSION" in _UT)
    finally:
        UT.user_data_dir = saved
        shutil.rmtree(root, ignore_errors=True)


def a3():
    """两个都更新时取版本更高的那个。"""
    root = fake_user_dir()
    saved = UT.user_data_dir
    UT.user_data_dir = lambda: root
    try:
        plant_installer(root, "90.0.0.0")
        plant_installer(root, "91.0.0.0")
        return M.find_ready_installer().get("version") == "91.0.0.0"
    finally:
        UT.user_data_dir = saved
        shutil.rmtree(root, ignore_errors=True)


def a4():
    """清理只删「装过或更旧」的，没装的那个留着（下次「检查更新」还要用）。"""
    root = fake_user_dir()
    saved = UT.user_data_dir
    UT.user_data_dir = lambda: root
    try:
        old = plant_installer(root, "0.1.0.0")
        new = plant_installer(root, "99.0.0.0")
        M.cleanup_old_installers()
        return (not old.exists()) and new.exists()
    finally:
        UT.user_data_dir = saved
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B 镜像测速：谁先通就用谁
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/slow"):
            time.sleep(2.0)
        if self.path.startswith("/dead"):
            self.send_response(404)
            self.end_headers()
            return
        if self.path.startswith("/html"):
            body = b"<html>mirror error page</html>"
        elif self.path.startswith("/short"):
            body = b"MZ"
        else:
            body = b"MZ" + b"x" * 2048
        if self.path.startswith("/brokenlen"):
            self.send_response(200)
            self.send_header("Content-Length", "5000")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


section("B 镜像测速：先到先得 + 流式下载")


def b1():
    """并发测速取「先响应的那个」，不等慢镜像跑完。"""
    srv, base = _serve()
    try:
        started = time.time()
        winner, elapsed = asyncio.run(U.race_candidates([f"{base}/slow", f"{base}/fast"]))
        used = time.time() - started
        return winner.endswith("/fast") and used < 1.5 and elapsed < 1.5
    finally:
        srv.shutdown()


def b2():
    """还没等慢镜像返回，就已经拿到快镜像的结果了。"""
    srv, base = _serve()
    try:
        started = time.time()
        winner, _ = asyncio.run(U.race_candidates([f"{base}/slow", f"{base}/fast"],
                                                  timeout=6))
        return winner.endswith("/fast") and (time.time() - started) < 1.2
    finally:
        srv.shutdown()


def b3():
    """失败/404 的候选会被跳过，剩下的照样能选中。"""
    srv, base = _serve()
    try:
        winner, _ = asyncio.run(U.race_candidates([f"{base}/dead", f"{base}/fast"]))
        return winner.endswith("/fast")
    finally:
        srv.shutdown()


def b4():
    """全都不通时抛异常（不能静默成功）。"""
    srv, base = _serve()
    try:
        asyncio.run(U.race_candidates([f"{base}/dead"], timeout=4))
        return False
    except Exception:
        return True
    finally:
        srv.shutdown()


def b5():
    """下载：落盘 + 走 .part 不改名前的临时文件不留残骸。"""
    srv, base = _serve()
    root = Path(tempfile.mkdtemp(prefix="lovomo_dl_"))
    try:
        dest = root / "Lovomo_Setup_9.9.9.exe"
        got = []
        size = asyncio.run(U.download_asset(f"{base}/ok", dest,
                                            on_progress=lambda r, t: got.append((r, t))))
        return (size == 2050 and dest.read_bytes()[:2] == b"MZ"
                and not (root / "Lovomo_Setup_9.9.9.exe.part").exists()
                and got and got[-1][0] == 2050)
    finally:
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def b6():
    """镜像回 HTML 错误页（200 但不是 exe）时必须失败并删掉半成品。"""
    srv, base = _serve()
    root = Path(tempfile.mkdtemp(prefix="lovomo_dl_"))
    try:
        dest = root / "x.exe"
        try:
            asyncio.run(U.download_asset(f"{base}/html", dest))
            U.check_downloaded_head(dest, "exe")
            return False
        except Exception:
            return not dest.exists() and not (root / "x.exe.part").exists()
    finally:
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def b6b():
    """压缩包按 PK 校验：拿 HTML 冒充压缩包也要被挡下。"""
    root = Path(tempfile.mkdtemp(prefix="lovomo_zip_"))
    try:
        fake = root / "Lovomo_Setup_9.9.9.zip"
        fake.write_bytes(b"<html>not a zip</html>")
        try:
            U.check_downloaded_head(fake, "archive")
            return False
        except Exception as e:
            return "压缩包" in str(e) and not fake.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def b7():
    """长度对不上（镜像骗人）也拒绝，不留一个半截的安装包。"""
    srv, base = _serve()
    root = Path(tempfile.mkdtemp(prefix="lovomo_dl_"))
    try:
        dest = root / "y.exe"
        try:
            asyncio.run(U.download_asset(f"{base}/brokenlen", dest))
            return False
        except Exception:
            return not dest.exists()
    finally:
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def a5():
    """发布里挂了 exe 就优先用它，只挂压缩包就返回压缩包（kind=archive）。"""
    exe_and_zip = {"assets": [
        {"name": "Lovomo_Setup_9.9.9.zip", "browser_download_url": "uz", "size": 9},
        {"name": "Lovomo_Setup_9.9.9.exe", "browser_download_url": "ue", "size": 8}]}
    only_zip = {"assets": [
        {"name": "说明.txt", "browser_download_url": "ut", "size": 1},
        {"name": "Lovomo_v9.9.9.zip", "browser_download_url": "uz", "size": 9}]}
    nothing = {"assets": [{"name": "notes.txt", "browser_download_url": "ut", "size": 1}]}
    return (U.pick_installer_asset(exe_and_zip).get("kind") == "exe"
            and U.pick_installer_asset(exe_and_zip).get("url") == "ue"
            and U.pick_installer_asset(only_zip).get("kind") == "archive"
            and U.pick_installer_asset(only_zip).get("url") == "uz"
            and U.pick_installer_asset(nothing) == {}
            and U.pick_installer_asset({}) == {})


def a6():
    """清理时压缩包也算一份（按文件名里的版本号判定）。"""
    root = fake_user_dir()
    saved = UT.user_data_dir
    UT.user_data_dir = lambda: root
    try:
        d = root / "update"
        d.mkdir(parents=True, exist_ok=True)
        old_zip = d / "Lovomo_Setup_0.1.0.0.zip"
        new_zip = d / "Lovomo_Setup_99.0.0.0.zip"
        old_zip.write_bytes(b"PK")
        new_zip.write_bytes(b"PK")
        M.cleanup_old_installers()
        return (not old_zip.exists()) and new_zip.exists()
    finally:
        UT.user_data_dir = saved
        shutil.rmtree(root, ignore_errors=True)


def _make_zip(path: Path, entries):
    import zipfile
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


def _zip_case(entries):
    """把 entries 打成压缩包 → 解出安装包，返回 (结果, dest 路径, 临时目录)。"""
    root = Path(tempfile.mkdtemp(prefix="lovomo_zip_"))
    archive = _make_zip(root / "Lovomo_Setup_9.9.9.zip", entries)
    dest = root / "Lovomo_Setup_9.9.9.exe"
    try:
        info = U.extract_installer(archive, dest)
    except Exception as e:
        return e, dest, root
    return info, dest, root


def b8():
    """压缩包（exe 在子目录里）能自动解出安装包。"""
    info, dest, root = _zip_case([
        ("readme.txt", b"hello"),
        ("dist/Lovomo_Setup.exe", b"MZ" + b"x" * 4096)])
    try:
        return (isinstance(info, dict) and info["entry"] == "dist/Lovomo_Setup.exe"
                and dest.is_file() and dest.read_bytes()[:2] == b"MZ"
                and dest.stat().st_size == 4098)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def b9():
    """压缩包里有多个 exe 时，优先挑名字带 Lovomo 的（不被体积带偏）。"""
    info, dest, root = _zip_case([
        ("big/other-tool.exe", b"MZ" + b"y" * 10000),
        ("Lovomo_Setup.exe", b"MZ" + b"x" * 2048)])
    try:
        return info["entry"] == "Lovomo_Setup.exe" and dest.stat().st_size == 2050
    finally:
        shutil.rmtree(root, ignore_errors=True)


def b10():
    """压缩包里没有 exe → 明确报错，不留半截文件。"""
    info, dest, root = _zip_case([("readme.txt", b"hello"), ("data.bin", b"\x00" * 64)])
    try:
        return isinstance(info, Exception) and "没有 .exe" in str(info) and not dest.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def b11():
    """解出来的不是可执行文件（占位/说明文件）也拒绝，并清掉残留。"""
    info, dest, root = _zip_case([("Lovomo_Setup.exe", b"this is not an exe")])
    try:
        return isinstance(info, Exception) and not dest.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def b12():
    """不支持的压缩格式直接报错（不静默当成 exe）。"""
    root = Path(tempfile.mkdtemp(prefix="lovomo_zip_"))
    try:
        bogus = root / "Lovomo_Setup_9.9.9.rar"
        bogus.write_bytes(b"Rar!")
        try:
            U.extract_installer(bogus, root / "x.exe")
            return False
        except Exception as e:
            return "不支持" in str(e)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# C 进度日志：替换那一行，不刷屏
# ---------------------------------------------------------------------------
section("C 进度日志：同一行替换")


def c1():
    """连续调用只占一行，内容是最后一次的（前缀只写一次）。"""
    saved = list(M.global_log_buffer)
    saved_line = dict(M._PROGRESS_LINE)
    try:
        M.global_log_buffer.clear()
        M._PROGRESS_LINE.update({"index": -1, "text": "", "head": ""})
        M.log_progress("[更新] 下载中 1%")
        M.log_progress("[更新] 下载中 50%")
        M.log_progress("[更新] 下载中 99%")
        lines = list(M.global_log_buffer)
        return (len(lines) == 1
                and M._LOG_PREFIX_RE.sub("", lines[0]) == "[更新] 下载中 99%"
                and M._PROGRESS_LINE["index"] == 0)
    finally:
        M.global_log_buffer.clear()
        M.global_log_buffer.extend(saved)
        M._PROGRESS_LINE.update(saved_line)


def c2():
    """夹了别的日志之后，进度另起一行（不会去改别人的行）。"""
    saved = list(M.global_log_buffer)
    saved_line = dict(M._PROGRESS_LINE)
    try:
        M.global_log_buffer.clear()
        M._PROGRESS_LINE.update({"index": -1, "text": "", "head": ""})
        M.log_progress("[更新] 下载中 10%")
        M.global_log_buffer.append("[更新] 镜像测速完成：直连最快")
        M.log_progress("[更新] 下载中 60%")
        return [M._LOG_PREFIX_RE.sub("", line) for line in M.global_log_buffer] == [
            "[更新] 下载中 10%", "[更新] 镜像测速完成：直连最快", "[更新] 下载中 60%"]
    finally:
        M.global_log_buffer.clear()
        M.global_log_buffer.extend(saved)
        M._PROGRESS_LINE.update(saved_line)


def c3():
    """进度文案带上百分比、大小与来源。"""
    text = M.update_progress_line(22020096, 46137344, "gh-proxy.com")
    return ("47%" in text) and ("21.0/44.0 MB" in text) and ("gh-proxy.com" in text)


# ---------------------------------------------------------------------------
# D 接口与前端接线（漏前缀那次教训：这里必须断言带 api/ 的完整调用串）
# ---------------------------------------------------------------------------
section("D 接口与前端接线")


def d1():
    """三个接口都注册了。"""
    return all(s in _WEBUI for s in (
        'add_post("/api/update/download", self.handle_update_download)',
        'add_get("/api/update/progress", self.handle_update_progress)',
        'add_post("/api/update/install", self.handle_update_install)',
    ))


def d2():
    """检查结果里带上安装包信息与下载状态（前端据此直接跳到安装提示）。"""
    return ('"installer": picked.get("installer", {})' in _WEBUI
            and "def _with_update_download" in _WEBUI
            and "result[\"download\"] = self._update_download_payload()" in _WEBUI)


def d3():
    """安装入口挂在 main() 注册的钩子上，且会走唯一退出入口。"""
    return ("_APP_HOOKS[\"install_update\"] = install_update_package" in _MAIN
            and "LOVOMO_RUN_EXE" in _MAIN
            and "request_quit()" in _MAIN)


def d4():
    """前端：弹窗三个按钮 + 进度条，请求串都带 api/ 前缀。"""
    return ('id="update-online"' in _HTML and 'id="update-install"' in _HTML
            and 'id="update-go"' in _HTML and 'id="update-bar-fill"' in _HTML
            and "apiPost('api/update/download'" in _HTML
            and "apiPost('api/update/install'" in _HTML
            and "apiGet('api/update/progress')" in _HTML)


def d5():
    """前端：下完弹「是否立即安装」，选否则提示怎么再来。"""
    return ("下载已完成，是否立即安装？安装时会关闭本程序。" in _HTML
            and "需要更新时请点击「检查更新」，安装包在更新后/卸载时才删除。" in _HTML)


def d6():
    """新增文案都有英文翻译（键与中文原文一字不差）。"""
    if '"前往 GitHub 更新": "Update from GitHub"' not in _HTML:
        return False
    if '"在线更新": "Update online"' not in _HTML:
        return False
    if '"立即安装": "Install now"' not in _HTML:
        return False
    return ('"后台下载": "Download in background"' in _HTML
            and '"下载完成": "Download complete"' in _HTML
            and '"测速中": "Testing mirrors"' in _HTML)


def d7():
    """安装脚本卸载时清掉更新目录（安装包在更新后/卸载时才删除）。"""
    iss = (ROOT / "Lovomo_Setup.iss").read_text(encoding="utf-8", errors="replace")
    return 'Type: filesandordirs; Name: "{localappdata}\\Lovomo\\update"' in iss


def d8():
    """后端接线：压缩包下成 .zip，解出 exe 之后再置完成，压缩包删掉。"""
    return ('kind = str(installer.get("kind") or "exe")' in _WEBUI
            and 'suffix = ".zip" if kind == "archive" else ".exe"' in _WEBUI
            and "await asyncio.to_thread(U.extract_installer, dest, target)" in _WEBUI
            and 'print(f"[更新] 压缩包已删除：{dest.name}")' in _WEBUI
            and 'sorted(update_dir().glob("*.zip"))' in _UT)


def main():
    print("在线更新：测速 / 下载 / 进度 / 安装")
    for n, f in [("安装包版本号解析", a1), ("只认比当前新的安装包", a2),
                 ("多个待装取版本最高", a3), ("清理装过的、留未装的", a4),
                 ("exe 优先、压缩包兜底", a5), ("清理也扫压缩包", a6),
                 ("测速取先响应的", b1), ("不等慢镜像跑完", b2),
                 ("跳过失败候选", b3), ("全不通时报错", b4),
                 ("下载落盘且无残骸", b5), ("HTML 错误页被拒", b6),
                 ("压缩包按 PK 校验", b6b), ("长度对不上被拒", b7),
                 ("压缩包自动解出 exe", b8), ("多个 exe 优先 Lovomo", b9),
                 ("压缩包没 exe 会报错", b10), ("非可执行文件被拒", b11),
                 ("不支持的压缩格式报错", b12),
                 ("进度只占一行", c1), ("进度不覆盖别人的行", c2),
                 ("进度文案信息完整", c3),
                 ("三个接口已注册", d1), ("检查结果带下载状态", d2),
                 ("安装钩子走退出入口", d3), ("前端按钮与请求串", d4),
                 ("下完询问安装", d5), ("新增文案有英文", d6),
                 ("卸载清理更新目录", d7), ("压缩包解压接线", d8)]:
        check(n, f)
    print(f"\n{'=' * 70}")
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    for n in FAIL:
        print("  - " + n)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

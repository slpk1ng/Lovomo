# -*- coding: utf-8 -*-
"""陪伴功能 WebUI 接口冒烟测试：起真实服务、打真实接口（LLM 用本地假服务）。"""
import asyncio
import contextlib
import io
import json
import os
import sys
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        if not result:
            raise AssertionError(f"断言为假: {result!r}")
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, repr(e)))
        print(f"  [FAIL] {name}: {e!r}")


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(ln) or b"{}")
        messages = body.get("messages") or []
        system = " ".join(str(m.get("content", "")) for m in messages
                          if m.get("role") == "system")
        if "承诺" in system:
            content = '{"promise": "明天讲丛雨的故事"}'
        elif "should_reply" in system:
            romance = "喜欢你" in json.dumps(messages, ensure_ascii=False)
            content = json.dumps({"should_reply": True, "mood_delta": 2, "mood_reason": "ok",
                                  "affection_delta": 1, "confession": False,
                                  "romance": romance}, ensure_ascii=False)
        else:
            joined = json.dumps(messages, ensure_ascii=False)
            zh = "本座…好像有点心动了呢。" if "喜欢你" in joined else "收到啦，下次讲给你听"
            content = json.dumps({"sentences": [{"zh": zh, "ja": "受信",
                                                 "emotion": "pingjing"}]},
                                 ensure_ascii=False)
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def main():
    import httpx
    import main as M

    section = print
    section("陪伴功能 WebUI 接口冒烟测试")
    llm = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        cfg = M.ConfigLoader(str(tmp / "config.json"))
        ref_root = tmp / "ref"
        (ref_root / "pingjing").mkdir(parents=True)
        (ref_root / "pingjing" / "ref.wav").write_bytes(b"RIFF")
        cfg.config.update({
            "memory_data_path": str(tmp / "data"), "ref_audio_root": str(ref_root),
            "auto_start_tts": False, "tts_reply_enabled": False,
            "streaming_enabled": False, "tools_enabled": False,
            "scheduler_enabled": False, "proactive_enabled": False,
            "greeting_events_enabled": False, "todo_enabled": False,
            "stickers_enabled": False, "profiles_enabled": False,
            "summary_enabled": False, "dynamic_context_enabled": False,
            "rag_enabled": False, "webui_enabled": True,
            "webui_host": "127.0.0.1", "webui_port": free_port(),
            "multi_role_enabled": False, "enable_time_awareness": False,
            "reply_judge_enabled": True, "mood_enabled": True,
            "affection_enabled": True,
            "llm_backend": "openai", "llm_model_name": "fake", "llm_api_key": "",
            "llm_base_url": f"http://127.0.0.1:{llm.server_address[1]}/v1",
            "roles": [
                {"character_key": "murasame", "character_name": "丛雨",
                 "personality_prompt": "你是丛雨", "ref_audio_root": str(ref_root)},
                {"character_key": "yukina", "character_name": "小雪",
                 "personality_prompt": "你是小雪", "ref_audio_root": str(ref_root)},
            ],
        })
        cfg.roles = cfg._parse_roles()
        # 固定好感的随机起伏，避免断言被概率抖动
        class _EvenRandom:
            @staticmethod
            def uniform(a, b):
                return 1.0

            @staticmethod
            def random():
                return 1.0

        import modules.affection as affection_mod
        affection_mod.random = _EvenRandom
        M.global_config = cfg
        M.app_context.global_config = M.global_config
        M.global_emotion_manager = M.EmotionManager(cfg)
        M.app_context.global_emotion_manager = M.global_emotion_manager
        M.memory_manager = M.MemoryManager(cfg)
        M.app_context.memory_manager = M.memory_manager
        M.db = M.DatabaseManager(M.memory_manager.data_path)
        M.stats_mgr = M.StatsManager(M.db)
        M.sticker_mgr = None
        M.app_context.sticker_mgr = M.sticker_mgr
        M.tool_registry = None
        M.profile_mgr = None
        M.app_context.profile_mgr = M.profile_mgr
        M.rag_mgr = None
        M.todo_mgr = None
        M.job_mgr = None
        M.app_context.job_mgr = M.job_mgr
        M.event_mgr = None
        M.app_context.event_mgr = M.event_mgr
        M.mood_mgr = M.MoodManager(M.memory_manager.data_path)
        M.app_context.mood_mgr = M.mood_mgr
        M.affection_mgr = M.AffectionManager(cfg, M.memory_manager.data_path)
        M.app_context.affection_mgr = M.affection_mgr
        M.promise_mgr = M.PromiseManager(M.memory_manager.data_path)
        M.app_context.promise_mgr = M.promise_mgr
        M.encounter_mgr = M.EncounterManager(cfg, M.memory_manager.data_path)
        M.app_context.encounter_mgr = M.encounter_mgr

        server = M.WebUIServer(cfg, M.memory_manager)
        base = f"http://127.0.0.1:{cfg.config['webui_port']}"

        async def flow():
            await server.start()
            try:
                async with httpx.AsyncClient(timeout=60) as hc:
                    r = await hc.get(base + "/api/test/roles")
                    check("聊天测试台：角色列表可取",
                          lambda: {x["character_key"] for x in r.json()["roles"]}
                          == {"murasame", "yukina"})

                    r = await hc.post(base + "/api/test/chat",
                                      json={"character_key": "murasame",
                                            "text": "在吗"})
                    data = r.json()
                    check("聊天测试台：完整链路出回复",
                          lambda: data["success"]
                          and data["sentences"][0]["zh"].startswith("收到啦"))
                    check("聊天测试台：心情与好感随对话落盘",
                          lambda: data["mood"] is not None
                          and data["affection"]["score"] > 0)
                    r = await hc.get(base + "/api/test/history?character_key=murasame")
                    hist = r.json()
                    check("聊天测试台：测试对话单独存档",
                          lambda: len(hist["history"]) == 2)
                    r = await hc.post(base + "/api/test/chat/clear",
                                      json={"character_key": "murasame"})
                    r = await hc.get(base + "/api/test/history?character_key=murasame")
                    check("聊天测试台：清空后历史为空",
                          lambda: r.json()["history"] == [])

                    hc.post(base + "/api/test/chat",
                            json={"character_key": "murasame", "text": "在吗"})
                    r = await hc.post(base + "/api/test/chat",
                                      json={"character_key": "murasame",
                                            "text": "那说好了，明天讲故事给我听哦"})
                    promised = False
                    for _ in range(50):
                        if any(p["status"] == "pending" for p in M.promise_mgr.items):
                            promised = True
                            break
                        await asyncio.sleep(0.2)
                    check("聊天测试台：角色台词带承诺字样时触发了承诺提取",
                          lambda: promised)

                    r = await hc.post(base + "/api/test/chat",
                                      json={"character_key": "murasame",
                                            "text": "送玫瑰"})
                    check("聊天测试台：送礼这类消息按普通消息处理并落好感",
                          lambda: r.json()["affection"]["score"] >= 2)

                    r = await hc.get(base + "/api/stats?range=7")
                    curves = r.json().get("curves") or []
                    check("统计接口带心情/好感曲线数据",
                          lambda: any(c["kind"] == "mood" for c in curves)
                          and any(c["kind"] == "affection" for c in curves))

                    r = await hc.post(base + "/api/test/chat",
                                      json={"character_key": "murasame",
                                            "text": "其实我一直喜欢你"})
                    aff = r.json()["affection"]
                    check("聊天测试台：出现恋爱意味后关系才进入「暧昧」",
                          lambda: aff["nature"] == "暧昧" and aff["stage"] == "暧昧")
                    check("聊天测试台：好感多少不参与放行，一句告白也不会自动记成伴侣",
                          lambda: aff["partner"] is False)

                    r = await hc.get(base + "/api/characters/export?character_key=murasame")
                    archive = r.json()
                    check("角色档案导出含人设/记忆/好感/心情",
                          lambda: archive["character"]["character_key"] == "murasame"
                          and archive["memories"]
                          # 好感按会话分别记录，导出的是原始存档键（可能带会话前缀）
                          and any(str(k).endswith(M.TEST_CHAT_USER)
                                  for k in archive["affection"])
                          and archive["moods"])

                    payload = {"kind": "character", "character": {
                        "character_key": "imported", "character_name": "导入角色",
                        "personality_prompt": "你是新角色"},
                        "memories": {"private_10001": {"character_name": "导入角色",
                                                       "history": []}},
                        "affection": {"u9": {"score": 50, "partner": False}},
                        "moods": {"private_x::u9::imported": {"mood": 44}}}
                    r = await hc.post(base + "/api/characters/import", json=payload)
                    check("角色档案导入为新角色",
                          lambda: r.json()["success"]
                          and "imported" in cfg.roles
                          and (M.memory_manager.data_path / f"imported_private_10001.json").exists()
                          and M.affection_mgr.records["imported"]["u9"]["score"] == 50
                          and M.mood_mgr.records["private_x::u9::imported"]["mood"] == 44)
                    r = await hc.post(base + "/api/characters/import", json=payload)
                    check("重复导入同名角色被拒绝",
                          lambda: r.status_code == 409)
                    payload["overwrite"] = True
                    r = await hc.post(base + "/api/characters/import", json=payload)
                    check("勾选覆盖后允许重新导入",
                          lambda: r.json()["success"])

                    r = await hc.post(base + "/api/test/chat/clear",
                                      json={"character_key": "murasame"})
                    state = M.affection_mgr.get("murasame", M.TEST_CHAT_USER)
                    check("聊天测试台：清空对话会把心情/好感/承诺一起清掉",
                          lambda: r.json()["success"] and state["score"] == 0
                          and state["partner"] is False and state["romance"] is False
                          and not any(k.startswith(M.TEST_CHAT_SESSION + "::")
                                      for k in M.mood_mgr.records)
                          and not [p for p in M.promise_mgr.items
                                   if p.get("session_id") == M.TEST_CHAT_SESSION])
            finally:
                await server.shutdown()

    asyncio.run(flow())
    print(f"\n{'=' * 70}")
    print(f"结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print(f"{'=' * 70}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

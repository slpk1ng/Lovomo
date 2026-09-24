# 仅供 UI 检查的独立 WebUI 服务（不连 NapCat / 不启动 TTS）
import asyncio, os, sys
sys.stdout.reconfigure(encoding='utf-8')
import main as M

async def run():
    M.global_config = M.ConfigLoader()
    M.global_config.config["webui_port"] = 11599
    M.global_config.config["webui_second_password"] = os.environ.get("LOVOMO_UI_SECOND", "")
    M.global_emotion_manager = M.EmotionManager(M.global_config)
    M.memory_manager = M.MemoryManager(M.global_config)
    M.mood_mgr = M.MoodManager(M.memory_manager.data_path)
    M.db = M.DatabaseManager(M.memory_manager.data_path)
    M.stats_mgr = M.StatsManager(M.db)
    M.sticker_mgr = M.StickerManager(M.global_config)
    M.tool_registry = M.ToolRegistry(M.global_config, M.memory_manager.data_path)
    M.profile_mgr = M.UserProfileManager(M.global_config, M.memory_manager.data_path)
    M.lexicon_mgr = M.LexiconManager(M.global_config, M.memory_manager.data_path)
    M.rag_mgr = M.RAGManager(M.global_config, M.memory_manager.data_path)
    M.sender = M.MessageSender(M.global_config, M.memory_manager, M.sticker_mgr, M.stats_mgr)
    M.todo_mgr = M.TodoManager(M.global_config, M.db, M.scheduler, M.sender,
                               emotions_provider=M.get_active_emotions)
    M.todo_mgr.ctx_provider = M.get_active_ctx
    M.job_mgr = M.ScheduledJobManager(M.global_config, M.memory_manager.data_path, M.scheduler,
                                      M.sender, M.get_active_ctx, M.get_active_emotions)
    M.event_mgr = M.EventManager(M.global_config, M.memory_manager.data_path, M.profile_mgr)
    M.scheduler.start()
    M.register_feature_jobs()
    M.job_mgr.register_all()
    server = M.WebUIServer(M.global_config, M.memory_manager)
    await server.start()
    print("UI test server ready on 11599", flush=True)
    await asyncio.Event().wait()  # 常驻

asyncio.run(run())

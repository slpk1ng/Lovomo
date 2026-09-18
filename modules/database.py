"""SQLite 数据库管理：交互统计、待办事项。

零第三方依赖（标准库 sqlite3），写入轻量，不会明显阻塞事件循环。
"""
import sqlite3
import threading
import time
from pathlib import Path

DB_FILENAME = "lovomo.db"
LEGACY_DB_FILENAMES = ("ltvm.db",)

# 表一旦建成就不再变更结构，后续新增的列只能靠 ALTER 补；
# 老用户手里的库可能停留在任意旧版本，缺哪些列要到运行时才知道。
_ADDED_COLUMNS = {
    "interactions": {
        "llm_ms": "REAL DEFAULT 0",
        "tts_ms": "REAL DEFAULT 0",
        "sentence_count": "INTEGER DEFAULT 0",
        "ok": "INTEGER DEFAULT 1",
        "llm_calls": "INTEGER DEFAULT 0",
        "tts_calls": "INTEGER DEFAULT 0",
        "tool_calls": "INTEGER DEFAULT 0",
    },
    "todos": {
        "created_at": "REAL",
        "session_type": "TEXT",
        "session_id": "TEXT",
        "user_id": "TEXT",
        "remind_time": "REAL",
        "status": "TEXT DEFAULT 'pending'",
        "source": "TEXT DEFAULT 'auto'",
        # 该条待办是否合成语音；NULL = 跟随配置项 todo_voice
        "use_voice": "INTEGER",
    },
}

_INDEXES = (
    ("idx_interactions_ts", "interactions", "ts"),
    ("idx_todos_status", "todos", "status"),
)


class _ExecResult:
    """写语句的结果快照（在持锁期间取出，避免锁外读游标拿到别的线程的值）。"""

    __slots__ = ("lastrowid", "rowcount")

    def __init__(self, lastrowid, rowcount):
        self.lastrowid = lastrowid
        self.rowcount = rowcount


class DatabaseManager:
    def __init__(self, data_path: Path):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_path / DB_FILENAME
        self._migrate_legacy_db()
        self._lock = threading.Lock()
        self._conn = None
        self._init_db()

    def _migrate_legacy_db(self):
        """旧版本用的库文件名直接改名沿用，避免老用户统计/待办凭空清空。"""
        if self.db_path.exists():
            return
        for legacy in LEGACY_DB_FILENAMES:
            old = self.data_path / legacy
            if not old.exists():
                continue
            try:
                old.rename(self.db_path)
                print(f"已把旧数据库 {legacy} 更名沿用为 {DB_FILENAME}。")
            except OSError as e:
                print(f"旧数据库 {legacy} 更名失败（将新建空库）: {e}")
            return

    def _connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 单连接被 bot 协程与 WebUI 线程共用：没有等待窗口时，
            # 另一侧持写锁会立刻抛 "database is locked"
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def _columns(self, conn, table: str) -> set:
        try:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            return set()

    def _ensure_columns_and_indexes(self, conn):
        """按当前实际表结构补列、补索引。

        不能把 ALTER / CREATE INDEX 塞进 executescript：那里任一条失败都会中断整段脚本，
        而旧库（例如缺 status 列的 todos 表）会让创建索引直接抛错，程序就起不来了。
        这里逐条执行并各自兜底，最坏情况是少一个索引，服务照常可用。
        """
        for table, columns in _ADDED_COLUMNS.items():
            existing = self._columns(conn, table)
            if not existing:
                continue
            for column, ddl in columns.items():
                if column in existing:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                except Exception as e:
                    print(f"[db] 补齐列失败 {table}.{column}: {e}")
        for name, table, column in _INDEXES:
            if column not in self._columns(conn, table):
                continue
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})")
            except Exception as e:
                print(f"[db] 创建索引失败 {name}: {e}")

    def _init_db(self):
        with self._lock:
            conn = self._connect()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    session_type TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    user_name TEXT,
                    character_key TEXT,
                    emotion TEXT,
                    llm_ms REAL DEFAULT 0,
                    tts_ms REAL DEFAULT 0,
                    sentence_count INTEGER DEFAULT 0,
                    ok INTEGER DEFAULT 1,
                    llm_calls INTEGER DEFAULT 0,
                    tts_calls INTEGER DEFAULT 0,
                    tool_calls INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    session_type TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    content TEXT NOT NULL,
                    remind_time REAL,
                    status TEXT DEFAULT 'pending',
                    source TEXT DEFAULT 'auto'
                );
            """)
            self._ensure_columns_and_indexes(conn)
            conn.commit()

    def execute(self, sql, params=()):
        """执行写语句，返回 (lastrowid, rowcount)。

        旧实现把游标返回给调用方，lastrowid 是在锁外读取的：另一个线程
        在此期间再执行一条 INSERT，取到的就是别人的自增 ID。
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(sql, params)
            conn.commit()
            return _ExecResult(cur.lastrowid, cur.rowcount)

    def query_all(self, sql, params=()):
        with self._lock:
            conn = self._connect()
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    # ---------- 交互统计 ----------
    def record_interaction(self, session_type, session_id, user_id, user_name,
                           character_key, emotion, llm_ms, tts_ms,
                           sentence_count, ok=True,
                           llm_calls=0, tts_calls=0, tool_calls=0):
        try:
            self.execute(
                "INSERT INTO interactions (ts, session_type, session_id, user_id, user_name,"
                " character_key, emotion, llm_ms, tts_ms, sentence_count, ok,"
                " llm_calls, tts_calls, tool_calls)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), session_type, str(session_id), str(user_id or ""), str(user_name or ""),
                 str(character_key or ""), str(emotion or ""), float(llm_ms or 0),
                 float(tts_ms or 0), int(sentence_count or 0), 1 if ok else 0,
                 int(llm_calls or 0), int(tts_calls or 0), int(tool_calls or 0))
            )
        except Exception as e:
            print(f"记录交互统计失败: {e}")

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

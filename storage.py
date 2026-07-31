import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ChatSettings:
    chat_id: int
    provider: str
    model: str | None = None
    system_prompt: str | None = None
    search_enabled: bool = False
    search_engine: str | None = None


class Storage:
    """
    Простое хранилище на SQLite (без новых зависимостей — используется
    встроенный sqlite3). Один файл БД на весь бот, настройки — по chat_id.
    Блокирующие вызовы sqlite3 выполняются в отдельном потоке через
    asyncio.to_thread, чтобы не блокировать event loop pymax.
    """

    def __init__(self, db_path: str, default_provider: str):
        self.db_path = db_path
        self.default_provider = default_provider
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT,
                    system_prompt TEXT,
                    search_enabled INTEGER NOT NULL DEFAULT 0,
                    search_engine TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_chat ON history(chat_id, id)")

    # ---- настройки чата ----

    async def get_settings(self, chat_id: int) -> ChatSettings:
        return await asyncio.to_thread(self._get_settings, chat_id)

    def _get_settings(self, chat_id: int) -> ChatSettings:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            return ChatSettings(chat_id=chat_id, provider=self.default_provider)
        return ChatSettings(
            chat_id=row["chat_id"],
            provider=row["provider"],
            model=row["model"],
            system_prompt=row["system_prompt"],
            search_enabled=bool(row["search_enabled"]),
            search_engine=row["search_engine"],
        )

    async def save_settings(self, settings: ChatSettings) -> None:
        await asyncio.to_thread(self._save_settings, settings)

    def _save_settings(self, settings: ChatSettings) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_settings (chat_id, provider, model, system_prompt, search_enabled, search_engine)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    provider=excluded.provider,
                    model=excluded.model,
                    system_prompt=excluded.system_prompt,
                    search_enabled=excluded.search_enabled,
                    search_engine=excluded.search_engine
                """,
                (
                    settings.chat_id,
                    settings.provider,
                    settings.model,
                    settings.system_prompt,
                    int(settings.search_enabled),
                    settings.search_engine,
                ),
            )

    # ---- история вопросов/ответов ----

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await asyncio.to_thread(self._add_message, chat_id, role, content)

    def _add_message(self, chat_id: int, role: str, content: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, datetime.now(timezone.utc).isoformat()),
            )

    async def get_recent_history(self, chat_id: int, turns: int) -> list[dict]:
        return await asyncio.to_thread(self._get_recent_history, chat_id, turns)

    def _get_recent_history(self, chat_id: int, turns: int) -> list[dict]:
        # turns = число пар "вопрос-ответ" -> берём последние turns*2 сообщений
        limit = max(turns, 0) * 2
        if limit == 0:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT role, content FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def clear_history(self, chat_id: int) -> None:
        await asyncio.to_thread(self._clear_history, chat_id)

    def _clear_history(self, chat_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))

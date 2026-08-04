"""
Хранилище данных бота.

Режим выбирается автоматически по наличию переменных окружения:
- TURSO_URL + TURSO_AUTH_TOKEN → Turso (libSQL облако, данные переживают рестарт)
- иначе                        → локальный SQLite (cache/history.db)

При локальном SQLite при каждом рестарте контейнера данные теряются —
подходит только для локальной разработки и тестов.
"""
import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import ProviderConfig

logger = logging.getLogger("max-ai-bridge.storage")

# ---------------------------------------------------------------------------
# Определяем режим хранилища
# ---------------------------------------------------------------------------

_TURSO_URL = os.environ.get("TURSO_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)

if _USE_TURSO:
    import libsql_client
    logger.info("Хранилище: Turso (%s)", _TURSO_URL)
else:
    logger.warning(
        "⚠️  TURSO_URL / TURSO_AUTH_TOKEN не заданы — используется локальный SQLite.\n"
        "   История диалогов и настройки будут потеряны при рестарте контейнера.\n"
        "   Для постоянного хранения зарегистрируйтесь на https://turso.tech и\n"
        "   добавьте переменные TURSO_URL и TURSO_AUTH_TOKEN."
    )

_LOCAL_DB_PATH = Path("cache/history.db")


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

@dataclass
class ChatSettings:
    chat_id: int
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    search_enabled: bool = False
    search_engine: str | None = None
    stream_enabled: bool = False


# ---------------------------------------------------------------------------
# Общий DDL (одинаков для обоих бэкендов)
# ---------------------------------------------------------------------------

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id INTEGER PRIMARY KEY,
        provider TEXT,
        model TEXT,
        system_prompt TEXT,
        search_enabled INTEGER NOT NULL DEFAULT 0,
        search_engine TEXT,
        stream_enabled INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_chat ON history(chat_id, id)",
    """
    CREATE TABLE IF NOT EXISTS providers (
        name TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL,
        default_model TEXT NOT NULL
    )
    """,
]

_MIGRATION_ALTER = (
    "ALTER TABLE chat_settings ADD COLUMN stream_enabled INTEGER NOT NULL DEFAULT 0"
)


# ---------------------------------------------------------------------------
# Бэкенд: Turso
# ---------------------------------------------------------------------------

class _TursoBackend:
    def __init__(self):
        self._client = libsql_client.create_client(
            url=_TURSO_URL, auth_token=_TURSO_TOKEN
        )

    async def init(self) -> None:
        for ddl in _DDL:
            await self._client.execute(ddl)
        try:
            await self._client.execute(_MIGRATION_ALTER)
        except Exception:
            pass  # колонка уже есть

    async def close(self) -> None:
        await self._client.close()

    async def execute(self, sql: str, params: list = None):
        return await self._client.execute(sql, params or [])


# ---------------------------------------------------------------------------
# Бэкенд: локальный SQLite (sync → запускаем в executor)
# ---------------------------------------------------------------------------

class _SQLiteBackend:
    def __init__(self):
        _LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(_LOCAL_DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    async def init(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sync_init)

    def _sync_init(self):
        cur = self._conn.cursor()
        for ddl in _DDL:
            cur.execute(ddl)
        try:
            cur.execute(_MIGRATION_ALTER)
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        self._conn.commit()

    async def close(self) -> None:
        self._conn.close()

    async def execute(self, sql: str, params: list = None):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_execute, sql, params or [])

    def _sync_execute(self, sql: str, params: list):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        self._conn.commit()
        # возвращаем объект с атрибутом rows — как у libsql_client
        return _SQLiteResult(cur.fetchall())


class _SQLiteResult:
    def __init__(self, rows):
        self.rows = [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# Фасад Storage — одинаковый API для обоих бэкендов
# ---------------------------------------------------------------------------

class Storage:
    def __init__(self):
        if _USE_TURSO:
            self._db = _TursoBackend()
        else:
            self._db = _SQLiteBackend()

    async def init_db(self) -> None:
        await self._db.init()

    async def close(self) -> None:
        await self._db.close()

    # ---- настройки чата ----

    async def get_settings(self, chat_id: int) -> ChatSettings:
        rs = await self._db.execute(
            "SELECT * FROM chat_settings WHERE chat_id = ?", [chat_id]
        )
        if not rs.rows:
            return ChatSettings(chat_id=chat_id)
        row = rs.rows[0]
        return ChatSettings(
            chat_id=row[0],
            provider=row[1],
            model=row[2],
            system_prompt=row[3],
            search_enabled=bool(row[4]),
            search_engine=row[5],
            stream_enabled=bool(row[6]),
        )

    async def save_settings(self, settings: ChatSettings) -> None:
        await self._db.execute(
            """
            INSERT INTO chat_settings
                (chat_id, provider, model, system_prompt, search_enabled, search_engine, stream_enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                provider=excluded.provider,
                model=excluded.model,
                system_prompt=excluded.system_prompt,
                search_enabled=excluded.search_enabled,
                search_engine=excluded.search_engine,
                stream_enabled=excluded.stream_enabled
            """,
            [
                settings.chat_id,
                settings.provider,
                settings.model,
                settings.system_prompt,
                int(settings.search_enabled),
                settings.search_engine,
                int(settings.stream_enabled),
            ],
        )

    # ---- история ----

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self._db.execute(
            "INSERT INTO history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [chat_id, role, content, datetime.now(timezone.utc).isoformat()],
        )

    async def get_recent_history(self, chat_id: int, turns: int) -> list[dict]:
        limit = max(turns, 0) * 2
        if limit == 0:
            return []
        rs = await self._db.execute(
            "SELECT role, content FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            [chat_id, limit],
        )
        return [{"role": row[0], "content": row[1]} for row in reversed(rs.rows)]

    async def clear_history(self, chat_id: int) -> None:
        await self._db.execute(
            "DELETE FROM history WHERE chat_id = ?", [chat_id]
        )

    # ---- провайдеры ----

    async def add_provider(self, provider: ProviderConfig) -> None:
        await self._db.execute(
            """
            INSERT INTO providers (name, kind, base_url, api_key, default_model)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                kind=excluded.kind,
                base_url=excluded.base_url,
                api_key=excluded.api_key,
                default_model=excluded.default_model
            """,
            [provider.name, provider.kind, provider.base_url, provider.api_key, provider.default_model],
        )

    async def get_providers(self) -> dict[str, ProviderConfig]:
        rs = await self._db.execute("SELECT * FROM providers")
        return {
            row[0]: ProviderConfig(
                name=row[0], kind=row[1], base_url=row[2],
                api_key=row[3], default_model=row[4],
            )
            for row in rs.rows
        }

    async def delete_provider(self, name: str) -> None:
        await self._db.execute(
            "DELETE FROM providers WHERE name = ?", [name]
        )

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from config import ProviderConfig

# libsql-client — тонкий async-клиент для Turso (протокол hrana over WebSocket/HTTP).
# pip install libsql-client
import libsql_client


def _make_client() -> libsql_client.Client:
    url = os.environ["TURSO_URL"]           # напр. libsql://your-db.turso.io
    token = os.environ["TURSO_AUTH_TOKEN"]  # JWT-токен из Turso dashboard
    return libsql_client.create_client(url=url, auth_token=token)


@dataclass
class ChatSettings:
    chat_id: int
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    search_enabled: bool = False
    search_engine: str | None = None
    stream_enabled: bool = False


class Storage:
    """
    Хранилище на Turso (libSQL / SQLite-совместимый).
    Все запросы идентичны оригинальным SQLite-запросам —
    только драйвер заменён на libsql_client.
    """

    def __init__(self):
        # клиент создаётся один раз, переиспользуется всё время жизни бота
        self._client = _make_client()

    async def init_db(self) -> None:
        """Создаёт таблицы если их нет. Вызывать один раз при старте."""
        await self._client.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                provider TEXT,
                model TEXT,
                system_prompt TEXT,
                search_enabled INTEGER NOT NULL DEFAULT 0,
                search_engine TEXT,
                stream_enabled INTEGER NOT NULL DEFAULT 0
            )
        """)
        # миграция: добавляем колонку если её нет (Turso не бросает исключение
        # на повторный ALTER TABLE IF NOT EXISTS — используем обычный подход)
        try:
            await self._client.execute(
                "ALTER TABLE chat_settings ADD COLUMN stream_enabled INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # колонка уже есть
        await self._client.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await self._client.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_chat ON history(chat_id, id)"
        )
        await self._client.execute("""
            CREATE TABLE IF NOT EXISTS providers (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                default_model TEXT NOT NULL
            )
        """)

    async def close(self) -> None:
        await self._client.close()

    # ---- настройки чата ----

    async def get_settings(self, chat_id: int) -> ChatSettings:
        rs = await self._client.execute(
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
        await self._client.execute(
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

    # ---- история вопросов/ответов ----

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self._client.execute(
            "INSERT INTO history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [chat_id, role, content, datetime.now(timezone.utc).isoformat()],
        )

    async def get_recent_history(self, chat_id: int, turns: int) -> list[dict]:
        limit = max(turns, 0) * 2
        if limit == 0:
            return []
        rs = await self._client.execute(
            "SELECT role, content FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            [chat_id, limit],
        )
        return [{"role": row[0], "content": row[1]} for row in reversed(rs.rows)]

    async def clear_history(self, chat_id: int) -> None:
        await self._client.execute(
            "DELETE FROM history WHERE chat_id = ?", [chat_id]
        )

    # ---- провайдеры нейросетей, добавленные из чата ----

    async def add_provider(self, provider: ProviderConfig) -> None:
        await self._client.execute(
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
        rs = await self._client.execute("SELECT * FROM providers")
        return {
            row[0]: ProviderConfig(
                name=row[0],
                kind=row[1],
                base_url=row[2],
                api_key=row[3],
                default_model=row[4],
            )
            for row in rs.rows
        }

    async def delete_provider(self, name: str) -> None:
        await self._client.execute(
            "DELETE FROM providers WHERE name = ?", [name]
        )

from pymax import File

import config
import shell_exec
import websearch
from storage import Storage

HELP_TEXT = (
    "Команды бота:\n"
    f"{config.COMMAND_PREFIX} id — узнать свой ID (для ADMIN_IDS в .env)\n"
    f"{config.COMMAND_PREFIX} status — текущие настройки этого чата\n"
    f"{config.COMMAND_PREFIX} providers — список доступных провайдеров/агрегаторов\n"
    f"{config.COMMAND_PREFIX} provider <имя> — сменить провайдера для этого чата\n"
    f"{config.COMMAND_PREFIX} model <модель> — сменить модель для этого чата\n"
    f"{config.COMMAND_PREFIX} system <текст> — задать системный промпт для этого чата\n"
    f"{config.COMMAND_PREFIX} system clear — сбросить системный промпт\n"
    f"{config.COMMAND_PREFIX} search on|off — вкл/выкл автодополнение ответов веб-поиском\n"
    f"{config.COMMAND_PREFIX} engine searxng|keenable — выбрать поисковый движок для этого чата\n"
    f"{config.COMMAND_PREFIX} find <запрос> — разовый веб-поиск (без обращения к нейросети)\n"
    f"{config.COMMAND_PREFIX} history [n] — последние n обменов (по умолчанию 5)\n"
    f"{config.COMMAND_PREFIX} reset — очистить сохранённую историю переписки с ИИ в этом чате\n"
)


def is_admin(sender_id: int | None) -> bool:
    if not config.ADMIN_IDS:
        return True  # ADMIN_IDS не задан в .env — ограничений нет, см. README
    return sender_id in config.ADMIN_IDS


def is_shell_admin(sender_id: int | None) -> bool:
    # Для shell-команд общий default "ADMIN_IDS пуст = разрешено всем" НЕ
    # действует: без явно перечисленных ID выполнение команд запрещено
    # для всех, включая пустой список. Это отдельная, более строгая проверка.
    return bool(config.ADMIN_IDS) and sender_id in config.ADMIN_IDS


async def handle(
    storage: Storage, chat_id: int, sender_id: int | None, text: str, message=None
) -> str | None:
    parts = text.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("help", ""):
        return HELP_TEXT

    if sub in ("id", "whoami"):
        return (
            f"Ваш ID: {sender_id}\n"
            f"Чтобы стать админом бота (команды provider/model/system/search/sh),\n"
            f"добавьте его в ADMIN_IDS в .env и перезапустите бота."
        )

    if sub == "status":
        s = await storage.get_settings(chat_id)
        provider = config.PROVIDERS.get(s.provider)
        model = s.model or (provider.default_model if provider else "?")
        engine = s.search_engine or config.SEARCH_ENGINE_DEFAULT
        return (
            f"Провайдер: {s.provider}\n"
            f"Модель: {model}\n"
            f"Системный промпт: {s.system_prompt or '(не задан)'}\n"
            f"Веб-поиск: {'включён' if s.search_enabled else 'выключен'} (движок: {engine})"
        )

    # выполнение shell-команд в контейнере — своя, отдельная и более строгая
    # проверка прав, не связанная с общим is_admin() ниже (см. README)
    if sub == "sh":
        if not config.SHELL_EXEC_ENABLED:
            return "⛔ Выполнение команд в контейнере отключено (SHELL_EXEC_ENABLED=false в .env)"
        if not is_shell_admin(sender_id):
            return "⛔ Эта команда доступна только пользователям, явно перечисленным в ADMIN_IDS"
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} sh <команда>"
        try:
            result = await shell_exec.run(rest, config.SHELL_EXEC_TIMEOUT)
        except Exception as e:
            return f"⚠️ Ошибка выполнения: {e}"

        if not result.truncated or message is None:
            return result.display_text

        # вывод не влез в сообщение — отдельно прикладываем файлом полный текст
        await message.answer(
            result.display_text,
            attachments=[File(raw=result.full_output.encode("utf-8"), name="output.txt")],
        )
        return None

    # команды ниже меняют настройки бота — доступны только админам (если ADMIN_IDS задан)
    if not is_admin(sender_id):
        return "⛔ Эта команда доступна только владельцу бота."

    if sub == "providers":
        lines = [
            f"- {name} ({p.kind}, модель по умолчанию: {p.default_model})"
            for name, p in config.PROVIDERS.items()
        ]
        return "Доступные провайдеры/агрегаторы:\n" + "\n".join(lines)

    if sub == "provider":
        if rest not in config.PROVIDERS:
            return f"Неизвестный провайдер {rest!r}. Доступные: {', '.join(config.PROVIDERS)}"
        s = await storage.get_settings(chat_id)
        s.provider = rest
        s.model = None  # сбрасываем модель на дефолтную для нового провайдера
        await storage.save_settings(s)
        return f"✅ Провайдер для этого чата: {rest}"

    if sub == "model":
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} model <название>"
        s = await storage.get_settings(chat_id)
        s.model = rest
        await storage.save_settings(s)
        return f"✅ Модель для этого чата: {rest}"

    if sub == "system":
        s = await storage.get_settings(chat_id)
        if rest.lower() == "clear":
            s.system_prompt = None
            await storage.save_settings(s)
            return "✅ Системный промпт сброшен"
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} system <текст> | {config.COMMAND_PREFIX} system clear"
        s.system_prompt = rest
        await storage.save_settings(s)
        return "✅ Системный промпт обновлён"

    if sub == "search":
        if rest.lower() not in ("on", "off", "вкл", "выкл"):
            return f"Использование: {config.COMMAND_PREFIX} search on|off"
        s = await storage.get_settings(chat_id)
        s.search_enabled = rest.lower() in ("on", "вкл")
        await storage.save_settings(s)
        return f"✅ Веб-поиск {'включён' if s.search_enabled else 'выключен'} для этого чата"

    if sub == "engine":
        if rest not in ("searxng", "keenable"):
            return f"Использование: {config.COMMAND_PREFIX} engine searxng|keenable"
        s = await storage.get_settings(chat_id)
        s.search_engine = rest
        await storage.save_settings(s)
        return f"✅ Поисковый движок для этого чата: {rest}"

    if sub == "find":
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} find <запрос>"
        s = await storage.get_settings(chat_id)
        engine = s.search_engine or config.SEARCH_ENGINE_DEFAULT
        if engine == "none":
            return f"Поиск не настроен. Сначала выберите движок: {config.COMMAND_PREFIX} engine searxng|keenable"
        try:
            results = await websearch.search(
                engine,
                rest,
                searxng_url=config.SEARXNG_URL,
                keenable_api_key=config.KEENABLE_API_KEY,
                max_results=config.SEARCH_MAX_RESULTS,
            )
        except Exception as e:
            return f"⚠️ Ошибка поиска: {e}"
        return websearch.format_results(results)

    if sub == "history":
        n = int(rest) if rest.isdigit() else 5
        history = await storage.get_recent_history(chat_id, n)
        if not history:
            return "История пуста"
        return "\n\n".join(f"[{m['role']}] {m['content']}" for m in history)

    if sub == "reset":
        await storage.clear_history(chat_id)
        return "✅ История переписки с ИИ в этом чате очищена"

    return f"Неизвестная команда {sub!r}. Наберите «{config.COMMAND_PREFIX} help»"

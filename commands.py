from pymax import File

import config
import provider_registry
import shell_exec
import websearch
from config import ProviderConfig
from storage import Storage

# ВАЖНО: в пользовательских строках этого файла использовать ТОЛЬКО квадратные скобки []
# для обозначения аргументов команд, НЕ угловые <>.
# Причина: текст может пересылаться через Telegram Bot API с parse_mode="HTML",
# где угловые скобки интерпретируются как HTML-теги и вызывают ошибку:
# "Unsupported start tag <имя>".
# Это касается HELP_TEXT и всех f-строк с подсказками по использованию команд ниже.
HELP_TEXT = (
    "Команды бота:\n"
    f"{config.COMMAND_PREFIX} id — узнать свой ID (для ADMIN_IDS в .env)\n"
    f"{config.COMMAND_PREFIX} status — текущие настройки этого чата\n"
    f"{config.COMMAND_PREFIX} providers — список доступных провайдеров/агрегаторов\n"
    f"{config.COMMAND_PREFIX} provider [имя] — сменить провайдера для этого чата\n"
    f"{config.COMMAND_PREFIX} provider add [имя] [kind] [base_url] [модель] [api_key] — добавить провайдера\n"
    f"{config.COMMAND_PREFIX} provider remove [имя] — удалить провайдера, добавленного из чата\n"
    f"{config.COMMAND_PREFIX} model [модель] — сменить модель для этого чата\n"
    f"{config.COMMAND_PREFIX} system [текст] — задать системный промпт для этого чата\n"
    f"{config.COMMAND_PREFIX} system clear — сбросить системный промпт\n"
    f"{config.COMMAND_PREFIX} search on|off — вкл/выкл автодополнение ответов веб-поиском\n"
    f"{config.COMMAND_PREFIX} stream on|off — вкл/выкл стриминг (по умолчанию выкл)\n"
    f"{config.COMMAND_PREFIX} engine searxng|keenable — выбрать поисковый движок для этого чата\n"
    f"{config.COMMAND_PREFIX} find [запрос] — разовый веб-поиск (без обращения к нейросети)\n"
    f"{config.COMMAND_PREFIX} history [n] — последние n обменов (по умолчанию 5)\n"
    f"{config.COMMAND_PREFIX} reset — очистить сохранённую историю переписки с ИИ в этом чате\n"
)


def is_admin(sender_id: int | None) -> bool:
    if not config.ADMIN_IDS:
        return True  # ADMIN_IDS не задан в .env — ограничений нет, см. README
    return sender_id in config.ADMIN_IDS


def is_strict_admin(sender_id: int | None, owner_id: int | None) -> bool:
    if config.ADMIN_IDS:
        return sender_id in config.ADMIN_IDS
    # ADMIN_IDS ещё не задан в .env — режим первичной настройки: доверяем
    # только владельцу аккаунта, на котором запущен бот (это тот же человек,
    # кто позже впишет свой ID в ADMIN_IDS), но не всем подряд, как в is_admin.
    return owner_id is not None and sender_id == owner_id


async def _try_delete_message(message) -> str:
    """Пытается удалить исходное сообщение (в нём мог быть API-ключ)."""
    if message is None:
        return ""
    try:
        await message.delete(for_me=False)
        return " Исходное сообщение с ключом удалено из чата."
    except Exception:
        return " ⚠️ Не удалось автоматически удалить сообщение с ключом — удалите вручную."


async def handle(
    storage: Storage,
    chat_id: int,
    sender_id: int | None,
    text: str,
    message=None,
    owner_id: int | None = None,
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
        from datetime import datetime, timezone
        s = await storage.get_settings(chat_id)
        provider_name = s.provider or await provider_registry.resolve_default(storage)
        provider = await provider_registry.get(storage, provider_name)
        model = s.model or (provider.default_model if provider else "?")
        engine = s.search_engine or config.SEARCH_ENGINE_DEFAULT
        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        time_line = (
            f"Текущая дата и время сервера: {now.strftime('%d.%m.%Y %H:%M')} (локальное), "
            f"{now_utc.strftime('%d.%m.%Y %H:%M')} UTC."
        )
        base_prompt = s.system_prompt or "Ты полезный ассистент. Отвечай на том языке, на котором задан вопрос."
        return (
            f"Провайдер: {provider_name or '(не выбран — см. ' + config.COMMAND_PREFIX + ' providers)'}\n"
            f"Модель: {model}\n"
            f"Системный промпт (как видит модель):\n{time_line}\n{base_prompt}\n"
            f"Веб-поиск: {'включён' if s.search_enabled else 'выключен'} (движок: {engine})\n"
            f"Стриминг: {'включён' if s.stream_enabled else 'выключен'}"
        )

    # выполнение shell-команд в контейнере — своя, отдельная и более строгая
    # проверка прав, не связанная с общим is_admin() ниже (см. README)
    if sub == "sh":
        if not config.SHELL_EXEC_ENABLED:
            return "⛔ Выполнение команд в контейнере отключено (SHELL_EXEC_ENABLED=false в .env)"
        if not is_strict_admin(sender_id, owner_id):
            return "⛔ Эта команда доступна только пользователям, явно перечисленным в ADMIN_IDS"
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} sh [команда]"
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

    # "provider add"/"provider remove" — свой, отдельный и более строгий гейт,
    # т.к. они добавляют/удаляют API-ключ, а не просто переключают настройку
    if sub == "provider":
        action, _, arg = rest.partition(" ")
        action = action.lower()

        if action == "add":
            if not is_strict_admin(sender_id, owner_id):
                return "⛔ Добавление провайдера доступно только пользователям, явно перечисленным в ADMIN_IDS"
            tokens = arg.split(maxsplit=4)
            if len(tokens) < 4:
                return (
                    f"Использование: {config.COMMAND_PREFIX} provider add "
                    f"[имя] [kind] [base_url] [модель] [api_key]\n"
                    f"kind: openai_compatible | gemini"
                )
            name, kind, base_url, model = tokens[:4]
            api_key = tokens[4] if len(tokens) == 5 else ""
            if kind not in ("openai_compatible", "gemini"):
                return "kind должен быть openai_compatible или gemini"
            await storage.add_provider(
                ProviderConfig(name=name, kind=kind, base_url=base_url, api_key=api_key, default_model=model)
            )
            note = await _try_delete_message(message)
            return f"✅ Провайдер {name!r} добавлен. Переключить: {config.COMMAND_PREFIX} provider {name}.{note}"

        if action == "remove":
            if not is_strict_admin(sender_id, owner_id):
                return "⛔ Удаление провайдера доступно только пользователям, явно перечисленным в ADMIN_IDS"
            if not arg:
                return f"Использование: {config.COMMAND_PREFIX} provider remove [имя]"
            await storage.delete_provider(arg.strip())
            return f"✅ Провайдер {arg.strip()!r} удалён из базы (провайдеры из .env этим не затрагиваются)"

        # иначе — обычное переключение провайдера для чата, под общим is_admin()
        if not is_admin(sender_id):
            return "⛔ Эта команда доступна только владельцу бота."
        name = rest.strip()
        providers = await provider_registry.all_providers(storage)
        if not providers:
            return f"Провайдеров пока нет. Добавьте: {config.COMMAND_PREFIX} provider add [имя] [kind] [base_url] [модель] [api_key]"
        if name not in providers:
            return f"Неизвестный провайдер {name!r}. Доступные: {', '.join(providers)}"
        s = await storage.get_settings(chat_id)
        s.provider = name
        s.model = None  # сбрасываем модель на дефолтную для нового провайдера
        await storage.save_settings(s)
        return f"✅ Провайдер для этого чата: {name}"

    # команды ниже меняют настройки бота — доступны только админам (если ADMIN_IDS задан)
    if not is_admin(sender_id):
        return "⛔ Эта команда доступна только владельцу бота."

    if sub == "providers":
        providers = await provider_registry.all_providers(storage)
        if not providers:
            return (
                f"Провайдеров пока нет. Добавьте: {config.COMMAND_PREFIX} provider add "
                f"[имя] [kind] [base_url] [модель] [api_key]"
            )
        lines = [f"- {name} ({p.kind}, модель по умолчанию: {p.default_model})" for name, p in providers.items()]
        return "Доступные провайдеры/агрегаторы:\n" + "\n".join(lines)

    if sub == "model":
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} model [название]"
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
            return f"Использование: {config.COMMAND_PREFIX} system [текст] | {config.COMMAND_PREFIX} system clear"
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

    if sub == "stream":
        if rest.lower() not in ("on", "off", "вкл", "выкл"):
            return f"Использование: {config.COMMAND_PREFIX} stream on|off"
        s = await storage.get_settings(chat_id)
        s.stream_enabled = rest.lower() in ("on", "вкл")
        await storage.save_settings(s)
        return f"✅ Стриминг {'включён' if s.stream_enabled else 'выключен'} для этого чата"

    if sub == "engine":
        if rest not in ("searxng", "keenable"):
            return f"Использование: {config.COMMAND_PREFIX} engine searxng|keenable"
        s = await storage.get_settings(chat_id)
        s.search_engine = rest
        await storage.save_settings(s)
        return f"✅ Поисковый движок для этого чата: {rest}"

    if sub == "find":
        if not rest:
            return f"Использование: {config.COMMAND_PREFIX} find [запрос]"
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

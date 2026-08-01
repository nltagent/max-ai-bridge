import asyncio
import logging
from datetime import datetime, timezone

import httpx
from pymax import Client, Message
from pymax.exceptions import ApiError

import commands
import config
import provider_registry
import websearch
from ai_providers import ask
from storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("max-ai-bridge")

# Лимит MAX на длину одного сообщения — 4000 символов.
# Берём чуть меньше, чтобы не упираться в граничные случаи.
MAX_MSG_LEN = 3800

client = Client(phone=config.MAX_PHONE, work_dir="cache", session_name="main.db")
storage = Storage(config.HISTORY_DB_PATH)


def _split_message(text: str) -> list[str]:
    """
    Режет текст на части не длиннее MAX_MSG_LEN.
    Старается разбивать по абзацам/строкам, а не посередине слова.
    """
    if len(text) <= MAX_MSG_LEN:
        return [text]

    parts = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            parts.append(text)
            break
        # ищем ближайший перенос строки до лимита
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            # нет переноса — режем по пробелу
            cut = text.rfind(" ", 0, MAX_MSG_LEN)
        if cut <= 0:
            # совсем нет — режем жёстко
            cut = MAX_MSG_LEN
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts


async def _send_long(message: Message, text: str) -> None:
    """Отправляет текст, автоматически разбивая на части если длиннее лимита."""
    parts = _split_message(text)
    for i, part in enumerate(parts):
        try:
            await message.answer(part)
        except ApiError as e:
            logger.error("ApiError при отправке части %d/%d: %s", i + 1, len(parts), e)
            raise


@client.on_start()
async def on_start(client: Client) -> None:
    logger.info("MAX-клиент запущен, жду сообщений с префиксом: %s", config.TRIGGER_PREFIX)
    logger.info("Команды настроек начинаются с: %s", config.COMMAND_PREFIX)
    logger.info("Ваш ID: %s", client.me.contact.id if client.me else "unknown")


@client.on_message()
async def on_message(message: Message, client: Client) -> None:
    try:
        await _handle(message, client)
    except ApiError as e:
        # ApiError из pymax не должна падать наружу — иначе event loop ломается
        # и следующие сообщения перестают обрабатываться (именно это произошло в логах)
        logger.error("ApiError в on_message (chat=%s): %s", message.chat_id, e)
    except Exception:
        logger.exception("Необработанное исключение в on_message (chat=%s)", message.chat_id)


async def _handle(message: Message, client: Client) -> None:
    if not message.text:
        return
    text = message.text.strip()

    # --- команды управления ботом ---
    if text.startswith(config.COMMAND_PREFIX):
        cmd_text = text[len(config.COMMAND_PREFIX):].strip()
        try:
            owner_id = client.me.contact.id if client.me else None
            reply = await commands.handle(
                storage, message.chat_id, message.sender, cmd_text,
                message=message, owner_id=owner_id,
            )
        except Exception:
            logger.exception("Ошибка при обработке команды %r в чате %s", cmd_text, message.chat_id)
            await message.answer("⚠️ Внутренняя ошибка при обработке команды, подробности — в логах.")
            return
        if reply:
            await _send_long(message, reply)
        return

    # --- обычный запрос к нейросети ---
    if not text.startswith(config.TRIGGER_PREFIX):
        return

    prompt = text[len(config.TRIGGER_PREFIX):].strip()
    if not prompt:
        return

    settings = await storage.get_settings(message.chat_id)
    provider_name = settings.provider or await provider_registry.resolve_default(storage)
    if provider_name is None:
        await message.answer(
            "⚠️ Провайдер нейросети ещё не настроен. Добавьте: "
            f"{config.COMMAND_PREFIX} provider add <имя> <kind> <base_url> <модель> [api_key]"
        )
        return
    provider = await provider_registry.get(storage, provider_name)
    if provider is None:
        await message.answer(
            f"⚠️ Провайдер {provider_name!r} не найден, "
            f"проверьте {config.COMMAND_PREFIX} providers"
        )
        return
    model = settings.model or provider.default_model

    # опциональный веб-поиск
    extra_context = None
    engine = settings.search_engine or config.SEARCH_ENGINE_DEFAULT
    if settings.search_enabled and engine != "none":
        try:
            results = await websearch.search(
                engine, prompt,
                searxng_url=config.SEARXNG_URL,
                keenable_api_key=config.KEENABLE_API_KEY,
                max_results=config.SEARCH_MAX_RESULTS,
            )
            extra_context = websearch.format_results(results)
        except Exception:
            logger.warning("Веб-поиск (%s) не удался, отвечаем без него", engine, exc_info=True)

    history = await storage.get_recent_history(message.chat_id, config.HISTORY_CONTEXT_TURNS)

    # дата/время сервера — в системный промпт, чтобы модель отвечала без поиска
    now = datetime.now()
    now_utc = datetime.now(timezone.utc)
    time_line = (
        f"Текущая дата и время сервера: {now.strftime('%d.%m.%Y %H:%M')} (локальное), "
        f"{now_utc.strftime('%d.%m.%Y %H:%M')} UTC."
    )
    base_prompt = (
        settings.system_prompt
        or "Ты полезный ассистент. Отвечай на том языке, на котором задан вопрос."
    )
    effective_system_prompt = f"{time_line}\n\n{base_prompt}"

    try:
        answer = await ask(provider, model, effective_system_prompt, history, prompt, extra_context)
    except httpx.HTTPStatusError as e:
        logger.error(
            "Провайдер %s (%s) вернул ошибку %s: %s",
            provider.name, model, e.response.status_code, e.response.text[:500],
        )
        await message.answer(f"⚠️ Нейросеть вернула ошибку {e.response.status_code}")
        return
    except Exception:
        logger.exception("Не удалось получить ответ от провайдера %s (%s)", provider.name, model)
        await message.answer("⚠️ Не удалось получить ответ от нейросети, попробуйте ещё раз.")
        return

    await storage.add_message(message.chat_id, "user", prompt)
    await storage.add_message(message.chat_id, "assistant", answer)
    await _send_long(message, answer)


async def main() -> None:
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())

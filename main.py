import asyncio
import logging
import random
from datetime import datetime, timezone

import httpx
from pymax import Client, Message
from pymax.exceptions import ApiError

import commands
import config
import provider_registry
import websearch
from ai_providers import ask_stream
from storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("max-ai-bridge")

MAX_MSG_LEN = 3800
FLUSH_MIN = 2.0
FLUSH_MAX = 3.0

# session_name="main.db" — критично: именно этот параметр обеспечивал
# работу сессии на Railway. Не менять.
client = Client(phone=config.MAX_PHONE, work_dir="cache", session_name="main.db")
storage = Storage(config.HISTORY_DB_PATH)


def _escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы — защита от падений при пересылке в Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_message(text: str) -> list[str]:
    """Режет текст на части ≤ MAX_MSG_LEN, стараясь не рвать посередине слова."""
    if len(text) <= MAX_MSG_LEN:
        return [text]
    parts = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = text.rfind(" ", 0, MAX_MSG_LEN)
        if cut <= 0:
            cut = MAX_MSG_LEN
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts


async def _send_long(message: Message, text: str) -> None:
    """Отправляет текст частями с экранированием HTML."""
    for i, part in enumerate(_split_message(text)):
        try:
            await message.answer(_escape_html(part))
        except ApiError as e:
            logger.error("ApiError при отправке части %d: %s", i + 1, e)
            raise


async def _send_streaming(message: Message, answer_chunks) -> str:
    """
    Стриминговая отправка через редактирование одного сообщения:
    - первый флаш → message.answer() → получаем объект для edit()
    - каждые FLUSH_MIN..FLUSH_MAX сек → current_msg.edit(накопленный текст)
    - при достижении ~MAX_MSG_LEN → фиксируем, начинаем новое сообщение
    Возвращает полный текст для истории (без HTML-экранирования).
    """
    full_text = ""
    window_text = ""
    current_msg = None
    last_flush = asyncio.get_event_loop().time()
    flush_interval = random.uniform(FLUSH_MIN, FLUSH_MAX)

    async def _flush(final: bool = False) -> None:
        nonlocal current_msg, last_flush, flush_interval
        if not window_text.strip():
            return
        escaped = _escape_html(window_text)
        try:
            if current_msg is None:
                current_msg = await message.answer(escaped)
            else:
                await current_msg.edit(escaped)
        except ApiError as e:
            logger.error("ApiError при стриминге: %s", e)
            return
        last_flush = asyncio.get_event_loop().time()
        if not final:
            flush_interval = random.uniform(FLUSH_MIN, FLUSH_MAX)

    async for chunk in answer_chunks:
        full_text += chunk
        window_text += chunk
        now = asyncio.get_event_loop().time()

        # окно заполнилось — фиксируем, начинаем следующее
        if len(window_text) >= MAX_MSG_LEN - 200:
            cut = window_text.rfind("\n\n", 0, MAX_MSG_LEN)
            if cut <= 0:
                cut = window_text.rfind("\n", 0, MAX_MSG_LEN)
            if cut <= 0:
                cut = window_text.rfind(" ", 0, MAX_MSG_LEN)
            if cut <= 0:
                cut = MAX_MSG_LEN
            rest = window_text[cut:].lstrip()
            window_text = window_text[:cut].rstrip()
            await _flush(final=True)
            current_msg = None
            window_text = rest
            last_flush = asyncio.get_event_loop().time()
            flush_interval = random.uniform(FLUSH_MIN, FLUSH_MAX)
            continue

        if now - last_flush >= flush_interval:
            await _flush(final=False)

    if window_text.strip():
        await _flush(final=True)

    return full_text


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
            "⚠️ Провайдер нейросети ещё не настроен. Добавьте:\n"
            f"{config.COMMAND_PREFIX} provider add [имя] [kind] [base_url] [модель] [api_key]"
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
        answer_gen = ask_stream(
            provider, model, effective_system_prompt, history, prompt, extra_context
        )
        full_answer = await _send_streaming(message, answer_gen)
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

    if full_answer:
        await storage.add_message(message.chat_id, "user", prompt)
        await storage.add_message(message.chat_id, "assistant", full_answer)


async def main() -> None:
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())

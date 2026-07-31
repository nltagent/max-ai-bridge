import asyncio
import logging
import os
import sys

import httpx
from pymax import Client, Message
from pymax.exceptions import ApiError

import commands
import config
import websearch
from ai_providers import ask
from storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("max-ai-bridge")

SESSION_PATH = os.path.join("cache", "main.db")

# Задержка перед первой попыткой авторизации если сессии нет (секунды)
AUTH_WARN_DELAY = int(os.environ.get("AUTH_WARN_DELAY", "30"))
# Задержка между повторными попытками при лимите SMS (секунды)
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "120"))
# Максимальное количество попыток (0 = бесконечно)
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "0"))

client = Client(phone=config.MAX_PHONE, work_dir="cache", session_name="main.db")
storage = Storage(config.HISTORY_DB_PATH, config.DEFAULT_PROVIDER)


@client.on_start()
async def on_start(client: Client) -> None:
    logger.info("MAX-клиент запущен, жду сообщений с префиксом: %s", config.TRIGGER_PREFIX)
    logger.info("Команды настроек начинаются с: %s", config.COMMAND_PREFIX)
    logger.info("Ваш ID: %s", client.me.contact.id if client.me else "unknown")


@client.on_message()
async def on_message(message: Message, client: Client) -> None:
    if not message.text:
        return
    text = message.text.strip()

    # --- команды управления ботом (модель, провайдер, поиск, история...) ---
    if text.startswith(config.COMMAND_PREFIX):
        cmd_text = text[len(config.COMMAND_PREFIX):].strip()
        try:
            reply = await commands.handle(storage, message.chat_id, message.sender, cmd_text, message=message)
        except Exception:
            logger.exception("Ошибка при обработке команды %r в чате %s", cmd_text, message.chat_id)
            await message.answer("⚠️ Внутренняя ошибка при обработке команды, подробности — в логах.")
            return
        if reply:
            await message.answer(reply)
        return

    # --- обычный запрос к нейросети ---
    if not text.startswith(config.TRIGGER_PREFIX):
        return

    prompt = text[len(config.TRIGGER_PREFIX):].strip()
    if not prompt:
        return

    settings = await storage.get_settings(message.chat_id)
    provider = config.PROVIDERS.get(settings.provider)
    if provider is None:
        await message.answer(f"⚠️ Провайдер {settings.provider!r} не настроен, проверьте AI_PROVIDERS в .env")
        return
    model = settings.model or provider.default_model

    # опциональный веб-поиск, если включён для этого чата
    extra_context = None
    engine = settings.search_engine or config.SEARCH_ENGINE_DEFAULT
    if settings.search_enabled and engine != "none":
        try:
            results = await websearch.search(
                engine,
                prompt,
                searxng_url=config.SEARXNG_URL,
                keenable_api_key=config.KEENABLE_API_KEY,
                max_results=config.SEARCH_MAX_RESULTS,
            )
            extra_context = websearch.format_results(results)
        except Exception:
            logger.warning("Веб-поиск (%s) не удался, отвечаем без него", engine, exc_info=True)
            extra_context = None

    history = await storage.get_recent_history(message.chat_id, config.HISTORY_CONTEXT_TURNS)

    try:
        answer = await ask(provider, model, settings.system_prompt, history, prompt, extra_context)
    except httpx.HTTPStatusError as e:
        logger.error(
            "Провайдер %s (%s) вернул ошибку %s: %s",
            provider.name, model, e.response.status_code, e.response.text[:500],
        )
        answer = f"⚠️ Нейросеть вернула ошибку {e.response.status_code}"
    except Exception:
        logger.exception("Не удалось получить ответ от провайдера %s (%s)", provider.name, model)
        answer = "⚠️ Не удалось получить ответ от нейросети, попробуйте ещё раз."
    else:
        await storage.add_message(message.chat_id, "user", prompt)
        await storage.add_message(message.chat_id, "assistant", answer)

    await message.answer(answer)


async def start_with_session() -> None:
    """Сессия есть — просто стартуем, без лишних проверок."""
    logger.info("Найдена сессия %s — запускаю клиент.", SESSION_PATH)
    await client.start()


async def start_with_auth() -> None:
    """Сессии нет — предупреждаем, даём время открыть Shell, затем пробуем авторизоваться."""
    logger.warning("=" * 60)
    logger.warning("Файл сессии не найден: %s", SESSION_PATH)
    logger.warning("Требуется ручная авторизация через SMS.")
    logger.warning("")
    logger.warning("Что делать:")
    logger.warning("  1. Откройте Shell контейнера в Railway")
    logger.warning("  2. Выполните:  python main.py")
    logger.warning("  3. Введите SMS-код для %s", config.MAX_PHONE)
    logger.warning("  4. После входа нажмите Ctrl+C — бот подхватит сессию сам")
    logger.warning("")
    logger.warning("Первая попытка авторизации через %d сек. (%d мин.) ...", AUTH_WARN_DELAY, AUTH_WARN_DELAY // 60)
    logger.warning("=" * 60)

    await asyncio.sleep(AUTH_WARN_DELAY)

    attempt = 0
    while True:
        attempt += 1

        # Если за время ожидания сессия появилась (вошли через Shell) — стартуем сразу
        if os.path.exists(SESSION_PATH):
            logger.info("Сессия появилась после ожидания — запускаю клиент.")
            await client.start()
            return

        try:
            logger.info("Попытка авторизации #%d ...", attempt)
            await client.start()
            return  # успех
        except ApiError as e:
            if "limit.violate" in str(e):
                logger.warning(
                    "Превышен лимит SMS-запросов (попытка #%d). "
                    "Жду %d сек. перед следующей попыткой.",
                    attempt, RETRY_DELAY,
                )
            else:
                logger.error("Ошибка API (попытка #%d): %s", attempt, e)
        except Exception as e:
            logger.error("Неожиданная ошибка (попытка #%d): %s", attempt, e)

        if MAX_RETRIES and attempt >= MAX_RETRIES:
            logger.critical(
                "Исчерпано максимальное количество попыток (%d). Завершаю процесс.",
                MAX_RETRIES,
            )
            sys.exit(1)

        logger.info(
            "Следующая попытка через %d сек. (%d мин.) ...",
            RETRY_DELAY, RETRY_DELAY // 60,
        )
        await asyncio.sleep(RETRY_DELAY)


async def main() -> None:
    os.makedirs("cache", exist_ok=True)

    if os.path.exists(SESSION_PATH):
        await start_with_session()
    else:
        await start_with_auth()


if __name__ == "__main__":
    asyncio.run(main())

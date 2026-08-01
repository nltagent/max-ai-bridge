import asyncio
import logging

import httpx
from pymax import Client, Message

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

client = Client(phone=config.MAX_PHONE, work_dir="cache", session_name="main.db")
storage = Storage(config.HISTORY_DB_PATH)


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
            owner_id = client.me.contact.id if client.me else None
            reply = await commands.handle(
                storage, message.chat_id, message.sender, cmd_text, message=message, owner_id=owner_id
            )
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
    provider_name = settings.provider or await provider_registry.resolve_default(storage)
    if provider_name is None:
        await message.answer(
            "⚠️ Провайдер нейросети ещё не настроен. Добавьте: "
            f"{config.COMMAND_PREFIX} provider add <имя> <kind> <base_url> <модель> [api_key]"
        )
        return
    provider = await provider_registry.get(storage, provider_name)
    if provider is None:
        await message.answer(f"⚠️ Провайдер {provider_name!r} не найден, проверьте {config.COMMAND_PREFIX} providers")
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
            extra_context = None  # поиск не критичен — отвечаем без него

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


async def main() -> None:
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())

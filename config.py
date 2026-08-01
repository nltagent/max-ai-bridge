import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class ProviderConfig:
    name: str
    kind: str  # openai_compatible | gemini
    base_url: str
    api_key: str
    default_model: str


def _load_providers() -> dict[str, ProviderConfig]:
    """
    Провайдеры (агрегаторы нейросетей) задаются одной переменной AI_PROVIDERS
    в формате JSON, например:

    AI_PROVIDERS={
      "openrouter": {"kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
                      "api_key": "sk-or-...", "default_model": "meta-llama/llama-3.3-70b-instruct:free"},
      "groq":       {"kind": "openai_compatible", "base_url": "https://api.groq.com/openai/v1",
                      "api_key": "gsk-...", "default_model": "llama-3.3-70b-versatile"},
      "gemini":     {"kind": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
                      "api_key": "AIza...", "default_model": "gemini-2.0-flash"}
    }

    Ключи словаря — произвольные имена, которыми провайдер переключается
    командой `!ai provider <имя>`.

    Для обратной совместимости со старым .env: если AI_PROVIDERS не задан,
    один провайдер "default" собирается из AI_KIND / AI_BASE_URL / AI_API_KEY / AI_MODEL.
    """
    raw = os.environ.get("AI_PROVIDERS")
    providers: dict[str, ProviderConfig] = {}

    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"AI_PROVIDERS содержит некорректный JSON: {e}") from e

        for name, cfg in data.items():
            for required in ("base_url", "default_model"):
                if required not in cfg:
                    raise RuntimeError(
                        f"AI_PROVIDERS: у провайдера {name!r} не задано обязательное поле {required!r}."
                    )
            providers[name] = ProviderConfig(
                name=name,
                kind=cfg.get("kind", "openai_compatible"),
                base_url=cfg["base_url"],
                api_key=cfg.get("api_key", ""),
                default_model=cfg["default_model"],
            )

    if not providers:
        # обе группы переменных не заданы — не ошибка: провайдеров пока просто
        # нет, их можно будет добавить командой "!ai provider add" из чата,
        # они сохранятся в SQLite и переживут рестарт (см. provider_registry.py)
        if os.environ.get("AI_BASE_URL") and os.environ.get("AI_MODEL"):
            providers["default"] = ProviderConfig(
                name="default",
                kind=os.environ.get("AI_KIND", "openai_compatible"),
                base_url=os.environ["AI_BASE_URL"],
                api_key=os.environ.get("AI_API_KEY", ""),
                default_model=os.environ["AI_MODEL"],
            )

    return providers


PROVIDERS = _load_providers()

# Провайдер по умолчанию из .env — необязателен. Если не задан, а провайдеров
# несколько или ноль, при использовании "@a" бот попросит выбрать явно
# (см. provider_registry.resolve_default).
DEFAULT_PROVIDER = os.environ.get("AI_DEFAULT_PROVIDER") or None
if DEFAULT_PROVIDER and PROVIDERS and DEFAULT_PROVIDER not in PROVIDERS:
    raise RuntimeError(
        f"AI_DEFAULT_PROVIDER={DEFAULT_PROVIDER!r} не найден среди AI_PROVIDERS: {list(PROVIDERS)}"
    )

MAX_PHONE = os.environ["MAX_PHONE"]
TRIGGER_PREFIX = os.environ.get("TRIGGER_PREFIX", "@a ")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!ai")

# ID пользователей MAX, которым разрешено менять настройки бота.
# Если не задано — команды настройки доступны всем, кто пишет боту (см. README).
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

HISTORY_CONTEXT_TURNS = _get_int("HISTORY_CONTEXT_TURNS", 6)
HISTORY_DB_PATH = os.environ.get("HISTORY_DB_PATH", "cache/history.db")

# none | searxng | keenable
SEARCH_ENGINE_DEFAULT = os.environ.get("SEARCH_ENGINE", "none")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")
KEENABLE_API_KEY = os.environ.get("KEENABLE_API_KEY", "")
SEARCH_MAX_RESULTS = _get_int("SEARCH_MAX_RESULTS", 5)

# Выполнение shell-команд в контейнере по команде из чата.
# ВЫКЛЮЧЕНО по умолчанию — это фактически удалённый шелл на вашем сервере,
# см. предупреждение в README перед тем как включать.
SHELL_EXEC_ENABLED = os.environ.get("SHELL_EXEC_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
SHELL_EXEC_TIMEOUT = _get_int("SHELL_EXEC_TIMEOUT", 30)

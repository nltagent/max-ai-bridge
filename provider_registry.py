"""
Единый реестр провайдеров нейросетей: те, что заданы в .env (config.PROVIDERS,
неизменяемые за время работы бота), плюс те, что добавлены командой
"!ai provider add" и хранятся в SQLite (storage) — их можно добавлять и
удалять на лету, без переменных окружения и без рестарта бота.

При совпадении имени провайдер из БД перекрывает одноимённый из .env —
считается более свежей настройкой.
"""
import config
from config import ProviderConfig
from storage import Storage


async def all_providers(storage: Storage) -> dict[str, ProviderConfig]:
    db_providers = await storage.get_providers()
    return {**config.PROVIDERS, **db_providers}


async def get(storage: Storage, name: str | None) -> ProviderConfig | None:
    if not name:
        return None
    providers = await all_providers(storage)
    return providers.get(name)


async def resolve_default(storage: Storage) -> str | None:
    """
    Имя провайдера, используемого, если в чате свой явно не выбран:
    сначала AI_DEFAULT_PROVIDER из .env (если такой провайдер существует),
    иначе — единственный существующий провайдер (если он ровно один),
    иначе None — нужно выбрать явно командой "!ai provider <имя>".
    """
    providers = await all_providers(storage)
    if config.DEFAULT_PROVIDER and config.DEFAULT_PROVIDER in providers:
        return config.DEFAULT_PROVIDER
    if len(providers) == 1:
        return next(iter(providers))
    return None

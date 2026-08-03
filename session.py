"""
Утилита для работы с сессией MAX через переменную окружения.

Сессионный файл (cache/main.db) кодируется в base64 и хранится
в переменной MAX_SESSION на Render. При каждом старте бота файл
восстанавливается из переменной на диск.

Как получить значение переменной (делается один раз локально):
    python session.py export
Выведет строку — скопируй её в Render Dashboard → MAX_SESSION.
"""
import base64
import os
import sys
from pathlib import Path

SESSION_PATH = Path("cache/main.db")
ENV_VAR = "MAX_SESSION"


def restore() -> bool:
    """
    Восстанавливает cache/main.db из переменной окружения MAX_SESSION.
    Возвращает True если файл восстановлен, False если переменная не задана.
    """
    encoded = os.environ.get(ENV_VAR)
    if not encoded:
        return False
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_bytes(base64.b64decode(encoded))
    return True


def export_session() -> str:
    """Читает cache/main.db и возвращает base64-строку для переменной окружения."""
    if not SESSION_PATH.exists():
        raise FileNotFoundError(f"Файл сессии не найден: {SESSION_PATH}")
    return base64.b64encode(SESSION_PATH.read_bytes()).decode()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "export":
        print(f"Использование: python session.py export")
        sys.exit(1)
    try:
        print(export_session())
    except FileNotFoundError as e:
        print(f"Ошибка: {e}")
        sys.exit(1)

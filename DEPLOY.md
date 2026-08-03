# Деплой на Render + Turso

## 1. Turso — создаём базу данных

1. Регистрируемся на https://turso.tech через GitHub (карта не нужна)
2. Создаём базу и получаем реквизиты:
   ```
   turso db create max-ai-bridge
   turso db show max-ai-bridge           # URL вида libsql://...turso.io
   turso db tokens create max-ai-bridge  # JWT-токен
   ```
3. Сохрани URL и токен — понадобятся на шаге 3

## 2. Получаем MAX_SESSION из существующей сессии

У тебя уже есть рабочая сессия (cache/main.db). Запусти в папке проекта:

```bash
python session.py export
```

Скрипт выведет длинную строку base64 — это и есть значение переменной MAX_SESSION.
Скопируй её целиком.

## 3. GitHub — заливаем код

1. Создай приватный репозиторий на GitHub
2. Положи туда все файлы проекта
3. cache/main.db коммитить НЕ нужно — сессия теперь живёт в переменной окружения

## 4. Render — создаём сервис

1. Идём на https://render.com, регистрируемся (карта не нужна для Free)
2. New → **Background Worker** (не Web Service — у Web Service есть таймаут засыпания!)
3. Подключаем GitHub-репозиторий
4. Runtime: **Docker**
5. В разделе Environment Variables добавляем:

   | Переменная          | Значение                                        |
   |---------------------|-------------------------------------------------|
   | MAX_PHONE           | +7XXXXXXXXXX                                    |
   | MAX_SESSION         | (вывод команды python session.py export)        |
   | TURSO_URL           | libsql://your-db.turso.io                       |
   | TURSO_AUTH_TOKEN    | eyJ... (токен из шага 1)                        |
   | AI_PROVIDERS        | см. .env.example                                |
   | TRIGGER_PREFIX      | @a  (с пробелом в конце)                        |
   | COMMAND_PREFIX      | !ai                                             |

6. Нажимаем **Create Background Worker**

## Что переживает рестарт

| Данные                  | Хранится             | Переживает рестарт |
|-------------------------|----------------------|--------------------|
| История диалогов        | Turso                | ✅ да               |
| Настройки чатов         | Turso                | ✅ да               |
| Провайдеры из чата      | Turso                | ✅ да               |
| Сессия MAX              | Переменная MAX_SESSION | ✅ да             |

## Если сессия протухнет

Авторизуйся заново локально:
```bash
python main.py   # вводишь SMS-код, потом Ctrl+C
python session.py export  # копируешь вывод
```
Вставляешь новое значение в Render Dashboard → MAX_SESSION → Save.
Render автоматически перезапустит сервис.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# cache/ нужна только для сессии MAX (main.db).
# История и настройки живут в Turso — диск не нужен.
RUN mkdir -p cache

CMD ["python", "main.py"]

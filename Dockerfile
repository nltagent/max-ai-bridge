FROM python:3.12-slim

WORKDIR /app

# сначала только requirements — чтобы слой с зависимостями кэшировался
# и не пересобирался при каждом изменении кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# cache/ — точка монтирования постоянного Volume на Railway
# (сессия MAX и история переписки должны переживать передеплой)
RUN mkdir -p cache

CMD ["python", "main.py"]

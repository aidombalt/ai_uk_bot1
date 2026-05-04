FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Системные зависимости минимальны (httpx умеет в TLS из коробки)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

# Конфиг и .env монтируются снаружи (см. docker-compose.yml).
# config.yaml опционален: если файла нет — бот стартует с дефолтами + .env.
ENV BALT_DOM_CONFIG=/app/config.yaml

CMD ["balt-dom-bot"]

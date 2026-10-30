FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --upgrade pip && pip install .

FROM base AS test

USER root
RUN pip install '.[dev]'
COPY tests ./tests
RUN pytest -q && ruff check app tests

FROM base AS runtime

RUN mkdir -p /app/data && chown -R nobody:nogroup /app
USER nobody

CMD ["python", "-m", "app.main"]

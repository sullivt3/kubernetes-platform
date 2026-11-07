# Backend image: API (uvicorn), orchestration worker (celery worker) and scheduler (celery beat)
# all run from this image with different commands. Build context: ./backend
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-deps .

# Never run as root.
RUN useradd --system --uid 10001 --home /app platform && chown -R platform /app
USER platform

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

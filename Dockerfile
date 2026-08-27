FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8787 \
    DB_PATH=data/trades.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY core ./core
COPY servers ./servers
COPY scripts ./scripts
COPY dashboard ./dashboard
COPY configs ./configs
COPY docker/entrypoint.py ./docker/entrypoint.py

RUN pip install --no-cache-dir -e . \
    && mkdir -p /app/data/history /app/data/backtests /app/data/nightly

EXPOSE 8787
VOLUME ["/app/data"]

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["dashboard"]

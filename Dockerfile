# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY calendar_sync ./calendar_sync
RUN python -m venv /venv && /venv/bin/pip install --upgrade pip && /venv/bin/pip install .

FROM python:3.12-slim AS runtime
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /nonexistent app \
    && mkdir /data && chown app:app /data
COPY --from=builder /venv /venv
COPY logging.json /app/logging.json
USER 10001:10001
WORKDIR /app
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
CMD ["uvicorn", "calendar_sync.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--log-config", "/app/logging.json"]

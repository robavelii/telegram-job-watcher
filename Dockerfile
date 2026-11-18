# Multi-arch base: builds natively on arm64 and x86-64.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CONFIG_DIR=/app/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Must match the owner of config/ on the host (`id -u`), so the mounted config files
# can stay mode 600. Set WATCHER_UID in .env; compose passes it here.
ARG UID=1000
RUN useradd --uid ${UID} --user-group --create-home --shell /usr/sbin/nologin watcher \
 && mkdir -p /data && chown watcher:watcher /data

COPY watcher ./watcher
USER watcher

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python", "-m", "watcher.health"]
CMD ["python", "-m", "watcher.main"]

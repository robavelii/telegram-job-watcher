# Multi-arch base: builds natively on the OCI Ampere (arm64) host and on x86 laptops.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CONFIG_DIR=/app/config

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# uid 1000 matches the `ubuntu` user on the server, so the mounted config files
# can stay mode 600 and owned by that user.
ARG UID=1000
RUN useradd --uid ${UID} --create-home --shell /usr/sbin/nologin watcher \
 && mkdir -p /data && chown watcher:watcher /data

COPY watcher ./watcher
USER watcher

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python", "-m", "watcher.health"]
CMD ["python", "-m", "watcher.main"]

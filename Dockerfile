FROM python:3.12-alpine

LABEL org.opencontainers.image.title="watcherr"
LABEL org.opencontainers.image.description="Automated stalled torrent auto-healing and indexer failover watchdog"
LABEL org.opencontainers.image.source="https://github.com/T1n777/watcherr"

WORKDIR /app

COPY watcherr.py /app/watcherr.py

RUN chmod +x /app/watcherr.py && \
    ln -s /app/watcherr.py /usr/local/bin/watcherr

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["watcherr"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY requirements.txt .
# Chromium is used only for the CheckVisaSlots page.
RUN pip install --no-cache-dir -r requirements.txt pytest \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

COPY watcher ./watcher
COPY tests ./tests
COPY docker-entrypoint.sh ./docker-entrypoint.sh

# Non-root user; /data is a bind mount owned by uid 10001 on the host.
RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["python", "-m", "watcher", "run"]

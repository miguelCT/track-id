# Debian slim (glibc), not Alpine. shazamio 0.8.1 pins shazamio-core==1.1.2,
# which publishes manylinux wheels for linux/amd64 and linux/arm64 but no
# musllinux wheel. Both Cloud Run and a Raspberry Pi can install it as-is.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=3)"

CMD ["python", "-m", "track_id"]

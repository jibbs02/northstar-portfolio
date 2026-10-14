FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419

ARG NORTHSTAR_BUILD_COMMIT
LABEL org.opencontainers.image.revision=$NORTHSTAR_BUILD_COMMIT

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 northstar \
    && useradd --uid 10001 --gid northstar --create-home --shell /usr/sbin/nologin northstar

WORKDIR /app
COPY pyproject.toml constraints-production.txt README.md ./
COPY northstar ./northstar
COPY app.py ./
RUN python -m pip install --no-cache-dir --upgrade 'pip==26.2.1' \
    && python -m pip install --no-cache-dir --constraint constraints-production.txt .

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers", "--no-server-header"]
